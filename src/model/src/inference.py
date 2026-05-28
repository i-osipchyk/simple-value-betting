"""
Per-second inference loop.

Reads /data/latest_tick.json (written atomically by the collector every second).
Open prices are embedded in the JSON by the collector — no REST calls needed here.
Runs all configured models and logs in green whenever edge is found.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import predictor
from config import settings

logger = logging.getLogger(__name__)


async def inference_loop() -> None:
    tick_path = Path(settings.local_data_dir) / "latest_tick.json"
    interval_s = settings.candle_interval_minutes * 60

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

        open_btc_binance: float | None = data.get("open_btc_binance")
        open_btc_coinbase: float | None = data.get("open_btc_coinbase")
        open_btc_kraken: float | None = data.get("open_btc_kraken")

        def _pct(current: float | None, open_price: float | None) -> float:
            if current is None or open_price is None or open_price == 0:
                return 0.0
            return (current - open_price) / open_price

        pct_change_binance = _pct(btc_usd, open_btc_binance)
        pct_change_coinbase = _pct(btc_coinbase, open_btc_coinbase)
        pct_change_kraken = _pct(btc_kraken, open_btc_kraken)

        now_ts = time.time()
        seconds_into_candle = int(now_ts) % interval_s
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
