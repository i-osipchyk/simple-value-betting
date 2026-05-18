# Data Reference

Schema definitions, field descriptions, and data flow for the polymarket-ml system.

---

## Raw Ticks Table (`raw_data.db`)

Written by the collector every second.

| Column | Type | Description |
|---|---|---|
| `datetime` | TIMESTAMP | UTC timestamp of the tick |
| `market_id` | VARCHAR | Polymarket market identifier |
| `yes_price` | FLOAT | Last traded price of YES token (0–1, implied probability) |
| `no_price` | FLOAT | Last traded price of NO token (0–1) |
| `btc_usd` | FLOAT | BTC/USD price from Chainlink oracle via Polymarket WS |

Notes:
- `yes_price + no_price` is typically slightly above 1.0 (the spread is the market maker fee)
- Values are carried forward from the last trade if no new trade occurs in that second
- All timestamps are UTC

---

## Parquet Export (raw)

File naming: `ticks_{market_id}_{YYYYMMDD_HHMMSS}.parquet`

Same schema as raw ticks table. One file per 5-minute candle per market, written at the candle boundary before reconnecting to the next candle.

---

## Features (training input)

Computed by `pipeline/features.py` on 5-minute resampled data.

| Feature | Type | Description |
|---|---|---|
| `market_id` | VARCHAR | Market identifier |
| `pct_change_open` | FLOAT | (btc_close - btc_open) / btc_open for the 5-min candle |
| `yes_price` | FLOAT | YES token price at snapshot |
| `no_price` | FLOAT | NO token price at snapshot |
| `spread` | FLOAT | yes_price + no_price - 1 (fee/liquidity proxy) |
| `time_remaining` | INTEGER | Seconds until interval_end at snapshot time |
| `resolved_yes` | BOOLEAN | Label: did market resolve as YES? (NULL until resolved) |

---

## Predictions Table (`predictions.db`)

Written by the model container on each `/predict` call.

| Column | Type | Description |
|---|---|---|
| `id` | VARCHAR | UUID, primary key |
| `predicted_at` | TIMESTAMP | When the prediction was made |
| `market_id` | VARCHAR | Market identifier |
| `yes_price` | FLOAT | Market yes price at prediction time |
| `no_price` | FLOAT | Market no price at prediction time |
| `btc_usd` | FLOAT | BTC/USD at prediction time |
| `pct_change_open` | FLOAT | Feature: BTC % change from candle open |
| `time_remaining` | INTEGER | Feature: seconds remaining in candle |
| `predicted_prob` | FLOAT | Model output: probability market resolves YES |
| `market_prob` | FLOAT | Implied probability from market (= yes_price) |
| `edge` | FLOAT | predicted_prob - market_prob (positive = bet YES) |
| `model_id` | VARCHAR | Which model version was used |
| `algorithm` | VARCHAR | Algorithm key, e.g. `logistic_regression`, `lightgbm` |
| `resolved_yes` | BOOLEAN | Ground truth, backfilled after market resolves |
| `resolved_at` | TIMESTAMP | When the market resolved |

---

## Model Metadata (`metadata.json`)

Saved alongside each `model.joblib`.

```json
{
  "model_id": "logistic_regression_20260518_090000",
  "trained_at": "2026-05-18T09:00:00Z",
  "algorithm": "logistic_regression",
  "feature_names": ["pct_change_open", "time_remaining", "yes_price", "no_price", "spread"],
  "training_data_range": {
    "start": "2026-05-01T00:00:00Z",
    "end": "2026-05-17T08:45:00Z"
  },
  "training_rows": 1842,
  "test_rows": 461,
  "metrics": {
    "auc_roc": 0.71,
    "brier_score": 0.18,
    "accuracy": 0.67,
    "calibration_by_bucket": {
      "0.5-0.6": { "predicted": 0.55, "actual": 0.52, "n": 120 },
      "0.6-0.7": { "predicted": 0.65, "actual": 0.63, "n": 87 },
      "0.7-0.8": { "predicted": 0.74, "actual": 0.71, "n": 54 },
      "0.8-0.9": { "predicted": 0.84, "actual": 0.80, "n": 31 }
    }
  }
}
```

---

## S3 Layout

```
s3://polymarket-ml-{env}/
  raw/
    ticks_{market_id}_{timestamp}.parquet
  models/
    logistic_regression_{timestamp}/
      model.joblib
      metadata.json
    lightgbm_{timestamp}/
      model.joblib
      metadata.json
    registry.json                           ← { "logistic_regression": "logistic_regression_20260518_090000",
                                                 "lightgbm": "lightgbm_20260518_090000" }
  predictions/
    predictions_{YYYYMMDD}.parquet
```

---

## Edge Calculation

For a given prediction:

```
edge = predicted_prob - market_prob

Positive edge → model thinks market will resolve YES more often than it's priced
Negative edge → model thinks NO is more likely than priced
Zero/small edge → skip, not enough signal

EV per dollar staked (before fees):
  EV = predicted_prob * (1/market_prob - 1) - (1 - predicted_prob) * 1
     = predicted_prob / market_prob - 1

After Polymarket fee (f = 0.02):
  payout = (1/market_prob) * (1 - f)
  EV = predicted_prob * (payout - 1) - (1 - predicted_prob)
```

Minimum edge to be profitable after fees:
```
predicted_prob / market_prob > 1 / (1 - fee)
→ predicted_prob > market_prob / (1 - 0.02)
→ predicted_prob > market_prob * 1.0204
```

Example: market at 0.75 → need model to predict at least **0.765** to break even after fees.

---

## Calibration Buckets

Used in `analysis/calibration.py` to validate that model probabilities are meaningful.

| Bucket | What it means |
|---|---|
| `predicted_prob` 0.5–0.6 | Model is slightly confident YES |
| `predicted_prob` 0.6–0.7 | Moderate confidence |
| `predicted_prob` 0.7–0.8 | High confidence |
| `predicted_prob` 0.8–1.0 | Very high confidence |

A well-calibrated model: when it predicts 0.75, the actual win rate in that bucket is ~75%. Systematic deviation (e.g. predicts 0.75 but wins only 60%) means the model is overconfident and edge estimates are inflated.
