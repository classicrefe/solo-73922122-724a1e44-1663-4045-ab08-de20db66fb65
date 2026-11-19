"""运行期配置。所有设置均来自环境变量，延迟读取以便测试注入。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # 读写连接（建快照临时表、健康检查、数据维护接口）
    admin_dsn: str
    # 查询连接（默认同 admin；Docker 中可指向只读角色）
    query_dsn: str
    max_rows: int = 500
    # 快照临时表名前缀，按请求唯一化
    temp_prefix: str = "snap"


def _load() -> Settings:
    admin = os.environ.get(
        "DATABASE_ADMIN_DSN",
        "postgresql://app:app@localhost:5432/lineage",
    )
    query = os.environ.get("DATABASE_QUERY_DSN", admin)
    return Settings(admin_dsn=admin, query_dsn=query)


def __getattr__(name: str):
    # 模块级 settings 每次访问都按当前环境变量重新计算
    if name == "settings":
        return _load()
    raise AttributeError(name)
