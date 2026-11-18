"""SQLite 持久化层。

- 数据库文件路径由环境变量 RECON_DB_PATH 指定（默认 ./recon.db），
  重启后数据（含已确认的匹配组）保留。
- 确认操作在单个 `BEGIN IMMEDIATE` 事务内完成"校验占用 + 写入"，
  SQLite 写锁保证并发确认不会重复占用；任一记录被占用则整体回滚，
  不存在部分生效。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    batch_id      TEXT NOT NULL REFERENCES batches(id),
    kind          TEXT NOT NULL CHECK (kind IN ('settlement', 'deposit')),
    external_id   TEXT NOT NULL,
    merchant      TEXT NOT NULL,
    arrival_date  TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    status        TEXT NOT NULL DEFAULT 'unconfirmed'
                  CHECK (status IN ('unconfirmed', 'confirmed')),
    PRIMARY KEY (batch_id, kind, external_id)
);

CREATE TABLE IF NOT EXISTS confirmations (
    id          TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL REFERENCES batches(id),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS confirmation_matches (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_id         TEXT NOT NULL REFERENCES confirmations(id),
    batch_id                TEXT NOT NULL,
    deposit_external_id     TEXT NOT NULL,
    settlement_external_ids TEXT NOT NULL  -- JSON 数组
);

CREATE INDEX IF NOT EXISTS idx_records_batch_status
    ON records (batch_id, status);
"""

_init_lock = threading.Lock()
_initialized_paths: set[str] = set()


def _db_path() -> str:
    return os.environ.get("RECON_DB_PATH", os.path.join(os.getcwd(), "recon.db"))


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    path = _db_path()
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    if path not in _initialized_paths:
        with _init_lock:
            if path not in _initialized_paths:
                conn.executescript(SCHEMA)
                _initialized_paths.add(path)
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.close()


class ConfirmError(Exception):
    """确认失败的业务错误，携带 HTTP 状态码。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def create_batch(settlements: list[dict], deposits: list[dict]) -> str:
    batch_id = uuid.uuid4().hex
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO batches (id, created_at) VALUES (?, ?)",
            (batch_id, _utcnow()),
        )
        for kind, rows in (("settlement", settlements), ("deposit", deposits)):
            for r in rows:
                conn.execute(
                    "INSERT INTO records (batch_id, kind, external_id, merchant,"
                    " arrival_date, amount_cents) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        batch_id,
                        kind,
                        r["id"],
                        r["merchant"],
                        r["arrival_date"],
                        r["amount_cents"],
                    ),
                )
        conn.execute("COMMIT")
    except Exception:
        _rollback_quietly(conn)
        raise
    finally:
        conn.close()
    return batch_id


def batch_exists(batch_id: str) -> bool:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_unconfirmed(batch_id: str) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """返回 (未确认结算单, 未确认银行到账)。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM records WHERE batch_id = ? AND status = 'unconfirmed'"
            " ORDER BY external_id",
            (batch_id,),
        ).fetchall()
        settlements = [r for r in rows if r["kind"] == "settlement"]
        deposits = [r for r in rows if r["kind"] == "deposit"]
        return settlements, deposits
    finally:
        conn.close()


def confirm(batch_id: str, matches: list[dict]) -> dict:
    """原子地确认一组匹配。

    任一匹配组不合法（编号不存在、商户/到账日不一致、张数或金额不符、
    请求内重复使用记录）→ 400；任一记录已被其他确认占用 → 409。
    失败时整体回滚，不产生任何部分生效。
    """
    conn = get_conn()
    try:
        # 写事务：并发确认在此串行化，事务内重查占用状态
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            "SELECT id FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            raise ConfirmError(404, f"批次不存在: {batch_id}")

        # 1) 请求内部不得重复使用任何记录
        seen: set[tuple[str, str]] = set()
        for m in matches:
            for kind, ext_id in [("deposit", m["deposit_id"])] + [
                ("settlement", sid) for sid in m["settlement_ids"]
            ]:
                key = (kind, ext_id)
                if key in seen:
                    raise ConfirmError(400, f"请求内重复使用记录: {ext_id}")
                seen.add(key)

        # 2) 逐组校验存在性、商户/到账日、金额合计、占用状态
        prepared: list[tuple[sqlite3.Row, list[sqlite3.Row]]] = []
        for m in matches:
            dep = conn.execute(
                "SELECT * FROM records WHERE batch_id = ? AND kind = 'deposit'"
                " AND external_id = ?",
                (batch_id, m["deposit_id"]),
            ).fetchone()
            if dep is None:
                raise ConfirmError(400, f"银行到账编号不存在: {m['deposit_id']}")
            slips = []
            for sid in m["settlement_ids"]:
                s = conn.execute(
                    "SELECT * FROM records WHERE batch_id = ? AND kind = 'settlement'"
                    " AND external_id = ?",
                    (batch_id, sid),
                ).fetchone()
                if s is None:
                    raise ConfirmError(400, f"结算单编号不存在: {sid}")
                slips.append(s)
            if not 1 <= len(slips) <= 3:
                raise ConfirmError(400, "一笔到账只能对应 1~3 张结算单")
            for s in slips:
                if (
                    s["merchant"] != dep["merchant"]
                    or s["arrival_date"] != dep["arrival_date"]
                ):
                    raise ConfirmError(
                        400,
                        f"商户或到账日不一致: 到账 {dep['external_id']} 与结算单"
                        f" {s['external_id']}",
                    )
            total = sum(s["amount_cents"] for s in slips)
            if total != dep["amount_cents"]:
                raise ConfirmError(
                    400,
                    f"金额不符: 到账 {dep['external_id']} 为 {dep['amount_cents']} 分,"
                    f"结算单合计 {total} 分",
                )
            occupied = [
                r["external_id"]
                for r in [dep, *slips]
                if r["status"] != "unconfirmed"
            ]
            if occupied:
                raise ConfirmError(
                    409, f"记录已被其他确认占用: {', '.join(sorted(occupied))}"
                )
            prepared.append((dep, slips))

        # 3) 全部校验通过，一次性写入（同事务，要么全成要么全不成）
        confirmation_id = uuid.uuid4().hex
        created_at = _utcnow()
        conn.execute(
            "INSERT INTO confirmations (id, batch_id, created_at) VALUES (?, ?, ?)",
            (confirmation_id, batch_id, created_at),
        )
        confirmed_matches = []
        for dep, slips in prepared:
            slip_ids = [s["external_id"] for s in slips]
            conn.execute(
                "INSERT INTO confirmation_matches (confirmation_id, batch_id,"
                " deposit_external_id, settlement_external_ids)"
                " VALUES (?, ?, ?, ?)",
                (confirmation_id, batch_id, dep["external_id"], json.dumps(slip_ids)),
            )
            conn.execute(
                "UPDATE records SET status = 'confirmed' WHERE batch_id = ?"
                " AND kind = 'deposit' AND external_id = ?",
                (batch_id, dep["external_id"]),
            )
            for s in slips:
                conn.execute(
                    "UPDATE records SET status = 'confirmed' WHERE batch_id = ?"
                    " AND kind = 'settlement' AND external_id = ?",
                    (batch_id, s["external_id"]),
                )
            confirmed_matches.append(
                {"deposit_id": dep["external_id"], "settlement_ids": slip_ids}
            )
        conn.execute("COMMIT")
        return {
            "batch_id": batch_id,
            "confirmation_id": confirmation_id,
            "created_at": created_at,
            "confirmed_matches": confirmed_matches,
        }
    except ConfirmError:
        _rollback_quietly(conn)
        raise
    except Exception:
        _rollback_quietly(conn)
        raise
    finally:
        conn.close()


def _record_json(row: sqlite3.Row) -> dict:
    return {
        "id": row["external_id"],
        "merchant": row["merchant"],
        "arrival_date": row["arrival_date"],
        "amount_cents": row["amount_cents"],
        "status": row["status"],
    }


def get_batch(batch_id: str) -> dict | None:
    conn = get_conn()
    try:
        b = conn.execute(
            "SELECT * FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if b is None:
            return None
        records = conn.execute(
            "SELECT * FROM records WHERE batch_id = ? ORDER BY kind, external_id",
            (batch_id,),
        ).fetchall()
        confirmations = conn.execute(
            "SELECT * FROM confirmations WHERE batch_id = ? ORDER BY created_at, id",
            (batch_id,),
        ).fetchall()
        match_rows = conn.execute(
            "SELECT * FROM confirmation_matches WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ).fetchall()
        by_confirmation: dict[str, list[dict]] = {}
        for m in match_rows:
            by_confirmation.setdefault(m["confirmation_id"], []).append(
                {
                    "deposit_id": m["deposit_external_id"],
                    "settlement_ids": json.loads(m["settlement_external_ids"]),
                }
            )
        return {
            "batch_id": b["id"],
            "created_at": b["created_at"],
            "settlements": [
                _record_json(r) for r in records if r["kind"] == "settlement"
            ],
            "deposits": [
                _record_json(r) for r in records if r["kind"] == "deposit"
            ],
            "confirmations": [
                {
                    "confirmation_id": c["id"],
                    "created_at": c["created_at"],
                    "matches": by_confirmation.get(c["id"], []),
                }
                for c in confirmations
            ],
        }
    finally:
        conn.close()
