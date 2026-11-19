#!/usr/bin/env bash
# 本地/容器内一键初始化预置数据
set -euo pipefail
python -m scripts.seed_db
