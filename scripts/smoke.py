"""手工端到端冒烟脚本（不依赖 pytest），覆盖关键溯源语义。"""
from __future__ import annotations

import os

DSN = "postgresql://postgres:@/lineage?host=/tmp/pgdata"
os.environ["DATABASE_ADMIN_DSN"] = DSN
os.environ["DATABASE_QUERY_DSN"] = DSN

from app.service import run_lineage_query
from app.sql_validate import UnsupportedSQLError, validate_sql


def show(title: str, sql: str) -> None:
    print("=" * 78)
    print(title)
    print("-" * 78)
    result = run_lineage_query(sql)
    print("columns:", result["columns"])
    for row, lin in zip(result["rows"], result["lineage"]):
        src = [(s["table"], s["row_id"], s["occurrence_count"]) for s in lin["sources"]]
        print(f"  row={row}  sources={src}  total={lin['source_count']}")


def expect_reject(label: str, sql: str) -> None:
    try:
        validate_sql(sql)
    except UnsupportedSQLError as exc:
        print(f"REJECT OK | {label}: {exc}")
        return
    raise AssertionError(f"{label} 应当被拒绝: {sql}")


# 1. 两层聚合 CTE + 外层 LEFT JOIN 汇总
show(
    "1) 两层聚合 CTE（先聚合再参与外层连接汇总）",
    """
    WITH batch_totals AS (
      SELECT s.batch AS batch, SUM(r.value) AS total
      FROM samples s
      INNER JOIN results r ON r.sample_id = s.id
      WHERE r.value IS NOT NULL
      GROUP BY s.batch
      HAVING SUM(r.value) > 10
    )
    SELECT b.batch, b.total, COUNT(s.id) AS sample_cnt
    FROM batch_totals b
    LEFT JOIN samples s ON s.batch = b.batch
    GROUP BY b.batch, b.total
    """,
)

# 2. 空值：SUM 忽略 NULL，COUNT 保留
show(
    "2) 空值处理（value 可空；SUM 忽略 NULL，COUNT(*) 统计行数）",
    """
    SELECT sample_id, COUNT(*) AS rows_cnt,
           COUNT(value) AS non_null_cnt, SUM(value) AS total
    FROM results
    GROUP BY sample_id
    HAVING COUNT(*) >= 1
    """,
)

# 3. LEFT JOIN 补空不算来源
show(
    "3) LEFT JOIN 补空（样本 7 无检测行，补空不引入 results 来源）",
    """
    SELECT s.id AS sample_id, r.id AS result_id, r.value
    FROM samples s
    LEFT JOIN results r ON r.sample_id = s.id
    """,
)

# 4. 同值不同组（分组列 batch 不输出，两组 COUNT 相同仍分开）
show(
    "4) 同值分组（隐藏分组列，输出相同值但来源必须分开）",
    "SELECT COUNT(*) AS c FROM samples GROUP BY batch",
)

# 5. CTE 重复使用 -> 来源累加（外层为非聚合连接，CTE 内先聚合）
show(
    "5) CTE 被外层重复引用（CTE 来源随每次引用累加）",
    """
    WITH per_batch AS (
      SELECT s.batch AS batch, COUNT(r.id) AS n
      FROM samples s
      LEFT JOIN results r ON r.sample_id = s.id
      GROUP BY s.batch
    )
    SELECT x.batch AS b1, y.batch AS b2, x.n + y.n AS summed
    FROM per_batch x
    INNER JOIN per_batch y ON y.batch = x.batch
    """,
)

# 6. 全局聚合（无 GROUP BY）
show(
    "6) 全局聚合（无 GROUP BY 的 COUNT/SUM）",
    "SELECT COUNT(*) AS c, SUM(value) AS s FROM results WHERE value IS NOT NULL",
)

# 7. 拒绝矩阵
rejects = {
    "子查询": "SELECT * FROM samples WHERE id IN (SELECT sample_id FROM results)",
    "UNION": "SELECT id FROM samples UNION SELECT id FROM results",
    "窗口函数": "SELECT ROW_NUMBER() OVER (ORDER BY id) rn FROM samples",
    "递归CTE": "WITH RECURSIVE t AS (SELECT 1 a) SELECT * FROM t",
    "两个CTE": "WITH a AS (SELECT id FROM samples GROUP BY id), b AS (SELECT id FROM results GROUP BY id) SELECT * FROM a, b",
    "多语句": "SELECT 1; SELECT 2",
    "DML": "DELETE FROM samples",
    "未知表": "SELECT * FROM orders",
    "未知列": "SELECT nope FROM samples",
    "歧义列": "SELECT id FROM samples s JOIN results r ON r.sample_id = s.id",
    "RIGHT JOIN": "SELECT * FROM samples s RIGHT JOIN results r ON r.sample_id = s.id",
    "非等值ON": "SELECT * FROM samples s JOIN results r ON r.value > s.id",
    "CROSS JOIN": "SELECT * FROM samples CROSS JOIN results",
    "AVG 聚合": "SELECT AVG(value) FROM results",
    "DISTINCT 聚合": "SELECT COUNT(DISTINCT sample_id) FROM results",
    "SELECT DISTINCT": "SELECT DISTINCT batch FROM samples",
    "ORDER BY": "SELECT id FROM samples ORDER BY id",
    "普通函数": "SELECT upper(batch) FROM samples",
    "CTE 未聚合": "WITH t AS (SELECT id FROM samples) SELECT * FROM t",
    "保留列名": "SELECT 1 AS __prov__ FROM samples",
}
for label, sql in rejects.items():
    expect_reject(label, sql)

print()
print("ALL SMOKE CHECKS PASSED")
