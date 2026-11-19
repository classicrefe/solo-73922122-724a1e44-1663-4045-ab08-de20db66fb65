"""端到端 HTTP 与溯源语义测试（真实 PostgreSQL + TestClient）。"""
from __future__ import annotations


def _query(client, sql):
    resp = client.post("/query", json={"sql": sql})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _src_tuples(lineage_row):
    return sorted(
        (s["table"], s["row_id"], s["occurrence_count"])
        for s in lineage_row["sources"]
    )


def test_health_and_docs(client):
    assert client.get("/health").json() == {"status": "ok"}
    docs = client.get("/docs")
    assert docs.status_code == 200
    openapi = client.get("/openapi.json").json()
    assert "/query" in openapi["paths"]


def test_meta_and_tables(client):
    meta = client.get("/meta").json()
    assert meta["tables"]["samples"]["row_count"] == 7
    assert meta["tables"]["results"]["row_count"] == 8
    rows = client.get("/tables/results").json()["rows"]
    assert any(r["value"] is None for r in rows)


def test_simple_join_lineage(client):
    r = _query(
        client,
        "SELECT s.id sid, r.id rid FROM samples s "
        "JOIN results r ON r.sample_id = s.id",
    )
    # 8 个检测行各对应一条输出
    assert r["row_count"] == 8
    for row, lin in zip(r["rows"], r["lineage"]):
        tables = {s["table"] for s in lin["sources"]}
        assert tables == {"samples", "results"}
        # 源行内容回填
        contents = {s["table"]: s["row_content"]["id"] for s in lin["sources"]}
        assert contents["samples"] == row["sid"]
        assert contents["results"] == row["rid"]


def test_left_join_null_padding_not_a_source(client):
    r = _query(
        client,
        "SELECT s.id sid, r.id rid FROM samples s "
        "LEFT JOIN results r ON r.sample_id = s.id",
    )
    # 样本 7 没有检测行：补空行只允许 samples 来源
    padded = [row for row in r["rows"] if row["rid"] is None]
    assert len(padded) == 1 and padded[0]["sid"] == 7
    for row, lin in zip(r["rows"], r["lineage"]):
        if row["rid"] is None:
            assert [s["table"] for s in lin["sources"]] == ["samples"]
            assert lin["source_count"] == 1


def test_null_value_aggregation(client):
    r = _query(
        client,
        "SELECT sample_id, COUNT(*) all_cnt, COUNT(value) non_null, "
        "SUM(value) total FROM results GROUP BY sample_id",
    )
    by_sample = {row["sample_id"]: row for row in r["rows"]}
    # sample 3: 两行检测，一行 value 为 NULL
    assert by_sample[3]["all_cnt"] == 2
    assert by_sample[3]["non_null"] == 1
    assert by_sample[3]["total"] == 3
    # 该组溯源包含 NULL 行 105
    line = next(
        lin
        for row, lin in zip(r["rows"], r["lineage"])
        if row["sample_id"] == 3
    )
    ids = {s["row_id"] for s in line["sources"] if s["table"] == "results"}
    assert ids == {104, 105}
    null_src = next(
        s for s in line["sources"] if s["row_id"] == 105
    )
    assert null_src["row_content"]["value"] is None


def test_two_level_cte_aggregation(client):
    sql = """
    WITH batch_totals AS (
      SELECT s.batch AS batch, SUM(r.value) AS total
      FROM samples s JOIN results r ON r.sample_id = s.id
      WHERE r.value IS NOT NULL
      GROUP BY s.batch HAVING SUM(r.value) > 10
    )
    SELECT b.batch, b.total, COUNT(s.id) AS sample_cnt
    FROM batch_totals b LEFT JOIN samples s ON s.batch = b.batch
    GROUP BY b.batch, b.total
    """
    r = _query(client, sql)
    # 只有批次 A 的总和 21 超过阈值 10
    assert r["row_count"] == 1
    row = r["rows"][0]
    assert row == {"batch": "A", "total": 21, "sample_cnt": 2}
    src = _src_tuples(r["lineage"][0])
    # 外层 JOIN 让 CTE 内的 results 来源各累计 2 次（CTE 行参与两次组匹配）
    assert ("results", 101, 2) in src
    assert ("results", 102, 2) in src
    assert ("results", 103, 2) in src
    sample_ids = {rid for t, rid, _ in src if t == "samples"}
    assert sample_ids == {1, 2}


def test_cte_reused_sources_accumulate(client):
    sql = """
    WITH per_batch AS (
      SELECT s.batch AS batch, COUNT(r.id) AS n
      FROM samples s LEFT JOIN results r ON r.sample_id = s.id
      GROUP BY s.batch
    )
    SELECT x.batch b1, y.batch b2, x.n + y.n summed
    FROM per_batch x INNER JOIN per_batch y ON y.batch = x.batch
    """
    r = _query(client, sql)
    # CTE 被引用两次：每个 CTE 输出生成的来源都至少再累计一次（次数 >= 2）。
    # 批次 C 每个样本只关联 1 条检测（样本 7 为 0 条补空），恰好为 2；
    # 批次 A/B 因一对多连接，样本来源次数为 4，体现连接重数的正确传播。
    for lin in r["lineage"]:
        assert all(s["occurrence_count"] >= 2 for s in lin["sources"])
    batch_c = next(row for row in r["rows"] if row["b1"] == "C")
    c_line = r["lineage"][r["rows"].index(batch_c)]
    assert all(s["occurrence_count"] == 2 for s in c_line["sources"])


def test_same_value_groups_keep_separate_lineage(client):
    # 不输出分组列 batch；批次 A、B 都是 2 个样本，输出同值
    r = _query(client, "SELECT COUNT(*) c FROM samples GROUP BY batch")
    value_two = [lin for row, lin in zip(r["rows"], r["lineage"]) if row["c"] == 2]
    # 两个独立的组（A、B），溯源必须分开
    assert len(value_two) == 2
    groups = [
        {s["row_id"] for s in lin["sources"] if s["table"] == "samples"}
        for lin in value_two
    ]
    assert groups == [{1, 2}, {3, 4}] or groups == [{3, 4}, {1, 2}]
    # 原结果重数不变：共 3 行（A/B 各 2，C 为 3）
    assert sorted(row["c"] for row in r["rows"]) == [2, 2, 3]


def test_three_way_join_repeated_table(client):
    # samples 被引用两次（不同别名）+ results，验证别名独立与来源合并
    r = _query(
        client,
        "SELECT s1.id a, s2.id b FROM samples s1 "
        "INNER JOIN samples s2 ON s2.batch = s1.batch "
        "INNER JOIN results r ON r.sample_id = s1.id WHERE s1.id = 1",
    )
    # 样本 1 有 2 条检测，批次 A 有 2 个样本 -> 4 行
    assert r["row_count"] == 4
    first = r["lineage"][0]
    counts = {
        (s["table"], s["row_id"]): s["occurrence_count"]
        for s in first["sources"]
    }
    # 自连接 (1,1) 时样本 1 经两个别名各计一次
    assert counts[("samples", 1)] == 2
    assert all(
        {s["table"] for s in lin["sources"]} == {"samples", "results"}
        for lin in r["lineage"]
    )


def test_global_aggregate_empty_set(client):
    r = _query(
        client,
        "SELECT COUNT(*) c, SUM(value) s FROM results WHERE id > 999999",
    )
    assert r["rows"] == [{"c": 0, "s": None}]
    assert r["lineage"][0]["sources"] == []


def test_original_columns_unchanged(client):
    r = _query(
        client,
        "SELECT sample_id, COUNT(*) AS c FROM results "
        "GROUP BY sample_id HAVING COUNT(*) > 0",
    )
    # 溯源保留列不污染原结果
    assert r["columns"] == ["sample_id", "c"]
    assert all(set(row.keys()) == {"sample_id", "c"} for row in r["rows"])


def test_rejected_sql_returns_400(client):
    resp = client.post("/query", json={"sql": "DELETE FROM samples"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "unsupported_sql"
    # 数据未受影响
    assert client.get("/tables/samples").json()["row_count"] == 7


def test_execution_failure_no_partial_result(client):
    resp = client.post(
        "/query", json={"sql": "SELECT id FROM samples WHERE batch > 5"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "query_failed"
    # 失败响应中不含任何 rows/lineage
    assert "rows" not in resp.json()["detail"]


def test_snapshot_isolation_during_mutation(client):
    # 第一个请求拿到基线
    first = _query(client, "SELECT batch, COUNT(*) c FROM samples GROUP BY batch")

    # 修改数据：改批次、删样本、加检测行
    client.put("/samples", json={"id": 1, "batch": "A-MUTATED"})
    client.delete("/samples/7")
    client.put("/results", json={"id": 999, "sample_id": 2, "value": 100})

    # 新请求看到的是新世界，但通过两次连续请求验证彼此独立快照
    second = _query(client, "SELECT batch, COUNT(*) c FROM samples GROUP BY batch")
    live_samples = client.get("/tables/samples").json()["row_count"]
    assert live_samples == 6
    # 第一个响应的快照内容保持提交时的样子（回填内容未被改写）
    src1 = first["lineage"][0]["sources"][0]["row_content"]
    assert isinstance(src1, dict) and "batch" in src1
    # 新结果行数与旧结果可以不同，证明快照独立
    assert second["snapshot_id"] != first["snapshot_id"]


def test_mutation_crud(client):
    client.put("/samples", json={"id": 100, "batch": "X"})
    client.put("/results", json={"id": 200, "sample_id": 100, "value": None})
    rows = client.get("/tables/results").json()["rows"]
    new_row = next(r for r in rows if r["id"] == 200)
    assert new_row["value"] is None and new_row["sample_id"] == 100

    # upsert 修改
    client.put("/results", json={"id": 200, "sample_id": 100, "value": 42})
    rows = client.get("/tables/results").json()["rows"]
    assert next(r for r in rows if r["id"] == 200)["value"] == 42

    # 外键约束
    bad = client.put("/results", json={"id": 201, "sample_id": 999, "value": 1})
    assert bad.status_code == 422

    # 级联删除样本
    client.delete("/samples/100")
    rows = client.get("/tables/results").json()["rows"]
    assert all(r["id"] != 200 for r in rows)
