"""
Polymarket market discovery + exchange REST helpers.

Fetches the YES/NO token IDs for the current candle market from the Gamma API.
Token IDs rotate at each candle boundary, so this module is called at startup
and at each reconnect.

Slug format: {PM_SLUG_PREFIX}-{unix_timestamp_of_candle_start}
e.g.        btc-updown-5m-1716000000
"""

import json
import logging
import time
from dataclasses import dataclass, field

import requests

from config import settings

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"

_BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
_COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
_KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"


@dataclass
class MarketInfo:
    market_id: str
    yes_token_id: str
    no_token_id: str
    # token_id -> "yes" | "no"
    asset_id_map: dict[str, str] = field(default_factory=dict)
    candle_ts: int = 0


def curr_candle_ts() -> int:
    """Unix timestamp of the start of the current candle."""
    now = int(time.time())
    interval = settings.candle_interval_minutes * 60
    return now - (now % interval)


def seconds_until_next_candle() -> float:
    """Seconds from now until the next candle boundary."""
    now = time.time()
    interval = settings.candle_interval_minutes * 60
    return interval - (now % interval)


def _fetch_once(ts: int, slug_prefix: str) -> MarketInfo:
    slug = f"{slug_prefix}-{ts}"
    url = f"{GAMMA_API}/markets/slug/{slug}"
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    outcomes: list[str] = json.loads(data["outcomes"])
    token_ids: list[str] = json.loads(data["clobTokenIds"])
    market_id: str = data.get("conditionId", slug)

    asset_id_map: dict[str, str] = {}
    yes_token_id = ""
    no_token_id = ""

    for token_id, outcome in zip(token_ids, outcomes):
        label = outcome.lower()
        # BTC short-interval markets use "up"/"down"; generic binary markets use "yes"/"no"
        if label in ("yes", "up"):
            yes_token_id = token_id
            asset_id_map[token_id] = "yes"
        elif label in ("no", "down"):
            no_token_id = token_id
            asset_id_map[token_id] = "no"
        else:
            asset_id_map[token_id] = label

    if not yes_token_id or not no_token_id:
        raise ValueError(f"Could not identify yes/no tokens in outcomes={outcomes}")

    return MarketInfo(
        market_id=market_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        asset_id_map=asset_id_map,
        candle_ts=ts,
    )


def fetch_market_info(
    slug_prefix: str,
    candle_ts: int | None = None,
    retries: int = 5,
    retry_delay: float = 3.0,
) -> MarketInfo:
    """Fetch market info for the given candle_ts (defaults to current candle)."""
    if candle_ts is None:
        candle_ts = curr_candle_ts()
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(1, retries + 1):
        try:
            info = _fetch_once(candle_ts, slug_prefix)
            logger.info(
                "Market fetched: id=%s  yes=%s...  no=%s...",
                info.market_id,
                info.yes_token_id[:8],
                info.no_token_id[:8],
            )
            return info
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "Attempt %d/%d failed to fetch market info: %s",
                attempt,
                retries,
                exc,
            )
            if attempt < retries:
                time.sleep(retry_delay)
    raise RuntimeError(f"Failed to fetch market info after {retries} attempts") from last_exc


def fetch_open_prices(candle_ts: int) -> tuple[float | None, float | None, float | None]:
    """Fetch the 5-minute candle open price from Binance, Coinbase, and Kraken REST APIs."""
    binance_open: float | None = None
    coinbase_open: float | None = None
    kraken_open: float | None = None

    try:
        url = f"{_BINANCE_KLINES_URL}?symbol=BTCUSDT&interval=5m&startTime={candle_ts * 1000}&limit=1"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data:
            binance_open = float(data[0][1])
    except Exception as exc:
        logger.warning("Binance candle open fetch failed for ts=%d: %s", candle_ts, exc)

    try:
        url = f"{_COINBASE_CANDLES_URL}?granularity=300"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        resp.raise_for_status()
        for c in resp.json():
            if int(c[0]) == candle_ts:
                coinbase_open = float(c[3])  # [time, low, high, open, close, volume]
                break
    except Exception as exc:
        logger.warning("Coinbase candle open fetch failed for ts=%d: %s", candle_ts, exc)

    try:
        url = f"{_KRAKEN_OHLC_URL}?pair=XBTUSD&interval=5&since={candle_ts - 1}"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        for c in resp.json().get("result", {}).get("XXBTZUSD", []):
            if int(c[0]) == candle_ts:
                kraken_open = float(c[1])  # [time, open, high, low, close, vwap, volume, count]
                break
    except Exception as exc:
        logger.warning("Kraken candle open fetch failed for ts=%d: %s", candle_ts, exc)

    logger.info(
        "Open prices fetched — binance=%s  coinbase=%s  kraken=%s",
        f"{binance_open:.2f}" if binance_open is not None else "N/A",
        f"{coinbase_open:.2f}" if coinbase_open is not None else "N/A",
        f"{kraken_open:.2f}" if kraken_open is not None else "N/A",
    )
    return binance_open, coinbase_open, kraken_open


def fetch_binance_resolution(candle_ts: int) -> bool | None:
    """Fetch resolved outcome from Binance klines: True=Up (close>open), False=Down, None=error."""
    try:
        url = f"{_BINANCE_KLINES_URL}?symbol=BTCUSDT&interval=5m&startTime={candle_ts * 1000}&limit=1"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data and len(data[0]) >= 5:
            open_price = float(data[0][1])
            close_price = float(data[0][4])
            return close_price > open_price
    except Exception as exc:
        logger.warning("Binance resolution fetch failed for ts=%d: %s", candle_ts, exc)
    return None


def fetch_gamma_resolution(candle_ts: int, slug_prefix: str) -> bool | None:
    """Fetch market resolution from Gamma API. Returns True=YES, False=NO, None=unresolved/error."""
    slug = f"{slug_prefix}-{candle_ts}"
    url = f"{GAMMA_API}/markets/slug/{slug}"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        outcomes = json.loads(data.get("outcomes", "[]"))
        prices = [float(p) for p in json.loads(data.get("outcomePrices", "[]"))]
        if outcomes and prices:
            yes_idx = next(
                (i for i, o in enumerate(outcomes) if o.lower() in ("yes", "up")), None
            )
            if yes_idx is not None:
                yes_price = prices[yes_idx]
                if yes_price >= 0.99:
                    return True
                elif yes_price <= 0.01:
                    return False
    except Exception as exc:
        logger.warning("Gamma resolution fetch failed for %s: %s", slug, exc)
    return None
