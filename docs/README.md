# simple-value-betting

Real-time ML pipeline for trading Polymarket binary markets. Collects live tick data from BTC up/down 5-minute markets, trains a classification model after each candle, runs inference every second, and opens $1 trades whenever the model detects edge against the market price.

---

## What It Does

1. **Collects** yes/no token prices and BTC/USD from Polymarket WebSocket every second
2. **Exports** a parquet batch at each 5-minute candle boundary
3. **Trains** a logistic regression classifier on all historical candles when each new batch arrives
4. **Infers** every second from live tick data, opens a $1 trade whenever edge is detected
5. **Resolves** trades at each candle boundary and logs per-market and overall P&L

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose

---

## Quick Start

```bash
# Copy and fill in config
cp .env.example .env

# Start both containers
docker compose up --build

# Model API
curl http://localhost:8000/status
curl -X POST http://localhost:8000/train
```

---

## Repository Structure

```
simple-value-betting/
├── collector/
│   ├── src/
│   │   ├── main.py               # Entry point: tick loop + candle rotation
│   │   ├── websocket_client.py   # Polymarket CLOB WebSocket connection
│   │   ├── market.py             # Gamma API market discovery
│   │   ├── storage.py            # DuckDB writes, parquet export, latest_tick.json
│   │   ├── s3_sync.py            # S3 upload abstraction
│   │   └── config.py
│   ├── Dockerfile
│   └── pyproject.toml
│
├── model/
│   ├── src/
│   │   ├── main.py               # Entry point: three asyncio tasks
│   │   ├── watcher.py            # Watches /data/raw/, trains on new parquet
│   │   ├── inference.py          # Per-second inference loop
│   │   ├── predictor.py          # Model inference + trade gating
│   │   ├── trainer.py            # Logistic regression + LightGBM training
│   │   ├── features.py           # Feature extraction from parquet files
│   │   ├── registry.py           # Model versioning and loading
│   │   ├── trades.py             # Trade ledger (parquet)
│   │   ├── storage.py            # Predictions DuckDB
│   │   ├── api.py                # FastAPI app
│   │   └── config.py
│   ├── Dockerfile
│   └── pyproject.toml
│
├── data/                         # Bind-mounted volume (not committed)
│   ├── raw/                      # Parquet files from collector
│   ├── models/                   # Trained model artifacts
│   ├── trades/                   # trades.parquet
│   ├── latest_tick.json          # Written every second by collector
│   ├── raw_data.db               # DuckDB (collector)
│   └── predictions.db            # DuckDB (model)
│
├── docs/
├── docker-compose.yml
└── .env
```

---

## Configuration

All configuration is via environment variables in `.env`:

```env
# Environment
ENV=local                     # local | aws

# Polymarket
PM_WS_URL=wss://ws-subscriptions-clob.polymarket.com
PM_SLUG_PREFIX=btc-updown-5m  # slug prefix for Gamma API market discovery
CANDLE_INTERVAL_MINUTES=5

# Storage
LOCAL_DATA_DIR=/data
AWS_BUCKET=polymarket-ml-prod
AWS_REGION=eu-central-1

# Collector tuning
EXPORT_INTERVAL_MINUTES=5
TICK_INTERVAL_SECONDS=1.0
RECONNECT_BASE_DELAY=1.0
RECONNECT_MAX_DELAY=60.0

# Model training
MIN_TRAINING_ROWS=500

# Features (JSON array — model retrains automatically when changed)
FEATURE_NAMES=["pct_change_open","time_remaining","spread"]

# Trade gating
MIN_EDGE_THRESHOLD=0.02       # minimum predicted_prob - market_prob
MIN_PREDICTED_PROB=0.7        # minimum model output to open a trade

# Polymarket fee
PM_FEE=0.02
```

---

## API Reference

Swagger UI: `http://localhost:8000/docs`

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/train` | Trigger model training manually |
| `POST` | `/predict` | Get a prediction for given market inputs |
| `GET` | `/models` | List all registered models with metrics |
| `GET` | `/status` | System status and prediction count |
