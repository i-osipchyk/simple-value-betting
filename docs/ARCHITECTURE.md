# Architecture

## Overview

Two Docker containers sharing a bind-mounted `/data/` volume. The collector streams live market data and writes it to disk every second; the model reads that data to run continuous inference and retrain after each completed candle.

---

## System Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Docker Compose                               │
│                                                                     │
│  ┌──────────────────────────┐     ┌───────────────────────────────┐ │
│  │   collector              │     │   model                       │ │
│  │                          │     │                               │ │
│  │  Polymarket WebSocket    │     │  [task 1] watcher             │ │
│  │   ├─ yes/no prices       │     │   watches /data/raw/          │ │
│  │   └─ BTC/USD (CL feed)   │     │   on new parquet:             │ │
│  │          │               │     │    - resolves open trades     │ │
│  │          ▼               │     │    - retrains model           │ │
│  │       DuckDB             │     │    - predicts for new candle  │ │
│  │    raw_data.db           │     │                               │ │
│  │          │               │     │  [task 2] inference loop      │ │
│  │    every 5 min ──────────┼────▶│   reads latest_tick.json      │ │
│  │    /data/raw/*.parquet   │     │   every second                │ │
│  │                          │     │   opens trade if edge found   │ │
│  │    every second ─────────┼────▶│                               │ │
│  │    /data/latest_tick.json│     │  [task 3] FastAPI :8000       │ │
│  │                          │     │   POST /train                 │ │
│  └──────────────────────────┘     │   POST /predict               │ │
│                                   │   GET  /models                │ │
│                                   │   GET  /status                │ │
│                                   └───────────────────────────────┘ │
│                                                                     │
│                       shared volume /data/                          │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Container 1: Collector

**Responsibility:** Auto-discover the current 5-minute BTC market, stream tick data every second, and export a parquet batch at each candle boundary.

### Market Discovery

At startup and at each 5-minute boundary the collector fetches the current candle's market from the Polymarket Gamma API using the slug `{PM_SLUG_PREFIX}-{candle_start_ts}` to get the market condition ID and YES/NO token IDs. These rotate every candle.

### Data Sources

| Source | Data | Notes |
|---|---|---|
| CLOB WebSocket `market` feed | yes price, no price | `best_ask` per update; `last_trade_price` as fallback |
| CLOB WebSocket `crypto_prices_chainlink` | BTC/USD | Same oracle Polymarket uses for resolution |

### Writes

- **`raw_data.db`** — one DuckDB row per second (tick table)
- **`/data/raw/ticks_{market_id}_{timestamp}.parquet`** — one file per completed candle, written at the boundary
- **`/data/latest_tick.json`** — atomically overwritten every second with the current tick (`datetime`, `market_id`, `yes_price`, `no_price`, `btc_usd`)

---

## Container 2: Model

**Responsibility:** Three concurrent asyncio tasks — a file watcher that retrains on each new candle, an inference loop that runs every second, and a FastAPI server.

### Task 1: Watcher (`watcher.py`)

Triggered by each new parquet file in `/data/raw/`:

1. Reads the completed candle's BTC price to determine market resolution (`btc_close > btc_open`)
2. Calls `trades.resolve_market()` to fill in P&L for all open trades on that market
3. Retrains the model on all historical parquet files
4. Extracts the start-of-new-candle snapshot and calls `predictor.predict()` to log the opening prediction

### Task 2: Inference Loop (`inference.py`)

Every second:

1. Reads `/data/latest_tick.json`
2. Computes `pct_change_open` from the BTC price at candle open vs now
3. Computes `time_remaining` from the wall clock
4. Calls `predictor.infer()` which:
   - Runs the model
   - Logs green `EDGE` if `edge >= MIN_EDGE_THRESHOLD` and `predicted_prob >= MIN_PREDICTED_PROB`
   - Opens a $1 trade in `trades.parquet` if both conditions are met

### Task 3: FastAPI (`api.py`)

HTTP API on port 8000. Mainly for manual inspection and triggering; the watcher and inference loop run autonomously.

---

## Feature Engineering

Features are configurable via `FEATURE_NAMES` in `.env` (JSON array). The model rejects a loaded model whose stored `feature_names` differ from the current config and retrains automatically.

**Current default:**

| Feature | Description |
|---|---|
| `pct_change_open` | BTC % change from candle open to now (0.0 when no BTC data) |
| `time_remaining` | Seconds left in the 5-minute candle |
| `spread` | `yes_price + no_price − 1` (fee/liquidity proxy) |

**Label:** `resolved_yes = 1` if `btc_close > btc_open`; fallback: `final_yes_price >= 0.5` when BTC data is absent.

All rows from every parquet file are used for training (one row per second per candle).

---

## Trade System

Each second where the model detects edge, a $1 trade is opened and stored in `/data/trades/trades.parquet`. Trades are held until the market's candle completes; the watcher then fills in `resolved_yes`, `resolved_at`, and `pnl`.

**Trade gating (all three must be true):**
- `edge >= MIN_EDGE_THRESHOLD` (default 0.02)
- `predicted_prob >= MIN_PREDICTED_PROB` (default 0.70)
- `predicted_prob > market_prob / (1 - PM_FEE)` (breakeven after fees)

**P&L calculation:**
```
win  →  stake * (1 / yes_price) * (1 - PM_FEE) - stake
loss →  -stake
```

---

## Model Versioning

Each training run produces a versioned directory. `registry.json` points to the latest version per algorithm. On load, the registry checks that stored `feature_names` match the current config; a mismatch causes a retrain.

```
/data/models/
  logistic_regression_20260519_120000/
    model.joblib
    metadata.json      ← algorithm, feature_names, training_rows, metrics
  registry.json        ← { "logistic_regression": "logistic_regression_20260519_120000" }
```

---

## Storage Layout

```
/data/
  raw_data.db                         ← DuckDB (collector ticks)
  predictions.db                      ← DuckDB (post-candle predictions)
  latest_tick.json                    ← latest tick, overwritten every second
  raw/
    ticks_{market_id}_{ts}.parquet    ← one file per completed candle
  models/
    {algorithm}_{timestamp}/
      model.joblib
      metadata.json
    registry.json
  trades/
    trades.parquet                    ← full trade ledger
```

---

## Technology Choices

| Component | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 | ML ecosystem, async WebSocket |
| Package manager | uv | Fast, lockfile, reproducible |
| Data store | DuckDB | Zero-ops analytical SQL, parquet-native |
| ML framework | scikit-learn (LR) + LightGBM | LR is fast and interpretable; LightGBM available for comparison |
| API | FastAPI | Async, typed, auto-docs |
| Containers | Docker + Compose | Reproducible local environment |
| Inter-container IPC | Shared bind-mount (`/data/`) | No network calls between containers |
| File watching | watchfiles `awatch` | Efficient async file system events |
