# System Diagrams

## Data flow

```
                        ┌─────────────────────────────────┐
                        │         Polymarket               │
                        │  yes/no prices  ·  BTC/USD       │
                        └──────────────┬──────────────────┘
                                       │ WebSocket (every second)
                                       ▼
                        ┌─────────────────────────────────┐
                        │           Collector              │
                        │                                  │
                        │  writes every tick to DuckDB     │
                        │  exports parquet at each         │
                        │  5-minute candle boundary        │
                        └──────────┬──────────────────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    │ every second                │ every 5 min
                    ▼                             ▼
        ┌──────────────────────┐    ┌──────────────────────────┐
        │ /data/latest_tick    │    │ /data/raw/*.parquet       │
        │ .json                │    │ (one file per candle)     │
        └──────────┬───────────┘    └────────────┬─────────────┘
                   │                             │
                   │ inference loop              │ watcher
                   │ (every second)              │ (on new file)
                   ▼                             ▼
        ┌──────────────────────────────────────────────────────┐
        │                      Model                           │
        │                                                      │
        │  inference loop:                                     │
        │    run model on current tick                         │
        │    if edge → open $1 trade in trades.parquet         │
        │    log green EDGE if edge found                      │
        │                                                      │
        │  watcher (on each new parquet):                      │
        │    resolve open trades for completed market          │
        │    retrain on all historical parquet files           │
        │    predict for the candle that just opened           │
        │                                                      │
        │  also reachable via FastAPI on port 8000             │
        └──────────────────────┬───────────────────────────────┘
                               │
               ┌───────────────┴───────────────┐
               ▼                               ▼
    /data/trades/trades.parquet       /data/predictions.db
    (one row per $1 trade,            (post-candle predictions,
     P&L filled on resolution)         written after each retrain)
```

---

## What the logs look like

```
no edge:
  pred  market=0xabc...   pred=0.487  mkt=0.500  edge=-0.0130

edge found — printed in green + bold:
  EDGE  market=0xabc...   pred=0.720  mkt=0.500  edge=+0.2200  model=logistic_regression_20260519_120000

market resolved — trade P&L:
  RESOLVED  market=0xabc...  outcome=YES  trades=3  wins=3  pnl=+1.23
  OVERALL   trades=41  wins=26  pnl=+4.87  roi=+11.9%
```
