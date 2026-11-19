"""预置演示数据（每张表远少于 500 行上限）。

场景设计：
- 批次 A/B/C 三组样本，其中 B、C 的 COUNT 相同但来源不同（同值不同组）；
- results.value 含 NULL，用于演示空值与 LEFT JOIN 补空；
- 批次 A 的 SUM 超过阈值，构成「异常汇总」，可由两层聚合 CTE 追溯到样本。
"""
from __future__ import annotations

SEED_SAMPLES = [
    {"id": 1, "batch": "A"},
    {"id": 2, "batch": "A"},
    {"id": 3, "batch": "B"},
    {"id": 4, "batch": "B"},
    {"id": 5, "batch": "C"},
    {"id": 6, "batch": "C"},
    {"id": 7, "batch": "C"},
]

SEED_RESULTS = [
    # 批次 A：数值较高，汇总后触发异常阈值
    {"id": 101, "sample_id": 1, "value": 8},
    {"id": 102, "sample_id": 1, "value": 6},
    {"id": 103, "sample_id": 2, "value": 7},
    # 批次 B：含空值
    {"id": 104, "sample_id": 3, "value": 3},
    {"id": 105, "sample_id": 3, "value": None},
    {"id": 106, "sample_id": 4, "value": 2},
    # 批次 C：整体偏低，且样本 7 没有任何检测行（LEFT JOIN 补空）
    {"id": 107, "sample_id": 5, "value": 3},
    {"id": 108, "sample_id": 6, "value": 2},
]


def seed_rows() -> dict[str, list[dict]]:
    return {
        "samples": [dict(r) for r in SEED_SAMPLES],
        "results": [dict(r) for r in SEED_RESULTS],
    }
