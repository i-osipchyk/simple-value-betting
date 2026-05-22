"""
Per-second inference loop.

Reads /data/latest_tick.json (written atomically by the collector every second),
runs the latest model, and logs in green whenever edge is found.
Does not write to predictions.db — that's reserved for the post-training snapshot
and manual /predict calls.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import predictor
import registry
from config import settings

logger = logging.getLogger(__name__)


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

        btc_usd: float | None = data.get("btc_usd")

        # Track BTC price at the start of each new candle for pct_change_open
        now = datetime.now(tz=timezone.utc)
        candle_ts = int(now.timestamp()) - (int(now.timestamp()) % interval_s)
        if candle_ts != last_candle_ts:
            last_candle_ts = candle_ts
            btc_candle_open = btc_usd

        if btc_usd is not None and btc_candle_open is not None and btc_candle_open != 0:
            pct_change_open = (btc_usd - btc_candle_open) / btc_candle_open
        else:
            pct_change_open = 0.0

        seconds_into_candle = int(now.timestamp()) % interval_s
        time_remaining = interval_s - seconds_into_candle

        await asyncio.to_thread(
            predictor.infer,
            data["market_id"],
            float(yes_price),
            float(no_price),
            float(btc_usd) if btc_usd is not None else 0.0,
            pct_change_open,
            time_remaining,
        )
