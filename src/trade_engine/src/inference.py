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

        btc_binance: float | None = data.get("btc_binance")
        btc_coinbase: float | None = data.get("btc_coinbase")
        btc_kraken: float | None = data.get("btc_kraken")

        open_btc_binance: float | None = data.get("open_btc_binance")
        open_btc_coinbase: float | None = data.get("open_btc_coinbase")
        open_btc_kraken: float | None = data.get("open_btc_kraken")

        def _pct(current: float | None, open_price: float | None) -> float:
            if current is None or open_price is None or open_price == 0:
                return 0.0
            return (current - open_price) / open_price

        def _flag(val: bool | None) -> float:
            if val is None:
                return 0.0
            return 1.0 if val else 0.0

        pct_change_binance = _pct(btc_binance, open_btc_binance)
        pct_change_coinbase = _pct(btc_coinbase, open_btc_coinbase)
        pct_change_kraken = _pct(btc_kraken, open_btc_kraken)

        above_ema9 = _flag(data.get("above_ema9"))
        above_ema20 = _flag(data.get("above_ema20"))
        above_ema34 = _flag(data.get("above_ema34"))
        above_all_emas = _flag(data.get("above_all_emas"))
        below_all_emas = _flag(data.get("below_all_emas"))

        def _flt(key: str, default: float = 0.0) -> float:
            v = data.get(key)
            return float(v) if v is not None else default

        ema9_value  = _flt("ema9_value")
        ema20_value = _flt("ema20_value")
        ema34_value = _flt("ema34_value")
        ema9_dist   = _flt("ema9_dist")
        ema20_dist  = _flt("ema20_dist")
        ema34_dist  = _flt("ema34_dist")
        prev_body_pct   = _flt("prev_body_pct")
        prev_wick_ratio = _flt("prev_wick_ratio")
        prev_rel_volume = _flt("prev_rel_volume", default=1.0)
        prev_green      = _flag(data.get("prev_green"))

        now_ts = time.time()
        seconds_into_candle = int(now_ts) % interval_s
        time_remaining = interval_s - seconds_into_candle

        try:
            await asyncio.to_thread(
                predictor.infer,
                data["market_id"],
                float(yes_price),
                float(no_price),
                float(btc_binance) if btc_binance is not None else 0.0,
                pct_change_binance,
                time_remaining,
                pct_change_coinbase,
                pct_change_kraken,
                above_ema9,
                above_ema20,
                above_ema34,
                above_all_emas,
                below_all_emas,
                ema9_value,
                ema20_value,
                ema34_value,
                ema9_dist,
                ema20_dist,
                ema34_dist,
                prev_body_pct,
                prev_wick_ratio,
                prev_rel_volume,
                prev_green,
            )
        except Exception:
            logger.exception("Inference error — skipping tick")
