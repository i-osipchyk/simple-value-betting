# System Diagrams

## Data Flow

```
                  ┌─────────────────────────────────────┐
                  │            Polymarket                │
                  │  yes/no prices (CLOB WebSocket)      │
                  └──────────────┬──────────────────────┘
                                 │
                  ┌──────────────┴──────────────────────┐
                  │            Binance                   │
                  │  BTC/USD (trade stream WebSocket)    │
                  └──────────────┬──────────────────────┘
                                 │ every second
                                 ▼
                  ┌─────────────────────────────────────┐
                  │           Collector                  │
                  │  stores ticks in DuckDB              │
                  │  exports parquet at candle boundary  │
                  └──────────┬──────────────────────────┘
                             │
             ┌───────────────┴───────────────┐
             │ every second                  │ every 5 min
             ▼                               ▼
 /data/latest_tick.json           /data/raw/*.parquet
                                  (one file per candle)
             │                               │
             │ inference loop                │ watcher
             │ (every second)                │ (on new file)
             ▼                               ▼
 ┌────────────────────────────────────────────────────────┐
 │              4 simulation containers                   │
 │                                                        │
 │  model          model_sl       analysis   analysis_sl  │
 │                                                        │
 │  inference loop (every second):                        │
 │    [sl only] check stop-loss on open positions         │
 │    compute edge vs market price                        │
 │    if filters pass → open $1 trade                     │
 │                                                        │
 │  watcher (on each new parquet):                        │
 │    resolve open trades for completed candle            │
 │    [model only] retrain logistic regression            │
 │    [analysis only] rebuild lookup table                │
 └───────────────────────┬────────────────────────────────┘
                         │
        ┌────────────────┴─────────────────┐
        ▼                                  ▼
/data/trades/                       /data/models/
  model_trades.parquet                registry.json
  model_sl_trades.parquet             logistic_regression_.../
  analysis_trades.parquet               model.joblib
  analysis_sl_trades.parquet            metadata.json
```

---

## Stop-Loss Flow

```
each tick (inference loop — sl containers only):

  for each open position on current market:
    if current_yes_price ≤ entry_price − 0.15:
      exit_price = current_yes_price
      pnl = exit_price / entry_price − 1
      mark trade as stop_loss, write pnl
      log STOP-LOSS in red

  then run normal edge check → open new trade if filters pass

on market resolution (watcher):
  resolve only trades where exit_reason is null
  (stopped-out trades already have pnl, skip them)
  log RESOLVED + OVERALL summary
```

---

## What the Logs Look Like

**No trade (filtered):**
```
SKIP  market=0xabc...   t=245s  yes=0.380  edge=+0.0820
```

**Edge found — trade opened (green + bold):**
```
EDGE YES  market=0xabc...   t=210s  pct=+0.00123  cells=1840/2175  YES=0.720  n=45  mkt=0.620  edge=+0.1000
```

**Stop-loss triggered (red + bold):**
```
STOP-LOSS  market=0xabc...  entry=0.620  exit=0.470  pnl=-0.242
OVERALL    trades=24  stopped=8  pnl=-3.41  roi=-14.2%
```

**Market resolved:**
```
RESOLVED  market=0xabc...  outcome=NO  trades=12  stopped=4  wins=2  pnl=-6.80
OVERALL   trades=180  stopped=42  wins=89  pnl=+4.32  roi=+2.4%
```

**Model retrained:**
```
Model ready: logistic_regression_20260522_071505  rows=118240  metrics={'auc_roc': 0.8391, 'brier_score': 0.1641, 'accuracy': 0.7558}
```
