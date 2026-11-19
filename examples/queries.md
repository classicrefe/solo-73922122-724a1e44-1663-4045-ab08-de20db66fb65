# 示例请求与预期溯源行为

以下示例基于 `scripts/seed_db.py` 的预置数据（7 个样本、8 条检测，含空值与
无检测的样本）。启动服务后可直接用 curl 复现，也可在 `http://localhost:8000/docs`
交互执行。

## 示例 1：两层聚合 —— CTE 先聚合，再参与外层连接汇总

场景：先按批次汇总检测值，筛出总和异常（> 10）的批次，再统计该批次样本数。

```sql
WITH batch_totals AS (
  SELECT s.batch AS batch, SUM(r.value) AS total
  FROM samples s
  INNER JOIN results r ON r.sample_id = s.id
  WHERE r.value IS NOT NULL
  GROUP BY s.batch
  HAVING SUM(r.value) > 10
)
SELECT b.batch, b.total, COUNT(s.id) AS sample_cnt
FROM batch_totals b
LEFT JOIN samples s ON s.batch = b.batch
GROUP BY b.batch, b.total;
```

预期：只有批次 A（8+6+7=21）通过 HAVING。返回 1 行
`{batch: A, total: 21, sample_cnt: 2}`，溯源同时包含：

- CTE 内参与求和的检测行 results 101/102/103（外层连接使各累计 2 次）；
- 外层 LEFT JOIN 匹配到的样本 samples 1、2。

```bash
curl -s -X POST localhost:8000/query -H 'Content-Type: application/json' \
  -d '{"sql": "WITH batch_totals AS (SELECT s.batch AS batch, SUM(r.value) AS total FROM samples s INNER JOIN results r ON r.sample_id = s.id WHERE r.value IS NOT NULL GROUP BY s.batch HAVING SUM(r.value) > 10) SELECT b.batch, b.total, COUNT(s.id) AS sample_cnt FROM batch_totals b LEFT JOIN samples s ON s.batch = b.batch GROUP BY b.batch, b.total"}'
```

## 示例 2：空值 —— COUNT 保留行、SUM 忽略 NULL，NULL 行仍是来源

样本 3 有两条检测，其中 105 的 `value` 为 NULL。

```sql
SELECT sample_id,
       COUNT(*)        AS rows_cnt,
       COUNT(value)    AS non_null_cnt,
       SUM(value)      AS total
FROM results
GROUP BY sample_id;
```

预期 sample_id=3 一行：`rows_cnt=2, non_null_cnt=1, total=3`，溯源同时包含
results 104 与 105，且 105 的 `row_content.value` 为 `null`（空值行计入来源，
只是不参与 SUM）。

## 示例 3：LEFT JOIN 补空不算来源

样本 7 没有任何检测行：

```sql
SELECT s.id AS sample_id, r.id AS result_id, r.value
FROM samples s
LEFT JOIN results r ON r.sample_id = s.id;
```

预期输出中样本 7 对应一行 `result_id=null, value=null`，其 `sources` 只含
`samples/7`，不出现任何 results 行 —— 右表补空不产生来源。

## 示例 4：同值不同组 —— 分组列不输出，输出同值但来源分开

```sql
SELECT COUNT(*) AS c FROM samples GROUP BY batch;
```

预期 3 行，计数为 `2, 3, 2`。两个值同为 2 的行（批次 A、B）在结果中分别
独立存在，溯源分别指向 `{1,2}` 与 `{3,4}`，不会因为输出值相同而合并；
原结果的列、值与重数保持不变。

## 示例 5：CTE 重复使用 —— 来源随每次引用累加

```sql
WITH per_batch AS (
  SELECT s.batch AS batch, COUNT(r.id) AS n
  FROM samples s
  LEFT JOIN results r ON r.sample_id = s.id
  GROUP BY s.batch
)
SELECT x.batch AS b1, y.batch AS b2, x.n + y.n AS summed
FROM per_batch x
INNER JOIN per_batch y ON y.batch = x.batch;
```

CTE 被外层引用两次，每次引用都独立内联一份，因此每个源行的出现次数都按
引用次数累加（一对多连接处还会按连接重数放大），在 `occurrence_count` 中体现。

## 数据维护（在两次查询之间制造变更，验证快照独立）

```bash
# 修改样本批次
curl -s -X PUT localhost:8000/samples -H 'Content-Type: application/json' \
  -d '{"id": 1, "batch": "A-MUTATED"}'
# 新增检测（value 可空）
curl -s -X PUT localhost:8000/results -H 'Content-Type: application/json' \
  -d '{"id": 999, "sample_id": 2, "value": null}'
# 删除
curl -s -X DELETE localhost:8000/samples/7
# 查看当前内容
curl -s localhost:8000/tables/samples
```

任何进行中的查询都固定在自己提交时的快照，变更只影响之后的新请求。
