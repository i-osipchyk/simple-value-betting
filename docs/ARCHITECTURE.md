# Architecture — Polymarket ML Trading System

## Overview

A two-container ML pipeline that collects real-time market data from Polymarket, trains multiple classification models in parallel to predict market resolution, generates predictions with edge signals, and stores everything for analysis. Designed for local development with a clean path to AWS deployment. Built to be extended with live trading execution.

---

## System Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Docker Compose                               │
│                                                                     │
│  ┌─────────────────────────┐      ┌──────────────────────────────┐  │
│  │   collector             │      │   model                      │  │
│  │                         │      │                              │  │
│  │  Polymarket WebSocket   │      │  FastAPI                     │  │
│  │   ├─ yes/no prices      │      │   ├─ POST /train             │  │
│  │   └─ btc/usd (CL feed)  │      │   ├─ POST /predict           │  │
│  │         │               │      │   └─ GET  /status            │  │
│  │         ▼               │      │         │                    │  │
│  │      DuckDB             │      │  Feature Pipeline            │  │
│  │   raw_data.db           │      │  Model Registry              │  │
│  │         │               │      │  predictions.db              │  │
│  │   every 5min            │      │         │                    │  │
│  │         ▼               │      │   every N hours              │  │
│  │   /data/raw/*.parquet   │      │         ▼                    │  │
│  └─────────────────────────┘      │   /data/models/              │  │
│              │                    │   /data/predictions/*.parquet│  │
│              └──────────┬─────────┘──────────────────────────────┘  │
│                         │                                           │
│                  shared volume                                      │
│                  /data/                                             │
└─────────────────────────────────────────────────────────────────────┘
                          │
                  ENV=aws │
                          ▼
                    ┌───────────┐
                    │    S3     │
                    │  /raw/    │
                    │  /models/ │
                    │  /preds/  │
                    └───────────┘
```

---

## Container 1: Collector

**Responsibility:** Auto-discover the current 5-minute BTC market from the Polymarket Gamma API, connect to the CLOB WebSocket, collect raw tick data every second, persist to DuckDB, and export a parquet batch at each candle boundary.

### Data Sources

At each 5-minute boundary the collector:
1. Fetches the current candle's market from `https://gamma-api.polymarket.com/markets/slug/{PM_SLUG_PREFIX}-{candle_ts}` to get the market condition ID and YES/NO token IDs (these rotate every candle).
2. Opens a new WebSocket connection subscribed to those token IDs.

| Topic | Data | Notes |
|---|---|---|
| `market` (CLOB feed) | yes price, no price | Top-of-book `best_ask` per update; `last_trade_price` as fallback |
| `crypto_prices_chainlink` | BTC/USD | Same oracle Polymarket uses for resolution |

### Storage

**DuckDB (`raw_data.db`)** — append-only tick table:

```sql
CREATE TABLE ticks (
    datetime        TIMESTAMP NOT NULL,
    market_id       VARCHAR   NOT NULL,
    yes_price       FLOAT,
    no_price        FLOAT,
    btc_usd         FLOAT
);
```

**Parquet export** — at each 5-minute candle boundary, the current batch is written to:
- Local: `/data/raw/ticks_{market_id}_{timestamp}.parquet`
- AWS: `s3://{bucket}/raw/ticks_{market_id}_{timestamp}.parquet`

### Behavior

- At startup and at each 5-minute boundary: fetches current market info (condition ID + token IDs) from the Gamma API, then opens a fresh WebSocket connection for the new candle
- Reconnects automatically within a candle on WebSocket drop (exponential backoff)
- Writes one tick row to DuckDB every second, carrying forward the last known prices
- Exports a parquet batch and rotates at the candle boundary (before reconnecting)
- Logs connection state, row counts, and export results

---

## Container 2: Model

**Responsibility:** Serve a FastAPI app that trains the classification model on a schedule, generates predictions on demand, and persists all outputs.

### API Endpoints

```
POST /train
  Body (optional): { "algorithm": "logistic_regression" }   ← default
                   { "algorithm": "lightgbm" }
  Reads raw parquet from storage
  Runs feature engineering pipeline
  Trains the specified algorithm
  Evaluates on holdout set
  Saves versioned model artifact under models/{algorithm}_{timestamp}/
  Updates registry.json to point latest for that algorithm
  Returns: { model_id, algorithm, metrics: { auc, accuracy, brier_score, calibration } }

POST /predict
  Body: { market_id, yes_price, no_price, btc_usd, candle_open, time_remaining,
          "algorithm": "logistic_regression" }   ← optional, defaults to logistic_regression
  Loads latest model for the requested algorithm (each cached separately)
  Returns predicted probability + edge vs market price
  Writes to predictions.db
  Returns: { predicted_prob, market_prob, edge, model_id, algorithm, timestamp }

GET /models
  Returns: list of all registered algorithms with their latest model_id, trained_at, metrics

GET /status
  Returns: { models: { algorithm: { model_id, trained_at, metrics } }, predictions_count, uptime }
```

### Feature Engineering Pipeline

Input: raw ticks resampled to 5-minute intervals

| Feature | Description |
|---|---|
| `pct_change_open` | (btc_close - btc_open) / btc_open for the 5-min candle |
| `time_remaining` | seconds until 5-min interval closes |
| `yes_price` | current implied probability (best_ask of YES token) |
| `no_price` | current implied probability (best_ask of NO token) |
| `spread` | yes_price + no_price − 1 (liquidity/fee proxy) |

Label: `resolved_yes` (boolean) — backfilled by resolution poller.

### Model

Multiple algorithms are trained and served in parallel. Each has its own versioned artifacts and its own latest pointer in the registry.

| Algorithm | Key | Notes |
|---|---|---|
| Logistic Regression | `logistic_regression` | **Default.** Fast, interpretable, well-calibrated baseline |
| LightGBM | `lightgbm` | Higher capacity; compare against LR to check if complexity pays off |

- **Evaluation:** AUC-ROC, Brier score, calibration curve by confidence bucket (same for all algorithms)
- **Split:** time-based (no data leakage)
- **Serialization:** `model.joblib` + `metadata.json` per algorithm version

### Model Versioning

Each algorithm has its own versioned directory. A single `registry.json` tracks the latest version per algorithm.

```
/data/models/
  logistic_regression_20260518_090000/
    model.joblib
    metadata.json        # trained_at, algorithm, metrics, feature_names, data_range
  logistic_regression_20260517_143022/
    model.joblib
    metadata.json
  lightgbm_20260518_090000/
    model.joblib
    metadata.json
  registry.json          # { "logistic_regression": "logistic_regression_20260518_090000",
                         #   "lightgbm": "lightgbm_20260518_090000" }
```

On S3: `registry.json` is an object key that maps algorithm names to their active model prefix. The model container reads it on startup and updates the relevant entry after each training run.

### Predictions DuckDB

```sql
CREATE TABLE predictions (
    id              VARCHAR PRIMARY KEY,
    predicted_at    TIMESTAMP,
    market_id       VARCHAR,
    yes_price       FLOAT,
    no_price        FLOAT,
    btc_usd         FLOAT,
    pct_change_open FLOAT,
    time_remaining  INTEGER,
    predicted_prob  FLOAT,
    market_prob     FLOAT,
    edge            FLOAT,          -- predicted_prob - market_prob
    model_id        VARCHAR,
    algorithm       VARCHAR,        -- e.g. "logistic_regression", "lightgbm"
    resolved_yes    BOOLEAN,        -- NULL until resolved
    resolved_at     TIMESTAMP
);
```

`resolved_yes` is backfilled by a background poller that checks Polymarket's REST API for resolved markets.

---

## Storage Layout

```
/data/                          (bind-mounted to ./data locally, S3 on AWS)
  raw/
    ticks_{market_id}_{ts}.parquet
  models/
    {algorithm}_{timestamp}/
      model.joblib
      metadata.json
    registry.json               # maps algorithm → latest model directory
  predictions/
    predictions_{date}.parquet
```

### Environment Switch

A single `ENV` variable controls all storage paths:

```
ENV=local  →  reads/writes to /data/ (bind-mounted to ./data on the host)
ENV=aws    →  reads/writes to s3://{BUCKET}/
```

No code changes between environments.

---

## Data Analysis Layer

Analysis is done outside the containers, in `notebooks/` and `analysis/`:

- Load predictions parquet locally or from S3
- Join with resolutions to compute ground truth
- Calibration curves: is a 0.8 model prediction right 80% of the time?
- Edge analysis by confidence bucket, time remaining, market type
- P&L simulation with configurable stake and fee model

---

## Future Extensions

The architecture is designed to accommodate these additions without structural changes:

### Live Trading (next logical step)
- Add `POST /execute` endpoint to model container
- Polymarket CLOB API for order placement
- Position tracking table in predictions.db
- Risk management config: max stake, max open positions, min edge threshold

### Multi-Market Support
- Collector subscribes to multiple market_ids
- Model trains per-market or with market_id as a feature
- Prediction runner loops over active markets

### Model Improvements
- Online learning / incremental retraining
- Feature store for pre-computed features
- Ensemble: combine logistic regression and LightGBM predictions into a meta-model
- Regime detection (high/low volatility BTC regimes)
- Additional algorithms: XGBoost, calibrated neural net

### Monitoring
- Prometheus metrics endpoint on model container
- Grafana dashboard for prediction volume, edge distribution, P&L
- Alerting on model drift (calibration degradation)

### SageMaker (if scaling)
- Replace local model store with SageMaker Model Registry
- Use SageMaker Batch Transform for scheduled predictions
- Training jobs on managed infrastructure

---

## Technology Choices

| Component | Choice | Rationale |
|---|---|---|
| Language | Python 3.12 | Ecosystem for ML, async WebSocket |
| Package manager | uv | Fast, lockfile, reproducible |
| Data store | DuckDB | Analytical SQL, zero ops, parquet-native |
| Cloud storage | S3 | Simple, cheap, parquet-compatible |
| ML framework | scikit-learn (LR default) + LightGBM | Multiple algorithms served in parallel; LR is fast and well-calibrated by default |
| API framework | FastAPI | Async, typed, auto-docs |
| Containerization | Docker + Compose | Local parity with EC2 |
| Scheduling | asyncio + boundary-aligned candle loop | Collector reconnects and exports at each 5-min boundary; no external scheduler |
| WebSocket | websockets (asyncio) | Lightweight, reliable |
