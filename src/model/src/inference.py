"""
Per-second inference loop.

Reads /data/latest_tick.json (written atomically by the collector every second),
runs all configured models, and logs in green whenever edge is found.
Does not write to predictions.db — that's reserved for the post-training snapshot
and manual /predict calls.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import predictor
from config import settings

logger = logging.getLogger(__name__)


async def inference_loop() -> None:
    tick_path = Path(settings.local_data_dir) / "latest_tick.json"
    interval_s = settings.candle_interval_minutes * 60

    # Track candle-open prices per exchange for pct_change computation
    btc_open: float | None = None
    coinbase_open: float | None = None
    kraken_open: float | None = None
    last_candle_ts: int = 0

    while True:
        await asyncio.sleep(1.0)

        if not tick_path.exists():
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
        btc_coinbase: float | None = data.get("btc_coinbase")
        btc_kraken: float | None = data.get("btc_kraken")

        now = datetime.now(tz=timezone.utc)
        candle_ts = int(now.timestamp()) - (int(now.timestamp()) % interval_s)
        if candle_ts != last_candle_ts:
            last_candle_ts = candle_ts
            btc_open = btc_usd
            coinbase_open = btc_coinbase
            kraken_open = btc_kraken

        def _pct(current: float | None, open_price: float | None) -> float:
            if current is None or open_price is None or open_price == 0:
                return 0.0
            return (current - open_price) / open_price

        pct_change_binance = _pct(btc_usd, btc_open)
        pct_change_coinbase = _pct(btc_coinbase, coinbase_open)
        pct_change_kraken = _pct(btc_kraken, kraken_open)

        seconds_into_candle = int(now.timestamp()) % interval_s
        time_remaining = interval_s - seconds_into_candle

        try:
            await asyncio.to_thread(
                predictor.infer,
                data["market_id"],
                float(yes_price),
                float(no_price),
                float(btc_usd) if btc_usd is not None else 0.0,
                pct_change_binance,
                time_remaining,
                pct_change_coinbase,
                pct_change_kraken,
            )
        except Exception:
            logger.exception("Inference error — skipping tick")
