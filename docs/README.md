# polymarket-ml

Real-time ML pipeline for predicting Polymarket binary market resolution using Chainlink BTC/USD price feed data. Collects live tick data, trains a classification model, generates predictions with edge signals, and stores everything for analysis and eventual live trading.

---

## What It Does

1. **Collects** yes/no token prices and BTC/USD oracle price from Polymarket WebSocket every second
2. **Trains** a LightGBM classifier on 15-minute candle features to predict market resolution probability
3. **Predicts** at each 15-minute interval close and computes edge vs market-implied probability
4. **Stores** all raw data and predictions in DuckDB, backed up to S3
5. **Analyses** calibration and edge distribution to find exploitable signals

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Python 3.12+
- AWS CLI (for deployment only)

---

## Quick Start (Local)

```bash
git clone https://github.com/you/polymarket-ml
cd polymarket-ml

# Copy and fill in config
cp .env.example .env

# Start both containers
docker compose up --build

# Model API is available at http://localhost:8000
# Trigger manual training
curl -X POST http://localhost:8000/train

# Check status
curl http://localhost:8000/status
```

---

## Repository Structure

```
polymarket-ml/
├── collector/                  # Data collection container
│   ├── src/
│   │   ├── websocket_client.py # Polymarket WS connection
│   │   ├── storage.py          # DuckDB writes + parquet export
│   │   ├── s3_sync.py          # S3 upload abstraction
│   │   └── config.py
│   ├── Dockerfile
│   └── pyproject.toml
│
├── model/                      # Model container (FastAPI)
│   ├── src/
│   │   ├── api/
│   │   │   ├── main.py         # FastAPI app
│   │   │   ├── routes/
│   │   │   │   ├── train.py
│   │   │   │   ├── predict.py
│   │   │   │   └── status.py
│   │   ├── pipeline/
│   │   │   ├── features.py     # Feature engineering
│   │   │   └── labels.py       # Resolution backfill poller
│   │   ├── training/
│   │   │   ├── train.py
│   │   │   └── evaluate.py
│   │   ├── inference/
│   │   │   └── predictor.py    # Model loading + caching
│   │   └── storage/
│   │       └── model_store.py  # local/S3 abstraction
│   ├── Dockerfile
│   └── pyproject.toml
│
├── analysis/                   # Standalone analysis scripts
│   ├── calibration.py          # Calibration curves by bucket
│   ├── edge.py                 # Edge analysis + P&L simulation
│   └── load_data.py            # Helper to load from local or S3
│
├── notebooks/                  # Jupyter notebooks for exploration
│   └── eda.ipynb
│
├── deploy/                     # Deployment scripts
│   ├── aws/
│   │   ├── setup_ec2.sh        # EC2 bootstrap script
│   │   ├── setup_s3.sh         # S3 bucket + IAM setup
│   │   └── deploy.sh           # Build, push, and restart on EC2
│   └── ci/
│       └── deploy.yml          # GitHub Actions workflow
│
├── docker-compose.yml          # Local development
├── docker-compose.aws.yml      # AWS overrides
├── .env.example
└── README.md
```

---

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env`:

```env
# Environment: local | aws
ENV=local

# Polymarket
PM_WS_URL=wss://ws-subscriptions-clob.polymarket.com/ws/market
PM_MARKET_ID=your-market-id-here

# Storage (local)
LOCAL_DATA_DIR=/data

# Storage (AWS)
AWS_BUCKET=your-bucket-name
AWS_REGION=eu-central-1

# Model training
TRAIN_INTERVAL_HOURS=24
MIN_TRAINING_ROWS=500

# Prediction
MIN_EDGE_THRESHOLD=0.05
```

---

## API Reference

Full docs available at `http://localhost:8000/docs` (Swagger UI).

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/train` | Trigger model training |
| `POST` | `/predict` | Get prediction for current features |
| `GET` | `/status` | Model and system status |

---

## Analysis

Analysis scripts run locally against exported parquet files:

```bash
cd analysis
uv run python calibration.py --data /data/predictions/
uv run python edge.py --data /data/predictions/ --fee 0.02
```

Or load from S3:

```bash
uv run python edge.py --source s3 --bucket your-bucket
```

---

## Deployment

See [DEPLOYMENT.md](./DEPLOYMENT.md) for full AWS deployment instructions.

Quick summary:
```bash
# One-time AWS setup
./deploy/aws/setup_s3.sh
./deploy/aws/setup_ec2.sh

# Deploy
./deploy/aws/deploy.sh
```

---

## Development

```bash
# Install dependencies for a container
cd collector
uv sync

# Run tests
uv run pytest

# Lint
uv run ruff check .
uv run mypy src/
```
