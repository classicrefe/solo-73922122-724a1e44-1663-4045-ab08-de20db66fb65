"""白名单校验器单元测试：覆盖所有明确拒绝的语法。"""
from __future__ import annotations

import pytest

from app.sql_validate import UnsupportedSQLError, validate_sql

ACCEPTED = [
    "SELECT id FROM samples",
    "SELECT s.id, r.value FROM samples s INNER JOIN results r ON r.sample_id = s.id",
    "SELECT s.id FROM samples s LEFT JOIN results r ON r.sample_id = s.id",
    "SELECT * FROM samples s JOIN results r ON r.sample_id = s.id AND s.id = r.sample_id",
    "SELECT batch, COUNT(*) c, SUM(value) s FROM results r "
    "JOIN samples s ON s.id = r.sample_id WHERE r.value IS NOT NULL "
    "GROUP BY s.batch HAVING SUM(r.value) > 1",
    "SELECT batch FROM samples WHERE batch IN ('A', 'B') AND id BETWEEN 1 AND 5",
    "WITH t AS (SELECT batch, COUNT(*) c FROM samples GROUP BY batch) "
    "SELECT t.batch, t.c FROM t JOIN samples s ON s.batch = t.batch",
    "SELECT COUNT(*) FROM samples GROUP BY batch HAVING COUNT(*) >= 2",
    "SELECT CASE WHEN value IS NULL THEN 0 ELSE value END FROM results",
    "SELECT -id + 1, (id * 2) FROM samples WHERE NOT (id = 1) OR id <= 3",
]

REJECTED = {
    "子查询": "SELECT * FROM samples WHERE id IN (SELECT sample_id FROM results)",
    "WHERE 子查询": "SELECT id FROM samples WHERE EXISTS (SELECT 1 FROM results)",
    "UNION": "SELECT id FROM samples UNION SELECT id FROM results",
    "INTERSECT": "SELECT id FROM samples INTERSECT SELECT sample_id FROM results",
    "窗口函数": "SELECT ROW_NUMBER() OVER (ORDER BY id) rn FROM samples",
    "递归 CTE": "WITH RECURSIVE t AS (SELECT 1 a) SELECT * FROM t",
    "两个 CTE": "WITH a AS (SELECT id, COUNT(*) c FROM samples GROUP BY id), "
    "b AS (SELECT id FROM results GROUP BY id) SELECT * FROM a, b",
    "多语句": "SELECT 1; SELECT 2",
    "INSERT": "INSERT INTO samples VALUES (1, 'A')",
    "UPDATE": "UPDATE samples SET batch = 'X' WHERE id = 1",
    "DELETE": "DELETE FROM samples",
    "DDL": "DROP TABLE samples",
    "未知表": "SELECT * FROM orders",
    "未知列": "SELECT nope FROM samples",
    "未知别名": "SELECT z.id FROM samples",
    "歧义列": "SELECT id FROM samples s JOIN results r ON r.sample_id = s.id",
    "RIGHT JOIN": "SELECT * FROM samples s RIGHT JOIN results r ON r.sample_id = s.id",
    "FULL JOIN": "SELECT * FROM samples s FULL JOIN results r ON r.sample_id = s.id",
    "非等值 ON": "SELECT * FROM samples s JOIN results r ON r.value > s.id",
    "CROSS JOIN": "SELECT * FROM samples CROSS JOIN results",
    "逗号连接": "SELECT * FROM samples, results",
    "JOIN USING": "SELECT * FROM samples s JOIN results r USING (id)",
    "AVG": "SELECT AVG(value) FROM results",
    "MIN": "SELECT MIN(value) FROM results",
    "COUNT DISTINCT": "SELECT COUNT(DISTINCT sample_id) FROM results",
    "SUM DISTINCT": "SELECT SUM(DISTINCT value) FROM results",
    "SELECT DISTINCT": "SELECT DISTINCT batch FROM samples",
    "ORDER BY": "SELECT id FROM samples ORDER BY id",
    "LIMIT": "SELECT id FROM samples LIMIT 1",
    "普通函数": "SELECT upper(batch) FROM samples",
    "CTE 未聚合": "WITH t AS (SELECT id FROM samples) SELECT * FROM t",
    "保留列名": "SELECT 1 AS __prov__ FROM samples",
    "BETWEEN SYMMETRIC": "SELECT id FROM samples WHERE id BETWEEN SYMMETRIC 1 AND 3",
    "CAST": "SELECT id::text FROM samples",
    "空 SQL": "   ",
}


@pytest.mark.parametrize("sql", ACCEPTED)
def test_accepted(sql):
    assert validate_sql(sql) is not None


@pytest.mark.parametrize("label,sql", list(REJECTED.items()), ids=list(REJECTED))
def test_rejected(label, sql):
    with pytest.raises(UnsupportedSQLError):
        validate_sql(sql)
