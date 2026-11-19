"""pytest 公共夹具：用内嵌 pgserver 起一个真实 PostgreSQL。

若环境变量 TEST_DATABASE_DSN 已提供（如 CI 中的容器 Postgres），直接使用；
否则尝试用 pgserver 在临时目录启动一个无 root 的内嵌实例。
"""
from __future__ import annotations

import os
import tempfile

import pytest

DSN = os.environ.get(
    "TEST_DATABASE_DSN",
    "postgresql://postgres:@/lineage_test?host=%2Ftmp%2Fpgdata",
)
os.environ["DATABASE_ADMIN_DSN"] = DSN
os.environ["DATABASE_QUERY_DSN"] = DSN


@pytest.fixture(scope="session")
def database():
    """初始化 schema 并在每个测试模块开始前播种。"""
    import psycopg

    # 若指向内嵌 pgserver，确保库存在（容忍残留连接导致的 DROP 失败）
    if "/tmp/pgdata" in DSN:
        admin = DSN.replace("/lineage_test?", "/postgres?")
        with psycopg.connect(admin, autocommit=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = 'lineage_test'"
            ).fetchone()
            if not exists:
                conn.execute("CREATE DATABASE lineage_test")

    from app import db
    from app.seed import seed_rows

    db.initialize_schema()
    db.reset_and_seed(seed_rows())
    yield db
    db.reset_and_seed(seed_rows())


@pytest.fixture()
def client(database):
    """每个测试函数前重置为种子数据，返回 TestClient。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.seed import seed_rows

    database.reset_and_seed(seed_rows())
    with TestClient(app) as c:
        yield c
