# 收单结算核对 API

收单结算数据员用于核对结算单与银行到账、找出待追查记录的后端服务。
FastAPI + SQLite，支持 `docker compose` 一键启动。

## 业务规则

- 创建核对批次时提交两张表：**结算单表**与**银行到账表**，各不超过 12 行；
  每行包含：表内唯一编号 `id`、商户 `merchant`、到账日 `arrival_date`、
  正整数分金额 `amount_cents`。
- 一笔银行到账可对应**同商户、同到账日**的 **1~3 张**结算单，
  且结算单金额合计必须等于该笔到账金额；每条记录在一次方案中只能使用一次。
- 分析（analyze）只针对**未确认**记录，求"对上结算单数量最多"的方案，
  返回匹配组与未匹配记录；若存在多个不同的最优方案，标记 `ambiguous: true`
  并给出**两份**方案，**不自动确认**。
- 数据员确认（confirm）选定匹配组后，再次分析会排除已占用记录。
- 确认是**原子**的：若所选记录已被其他确认占用，整次确认拒绝（409），
  不会部分生效；并发确认不会重复占用；SQLite 落盘，重启后确认结果保留。

## 启动

```bash
docker compose up --build
# API 位于 http://localhost:8000 ，交互文档 http://localhost:8000/docs
```

SQLite 数据库文件存放在名为 `recon-data` 的卷中（容器内 `/data/recon.db`），
由环境变量 `RECON_DB_PATH` 指定，容器重启后数据保留。

本地开发（无需 Docker）：

```bash
pip install -r requirements-dev.txt
RECON_DB_PATH=./recon.db uvicorn app.main:app --reload
pytest                 # 运行测试
```

## API

### POST /batches — 创建核对批次

```json
{
  "settlements": [
    {"id": "S1", "merchant": "星巴克", "arrival_date": "2026-11-01", "amount_cents": 100},
    {"id": "S2", "merchant": "星巴克", "arrival_date": "2026-11-01", "amount_cents": 200}
  ],
  "deposits": [
    {"id": "D1", "merchant": "星巴克", "arrival_date": "2026-11-01", "amount_cents": 300}
  ]
}
```

- 两表各 ≤12 行；表内 `id` 唯一；`amount_cents` 必须为正整数（分），否则 422。
- 返回 `{"batch_id": "..."}`（201）。

### POST /batches/{batch_id}/analyze — 分析未确认记录

```json
{
  "batch_id": "...",
  "ambiguous": false,
  "auto_confirmed": false,
  "solutions": [
    {
      "index": 0,
      "matched_settlement_count": 2,
      "matches": [
        {"deposit_id": "D1", "settlement_ids": ["S1", "S2"],
         "merchant": "星巴克", "arrival_date": "2026-11-01", "amount_cents": 300}
      ],
      "unmatched_settlements": [ ... ],
      "unmatched_deposits": [ ... ]
    }
  ]
}
```

- 只分析未确认记录；已确认的记录自动排除。
- `ambiguous: true` 时 `solutions` 含两份不同的最优方案，需人工选择确认。

### POST /batches/{batch_id}/confirm — 确认选定匹配组（201）

```json
{
  "matches": [
    {"deposit_id": "D1", "settlement_ids": ["S1", "S2"]}
  ]
}
```

- 逐组校验：编号存在、同商户同到账日、1~3 张、金额合计相等（违反 → 400）。
- 任一记录已被其他确认占用 → **整次拒绝**（409），无任何部分生效。
- 并发确认通过 SQLite `BEGIN IMMEDIATE` 写事务串行化，不会重复占用。

### GET /batches/{batch_id} — 查看批次

返回两表全部记录（含 `status`: `unconfirmed`/`confirmed`）与全部已确认匹配组。

## 设计说明

- **匹配算法**：匹配被限制在 (商户, 到账日) 组内，各组独立，全局最优等于各组
  最优之和。组内用 DFS + 记忆化（状态 = 到账下标 × 结算单占用位掩码）枚举最优解，
  并收集至多 2 个不同最优方案用于歧义判定。每表 ≤12 行，搜索规模有界。
- **原子性与并发**：确认在单个 `BEGIN IMMEDIATE` 事务内完成"校验占用 + 写入"，
  SQLite 写锁保证并发事务串行执行，后进入的事务重查占用状态，冲突即整体回滚。
- **持久化**：确认结果写入 `confirmations` / `confirmation_matches` 表并更新记录
  状态，数据库文件挂载在卷上，重启保留。
