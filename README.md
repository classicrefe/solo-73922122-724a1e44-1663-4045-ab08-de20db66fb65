# 异常汇总溯源 API

帮助检验分析员回答「这条异常汇总由哪些样本构成」。提交一条受管 `SELECT`，
系统在**同一已提交快照**上执行查询，并对每一行结果返回：

- 原始结果列与值（列、值、重数与直接执行 SQL 完全一致）；
- `sources`：构成该行的每个源行，含
  - `table` / `row_id`：源表与源行 ID；
  - `row_content`：该源行在**快照当时**的完整内容；
  - `occurrence_count`：该源行对结果行的出现次数。

技术栈：FastAPI + PostgreSQL + SQLGlot。

## 一、快速启动（Docker Compose）

```bash
docker compose up --build
```

启动时 api 容器会自动幂等建表并写入预置演示数据（7 样本、8 检测）。
如需手动重新播种：

```bash
docker compose exec api python -m scripts.seed_db
```

- API 与 Swagger 文档：<http://localhost:8000/docs>
- OpenAPI：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/health>

停止：`docker compose down`（加 `-v` 同时清空数据卷）。

## 二、支持与拒绝的 SQL 边界

**支持（白名单，其余一律拒绝并返回 400）：**

- 单条 `SELECT`，可带 **一个非递归 CTE**，且 CTE 主体必须先 `GROUP BY` 聚合，
  再参与外层连接/汇总；
- `INNER JOIN` / `LEFT JOIN`，`ON` 为列与列的等值条件（可用 `AND` 连接多个）；
- `WHERE`、`GROUP BY`（含序号）、`HAVING`；
- 聚合仅 `COUNT` / `SUM`；
- 表达式：比较、`AND/OR/NOT`、`IS NULL`、`IN`、`BETWEEN`、`LIKE`、算术、
  `CASE WHEN`、字面量、`SELECT * / t.*`；
- 仅允许访问预置表 `samples(id, batch)` 与 `results(id, sample_id, value)`。

**明确拒绝：** 多语句/堆叠查询、DML/DDL、普通子查询、集合操作
（UNION 等）、窗口函数、递归 CTE、多个 CTE、RIGHT/FULL/CROSS/逗号连接、
非等值 ON、JOIN USING、`DISTINCT`、`ORDER BY/LIMIT/OFFSET`、COUNT/SUM 之外
的函数与聚合、未知表/列/别名、歧义列、保留列名 `__prov__` 等。

## 三、溯源语义（关键设计）

1. **固定快照**：每个请求在一个 `REPEATABLE READ` 事务内，把两张表复制到
   临时快照表，所有计算只针对快照。期间任意增改删不影响本次结果与来源；
   失败整体回滚，**不返回部分依据**。
2. **连接来源**：INNER JOIN 合并两侧来源；LEFT JOIN 右表未匹配的补空行
   只保留左表来源，补空不算来源。
3. **分组聚合**：组内来源按 `(表, 行ID)` 累加出现次数；分组列可以不全输出，
   不同组即使输出值相同，来源仍然分开，原结果列/值/重数不变。
4. **CTE 复用累加**：CTE 被内联为派生表，每次引用独立计算一遍，因此 CTE
   来源随外层重复使用而累加。
5. **空值**：`value` 可空；NULL 行仍是来源（出现在 `sources` 中，内容保留
   `null`），只是按 SQL 语义不参与 `SUM`。

溯源状态用 PostgreSQL jsonb 在 SQL 内随查询传播，配合两个会话级临时聚合
（状态聚合 `prov_agg_state` 与最终聚合 `prov_agg`）实现嵌套 CTE 的二次汇总。

## 四、HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/query` | 提交受管 SELECT，返回结果与逐行溯源 |
| PUT | `/samples` | 新增/修改样本 `{id, batch}` |
| DELETE | `/samples/{id}` | 删除样本（连带其检测行） |
| PUT | `/results` | 新增/修改检测 `{id, sample_id, value}`（value 可空） |
| DELETE | `/results/{id}` | 删除检测行 |
| GET | `/tables/{samples|results}` | 查看当前表内容 |
| GET | `/meta` | 表结构、行数、上限 |
| GET | `/health` | 健康检查 |

`POST /query` 响应示例（节选）：

```json
{
  "snapshot_id": "…",
  "submitted_sql": "SELECT batch, COUNT(*) c FROM samples GROUP BY batch",
  "columns": ["batch", "c"],
  "rows": [{"batch": "A", "c": 2}],
  "lineage": [
    {
      "sources": [
        {"table": "samples", "row_id": 1, "occurrence_count": 1,
         "row_content": {"id": 1, "batch": "A"}}
      ],
      "source_count": 1
    }
  ],
  "row_count": 1
}
```

更多带数据的例子见 [`examples/queries.md`](examples/queries.md)。

## 五、本地开发与测试（无 Docker 时）

```bash
pip install -r requirements.txt
export DATABASE_ADMIN_DSN=postgresql://user:pass@host:5432/lineage
export DATABASE_QUERY_DSN=$DATABASE_ADMIN_DSN
python -m scripts.seed_db
uvicorn app.main:app --reload

# 测试（TEST_DATABASE_DSN 指向一个可用 Postgres）
TEST_DATABASE_DSN=$DATABASE_ADMIN_DSN pytest -q
```

表行数各有 500 行上限，超限的写入会被拒绝（422）。

## 六、目录结构

```
app/
  config.py         环境配置
  db.py             连接、快照事务、临时溯源函数、数据维护
  sql_validate.py   SQL 白名单与语义校验
  sql_rewrite.py    AST 改写：快照替换 + jsonb 溯源传播
  service.py        校验→快照→改写→执行→回填源行内容
  main.py           FastAPI 路由
  seed.py           预置数据
scripts/            建库播种与冒烟脚本
tests/              校验器单元测试 + HTTP/溯源端到端测试
examples/           五类示例说明
docker-compose.yml  db + api 一键启动
```
