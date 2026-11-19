"""把 app/seed.py 的预置数据写入数据库（幂等整表重置）。"""
from __future__ import annotations

from app import db
from app.seed import seed_rows


def main() -> None:
    db.initialize_schema()
    db.reset_and_seed(seed_rows())
    counts = db.count_rows()
    print(f"seed complete: {counts}")


if __name__ == "__main__":
    main()
