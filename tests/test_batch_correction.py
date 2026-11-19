"""批次更正接口测试：前后统计、快照追溯、原子性与竞争冲突。"""
from __future__ import annotations

import threading

import pytest

from app import db
from app.errors import ConcurrentModification
from app.seed import seed_rows

CORRECTION_SQL = "SELECT batch, COUNT(*) c FROM samples GROUP BY batch"


def _correct(client, **payload):
    body = {"threshold": 10, **payload}
    return client.post("/batch-corrections", json=body)


def _query_rows(client, sql, snapshot_id=None):
    body = {"sql": sql}
    if snapshot_id is not None:
        body["snapshot_id"] = snapshot_id
    resp = client.post("/query", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_correction_returns_before_and_after_for_both_batches(client):
    resp = _correct(client, sample_id=1, expected_batch="A", new_batch="B")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["threshold"] == 10
    effects = {e["role"]: e for e in body["batches"]}
    assert set(effects) == {"old", "new"}

    # 原批次 A：2 样本 / 21 超阈；移走样本 1 后 1 样本 / 7 不超阈
    old = effects["old"]
    assert old["batch"] == "A"
    assert old["before"] == {
        "sample_count": 2,
        "non_null_value_sum": 21,
        "exceeds_threshold": True,
    }
    assert old["after"] == {
        "sample_count": 1,
        "non_null_value_sum": 7,
        "exceeds_threshold": False,
    }

    # 新批次 B：2 样本 / 5；接收样本 1 后 3 样本 / 19 超阈
    new = effects["new"]
    assert new["batch"] == "B"
    assert new["before"] == {
        "sample_count": 2,
        "non_null_value_sum": 5,
        "exceeds_threshold": False,
    }
    assert new["after"] == {
        "sample_count": 3,
        "non_null_value_sum": 19,
        "exceeds_threshold": True,
    }


def test_correction_commits_change(client):
    _correct(client, sample_id=1, expected_batch="A", new_batch="B")
    rows = client.get("/tables/samples").json()["rows"]
    assert next(r["batch"] for r in rows if r["id"] == 1) == "B"
    assert client.get("/tables/samples").json()["row_count"] == 7


def test_query_traces_before_and_after_snapshots(client):
    body = _correct(
        client, sample_id=1, expected_batch="A", new_batch="B"
    ).json()
    old_effect = next(e for e in body["batches"] if e["role"] == "old")

    before = _query_rows(
        client, CORRECTION_SQL, old_effect["before_snapshot_id"]
    )
    after = _query_rows(
        client, CORRECTION_SQL, old_effect["after_snapshot_id"]
    )

    counts_before = {r["batch"]: r["c"] for r in before["rows"]}
    counts_after = {r["batch"]: r["c"] for r in after["rows"]}
    assert counts_before == {"A": 2, "B": 2, "C": 3}
    assert counts_after == {"A": 1, "B": 3, "C": 3}
    assert before["snapshot_id"] == old_effect["before_snapshot_id"]
    assert after["snapshot_id"] == old_effect["after_snapshot_id"]

    # 行内容也必须是快照当时的样子：更正前样本 1 仍属于 A
    pre = _query_rows(
        client,
        "SELECT id, batch FROM samples WHERE id = 1",
        old_effect["before_snapshot_id"],
    )
    post = _query_rows(
        client,
        "SELECT id, batch FROM samples WHERE id = 1",
        old_effect["after_snapshot_id"],
    )
    assert pre["lineage"][0]["sources"][0]["row_content"] == {
        "id": 1,
        "batch": "A",
    }
    assert post["lineage"][0]["sources"][0]["row_content"] == {
        "id": 1,
        "batch": "B",
    }
    # 当前数据与更正后快照一致
    live = _query_rows(client, CORRECTION_SQL)
    assert live["rows"] == after["rows"]


def test_correction_unknown_sample_is_404(client):
    resp = _correct(client, sample_id=999, expected_batch="A", new_batch="B")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "sample_not_found"


def test_correction_wrong_expected_batch_is_409(client):
    resp = _correct(client, sample_id=1, expected_batch="Z", new_batch="B")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "batch_precondition_failed"
    assert detail["current_batch"] == "A"
    # 未应用任何修改
    rows = client.get("/tables/samples").json()["rows"]
    assert next(r["batch"] for r in rows if r["id"] == 1) == "A"


def test_same_batch_correction_is_noop(client):
    resp = _correct(client, sample_id=1, expected_batch="A", new_batch="A")
    assert resp.status_code == 200
    body = resp.json()
    # 同一批次只返回一条结论，前后相同
    assert len(body["batches"]) == 1
    effect = body["batches"][0]
    assert effect["before"] == effect["after"]


def test_unknown_snapshot_is_404(client):
    resp = client.post(
        "/query", json={"sql": CORRECTION_SQL, "snapshot_id": "does-not-exist"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "snapshot_not_found"


# --------------------------------------------------------------------------- #
# 竞争更新：在「计算完、提交前」插入并发已提交写入，验证整次冲突回滚
# --------------------------------------------------------------------------- #
def _race(database, mutation):
    """让更正事务在归档「更正前」快照后暂停，插入并发写入，再放行。

    返回 (异常或 None, 实际结果)。暂停点用 monkeypatch 替换内部归档函数，
    仅用于测试，避免依赖真实网络时序。
    """
    original = database._archive_current_snapshot
    ready = threading.Event()
    release = threading.Event()

    def paused(conn, kind, sample_id):
        result = original(conn, kind, sample_id)
        if kind.endswith("before"):
            ready.set()
            assert release.wait(10)
        return result

    database._archive_current_snapshot = paused
    outcome: dict[str, object] = {}

    def worker():
        try:
            outcome["value"] = database.correct_sample_batch(1, "A", "B")
        except Exception as exc:  # noqa: BLE001 - 记录供断言
            outcome["error"] = exc

    thread = threading.Thread(target=worker)
    try:
        thread.start()
        assert ready.wait(10)
        mutation()
        release.set()
        thread.join(10)
        assert not thread.is_alive()
    finally:
        database._archive_current_snapshot = original
        release.set()

    return outcome


def test_conflict_when_old_batch_result_added(database):
    database.reset_and_seed(seed_rows())
    outcome = _race(database, lambda: db.upsert_result(999, 1, 5))
    assert isinstance(outcome.get("error"), ConcurrentModification)
    # 整次回滚：样本批次未变，也没有留下任何归档快照
    rows = db.list_table("samples")
    assert next(r["batch"] for r in rows if r["id"] == 1) == "A"
    with db.admin_connect(autocommit=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM snapshot_meta").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM snapshot_archive"
        ).fetchone()[0] == 0


def test_conflict_when_sample_moved_into_new_batch(database):
    database.reset_and_seed(seed_rows())
    # 样本 5 从 C 移入新批次 B：改动的是 B（新批次）
    outcome = _race(database, lambda: db.upsert_sample(5, "B"))
    assert isinstance(outcome.get("error"), ConcurrentModification)
    rows = {r["id"]: r["batch"] for r in db.list_table("samples")}
    assert rows[1] == "A" and rows[5] == "B"


def test_unrelated_batch_change_does_not_block(database):
    database.reset_and_seed(seed_rows())
    # 批次 C 的样本 6 改到 Z：与 A/B 无关，更正应成功
    outcome = _race(database, lambda: db.upsert_sample(6, "Z"))
    assert "error" not in outcome
    result = outcome["value"]
    assert result["after"]["B"]["sample_count"] == 3
    rows = {r["id"]: r["batch"] for r in db.list_table("samples")}
    assert rows[1] == "B" and rows[6] == "Z"


def test_identical_result_write_does_not_block(database):
    """重复写入完全相同的检测行不算改动，不应造成误报冲突。"""
    database.reset_and_seed(seed_rows())
    # 检测 101 原本就是 sample 1 / value 8
    outcome = _race(database, lambda: db.upsert_result(101, 1, 8))
    assert "error" not in outcome
    assert outcome["value"]["after"]["A"]["sample_count"] == 1


def test_failure_after_update_rolls_back_everything(database):
    """提交前最后一步失败：样本批次、前后归档都必须整体回滚。"""
    database.reset_and_seed(seed_rows())
    original = database._archive_current_snapshot

    def fail_on_after(conn, kind, sample_id):
        if kind.endswith("after"):
            raise RuntimeError("injected failure")
        return original(conn, kind, sample_id)

    database._archive_current_snapshot = fail_on_after
    try:
        with pytest.raises(RuntimeError):
            database.correct_sample_batch(1, "A", "B")
    finally:
        database._archive_current_snapshot = original

    rows = {r["id"]: r["batch"] for r in db.list_table("samples")}
    assert rows[1] == "A"
    with db.admin_connect(autocommit=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM snapshot_meta").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM snapshot_archive"
        ).fetchone()[0] == 0


def test_concurrent_conflict_maps_to_409_over_http(client, monkeypatch):
    """序列化失败（真实的并发检测路径）应映射为 409 且不改数据。"""
    import psycopg

    original = db._lock_batch_revisions

    def always_conflict(conn, batches):
        raise psycopg.errors.SerializationFailure(
            "could not serialize access due to concurrent update"
        )

    monkeypatch.setattr(db, "_lock_batch_revisions", always_conflict)
    resp = _correct(client, sample_id=1, expected_batch="A", new_batch="B")
    monkeypatch.setattr(db, "_lock_batch_revisions", original)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "concurrent_modification"
    # 未应用修改
    rows = client.get("/tables/samples").json()["rows"]
    assert next(r["batch"] for r in rows if r["id"] == 1) == "A"
