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

启动时 api 容器会自动幂等建表并写入预置演示数据（7 样本、8 检测）；
若 `samples` 已有数据则跳过播种，容器重启不会覆盖已提交的批次更正。
如需手动重新播种（`--reset` 会连同批次修订号与归档快照一起清空）：

```bash
docker compose exec api python -m scripts.seed_db
docker compose exec api python -m scripts.seed_db --reset
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

6. **历史快照归档**：批次更正会在同一事务内把更正前、更正后两份完整表内容
   写入 `snapshot_archive`（`/query?snapshot_id=…` 时加载为查询临时表），
   因此更正后的任意查询都能复现「更正前」或「更正后」当时的完整来源。

## 四、HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/query` | 提交受管 SELECT，返回结果与逐行溯源；可带 `snapshot_id` 追溯历史快照 |
| POST | `/batch-corrections` | 更正样本批次，返回受影响批次更正前后的异常汇总 |
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

### 4.1 批次更正 `POST /batch-corrections`

检验员更正样本批次时，需要先知道「异常汇总会怎样变化」。请求：

```json
{
  "sample_id": 1,
  "expected_batch": "A",
  "new_batch": "B",
  "threshold": 10
}
```

`expected_batch` 是调用方期望的旧批次（乐观锁）；`threshold` 为异常阈值，
**非空检测值总和 > 阈值** 记为超阈。

响应同时给出**原批次**（`role: "old"`）与**新批次**（`role: "new"`）在
**更正前快照**与**成功提交后**的样本数、非空检测值总和、是否超阈，并附两份
可追溯快照 ID：

```json
{
  "sample_id": 1,
  "expected_batch": "A",
  "new_batch": "B",
  "threshold": 10,
  "applied": true,
  "batches": [
    {"batch": "A", "role": "old",
     "before": {"sample_count": 2, "non_null_value_sum": 21, "exceeds_threshold": true},
     "after":  {"sample_count": 1, "non_null_value_sum": 7,  "exceeds_threshold": false},
     "before_snapshot_id": "batch-correction-before-…",
     "after_snapshot_id":  "batch-correction-after-…"},
    {"batch": "B", "role": "new",
     "before": {"sample_count": 2, "non_null_value_sum": 5,  "exceeds_threshold": false},
     "after":  {"sample_count": 3, "non_null_value_sum": 19, "exceeds_threshold": true},
     "before_snapshot_id": "batch-correction-before-…",
     "after_snapshot_id":  "batch-correction-after-…"}
  ]
}
```

语义与失败处理：

- **前后结论的来源**：`before` 取自事务开始时的固定快照；`after` 取自同一
  事务内**更正提交后**重新读取的数据，而非按增量推算。
- **原子性**：校验、前后统计、批次更新、两份快照归档都在同一事务内；任一步
  失败整体回滚，不会留下半次修改或半个归档快照。
- **竞争冲突**：若在「计算完成」到「提交」之间，**原批次或新批次**的样本/检测
  被其他请求改动并提交，整次更正返回 `409 concurrent_modification`，不应用
  基于过时数据的结论。**其他批次**的改动不影响本次更正（不会误报冲突）。
- **期望批次不符**：返回 `409 batch_precondition_failed`，附带 `current_batch`。
- **样本不存在**：返回 `404 sample_not_found`。
- **新批次不存在**时按空批次处理（`sample_count=0`、`non_null_value_sum=0`），
  更正会自然创建该批次。
- 未发生实际变化的重复写入（例如把同一检测行原样 upsert）不推进批次修订号，
  不会触发误报冲突。
- `expected_batch == new_batch` 时视为幂等更正，只返回一条前后相同的结论。

**并发检测实现**：`samples` / `results` 上的触发器维护 `batch_revision` 表，
每次写入都会在同一事务内推进受影响批次的修订号。更正事务在可重复读下先对
原/新两个批次修订行加锁；若这两行在本事务快照之后被其他已提交事务改写过，
PostgreSQL 的「先更新者胜」会抛出序列化失败，接口据此返回 409。其他批次的
修订行不参与加锁，因此不妨碍更正。

**追溯更正前/后来源**：把返回的 `before_snapshot_id` / `after_snapshot_id`
传给 `POST /query`，即可在**当时那份完整快照**上执行任意受管 SELECT，
`rows`、`lineage`、`row_content` 均为快照当时的样子：

```bash
curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' \
  -d '{"sql": "SELECT batch, COUNT(*) c FROM samples GROUP BY batch",
       "snapshot_id": "batch-correction-before-…"}'
```

指定不存在的 `snapshot_id` 返回 `404 snapshot_not_found`。不传 `snapshot_id`
时仍读取当前已提交数据。

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
  errors.py         批次更正的可预期失败（404/409）
  db.py             连接、快照事务与归档、批次修订触发器、更正事务、数据维护
  sql_validate.py   SQL 白名单与语义校验
  sql_rewrite.py    AST 改写：快照替换 + jsonb 溯源传播
  service.py        校验→快照→改写→执行→回填源行内容；批次更正结论
  main.py           FastAPI 路由
  seed.py           预置数据
scripts/            建库播种与冒烟脚本
tests/              校验器单元测试 + HTTP/溯源/批次更正（含竞争）端到端测试
examples/           示例说明
docker-compose.yml  db + api 一键启动
```
