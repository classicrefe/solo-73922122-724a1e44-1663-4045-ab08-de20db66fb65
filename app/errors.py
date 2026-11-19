"""业务异常：批次更正的可预期失败（由 HTTP 层映射为 404/409）。"""
from __future__ import annotations


class SampleNotFound(LookupError):
    """目标样本不存在。"""

    def __init__(self, sample_id: int) -> None:
        self.sample_id = sample_id
        super().__init__(f"样本 {sample_id} 不存在")


class BatchPreconditionFailed(RuntimeError):
    """样本当前批次与调用方期望的旧批次不一致。"""

    def __init__(self, sample_id: int, expected: str, current: str) -> None:
        self.sample_id = sample_id
        self.expected = expected
        self.current = current
        super().__init__(
            f"样本 {sample_id} 当前批次为 {current!r}，与期望旧批次 {expected!r} 不一致"
        )


class ConcurrentModification(RuntimeError):
    """原批次或新批次在计算到提交之间被其他请求改动。"""

    def __init__(self, batch: str) -> None:
        self.batch = batch
        super().__init__(
            f"批次 {batch!r} 在计算与提交之间被其他请求改动，已放弃本次更正"
        )


class SnapshotNotFound(LookupError):
    """请求追溯的历史快照不存在。"""

    def __init__(self, snapshot_id: str) -> None:
        self.snapshot_id = snapshot_id
        super().__init__(f"快照 {snapshot_id} 不存在")
