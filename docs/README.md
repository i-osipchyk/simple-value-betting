# simple-value-betting

Real-time ML pipeline for simulating trades on Polymarket BTC up/down 5-minute binary markets. Collects live tick data, trains a logistic regression model after each candle, and runs four parallel trade simulations every second — comparing a model-based and empirical approach, each with and without a stop-loss.

---

## What It Does

1. **Collects** YES/NO token prices from Polymarket WebSocket and BTC/USD from Binance every second
2. **Exports** a parquet batch at each 5-minute candle boundary
3. **Trains** a logistic regression classifier on all historical candles after each new batch
4. **Simulates** $1 trades across four containers whenever edge is detected
5. **Resolves** trades at each candle boundary and logs per-market and overall P&L

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose

---

## Quick Start

```bash
cp .env.example .env
docker compose up --build

# Model API
curl http://localhost:8000/status
curl -X POST http://localhost:8000/train
```

---

## Repository Structure

```
simple-value-betting/
├── src/
│   ├── collector/               # Tick collection and candle export
│   │   ├── src/
│   │   │   ├── main.py          # Entry point: tick loop + candle rotation
│   │   │   ├── btc_feed.py      # Binance WebSocket BTC/USD feed
│   │   │   ├── websocket_client.py
│   │   │   ├── market.py        # Gamma API market discovery
│   │   │   ├── storage.py       # DuckDB writes, parquet export, latest_tick.json
│   │   │   ├── s3_sync.py
│   │   │   └── config.py
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   │
│   ├── model/                   # Logistic regression — inference + trade simulation
│   │   ├── src/
│   │   │   ├── main.py          # Entry point: watcher + inference + FastAPI
│   │   │   ├── watcher.py       # Watches /data/raw/, resolves trades, retrains
│   │   │   ├── inference.py     # Per-second inference loop
│   │   │   ├── predictor.py     # Model inference + trade gating
│   │   │   ├── trainer.py       # Logistic regression training
│   │   │   ├── features.py      # Feature extraction
│   │   │   ├── registry.py      # Model versioning
│   │   │   ├── trades.py        # Trade ledger → model_trades.parquet
│   │   │   ├── storage.py       # Predictions DuckDB
│   │   │   ├── api.py           # FastAPI app
│   │   │   └── config.py
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   │
│   ├── model_sl/                # Same as model but with stop-loss
│   │   ├── src/
│   │   │   ├── inference.py     # Adds stop-loss check before predictor.infer()
│   │   │   └── trades.py        # → model_sl_trades.parquet; adds exit_reason/exit_price
│   │   └── Dockerfile           # Overlays model_sl/src/ on top of model/src/
│   │
│   ├── analysis/                # Empirical lookup table — inference + trade simulation
│   │   ├── src/
│   │   │   ├── main.py
│   │   │   ├── watcher.py       # Watches /data/raw/, resolves trades, rebuilds table
│   │   │   ├── inference.py     # Per-second inference loop using lookup table
│   │   │   ├── lookup.py        # Build and query empirical probability table
│   │   │   ├── trades.py        # Trade ledger → analysis_trades.parquet
│   │   │   └── config.py
│   │   ├── Dockerfile
│   │   └── pyproject.toml
│   │
│   └── analysis_sl/             # Same as analysis but with stop-loss
│       ├── src/
│       │   ├── inference.py     # Adds stop-loss check before lookup trade
│       │   └── trades.py        # → analysis_sl_trades.parquet
│       └── Dockerfile           # Overlays analysis_sl/src/ on top of analysis/src/
│
├── data/                        # Bind-mounted volume (not committed)
│   ├── raw/                     # Parquet candle files from collector
│   ├── models/                  # Trained model artifacts + registry.json
│   ├── trades/                  # Four parquet trade ledgers (one per container)
│   ├── trades_archive/          # Archived trade snapshots by date
│   ├── latest_tick.json         # Written every second by collector
│   └── raw_data.db              # DuckDB (collector ticks)
│
├── notebooks/                   # Jupyter analysis notebooks
│   ├── filtered_trades.ipynb    # Main analysis: P&L by edge/time/price/BTC%
│   ├── edge_analysis.ipynb      # Edge sweep and calibration
│   └── trade_analysis.ipynb     # Historical trade analysis
│
├── docs/
├── docker-compose.yml
└── .env
```

---

## Configuration

All configuration is via environment variables in `.env`:

```env
# Polymarket
PM_WS_URL=wss://ws-subscriptions-clob.polymarket.com
PM_SLUG_PREFIX=btc-updown-5m
CANDLE_INTERVAL_MINUTES=5

# Storage
LOCAL_DATA_DIR=/data

# Model training
MIN_TRAINING_ROWS=500
FEATURE_NAMES=["pct_change_open","time_remaining","spread"]

# Trade gating
MIN_EDGE_THRESHOLD=0.01

# Polymarket fee (used in P&L calculation)
PM_FEE=0.02

# Empirical lookup table (analysis container)
TIME_BUCKET_SECONDS=30
PCT_CHANGE_BUCKET_SIZE=0.001
MIN_BUCKET_COUNT=10
```

---

## Trade Filters

All four containers apply the same filters before opening a simulated trade:

| Filter | Value | Reason |
|---|---|---|
| `yes_price` | `[0.40, 0.97)` | Extreme prices have unreliable edge |
| `time_remaining` | `[100s, 285s]` | Skip first 15s (stale data) and last 100s (low liquidity) |
| `edge` | `[0.01, 0.15]` | Edge > 0.15 means model strongly disagrees with market (likely wrong) |
| `pct_change_open` | `≠ 0` | No BTC data yet — feature is meaningless |
| `side` | YES only | NO trades systematically underperform |

---

## API Reference

Swagger UI: `http://localhost:8000/docs`

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/train` | Trigger model retraining manually |
| `POST` | `/predict` | Get a prediction for given market inputs |
| `GET` | `/models` | List all registered models with metrics |
| `GET` | `/status` | System status and prediction count |

---

## Roadmap

- Add L1/L2 regularization to prevent overfitting on market data
- Feature engineering: remove `spread` from model features (may be used as an execution filter)
- Training data investigation: analyze bucket splits for probabilities
- Binomial test on trade win rate vs break-even rate to validate edge significance
- Test on less liquid markets where mispricing is more likely to persist
