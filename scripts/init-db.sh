#!/bin/sh
# PostgreSQL 容器首次启动时执行：应用账号本身即库 owner，无需额外角色。
set -e
echo "init-db: database ${POSTGRES_DB} ready for user ${POSTGRES_USER}"
