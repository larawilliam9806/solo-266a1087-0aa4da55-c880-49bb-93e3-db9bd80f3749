"""收单结算核对 API（FastAPI）。

端点：
- POST /batches                    创建核对批次（提交结算单表 + 银行到账表，各 ≤12 行）
- GET  /batches/{batch_id}         查看批次（记录状态 + 已确认匹配组）
- POST /batches/{batch_id}/analyze 分析未确认记录，返回最优匹配方案（歧义时给两份，不自动确认）
- POST /batches/{batch_id}/confirm 确认选定匹配组（原子生效，占用冲突则整次拒绝）
- GET  /healthz                    健康检查
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, StrictInt

from . import db
from .matching import Record, analyze


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="收单结算核对 API", version="1.0.0", lifespan=lifespan)


# ---------- 请求模型 ----------


class RecordIn(BaseModel):
    """一行记录：唯一编号、商户、到账日、正整数分金额。"""

    id: str = Field(min_length=1, max_length=64)
    merchant: str = Field(min_length=1, max_length=128)
    arrival_date: date
    amount_cents: StrictInt = Field(gt=0)


class BatchCreate(BaseModel):
    settlements: list[RecordIn] = Field(default_factory=list, max_length=12)
    deposits: list[RecordIn] = Field(default_factory=list, max_length=12)


class MatchIn(BaseModel):
    deposit_id: str = Field(min_length=1)
    settlement_ids: list[str] = Field(min_length=1, max_length=3)


class ConfirmIn(BaseModel):
    matches: list[MatchIn] = Field(min_length=1, max_length=12)


# ---------- 端点 ----------


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/batches", status_code=201)
def create_batch(payload: BatchCreate) -> dict:
    for table_name, rows in (
        ("settlements", payload.settlements),
        ("deposits", payload.deposits),
    ):
        ids = [r.id for r in rows]
        if len(ids) != len(set(ids)):
            raise HTTPException(422, f"{table_name} 表内编号不唯一")

    def to_row(r: RecordIn) -> dict:
        return {
            "id": r.id,
            "merchant": r.merchant,
            "arrival_date": r.arrival_date.isoformat(),
            "amount_cents": r.amount_cents,
        }

    batch_id = db.create_batch(
        [to_row(r) for r in payload.settlements],
        [to_row(r) for r in payload.deposits],
    )
    return {"batch_id": batch_id}


@app.get("/batches/{batch_id}")
def get_batch(batch_id: str) -> dict:
    data = db.get_batch(batch_id)
    if data is None:
        raise HTTPException(404, f"批次不存在: {batch_id}")
    return data


@app.post("/batches/{batch_id}/analyze")
def analyze_batch(batch_id: str) -> dict:
    if not db.batch_exists(batch_id):
        raise HTTPException(404, f"批次不存在: {batch_id}")

    settlements, deposits = db.get_unconfirmed(batch_id)

    def to_record(r) -> Record:
        return Record(
            id=r["external_id"],
            merchant=r["merchant"],
            arrival_date=r["arrival_date"],
            amount_cents=r["amount_cents"],
        )

    slip_records = [to_record(r) for r in settlements]
    dep_records = [to_record(r) for r in deposits]

    ambiguous, solutions = analyze(dep_records, slip_records)

    def record_json(r: Record) -> dict:
        return {
            "id": r.id,
            "merchant": r.merchant,
            "arrival_date": r.arrival_date,
            "amount_cents": r.amount_cents,
        }

    out_solutions = []
    for i, sol in enumerate(solutions):
        out_solutions.append(
            {
                "index": i,
                "matched_settlement_count": sol.matched_settlement_count,
                "matches": [
                    {
                        "deposit_id": m.deposit_id,
                        "settlement_ids": list(m.settlement_ids),
                        "merchant": m.merchant,
                        "arrival_date": m.arrival_date,
                        "amount_cents": m.amount_cents,
                    }
                    for m in sol.matches
                ],
                "unmatched_settlements": [
                    record_json(r)
                    for r in slip_records
                    if r.id not in sol.matched_settlement_ids
                ],
                "unmatched_deposits": [
                    record_json(r)
                    for r in dep_records
                    if r.id not in sol.matched_deposit_ids
                ],
            }
        )

    return {
        "batch_id": batch_id,
        "unconfirmed_settlement_count": len(slip_records),
        "unconfirmed_deposit_count": len(dep_records),
        "ambiguous": ambiguous,
        "auto_confirmed": False,
        "message": (
            "存在多个最优方案，请人工选择后调用 confirm 确认（分析不会自动确认）"
            if ambiguous
            else "已给出唯一最优方案，确认前请人工复核（分析不会自动确认）"
        ),
        "solutions": out_solutions,
    }


@app.post("/batches/{batch_id}/confirm", status_code=201)
def confirm_batch(batch_id: str, payload: ConfirmIn) -> dict:
    try:
        return db.confirm(batch_id, [m.model_dump() for m in payload.matches])
    except db.ConfirmError as e:
        raise HTTPException(e.status_code, e.detail) from e
