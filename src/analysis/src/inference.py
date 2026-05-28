"""
Per-second inference loop.

Reads /data/latest_tick.json every second and logs one line showing:
  - how many cells in the lookup table have enough data (>= min_bucket_count)
  - the empirical YES / NO probability for the current bucket (if found)
  - edge vs market price
Prints in green when an edge is detected and opens a $1 trade.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import trades
import watcher as watcher_module
from config import settings
from lookup import lookup, lookup_raw

logger = logging.getLogger(__name__)

_GREEN = "\033[32m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


async def inference_loop() -> None:
    tick_path = Path(settings.local_data_dir) / "latest_tick.json"
    interval_s = settings.candle_interval_minutes * 60

    btc_candle_open: float | None = None
    last_candle_ts: int = 0

    while True:
        await asyncio.sleep(1.0)

        if not tick_path.exists():
            continue

        table = watcher_module.get_table()
        if not table:
            continue

        try:
            data = json.loads(tick_path.read_text())
        except Exception:
            continue

        yes_price = data.get("yes_price")
        no_price = data.get("no_price")
        if yes_price is None or no_price is None:
            continue

        btc_usd: float | None = data.get("btc_binance") or data.get("btc_usd")
        market_id: str = data["market_id"]

        now = datetime.now(tz=timezone.utc)
        candle_ts = int(now.timestamp()) - (int(now.timestamp()) % interval_s)
        if candle_ts != last_candle_ts:
            last_candle_ts = candle_ts
            btc_candle_open = btc_usd

        if btc_usd is not None and btc_candle_open is not None and btc_candle_open != 0:
            pct_change_binance = (btc_usd - btc_candle_open) / btc_candle_open
        else:
            pct_change_binance = 0.0

        seconds_into_candle = int(now.timestamp()) % interval_s
        time_remaining = interval_s - seconds_into_candle

        total_cells = len(table)
        qualified_cells = sum(1 for n, _ in table.values() if n >= settings.min_bucket_count)

        result = lookup(
            table,
            time_remaining,
            pct_change_binance,
            settings.time_bucket_seconds,
            settings.pct_change_bucket_size,
            settings.min_bucket_count,
        )

        market_prob = float(yes_price)

        if result is None:
            raw = lookup_raw(
                table, time_remaining, pct_change_binance,
                settings.time_bucket_seconds, settings.pct_change_bucket_size,
            )
            n_raw = raw[0] if raw is not None else 0
            logger.info(
                "market=%-20s  t=%3ds  pct=%+.5f  cells=%d/%d  no_match (n=%d, need %d)",
                market_id[:20],
                time_remaining,
                pct_change_binance,
                qualified_cells,
                total_cells,
                n_raw,
                settings.min_bucket_count,
            )
            continue

        empirical_prob, n = result
        yes_edge = empirical_prob - market_prob
        model_id = f"empirical_n{n}"
        if (
            float(yes_price) <= 0.04 or float(yes_price) >= 0.97
            or time_remaining > interval_s - 15
            or float(yes_price) < 0.40
            or time_remaining < 100
            or yes_edge > 0.15
            or pct_change_binance == 0.0
            or yes_edge < settings.min_edge_threshold
        ):
            logger.info(
                "SKIP  market=%-20s  t=%3ds  yes=%.3f  yes_edge=%+.4f",
                market_id[:20], time_remaining, float(yes_price), yes_edge,
            )
            continue

        msg = (
            f"EDGE YES  market={market_id[:20]:<20}  "
            f"t={time_remaining:3d}s  pct={pct_change_binance:+.5f}  "
            f"cells={qualified_cells}/{total_cells}  "
            f"YES={empirical_prob:.3f}  n={n}  mkt={market_prob:.3f}  edge={yes_edge:+.4f}"
        )
        logger.info("%s%s%s%s", _GREEN, _BOLD, msg, _RESET)
        await asyncio.to_thread(
            trades.open_trade,
            market_id=market_id,
            yes_price=float(yes_price),
            no_price=float(no_price),
            btc_usd=float(btc_usd) if btc_usd is not None else 0.0,
            pct_change_binance=pct_change_binance,
            time_remaining=time_remaining,
            side="YES",
            predicted_prob=empirical_prob,
            edge=yes_edge,
            model_id=model_id,
        )
