# Data Reference

Schema definitions and field descriptions for all data written by the pipeline.

---

## Raw Ticks (`raw_data.db`)

Written by the collector every second.

| Column | Type | Description |
|---|---|---|
| `datetime` | TIMESTAMP | UTC timestamp of the tick |
| `market_id` | VARCHAR | Polymarket market condition ID |
| `yes_price` | FLOAT | Best ask of YES token (0–1, implied probability) |
| `no_price` | FLOAT | Best ask of NO token (0–1) |
| `btc_usd` | FLOAT | BTC/USD from Chainlink oracle via Polymarket WebSocket |

Notes:
- `yes_price + no_price` is typically slightly above 1.0 (the spread is the market maker fee)
- Prices are carried forward from the last update if no new update arrives in that second

---

## Parquet Export (`/data/raw/`)

File naming: `ticks_{market_id}_{YYYYMMDD_HHMMSS}.parquet`

Same schema as raw ticks. One file per completed 5-minute candle, written at the boundary before reconnecting to the next candle. These files are the primary training input for the model.

---

## Latest Tick (`/data/latest_tick.json`)

Written atomically by the collector every second. Read by the model inference loop.

```json
{
  "datetime": "2026-05-19T12:00:01.000000+00:00",
  "market_id": "0xabc...",
  "yes_price": 0.52,
  "no_price": 0.49,
  "btc_usd": 68421.5
}
```

---

## Features (model input)

The set of features is configurable via `FEATURE_NAMES` in `.env`. The default is:

| Feature | Type | Description |
|---|---|---|
| `pct_change_open` | FLOAT | BTC % change from candle open to current tick (0.0 when BTC data absent) |
| `time_remaining` | INTEGER | Seconds remaining in the current 5-minute candle |
| `spread` | FLOAT | `yes_price + no_price − 1` (fee/liquidity proxy) |

Available features that can be added back via `FEATURE_NAMES`:

| Feature | Description |
|---|---|
| `yes_price` | Current YES token price (market-implied probability) |
| `no_price` | Current NO token price |

Label: `resolved_yes = 1` if `btc_close > btc_open` for the candle; fallback `final_yes_price >= 0.5` when BTC data is absent.

---

## Trades (`/data/trades/trades.parquet`)

One row per $1 trade opened by the inference loop. `resolved_yes`, `resolved_at`, and `pnl` are null until the market's candle completes.

| Column | Type | Description |
|---|---|---|
| `trade_id` | STRING | UUID |
| `opened_at` | TIMESTAMP (UTC) | When the trade was opened |
| `market_id` | STRING | Market condition ID |
| `yes_price` | FLOAT | YES price at trade open |
| `no_price` | FLOAT | NO price at trade open |
| `btc_usd` | FLOAT | BTC/USD at trade open |
| `pct_change_open` | FLOAT | Feature value at trade open |
| `time_remaining` | INT32 | Feature value at trade open |
| `spread` | FLOAT | Feature value at trade open |
| `predicted_prob` | FLOAT | Model output at trade open |
| `edge` | FLOAT | `predicted_prob − yes_price` at trade open |
| `model_id` | STRING | Model version used |
| `stake` | FLOAT | Always 1.0 |
| `resolved_yes` | BOOL | Whether market resolved YES (null until resolved) |
| `resolved_at` | TIMESTAMP (UTC) | When resolution was determined (null until resolved) |
| `pnl` | FLOAT | `stake*(1/yes_price)*(1−fee)−stake` if YES, else `−stake` |

---

## Predictions (`predictions.db`)

Written once per candle per algorithm, after each retraining cycle.

| Column | Type | Description |
|---|---|---|
| `id` | VARCHAR | UUID |
| `predicted_at` | TIMESTAMP | When the prediction was made |
| `market_id` | VARCHAR | Market condition ID |
| `yes_price` | FLOAT | YES price at prediction time |
| `no_price` | FLOAT | NO price at prediction time |
| `btc_usd` | FLOAT | BTC/USD at prediction time |
| `pct_change_open` | FLOAT | Feature: BTC % change from candle open |
| `time_remaining` | INTEGER | Feature: seconds remaining in candle |
| `predicted_prob` | FLOAT | Model output: probability of YES resolution |
| `market_prob` | FLOAT | Market-implied probability (`yes_price`) |
| `edge` | FLOAT | `predicted_prob − market_prob` |
| `model_id` | VARCHAR | Model version used |
| `algorithm` | VARCHAR | e.g. `logistic_regression` |

---

## Model Metadata (`metadata.json`)

Saved alongside each `model.joblib`.

```json
{
  "model_id": "logistic_regression_20260519_120000",
  "trained_at": "2026-05-19T12:00:00Z",
  "algorithm": "logistic_regression",
  "feature_names": ["pct_change_open", "time_remaining", "spread"],
  "training_rows": 42962,
  "test_rows": 10741,
  "metrics": {
    "auc_roc": 0.523,
    "brier_score": 0.254,
    "accuracy": 0.414
  }
}
```

If the `feature_names` stored here do not match `FEATURE_NAMES` in config, the registry rejects this model and the watcher triggers a fresh retraining.

---

## Edge Calculation

```
edge = predicted_prob - market_prob   (market_prob = yes_price)

Breakeven after Polymarket fee (2%):
  predicted_prob > market_prob / (1 - 0.02)
  e.g. market at 0.50  →  need predicted_prob > 0.510 to break even

P&L per trade:
  win  →  1.0 * (1 / yes_price) * 0.98 - 1.0
  loss →  -1.0
```

A trade is opened only when all three conditions hold:
1. `edge >= MIN_EDGE_THRESHOLD` (default 0.02)
2. `predicted_prob >= MIN_PREDICTED_PROB` (default 0.70)
3. `predicted_prob > market_prob / (1 - PM_FEE)`
