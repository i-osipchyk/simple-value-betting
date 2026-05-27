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
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import predictor
from config import settings

logger = logging.getLogger(__name__)

_BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
_COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
_KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"


def _fetch_binance_open(candle_ts: int) -> float | None:
    url = f"{_BINANCE_KLINES_URL}?symbol=BTCUSDT&interval=5m&startTime={candle_ts * 1000}&limit=1"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        if data:
            return float(data[0][1])
    except Exception as exc:
        logger.warning("Binance candle open fetch failed: %s", exc)
    return None


def _fetch_coinbase_open(candle_ts: int) -> float | None:
    url = f"{_COINBASE_CANDLES_URL}?granularity=300"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            candles = json.loads(resp.read())
        for c in candles:
            if int(c[0]) == candle_ts:
                return float(c[3])  # [time, low, high, open, close, volume]
    except Exception as exc:
        logger.warning("Coinbase candle open fetch failed: %s", exc)
    return None


def _fetch_kraken_open(candle_ts: int) -> float | None:
    url = f"{_KRAKEN_OHLC_URL}?pair=XBTUSD&interval=5&since={candle_ts - 1}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        candles = data.get("result", {}).get("XXBTZUSD", [])
        for c in candles:
            if int(c[0]) == candle_ts:
                return float(c[1])  # [time, open, high, low, close, vwap, volume, count]
    except Exception as exc:
        logger.warning("Kraken candle open fetch failed: %s", exc)
    return None


async def inference_loop() -> None:
    tick_path = Path(settings.local_data_dir) / "latest_tick.json"
    interval_s = settings.candle_interval_minutes * 60

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
            # Fetch authoritative candle open prices from exchange REST APIs in parallel.
            # Fall back to the current tick price if a request fails.
            binance_open, cb_open, kr_open = await asyncio.gather(
                asyncio.to_thread(_fetch_binance_open, candle_ts),
                asyncio.to_thread(_fetch_coinbase_open, candle_ts),
                asyncio.to_thread(_fetch_kraken_open, candle_ts),
            )
            btc_open = binance_open if binance_open is not None else btc_usd
            coinbase_open = cb_open if cb_open is not None else btc_coinbase
            kraken_open = kr_open if kr_open is not None else btc_kraken
            logger.info(
                "Candle open prices — binance=%.2f  coinbase=%.2f  kraken=%.2f",
                btc_open or 0, coinbase_open or 0, kraken_open or 0,
            )

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
