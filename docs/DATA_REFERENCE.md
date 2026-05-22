# Data Reference

Schema definitions for all data written by the pipeline.

---

## Raw Ticks (`raw_data.db`)

Written by the collector every second.

| Column | Type | Description |
|---|---|---|
| `datetime` | TIMESTAMP | UTC timestamp of the tick |
| `market_id` | VARCHAR | Polymarket market condition ID |
| `yes_price` | FLOAT | Best ask of YES token (0–1) |
| `no_price` | FLOAT | Best ask of NO token (0–1) |
| `btc_usd` | FLOAT | BTC/USD from Binance WebSocket |

Notes:
- `yes_price + no_price` is typically slightly above 1.0 — the difference is the spread
- Prices are carried forward from the last update if no new WebSocket message arrives in that second
- BTC price comes from Binance, not Polymarket's Chainlink oracle

---

## Parquet Export (`/data/raw/`)

File naming: `ticks_{market_id}_{YYYYMMDD_HHMMSS}.parquet`

Same schema as raw ticks. One file per completed 5-minute candle. These are the primary training input for the model and the source for building the empirical lookup table.

---

## Latest Tick (`/data/latest_tick.json`)

Written atomically every second by the collector. Read by all four inference containers.

```json
{
  "datetime": "2026-05-22T07:00:01.000000+00:00",
  "market_id": "0xabc...",
  "yes_price": 0.62,
  "no_price": 0.39,
  "btc_usd": 77500.0
}
```

---

## Trade Ledgers (`/data/trades/`)

Four separate parquet files — one per container. All share the same base schema; stop-loss containers add two extra columns.

### Base schema (model_trades.parquet, analysis_trades.parquet)

| Column | Type | Description |
|---|---|---|
| `trade_id` | STRING | UUID |
| `opened_at` | TIMESTAMP (UTC) | When the trade was opened |
| `market_id` | STRING | Market condition ID |
| `yes_price` | FLOAT | YES price at trade open |
| `no_price` | FLOAT | NO price at trade open |
| `btc_usd` | FLOAT | BTC/USD at trade open |
| `pct_change_open` | FLOAT | BTC % change from candle open |
| `time_remaining` | INT32 | Seconds remaining in candle |
| `spread` | FLOAT | `yes_price + no_price − 1` |
| `side` | STRING | Always `"YES"` |
| `predicted_prob` | FLOAT | Model/empirical YES probability |
| `edge` | FLOAT | `predicted_prob − yes_price` |
| `model_id` | STRING | Model version or empirical cell descriptor |
| `stake` | FLOAT | Always `1.0` |
| `resolved_yes` | BOOL | Market outcome (null until resolved) |
| `resolved_at` | TIMESTAMP (UTC) | When resolution was determined |
| `pnl` | FLOAT | Profit/loss (null until resolved) |

### Additional columns (model_sl_trades.parquet, analysis_sl_trades.parquet)

| Column | Type | Description |
|---|---|---|
| `exit_reason` | STRING | `"resolved"`, `"stop_loss"`, or null (still open) |
| `exit_price` | FLOAT | Price at which position was closed early (stop-loss only) |

### P&L values

```
Resolution win:   stake * (1 / yes_price) * (1 − 0.02) − stake
Resolution loss:  −stake
Stop-loss exit:   stake * (exit_price / yes_price) − stake
```

---

## Features (model input)

| Feature | Type | Description |
|---|---|---|
| `pct_change_open` | FLOAT | BTC % change from candle open to current tick |
| `time_remaining` | INTEGER | Seconds remaining in the 5-minute candle |
| `spread` | FLOAT | `yes_price + no_price − 1` |

Configurable via `FEATURE_NAMES` in `.env`. Label: `resolved_yes = 1` if `btc_close > btc_open`; fallback `final_yes_price ≥ 0.5` when BTC data is absent.

---

## Model Metadata (`metadata.json`)

```json
{
  "model_id": "logistic_regression_20260522_070000",
  "trained_at": "2026-05-22T07:00:00Z",
  "algorithm": "logistic_regression",
  "feature_names": ["pct_change_open", "time_remaining", "spread"],
  "training_rows": 115915,
  "test_rows": 28979,
  "metrics": {
    "auc_roc": 0.8372,
    "brier_score": 0.1648,
    "accuracy": 0.7545
  }
}
```

If `feature_names` stored here do not match `FEATURE_NAMES` in config, the model is rejected and the watcher triggers a fresh retrain.

---

## Edge Calculation

```
edge = predicted_prob − yes_price

P&L per trade:
  win  →  1.0 * (1 / yes_price) * 0.98 − 1.0
  loss →  −1.0

Stop-loss P&L:
  exit →  exit_price / yes_price − 1.0
```

Trade is opened only when all filters pass:

| Filter | Condition |
|---|---|
| `yes_price` | `≥ 0.40` and `< 0.97` |
| `time_remaining` | `≥ 100` and `≤ 285` |
| `edge` | `≥ MIN_EDGE_THRESHOLD (0.01)` and `≤ 0.15` |
| `pct_change_open` | `≠ 0.0` |
