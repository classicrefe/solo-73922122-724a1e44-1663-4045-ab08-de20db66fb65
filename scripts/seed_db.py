"""初始化 schema 并写入预置数据。

- 默认：仅在 samples 为空时播种（容器重启不会覆盖已提交的批次更正）；
- ``--reset``：整表重置为预置数据（连同批次修订号与归档快照一起清空）。
"""
from __future__ import annotations

import sys

from app import db
from app.seed import seed_rows


def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    reset = "--reset" in args
    db.initialize_schema()
    if reset:
        db.reset_and_seed(seed_rows())
        action = "reset+seed"
    else:
        action = "seed" if db.seed_if_empty(seed_rows()) else "skipped (non-empty)"
    counts = db.count_rows()
    print(f"init complete: {action}; counts={counts}")


if __name__ == "__main__":
    main()
