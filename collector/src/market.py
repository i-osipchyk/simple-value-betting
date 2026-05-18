"""
Polymarket market discovery.

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


def seconds_until_reconnect() -> int:
    """Seconds from now until the next candle boundary (+1s buffer)."""
    now = time.time()
    interval = settings.candle_interval_minutes * 60
    next_boundary = now - (now % interval) + interval
    return int(next_boundary - now) + 1


def _fetch_once(ts: int) -> MarketInfo:
    slug = f"{settings.pm_slug_prefix}-{ts}"
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


def fetch_market_info(retries: int = 5, retry_delay: float = 3.0) -> MarketInfo:
    """Fetch market info for the current candle, retrying on transient errors."""
    ts = curr_candle_ts()
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(1, retries + 1):
        try:
            info = _fetch_once(ts)
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
