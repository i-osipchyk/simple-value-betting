"""
Per-second inference loop with stop-loss for the empirical lookup container.

Same as analysis/inference.py but before the lookup trade decision it checks
every open position for the current market: if yes_price has fallen to
entry_price - 0.15 the position is closed at the current bid price.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import trades
import watcher as watcher_module
from config import settings
from lookup import lookup

logger = logging.getLogger(__name__)

STOP_LOSS_DELTA = 0.15

_GREEN = "\033[32m"
_BOLD = "\033[1m"
_RED = "\033[31m"
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

        btc_usd: float | None = data.get("btc_usd")
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

        # Check stop-loss for every open position in this market
        open_positions = await asyncio.to_thread(trades.get_open_positions, market_id)
        for pos in open_positions:
            stop_level = pos["yes_price"] - STOP_LOSS_DELTA
            if float(yes_price) <= stop_level:
                exit_price = float(yes_price)
                pnl = pos["stake"] * (exit_price / pos["yes_price"]) - pos["stake"]
                await asyncio.to_thread(trades.stop_loss_exit, pos["trade_id"], exit_price)
                logger.info(
                    "%s%sSTOP-LOSS  market=%-20s  entry=%.3f  exit=%.3f  pnl=%+.3f%s",
                    _RED, _BOLD, market_id[:20], pos["yes_price"], exit_price, pnl, _RESET,
                )

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
