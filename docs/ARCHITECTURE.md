# Architecture

## Overview

Five Docker containers sharing a bind-mounted `/data/` volume. The collector streams live market data; four simulation containers run inference every second and record trades to separate parquet ledgers for comparison.

---

## System Diagram

```
┌──────────────────────────────────────────────────────────────────────────┐
│                            Docker Compose                                │
│                                                                          │
│  ┌──────────────────────────┐                                            │
│  │   collector              │                                            │
│  │                          │                                            │
│  │  Polymarket WebSocket     │──── every second ───▶ /data/latest_tick.json
│  │   yes/no prices           │                                            │
│  │                          │──── every 5 min ────▶ /data/raw/*.parquet  │
│  │  Binance WebSocket        │                                            │
│  │   BTC/USD price          │                                            │
│  └──────────────────────────┘                                            │
│                                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐  │
│  │   model      │  │  model_sl    │  │  analysis    │  │ analysis_sl │  │
│  │              │  │              │  │              │  │             │  │
│  │  logistic    │  │  logistic    │  │  empirical   │  │  empirical  │  │
│  │  regression  │  │  regression  │  │  lookup      │  │  lookup     │  │
│  │              │  │  + stop-loss │  │  table       │  │  + stop-loss│  │
│  │  :8000 API   │  │              │  │              │  │             │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────┬──────┘  │
│         │                │                 │                 │          │
│         ▼                ▼                 ▼                 ▼          │
│  model_trades   model_sl_trades   analysis_trades  analysis_sl_trades   │
│  .parquet       .parquet          .parquet         .parquet             │
│                                                                          │
│                       shared volume /data/                               │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Container: Collector

**Responsibility:** Auto-discover the current 5-minute BTC market, stream tick data every second, export a parquet batch at each candle boundary.

### Data Sources

| Source | Data |
|---|---|
| Polymarket CLOB WebSocket `market` feed | YES/NO token best ask prices |
| Binance WebSocket `btcusdt@trade` | BTC/USD real-time price |

BTC price comes from Binance (`btc_feed.py`) rather than Polymarket's Chainlink oracle feed — lower latency and more reliable for intra-candle `pct_change_open` computation.

### Writes

- **`raw_data.db`** — one DuckDB row per second
- **`/data/raw/ticks_{market_id}_{timestamp}.parquet`** — one file per completed candle
- **`/data/latest_tick.json`** — atomically overwritten every second

---

## Container: Model

**Responsibility:** Three concurrent asyncio tasks — file watcher, inference loop, FastAPI server.

### Task 1: Watcher (`watcher.py`)

Triggered by each new parquet file in `/data/raw/`:
1. Reads the completed candle to determine resolution (`btc_close > btc_open`)
2. Calls `trades.resolve_market()` to fill P&L for all open trades
3. Retrains the logistic regression on all historical parquet files

### Task 2: Inference Loop (`inference.py` → `predictor.py`)

Every second:
1. Reads `/data/latest_tick.json`
2. Computes `pct_change_open` from BTC at candle open vs now
3. Computes `time_remaining` from wall clock
4. Calls `predictor.infer()` which runs the model and opens a trade if all filters pass

### Task 3: FastAPI (`api.py`)

HTTP API on port 8000 for manual inspection and triggering.

---

## Container: model_sl

Identical to `model` except:
- `inference.py` checks all open positions for the current market **before** calling `predictor.infer()`. If `current_yes_price ≤ entry_price − 0.15`, the position is closed at the current bid.
- `trades.py` writes to `model_sl_trades.parquet` and adds `exit_reason` / `exit_price` fields.
- The Dockerfile copies all of `model/src/` then overlays only these two files.

---

## Container: Analysis

**Responsibility:** Same inference/trade loop as `model` but uses an empirical lookup table instead of a trained ML model.

### Lookup Table

Built from all historical parquet files. Each cell `(time_bucket, pct_change_bucket)` stores `(n, empirical_yes_rate)` — the count and observed YES resolution rate for that combination. A trade is opened when the empirical rate exceeds the market price by at least `MIN_EDGE_THRESHOLD` and the cell has `n ≥ MIN_BUCKET_COUNT`.

The table is rebuilt in memory on every new candle arrival.

---

## Container: analysis_sl

Identical to `analysis` except stop-loss logic is applied in `inference.py` and trades go to `analysis_sl_trades.parquet`.

---

## Feature Engineering

| Feature | Description |
|---|---|
| `pct_change_open` | BTC % change from candle open to now |
| `time_remaining` | Seconds remaining in the 5-minute candle |
| `spread` | `yes_price + no_price − 1` (fee/liquidity proxy) |

**Label:** `resolved_yes = 1` if `btc_close > btc_open`; fallback `final_yes_price ≥ 0.5` when BTC data is absent.

Configurable via `FEATURE_NAMES` in `.env`. A model stored with different feature names is rejected by the registry and triggers a retrain.

---

## Trade System

### Filters (all containers)

| Condition | Value |
|---|---|
| `yes_price` | `[0.40, 0.97)` |
| `time_remaining` | `[100, 285]` seconds |
| `edge` | `[0.01, 0.15]` |
| `pct_change_open` | `≠ 0` |
| side | YES only |

### P&L

```
Resolution win:   stake * (1 / yes_price) * (1 − PM_FEE) − stake
Resolution loss:  −stake
Stop-loss exit:   stake * (exit_price / entry_price) − stake
```

Stop-loss triggers when `yes_price ≤ entry_price − 0.15`. No fee is applied on stop-loss exits (fee applies on winning resolution only).

---

## Model Versioning

```
/data/models/
  logistic_regression_20260521_130000/
    model.joblib
    metadata.json      ← algorithm, feature_names, training_rows, metrics
  registry.json        ← { "logistic_regression": "logistic_regression_..." }
```

---

## Storage Layout

```
/data/
  raw_data.db                         ← DuckDB (collector ticks)
  predictions.db                      ← DuckDB (post-candle model snapshots)
  latest_tick.json                    ← current tick, overwritten every second
  raw/
    ticks_{market_id}_{ts}.parquet    ← one file per completed candle
  models/
    {algorithm}_{timestamp}/
      model.joblib
      metadata.json
    registry.json
  trades/
    model_trades.parquet
    model_sl_trades.parquet
    analysis_trades.parquet
    analysis_sl_trades.parquet
  trades_archive/
    {YYYYMMDD}/
      model_trades.parquet
      analysis_trades.parquet
      ...
```

---

## Technology Choices

| Component | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 | ML ecosystem, async WebSocket |
| Package manager | uv | Fast, lockfile, reproducible |
| Data store | DuckDB | Zero-ops analytical SQL, parquet-native |
| ML model | scikit-learn logistic regression | Fast, interpretable, good baseline |
| API | FastAPI | Async, typed, auto-docs |
| Containers | Docker + Compose | Reproducible local environment |
| Inter-container IPC | Shared bind-mount (`/data/`) | No network calls between containers |
| File watching | watchfiles `awatch` | Efficient async filesystem events |
