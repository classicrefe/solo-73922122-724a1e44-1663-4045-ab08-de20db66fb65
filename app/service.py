"""溯源查询服务：校验 -> 快照 -> 改写 -> 执行 -> 回填源行内容。"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from . import db
from .sql_rewrite import PROV_COLUMN, render_sql, rewrite
from .sql_validate import UnsupportedSQLError, validate_sql


class QueryFailure(RuntimeError):
    """查询执行失败；此时不返回任何部分依据。"""


def _jsonable(value: Any) -> Any:
    """把 PostgreSQL 类型转成可 JSON 序列化的普通值。"""
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    return value


def run_lineage_query(sql: str) -> dict[str, Any]:
    """完整执行一条受管 SELECT，返回原结果与逐行溯源。

    流程全部处于同一快照事务内：任何阶段失败都回滚，不返回部分依据。
    """
    # 1) 白名单校验（事务前先拦截非法 SQL）
    try:
        validated = validate_sql(sql)
    except UnsupportedSQLError:
        raise
    except Exception as exc:
        raise UnsupportedSQLError(f"SQL 无法解析：{exc}") from exc

    # 2) 固定快照事务
    with db.snapshot_transaction() as (conn, snapshot_id, snapshot_map):
        # 3) 生成携带 __prov__ 的溯源 SQL
        lineage_ast = rewrite(validated, snapshot_map)
        lineage_sql = render_sql(lineage_ast)

        # 4) 执行；执行错误（如类型不匹配）整体回滚
        try:
            rows = db.execute_sql(conn, lineage_sql)
        except Exception as exc:
            raise QueryFailure(f"查询执行失败：{exc}") from exc

        # 5) 读取快照内容用于回填源行当时内容
        contents = db.fetch_snapshot_contents(conn, snapshot_map)

    # 6) 拆分原结果列与溯源列，保证原结果列、值与重数不变
    result_columns = [k for k in (rows[0].keys() if rows else []) if k != PROV_COLUMN]
    result_rows = [
        {col: _jsonable(row[col]) for col in result_columns} for row in rows
    ]

    lineage_rows = []
    for row in rows:
        sources = []
        for entry in row.get(PROV_COLUMN) or []:
            table = entry["t"]
            row_id = entry["id"]
            occurrence = entry["n"]
            snapshot_row = contents[table].get(row_id)
            if snapshot_row is None:
                # 理论上不可能：溯源 ID 全部来自同一份快照
                raise QueryFailure("溯源引用了快照中不存在的行")
            sources.append(
                {
                    "table": table,
                    "row_id": row_id,
                    "occurrence_count": occurrence,
                    "row_content": {
                        k: _jsonable(v) for k, v in snapshot_row.items()
                    },
                }
            )
        lineage_rows.append(
            {
                "sources": sources,
                "source_count": sum(s["occurrence_count"] for s in sources),
            }
        )

    return {
        "snapshot_id": snapshot_id,
        "submitted_sql": sql,
        "columns": result_columns,
        "rows": result_rows,
        "lineage": lineage_rows,
        "row_count": len(result_rows),
    }
