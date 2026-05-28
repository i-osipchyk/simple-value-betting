"""
Per-second inference loop with stop-loss.

Same as model/inference.py but before running the model it checks every open
position for the current market: if yes_price has fallen to entry_price - 0.15
the position is closed at the current bid price.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import predictor
import registry
import trades
from config import settings

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

        model, metadata = registry.load_model("logistic_regression")
        if model is None:
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

        await asyncio.to_thread(
            predictor.infer,
            market_id,
            float(yes_price),
            float(no_price),
            float(btc_usd) if btc_usd is not None else 0.0,
            pct_change_binance,
            time_remaining,
        )
