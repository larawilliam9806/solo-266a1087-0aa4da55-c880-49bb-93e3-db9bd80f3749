"""端到端测试：批次校验、最优匹配、歧义、原子确认、并发、重启保留。"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RECON_DB_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def rec(rid, merchant="M1", day="2026-11-01", amount=100):
    return {
        "id": rid,
        "merchant": merchant,
        "arrival_date": day,
        "amount_cents": amount,
    }


def make_batch(client, settlements, deposits):
    resp = client.post(
        "/batches", json={"settlements": settlements, "deposits": deposits}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["batch_id"]


# ---------- 批次创建校验 ----------


def test_create_batch_ok(client):
    batch_id = make_batch(client, [rec("S1")], [rec("D1")])
    data = client.get(f"/batches/{batch_id}").json()
    assert data["settlements"][0]["status"] == "unconfirmed"
    assert data["deposits"][0]["status"] == "unconfirmed"
    assert data["confirmations"] == []


def test_create_batch_row_limit(client):
    settlements = [rec(f"S{i}") for i in range(13)]
    resp = client.post("/batches", json={"settlements": settlements, "deposits": []})
    assert resp.status_code == 422
    deposits = [rec(f"D{i}") for i in range(13)]
    resp = client.post("/batches", json={"settlements": [], "deposits": deposits})
    assert resp.status_code == 422
    # 恰好 12 行允许
    ok = client.post(
        "/batches",
        json={
            "settlements": [rec(f"S{i}") for i in range(12)],
            "deposits": [rec(f"D{i}") for i in range(12)],
        },
    )
    assert ok.status_code == 201


def test_create_batch_unique_ids(client):
    resp = client.post(
        "/batches",
        json={"settlements": [rec("S1"), rec("S1")], "deposits": []},
    )
    assert resp.status_code == 422
    resp = client.post(
        "/batches",
        json={"settlements": [], "deposits": [rec("D1"), rec("D1", amount=200)]},
    )
    assert resp.status_code == 422


def test_create_batch_amount_must_be_positive_int(client):
    for bad in (0, -5, 1.5, "100", True, None):
        resp = client.post(
            "/batches",
            json={"settlements": [rec("S1", amount=bad)], "deposits": []},
        )
        assert resp.status_code == 422, f"amount={bad!r} 应被拒绝"


def test_create_batch_required_fields(client):
    resp = client.post(
        "/batches",
        json={"settlements": [{"id": "S1", "merchant": "M1"}], "deposits": []},
    )
    assert resp.status_code == 422
    resp = client.post(
        "/batches",
        json={
            "settlements": [rec("S1", day="not-a-date")],
            "deposits": [],
        },
    )
    assert resp.status_code == 422


# ---------- 分析 ----------


def test_analyze_basic_match(client):
    # D1=300 ↔ S1(100)+S2(200)；S3 商户不同、S4 日期不同、D2 无对应 → 未匹配
    batch_id = make_batch(
        client,
        settlements=[
            rec("S1", amount=100),
            rec("S2", amount=200),
            rec("S3", merchant="M2", amount=300),
            rec("S4", day="2026-11-02", amount=300),
        ],
        deposits=[rec("D1", amount=300), rec("D2", amount=999)],
    )
    resp = client.post(f"/batches/{batch_id}/analyze")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ambiguous"] is False
    assert body["auto_confirmed"] is False
    assert len(body["solutions"]) == 1
    sol = body["solutions"][0]
    assert sol["matched_settlement_count"] == 2
    assert sol["matches"] == [
        {
            "deposit_id": "D1",
            "settlement_ids": ["S1", "S2"],
            "merchant": "M1",
            "arrival_date": "2026-11-01",
            "amount_cents": 300,
        }
    ]
    assert {r["id"] for r in sol["unmatched_settlements"]} == {"S3", "S4"}
    assert {r["id"] for r in sol["unmatched_deposits"]} == {"D2"}


def test_analyze_max_settlement_count(client):
    # D1=300：可以 1 张 S3(300)，也可以 S1+S2(100+200) → 最优应对上 2 张
    batch_id = make_batch(
        client,
        settlements=[rec("S1", amount=100), rec("S2", amount=200), rec("S3", amount=300)],
        deposits=[rec("D1", amount=300)],
    )
    body = client.post(f"/batches/{batch_id}/analyze").json()
    assert body["ambiguous"] is False
    sol = body["solutions"][0]
    assert sol["matched_settlement_count"] == 2
    assert sol["matches"][0]["settlement_ids"] == ["S1", "S2"]
    assert {r["id"] for r in sol["unmatched_settlements"]} == {"S3"}


def test_analyze_ambiguous_gives_two_solutions(client):
    # D1=D2=300，结算单 100/200/300：两种最优方案都对上 3 张
    batch_id = make_batch(
        client,
        settlements=[rec("S1", amount=100), rec("S2", amount=200), rec("S3", amount=300)],
        deposits=[rec("D1", amount=300), rec("D2", amount=300)],
    )
    body = client.post(f"/batches/{batch_id}/analyze").json()
    assert body["ambiguous"] is True
    assert len(body["solutions"]) == 2
    s0, s1 = body["solutions"]
    assert s0["matched_settlement_count"] == s1["matched_settlement_count"] == 3
    # 两份方案必须不同：各自由一笔 1对2 + 一笔 1对1 组成，但归属不同
    m0 = {m["deposit_id"]: m["settlement_ids"] for m in s0["matches"]}
    m1 = {m["deposit_id"]: m["settlement_ids"] for m in s1["matches"]}
    assert m0 != m1
    assert sorted(m0["D1"] + m0["D2"]) == ["S1", "S2", "S3"]
    # 分析不得自动确认
    data = client.get(f"/batches/{batch_id}").json()
    assert all(r["status"] == "unconfirmed" for r in data["settlements"])
    assert data["confirmations"] == []


def test_analyze_respects_one_to_three_limit(client):
    # D1=400，四张 100：一笔到账最多 3 张，无法 4 张合并 → 只能对上 3 张
    batch_id = make_batch(
        client,
        settlements=[rec(f"S{i}", amount=100) for i in range(1, 5)],
        deposits=[rec("D1", amount=400)],
    )
    body = client.post(f"/batches/{batch_id}/analyze").json()
    sol = body["solutions"][0]
    assert sol["matched_settlement_count"] == 0
    assert {r["id"] for r in sol["unmatched_settlements"]} == {"S1", "S2", "S3", "S4"}


def test_analyze_unknown_batch(client):
    assert client.post("/batches/nope/analyze").status_code == 404


# ---------- 确认 ----------


def test_confirm_then_reanalyze_excludes_occupied(client):
    batch_id = make_batch(
        client,
        settlements=[
            rec("S1", amount=100),
            rec("S2", amount=200),
            rec("S3", amount=50),
            rec("S4", amount=50),
        ],
        deposits=[rec("D1", amount=300), rec("D2", amount=100)],
    )
    resp = client.post(
        f"/batches/{batch_id}/confirm",
        json={"matches": [{"deposit_id": "D1", "settlement_ids": ["S1", "S2"]}]},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["confirmed_matches"] == [
        {"deposit_id": "D1", "settlement_ids": ["S1", "S2"]}
    ]

    # 再次分析：已确认记录被排除
    body = client.post(f"/batches/{batch_id}/analyze").json()
    assert body["unconfirmed_settlement_count"] == 2
    assert body["unconfirmed_deposit_count"] == 1
    sol = body["solutions"][0]
    assert sol["matches"] == [
        {
            "deposit_id": "D2",
            "settlement_ids": ["S3", "S4"],
            "merchant": "M1",
            "arrival_date": "2026-11-01",
            "amount_cents": 100,
        }
    ]

    data = client.get(f"/batches/{batch_id}").json()
    status = {r["id"]: r["status"] for r in data["settlements"] + data["deposits"]}
    assert status == {
        "S1": "confirmed",
        "S2": "confirmed",
        "S3": "unconfirmed",
        "S4": "unconfirmed",
        "D1": "confirmed",
        "D2": "unconfirmed",
    }
    assert len(data["confirmations"]) == 1


def test_confirm_conflict_rejects_entire_request(client):
    """所选记录已被占用 → 整次确认拒绝，不得部分生效。"""
    batch_id = make_batch(
        client,
        settlements=[
            rec("S1", amount=100),
            rec("S2", amount=200),
            rec("S3", amount=300),
            rec("S4", amount=50),
        ],
        deposits=[rec("D1", amount=300), rec("D2", amount=50)],
    )
    ok = client.post(
        f"/batches/{batch_id}/confirm",
        json={"matches": [{"deposit_id": "D1", "settlement_ids": ["S1", "S2"]}]},
    )
    assert ok.status_code == 201

    # 第二次确认：D1↔S3 本身合法但 D1 已被占用；D2↔S4 完全合法
    resp = client.post(
        f"/batches/{batch_id}/confirm",
        json={
            "matches": [
                {"deposit_id": "D1", "settlement_ids": ["S3"]},
                {"deposit_id": "D2", "settlement_ids": ["S4"]},
            ]
        },
    )
    assert resp.status_code == 409

    # 不得部分生效：D2/S4 仍须未确认，且没有新增确认记录
    data = client.get(f"/batches/{batch_id}").json()
    status = {r["id"]: r["status"] for r in data["settlements"] + data["deposits"]}
    assert status["D2"] == "unconfirmed"
    assert status["S4"] == "unconfirmed"
    assert len(data["confirmations"]) == 1


def test_confirm_invalid_matches(client):
    batch_id = make_batch(
        client,
        settlements=[
            rec("S1", amount=100),
            rec("S2", amount=200),
            rec("S3", merchant="M2", amount=300),
        ],
        deposits=[rec("D1", amount=300)],
    )
    # 金额不等
    resp = client.post(
        f"/batches/{batch_id}/confirm",
        json={"matches": [{"deposit_id": "D1", "settlement_ids": ["S1"]}]},
    )
    assert resp.status_code == 400
    # 商户不同
    resp = client.post(
        f"/batches/{batch_id}/confirm",
        json={"matches": [{"deposit_id": "D1", "settlement_ids": ["S3"]}]},
    )
    assert resp.status_code == 400
    # 编号不存在
    resp = client.post(
        f"/batches/{batch_id}/confirm",
        json={"matches": [{"deposit_id": "D1", "settlement_ids": ["SX"]}]},
    )
    assert resp.status_code == 400
    # 请求内重复使用同一结算单
    resp = client.post(
        f"/batches/{batch_id}/confirm",
        json={
            "matches": [
                {"deposit_id": "D1", "settlement_ids": ["S1", "S2"]},
                {"deposit_id": "D1", "settlement_ids": ["S1"]},
            ]
        },
    )
    assert resp.status_code == 400
    # 超过 3 张（Pydantic 校验）
    resp = client.post(
        f"/batches/{batch_id}/confirm",
        json={"matches": [{"deposit_id": "D1", "settlement_ids": ["S1", "S2", "S3", "S1"]}]},
    )
    assert resp.status_code == 422
    # 全部失败后数据不变
    data = client.get(f"/batches/{batch_id}").json()
    assert data["confirmations"] == []
    assert all(r["status"] == "unconfirmed" for r in data["settlements"])


def test_confirm_unknown_batch(client):
    resp = client.post(
        "/batches/nope/confirm",
        json={"matches": [{"deposit_id": "D1", "settlement_ids": ["S1"]}]},
    )
    assert resp.status_code == 404


def test_concurrent_confirm_no_double_occupancy(client):
    """并发确认同一批记录：至多一个成功，其余 409，不会重复占用。"""
    batch_id = make_batch(
        client,
        settlements=[rec("S1", amount=100), rec("S2", amount=200)],
        deposits=[rec("D1", amount=300)],
    )
    payload = {"matches": [{"deposit_id": "D1", "settlement_ids": ["S1", "S2"]}]}
    results = []

    def worker():
        resp = client.post(f"/batches/{batch_id}/confirm", json=payload)
        results.append(resp.status_code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(201) == 1
    assert results.count(409) == 7
    data = client.get(f"/batches/{batch_id}").json()
    assert len(data["confirmations"]) == 1


def test_restart_preserves_confirmations(tmp_path, monkeypatch):
    """同一数据库文件重新打开（模拟重启）后，确认结果保留。"""
    db_file = str(tmp_path / "restart.db")
    monkeypatch.setenv("RECON_DB_PATH", db_file)

    with TestClient(app) as c1:
        batch_id = make_batch(
            c1,
            settlements=[rec("S1", amount=100), rec("S2", amount=200)],
            deposits=[rec("D1", amount=300)],
        )
        resp = c1.post(
            f"/batches/{batch_id}/confirm",
            json={"matches": [{"deposit_id": "D1", "settlement_ids": ["S1", "S2"]}]},
        )
        assert resp.status_code == 201

    # 模拟进程重启：新建 client，同一数据库文件
    with TestClient(app) as c2:
        data = c2.get(f"/batches/{batch_id}").json()
        assert len(data["confirmations"]) == 1
        assert all(
            r["status"] == "confirmed"
            for r in data["settlements"] + data["deposits"]
        )
        body = c2.post(f"/batches/{batch_id}/analyze").json()
        assert body["unconfirmed_settlement_count"] == 0
        assert body["solutions"][0]["matches"] == []
