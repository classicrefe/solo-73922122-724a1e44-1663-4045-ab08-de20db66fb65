"""PostgreSQL 访问层：schema 初始化、快照事务、溯源函数、数据维护。"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from .config import settings
from .errors import (
    BatchPreconditionFailed,
    ConcurrentModification,
    SampleNotFound,
    SnapshotNotFound,
)
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
    # 每个批次一个单调递增的修订号：samples/results 的任一写入都会在
    # 同一事务内推进受影响批次的修订号，用于更正接口的乐观并发检测。
    """
    CREATE TABLE IF NOT EXISTS batch_revision (
      batch    VARCHAR PRIMARY KEY,
      revision BIGINT NOT NULL DEFAULT 0
    )
    """,
    # 持久化快照归档：更正前后的完整表内容，供 /query?snapshot_id=... 追溯。
    """
    CREATE TABLE IF NOT EXISTS snapshot_archive (
      snapshot_id VARCHAR NOT NULL,
      table_name  VARCHAR NOT NULL,
      row_id      BIGINT  NOT NULL,
      row_data    JSONB   NOT NULL,
      PRIMARY KEY (snapshot_id, table_name, row_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshot_meta (
      snapshot_id       VARCHAR PRIMARY KEY,
      kind              VARCHAR NOT NULL,
      related_sample_id INTEGER,
      created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE OR REPLACE FUNCTION bump_batch_revision(b VARCHAR)
    RETURNS void
    LANGUAGE plpgsql AS $$
    BEGIN
      INSERT INTO batch_revision(batch, revision) VALUES (b, 1)
      ON CONFLICT (batch) DO UPDATE
        SET revision = batch_revision.revision + 1;
    END;
    $$
    """,
    # 样本批次变化会同时影响原批次与新批次；按批次名排序推进，减少死锁。
    """
    CREATE OR REPLACE FUNCTION samples_batch_revision_trg()
    RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        PERFORM bump_batch_revision(NEW.batch);
      ELSIF TG_OP = 'DELETE' THEN
        PERFORM bump_batch_revision(OLD.batch);
      ELSIF OLD.batch IS DISTINCT FROM NEW.batch THEN
        IF OLD.batch < NEW.batch THEN
          PERFORM bump_batch_revision(OLD.batch);
          PERFORM bump_batch_revision(NEW.batch);
        ELSE
          PERFORM bump_batch_revision(NEW.batch);
          PERFORM bump_batch_revision(OLD.batch);
        END IF;
      END IF;
      RETURN NULL;
    END;
    $$
    """,
    """
    DROP TRIGGER IF EXISTS samples_batch_revision ON samples
    """,
    """
    CREATE TRIGGER samples_batch_revision
    AFTER INSERT OR UPDATE OR DELETE ON samples
    FOR EACH ROW EXECUTE FUNCTION samples_batch_revision_trg()
    """,
    # 检测行的任何**实际**变更都会推进其所属样本当前批次（以及 sample_id
    # 变更时的旧批次）的修订号。
    """
    CREATE OR REPLACE FUNCTION results_batch_revision_trg()
    RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE
      old_batch VARCHAR;
      new_batch VARCHAR;
    BEGIN
      -- 相同的 upsert 不算改动，不推进修订号，避免误报冲突
      IF TG_OP = 'UPDATE'
         AND OLD.sample_id IS NOT DISTINCT FROM NEW.sample_id
         AND OLD.value IS NOT DISTINCT FROM NEW.value THEN
        RETURN NULL;
      END IF;
      IF TG_OP IN ('INSERT', 'UPDATE') THEN
        SELECT batch INTO new_batch FROM samples WHERE id = NEW.sample_id;
      END IF;
      IF TG_OP IN ('UPDATE', 'DELETE') THEN
        SELECT batch INTO old_batch FROM samples WHERE id = OLD.sample_id;
      END IF;
      IF old_batch IS NULL THEN
        PERFORM bump_batch_revision(new_batch);
      ELSIF TG_OP = 'DELETE' OR new_batch IS NULL THEN
        PERFORM bump_batch_revision(old_batch);
      ELSIF old_batch = new_batch THEN
        PERFORM bump_batch_revision(old_batch);
      ELSIF old_batch < new_batch THEN
        PERFORM bump_batch_revision(old_batch);
        PERFORM bump_batch_revision(new_batch);
      ELSE
        PERFORM bump_batch_revision(new_batch);
        PERFORM bump_batch_revision(old_batch);
      END IF;
      RETURN NULL;
    END;
    $$
    """,
    """
    DROP TRIGGER IF EXISTS results_batch_revision ON results
    """,
    """
    CREATE TRIGGER results_batch_revision
    AFTER INSERT OR UPDATE OR DELETE ON results
    FOR EACH ROW EXECUTE FUNCTION results_batch_revision_trg()
    """,
]


def admin_connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(settings.admin_dsn, autocommit=autocommit)


def query_connect() -> psycopg.Connection:
    # 查询连接默认事务块，由 execute_lineage_query 统一提交/回滚
    return psycopg.connect(settings.query_dsn)


def initialize_schema() -> None:
    """创建物理表、批次修订触发器与快照归档（幂等）。"""
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
def snapshot_transaction(
    archived_snapshot_id: str | None = None,
) -> Iterator[tuple[psycopg.Connection, str, dict[str, str]]]:
    """开启一个固定快照事务，返回 (连接, snapshot_id, 物理表->临时表映射)。

    临时表 ON COMMIT DROP：事务提交或回滚后自动释放；同一事务内所有语句
    都基于这一份快照，期间其他事务的增改删不影响本次结果。

    传入 ``archived_snapshot_id`` 时，不读取当前表，而是读取更正接口归档的
    历史快照；这样 ``/query`` 可以追溯更正前或更正后的完整来源。
    """
    internal_id = uuid.uuid4().hex
    snapshot_map = {
        table: f"{settings.temp_prefix}_{internal_id}_{table}"
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
        if archived_snapshot_id is None:
            for table, temp in snapshot_map.items():
                conn.execute(f"INSERT INTO {temp} SELECT * FROM {table}")
            result_snapshot_id = internal_id
        else:
            _load_archived_snapshot(conn, archived_snapshot_id, snapshot_map)
            result_snapshot_id = archived_snapshot_id
        conn.execute(_PROV_SFUNC)
        conn.execute(_PROV_FINAL)
        conn.execute(_PROV_AGGREGATE_FINAL)
        conn.execute(_PROV_AGGREGATE_STATE)
        yield conn, result_snapshot_id, snapshot_map
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _load_archived_snapshot(
    conn: psycopg.Connection,
    archived_snapshot_id: str,
    snapshot_map: dict[str, str],
) -> None:
    """把归档快照装入本次查询的临时表。

    归档以 jsonb 保存整行；这里用 ``->>`` 取文本后按列类型回写，避免经
    Python float 往返造成 NUMERIC 精度损失。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM snapshot_meta WHERE snapshot_id = %s",
            (archived_snapshot_id,),
        )
        if cur.fetchone() is None:
            raise SnapshotNotFound(archived_snapshot_id)
        for table, temp in snapshot_map.items():
            columns = list(TABLE_SCHEMA[table])
            select_cols = ", ".join(
                f"(row_data ->> '{col}')::{TABLE_SCHEMA[table][col]}"
                for col in columns
            )
            cur.execute(
                f"INSERT INTO {temp} ({', '.join(columns)}) "
                f"SELECT {select_cols} FROM snapshot_archive "
                f"WHERE snapshot_id = %s AND table_name = %s ORDER BY row_id",
                (archived_snapshot_id, table),
            )


# --------------------------------------------------------------------------- #
# 批次更正：统计、乐观并发检测、原批次/新批次联动修订
# --------------------------------------------------------------------------- #
def batch_stats(conn: psycopg.Connection, batch: str) -> dict[str, Any]:
    """统计某批次在给定快照/事务视图内的样本数、非空检测值总和与样本 ID。"""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id FROM samples WHERE batch = %s ORDER BY id", (batch,)
        )
        sample_ids = [row["id"] for row in cur.fetchall()]
        if not sample_ids:
            return {
                "batch": batch,
                "sample_count": 0,
                "non_null_value_sum": 0,
                "sample_ids": [],
            }
        cur.execute(
            "SELECT COALESCE(SUM(value), 0) AS total FROM results "
            "WHERE sample_id = ANY(%s) AND value IS NOT NULL",
            (sample_ids,),
        )
        total = cur.fetchone()["total"]
    return {
        "batch": batch,
        "sample_count": len(sample_ids),
        "non_null_value_sum": _jsonable(total),
        "sample_ids": sample_ids,
    }


def _lock_batch_revisions(
    conn: psycopg.Connection, batches: list[str]
) -> None:
    """在可重复读事务内锁定批次修订行，实现「先更新者胜」的冲突检测。

    ``INSERT ... ON CONFLICT DO UPDATE SET revision = revision`` 会取得该
    批次修订行上的写锁。若该行在本事务快照之后被其他已提交事务改写过
    （触发器对原批次/新批次的任何 samples/results 写入都会推进修订号），
    PostgreSQL 在可重复读下会抛出 SerializationFailure，由调用方映射成
    409 冲突。其他批次的修订行不在加锁集合内，因此不会互相妨碍。
    """
    for batch in batches:
        conn.execute(
            "INSERT INTO batch_revision(batch, revision) VALUES (%s, 0) "
            "ON CONFLICT (batch) DO UPDATE "
            "SET revision = batch_revision.revision",
            (batch,),
        )


def correct_sample_batch(
    sample_id: int,
    expected_batch: str,
    new_batch: str,
) -> dict[str, Any]:
    """原子地更正样本批次，返回前后快照统计与两个可追溯快照 ID。

    全过程在一个可重复读事务内：

    1. 读样本并校验期望旧批次；
    2. 在事务开始的固定快照上计算「更正前」统计并归档该快照；
    3. 对原批次与新批次的修订行加锁（会等待并发写入）；
    4. 在同一事务内更新样本批次，并据此重新读取「更正后」统计与归档。

    可重复读的「先更新者胜」保证：若原批次或新批次的样本/检测在本事务
    快照之后被其他请求改动并提交，第 3 步加锁时会抛出 SerializationFailure，
    整次更正回滚并返回冲突；其他批次的行不参与加锁，因此不受影响。

    任一步失败整体回滚：不会留下半次修改，也不会留下半个归档快照。
    """
    conn = admin_connect()
    try:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.execute("SET search_path TO public")

        row = conn.execute(
            "SELECT batch FROM samples WHERE id = %s", (sample_id,)
        ).fetchone()
        if row is None:
            raise SampleNotFound(sample_id)
        current_batch = row[0]
        if current_batch != expected_batch:
            raise BatchPreconditionFailed(
                sample_id, expected_batch, current_batch
            )

        # 「更正前」结论：事务开始快照上，原批次与新批次各自的统计。
        before = {
            current_batch: batch_stats(conn, current_batch),
            new_batch: batch_stats(conn, new_batch),
        }
        before_snapshot_id = _archive_current_snapshot(
            conn, "batch-correction-before", sample_id
        )

        # 先锁定两个批次修订行；不同更正按批次名排序加锁，避免死锁。
        batches = sorted({current_batch, new_batch})
        try:
            _lock_batch_revisions(conn, batches)
        except psycopg.errors.SerializationFailure as exc:
            raise ConcurrentModification(",".join(batches)) from exc

        updated = conn.execute(
            "UPDATE samples SET batch = %s WHERE id = %s AND batch = %s",
            (new_batch, sample_id, expected_batch),
        ).rowcount
        if updated != 1:
            raise ConcurrentModification(",".join(batches))

        # 「更正后」结论直接取更正完成后的本事务视图，而非投影推算。
        after = {
            current_batch: batch_stats(conn, current_batch),
            new_batch: batch_stats(conn, new_batch),
        }
        after_snapshot_id = _archive_current_snapshot(
            conn, "batch-correction-after", sample_id
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "sample_id": sample_id,
        "expected_batch": expected_batch,
        "new_batch": new_batch,
        "before": before,
        "after": after,
        "before_snapshot_id": before_snapshot_id,
        "after_snapshot_id": after_snapshot_id,
    }


def _archive_current_snapshot(
    conn: psycopg.Connection, kind: str, related_sample_id: int
) -> str:
    """把当前事务视图内的两张表完整归档，返回 snapshot_id。"""
    snapshot_id = kind.replace("_", "-") + "-" + uuid.uuid4().hex
    conn.execute(
        "INSERT INTO snapshot_meta(snapshot_id, kind, related_sample_id) "
        "VALUES (%s, %s, %s)",
        (snapshot_id, kind, related_sample_id),
    )
    for table in TABLE_SCHEMA:
        conn.execute(
            "INSERT INTO snapshot_archive(snapshot_id, table_name, row_id, "
            "row_data) SELECT %s, %s, id, to_jsonb(t) FROM "
            f"(SELECT * FROM {table}) t",
            (snapshot_id, table),
        )
    return snapshot_id


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


def seed_if_empty(rows: dict[str, list[dict[str, Any]]]) -> bool:
    """仅当 samples 表为空时写入预置数据；返回是否执行了写入。

    容器重启不会覆盖已提交的更正与归档快照，`--reset` 才整表重置。
    """
    with admin_connect() as conn:
        # 串行化并发启动：拿到 advisory lock 后再判断是否为空
        conn.execute("SELECT pg_advisory_xact_lock(918273645)")
        count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        if count:
            conn.commit()
            return False
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
    return True


def reset_and_seed(rows: dict[str, list[dict[str, Any]]]) -> None:
    """整表重置（用于初始化/演示）。"""
    with admin_connect() as conn:
        # 触发器在 TRUNCATE 上不触发，显式清空修订号与归档快照，
        # 避免旧快照/旧修订号泄漏到新一轮演示数据。
        conn.execute(
            "TRUNCATE results, samples, snapshot_archive, snapshot_meta, "
            "batch_revision RESTART IDENTITY"
        )
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
