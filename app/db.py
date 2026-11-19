"""PostgreSQL 访问层：schema 初始化、快照事务、溯源函数、数据维护。"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from .config import settings
from .sql_rewrite import PROV_ADD, PROV_AGG
from .sql_validate import TABLE_SCHEMA

# 会话级溯源函数与聚合（创建在 pg_temp，事务结束随临时对象释放）
_PROV_SFUNC = f"""
CREATE OR REPLACE FUNCTION pg_temp.{PROV_ADD}(state jsonb, item jsonb)
RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
  result jsonb := COALESCE(state, '{{}}'::jsonb);
  tk text;
  ik text;
  cur int;
  old_table jsonb;
  new_table jsonb;
BEGIN
  IF item IS NULL THEN
    RETURN result;
  END IF;
  FOR tk IN SELECT jsonb_object_keys(item) LOOP
    old_table := COALESCE(result -> tk, '{{}}'::jsonb);
    new_table := old_table;
    FOR ik IN SELECT jsonb_object_keys(item -> tk) LOOP
      cur := COALESCE((old_table ->> ik)::int, 0)
             + (item -> tk ->> ik)::int;
      -- 用 || 重建该表的计数对象（同名键覆盖为累加后的新值）
      new_table := new_table || jsonb_build_object(ik, cur);
    END LOOP;
    result := result || jsonb_build_object(tk, new_table);
  END LOOP;
  RETURN result;
END;
$$
"""

_PROV_FINAL = f"""
CREATE OR REPLACE FUNCTION pg_temp.{PROV_AGG}_final(state jsonb)
RETURNS jsonb
LANGUAGE sql IMMUTABLE AS $$
  SELECT COALESCE(
    jsonb_agg(
      jsonb_build_object('t', t, 'id', id, 'n', n)
      ORDER BY t, id
    ),
    '[]'::jsonb
  )
  FROM (
    SELECT tk.key AS t,
           (ik.key)::bigint AS id,
           (ik.value)::int AS n
    FROM jsonb_each(COALESCE(state, '{{}}'::jsonb)) tk,
         jsonb_each(tk.value) ik
  ) flat
$$
"""

_PROV_AGGREGATE_FINAL = f"""
CREATE AGGREGATE pg_temp.{PROV_AGG}(jsonb) (
  SFUNC = pg_temp.{PROV_ADD},
  STYPE = jsonb,
  FINALFUNC = pg_temp.{PROV_AGG}_final,
  INITCOND = '{{}}'
)
"""

# 不带 finalfunc 的聚合：输出仍是嵌套计数对象，供 CTE/派生表向外继续合并
_PROV_AGGREGATE_STATE = f"""
CREATE AGGREGATE pg_temp.{PROV_AGG}_state(jsonb) (
  SFUNC = pg_temp.{PROV_ADD},
  STYPE = jsonb,
  INITCOND = '{{}}'
)
"""

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS samples (
      id    INTEGER PRIMARY KEY,
      batch VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS results (
      id        INTEGER PRIMARY KEY,
      sample_id INTEGER NOT NULL REFERENCES samples(id),
      value     NUMERIC
    )
    """,
]


def admin_connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(settings.admin_dsn, autocommit=autocommit)


def query_connect() -> psycopg.Connection:
    # 查询连接默认事务块，由 execute_lineage_query 统一提交/回滚
    return psycopg.connect(settings.query_dsn)


def initialize_schema() -> None:
    """创建物理表（幂等）。"""
    with admin_connect(autocommit=True) as conn:
        for stmt in DDL_STATEMENTS:
            conn.execute(stmt)


def count_rows() -> dict[str, int]:
    with admin_connect(autocommit=True) as conn:
        out = {}
        for table in TABLE_SCHEMA:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                out[table] = cur.fetchone()[0]
    return out


def assert_row_caps() -> None:
    counts = count_rows()
    for table, n in counts.items():
        if n > settings.max_rows:
            raise ValueError(
                f"表 {table} 现有 {n} 行，超过上限 {settings.max_rows} 行"
            )


@contextmanager
def snapshot_transaction() -> Iterator[
    tuple[psycopg.Connection, str, dict[str, str]]
]:
    """开启一个固定快照事务，返回 (连接, snapshot_id, 物理表->临时表映射)。

    临时表 ON COMMIT DROP：事务提交或回滚后自动释放；同一事务内所有语句
    都基于这一份快照，期间其他事务的增改删不影响本次结果。
    """
    snapshot_id = uuid.uuid4().hex
    snapshot_map = {
        table: f"{settings.temp_prefix}_{snapshot_id}_{table}"
        for table in TABLE_SCHEMA
    }
    conn = query_connect()
    try:
        # REPEATABLE READ：事务内所有读都取事务开始时的同一快照
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        # 临时表与 pg_temp 对象优先解析（溯源函数调用本身已显式限定）
        conn.execute('SET search_path TO pg_temp, public')
        for table, temp in snapshot_map.items():
            conn.execute(
                f"CREATE TEMP TABLE {temp} (LIKE {table} INCLUDING ALL) "
                f"ON COMMIT DROP"
            )
            conn.execute(f"INSERT INTO {temp} SELECT * FROM {table}")
        conn.execute(_PROV_SFUNC)
        conn.execute(_PROV_FINAL)
        conn.execute(_PROV_AGGREGATE_FINAL)
        conn.execute(_PROV_AGGREGATE_STATE)
        yield conn, snapshot_id, snapshot_map
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_snapshot_contents(
    conn: psycopg.Connection, snapshot_map: dict[str, str]
) -> dict[str, dict[int, dict[str, Any]]]:
    """读取快照中每张表的全部行内容，用于溯源回填。"""
    contents: dict[str, dict[int, dict[str, Any]]] = {}
    with conn.cursor(row_factory=dict_row) as cur:
        for table, temp in snapshot_map.items():
            cur.execute(f"SELECT * FROM {temp}")
            contents[table] = {row["id"]: row for row in cur.fetchall()}
    return contents


def execute_sql(conn: psycopg.Connection, sql: str) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql)
        return cur.fetchall()


# --------------------------------------------------------------------------- #
# 数据维护接口（各自短事务，立即生效，不影响进行中的旧快照事务）
# --------------------------------------------------------------------------- #
def upsert_sample(sample_id: int, batch: str) -> None:
    assert_row_caps()
    with admin_connect(autocommit=True) as conn:
        conn.execute(
            "INSERT INTO samples(id, batch) VALUES (%s, %s) "
            "ON CONFLICT (id) DO UPDATE SET batch = EXCLUDED.batch",
            (sample_id, batch),
        )
    assert_row_caps()


def delete_sample(sample_id: int) -> None:
    with admin_connect(autocommit=True) as conn:
        conn.execute("DELETE FROM results WHERE sample_id = %s", (sample_id,))
        conn.execute("DELETE FROM samples WHERE id = %s", (sample_id,))


def upsert_result(
    result_id: int, sample_id: int, value: float | int | None
) -> None:
    assert_row_caps()
    with admin_connect(autocommit=True) as conn:
        # 显式检查外键，给出可读错误
        exists = conn.execute(
            "SELECT 1 FROM samples WHERE id = %s", (sample_id,)
        ).fetchone()
        if not exists:
            raise ValueError(f"sample_id={sample_id} 不存在")
        conn.execute(
            "INSERT INTO results(id, sample_id, value) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET sample_id = EXCLUDED.sample_id, "
            "value = EXCLUDED.value",
            (result_id, sample_id, value),
        )
    assert_row_caps()


def delete_result(result_id: int) -> None:
    with admin_connect(autocommit=True) as conn:
        conn.execute("DELETE FROM results WHERE id = %s", (result_id,))


def _jsonable(value: Any) -> Any:
    """把 PostgreSQL 类型转成可 JSON 序列化的普通值。"""
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    return value


def list_table(table: str) -> list[dict[str, Any]]:
    if table not in TABLE_SCHEMA:
        raise ValueError(f"未知表 {table}")
    with admin_connect(autocommit=True) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT * FROM {table} ORDER BY id")
            return [
                {k: _jsonable(v) for k, v in row.items()}
                for row in cur.fetchall()
            ]


def reset_and_seed(rows: dict[str, list[dict[str, Any]]]) -> None:
    """整表重置（用于初始化/演示）。"""
    with admin_connect() as conn:
        conn.execute("TRUNCATE results, samples RESTART IDENTITY")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO samples(id, batch) VALUES (%(id)s, %(batch)s)",
                rows["samples"],
            )
            cur.executemany(
                "INSERT INTO results(id, sample_id, value) "
                "VALUES (%(id)s, %(sample_id)s, %(value)s)",
                rows["results"],
            )
        conn.commit()
