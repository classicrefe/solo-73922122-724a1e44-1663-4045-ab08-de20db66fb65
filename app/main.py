"""FastAPI 应用：查询溯源、批次更正、数据维护、表内容查看与健康检查。"""
from __future__ import annotations

from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import db
from .errors import (
    BatchPreconditionFailed,
    ConcurrentModification,
    SampleNotFound,
    SnapshotNotFound,
)
from .service import QueryFailure, correct_batch, run_lineage_query
from .sql_validate import TABLE_SCHEMA, UnsupportedSQLError


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.initialize_schema()
    yield


app = FastAPI(
    title="异常汇总溯源 API",
    version="1.1.0",
    lifespan=lifespan,
    description=(
        "提交受管 SELECT，返回原结果以及每一行由哪些源样本/检测行构成"
        "（含源行 ID、快照当时内容与出现次数）。支持 INNER/LEFT 等值连接、"
        "WHERE、GROUP BY、HAVING、COUNT/SUM 与一个先聚合的非递归 CTE，"
        "其他语法一律拒绝。所有依据固定在同一已提交快照上。\n\n"
        "另提供批次更正接口：原子地移动样本批次并返回受影响批次更正前后"
        "的样本数、非空检测值总和与是否超阈；并发改动原/新批次时整次冲突"
        "回滚。"
    ),
)


# --------------------------------------------------------------------------- #
# 请求 / 响应模型
# --------------------------------------------------------------------------- #
class QueryRequest(BaseModel):
    sql: str = Field(..., description="受管 SELECT 语句")
    snapshot_id: str | None = Field(
        default=None,
        description=(
            "可选：批次更正返回的 before/after 快照 ID。"
            "指定后在该历史快照上执行查询，用于追溯更正前或更正后的完整来源。"
        ),
    )


class SourceItem(BaseModel):
    table: str
    row_id: int
    occurrence_count: int
    row_content: dict[str, Any]


class LineageItem(BaseModel):
    sources: list[SourceItem]
    source_count: int


class QueryResponse(BaseModel):
    snapshot_id: str
    submitted_sql: str
    columns: list[str]
    rows: list[dict[str, Any]]
    lineage: list[LineageItem]
    row_count: int


class SampleIn(BaseModel):
    id: int = Field(..., ge=0)
    batch: str = Field(..., min_length=1, max_length=100)


class ResultIn(BaseModel):
    id: int = Field(..., ge=0)
    sample_id: int = Field(..., ge=0)
    value: float | int | None = None


class IdIn(BaseModel):
    id: int = Field(..., ge=0)


class BatchCorrectionIn(BaseModel):
    sample_id: int = Field(..., ge=0, description="要更正的样本 ID")
    expected_batch: str = Field(
        ..., min_length=1, max_length=100, description="调用方期望的旧批次"
    )
    new_batch: str = Field(
        ..., min_length=1, max_length=100, description="更正后的新批次"
    )
    threshold: float | int = Field(
        ...,
        description="异常阈值：非空检测值总和严格大于该值即视为超阈",
    )


class BatchSummary(BaseModel):
    sample_count: int
    non_null_value_sum: float | int
    exceeds_threshold: bool


class BatchEffect(BaseModel):
    batch: str
    role: str = Field(..., description="old=原批次；new=新批次")
    before: BatchSummary = Field(..., description="更正前快照上的结论")
    after: BatchSummary = Field(..., description="成功提交后的结论")
    before_snapshot_id: str
    after_snapshot_id: str


class BatchCorrectionResponse(BaseModel):
    sample_id: int
    expected_batch: str
    new_batch: str
    threshold: float | int
    applied: bool
    batches: list[BatchEffect]


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["运维"])
def health() -> dict[str, str]:
    db.count_rows()  # 触发一次真实连接
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# 查询溯源
# --------------------------------------------------------------------------- #
@app.post(
    "/query",
    response_model=QueryResponse,
    tags=["溯源查询"],
    summary="提交受管 SELECT 并返回结果与逐行溯源",
)
def query(payload: QueryRequest) -> QueryResponse:
    try:
        result = run_lineage_query(payload.sql, payload.snapshot_id)
    except UnsupportedSQLError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "unsupported_sql", "message": str(exc)},
        )
    except SnapshotNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "snapshot_not_found", "message": str(exc)},
        )
    except QueryFailure as exc:
        # 执行失败：事务已回滚，不返回部分依据
        raise HTTPException(
            status_code=422,
            detail={"error": "query_failed", "message": str(exc)},
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_query", "message": str(exc)},
        )
    except Exception as exc:  # noqa: BLE001 - 兜底，保证不返回部分依据
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "查询处理失败，未返回任何依据"},
        ) from exc
    return QueryResponse(**result)


# --------------------------------------------------------------------------- #
# 批次更正（原子操作 + 乐观并发冲突检测）
# --------------------------------------------------------------------------- #
@app.post(
    "/batch-corrections",
    response_model=BatchCorrectionResponse,
    tags=["批次更正"],
    summary="更正样本批次并返回受影响批次更正前后的异常汇总",
    responses={
        404: {"description": "样本不存在"},
        409: {"description": "期望旧批次不符，或原/新批次被并发改动"},
    },
)
def correct_sample_batch(
    payload: BatchCorrectionIn,
) -> BatchCorrectionResponse:
    try:
        result = correct_batch(
            sample_id=payload.sample_id,
            expected_batch=payload.expected_batch,
            new_batch=payload.new_batch,
            threshold=payload.threshold,
        )
    except SampleNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "sample_not_found", "message": str(exc)},
        )
    except BatchPreconditionFailed as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "batch_precondition_failed",
                "message": str(exc),
                "expected_batch": exc.expected,
                "current_batch": exc.current,
            },
        )
    except ConcurrentModification as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "concurrent_modification",
                "message": str(exc),
                "batch": exc.batch,
            },
        )
    except Exception as exc:  # noqa: BLE001 - 兜底：整次回滚，不留半次修改
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "批次更正失败，未应用任何修改",
            },
        ) from exc
    return BatchCorrectionResponse(**result)



# --------------------------------------------------------------------------- #
# 数据维护（期间可改批次、增改删检测）
# --------------------------------------------------------------------------- #
@app.put("/samples", tags=["数据维护"], summary="新增或修改样本（upsert）")
def upsert_sample(payload: SampleIn) -> dict[str, Any]:
    try:
        db.upsert_sample(payload.id, payload.batch)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"status": "ok", "table": "samples", "id": payload.id}


@app.delete("/samples/{sample_id}", tags=["数据维护"], summary="删除样本（连带其检测行）")
def remove_sample(sample_id: int) -> dict[str, Any]:
    db.delete_sample(sample_id)
    return {"status": "ok", "table": "samples", "id": sample_id}


@app.put("/results", tags=["数据维护"], summary="新增或修改检测结果（upsert，value 可空）")
def upsert_result(payload: ResultIn) -> dict[str, Any]:
    try:
        db.upsert_result(payload.id, payload.sample_id, payload.value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"status": "ok", "table": "results", "id": payload.id}


@app.delete("/results/{result_id}", tags=["数据维护"], summary="删除检测结果")
def remove_result(result_id: int) -> dict[str, Any]:
    db.delete_result(result_id)
    return {"status": "ok", "table": "results", "id": result_id}


# --------------------------------------------------------------------------- #
# 表内容查看
# --------------------------------------------------------------------------- #
@app.get("/tables/{name}", tags=["数据查看"], summary="查看 samples 或 results 当前内容")
def get_table(name: str) -> dict[str, Any]:
    if name not in TABLE_SCHEMA:
        raise HTTPException(status_code=404, detail=f"未知表 {name}，仅支持 {list(TABLE_SCHEMA)}")
    rows = db.list_table(name)
    return {"table": name, "row_count": len(rows), "rows": rows}


@app.get("/meta", tags=["数据查看"], summary="表结构与行数")
def meta() -> dict[str, Any]:
    counts = db.count_rows()
    return {
        "tables": {
            name: {"columns": list(cols), "row_count": counts[name]}
            for name, cols in TABLE_SCHEMA.items()
        },
        "max_rows_per_table": 500,
    }
