"""
Feature extraction from raw tick parquet files.

Each parquet file is one completed 5-minute candle. Every row is used as a
training example — each second of the candle is a distinct snapshot with its
own yes_price, no_price, pct_change_binance, and time_remaining. All rows within
the same candle share the same label (resolved_yes).

resolved_yes is sourced from the Polymarket Gamma API when available (authoritative),
falling back to BTC tick close > open when the market is not yet resolved.
"""

import json
import logging
import urllib.request
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

_GAMMA_URL = "https://gamma-api.polymarket.com/markets/slug"
_SLUG_PREFIX = "btc-updown-5m"
_resolution_cache: dict[int, bool | None] = {}

_BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
_COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
_KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
# candle_ts → (binance_open, coinbase_open, kraken_open)
_candle_open_cache: dict[int, tuple[float | None, float | None, float | None]] = {}


def _fetch_candle_opens(candle_ts: int) -> tuple[float | None, float | None, float | None]:
    """Fetch the 5m candle open price from all three exchanges. Results are cached."""
    if candle_ts in _candle_open_cache:
        return _candle_open_cache[candle_ts]

    binance_open: float | None = None
    coinbase_open: float | None = None
    kraken_open: float | None = None

    try:
        url = f"{_BINANCE_KLINES_URL}?symbol=BTCUSDT&interval=5m&startTime={candle_ts * 1000}&limit=1"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        if data:
            binance_open = float(data[0][1])
    except Exception as exc:
        logger.debug("Binance candle open fetch failed for ts=%d: %s", candle_ts, exc)

    try:
        url = f"{_COINBASE_CANDLES_URL}?granularity=300"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            candles = json.loads(resp.read())
        for c in candles:
            if int(c[0]) == candle_ts:
                coinbase_open = float(c[3])  # [time, low, high, open, close, volume]
                break
    except Exception as exc:
        logger.debug("Coinbase candle open fetch failed for ts=%d: %s", candle_ts, exc)

    try:
        url = f"{_KRAKEN_OHLC_URL}?pair=XBTUSD&interval=5&since={candle_ts - 1}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        for c in data.get("result", {}).get("XXBTZUSD", []):
            if int(c[0]) == candle_ts:
                kraken_open = float(c[1])  # [time, open, high, low, close, ...]
                break
    except Exception as exc:
        logger.debug("Kraken candle open fetch failed for ts=%d: %s", candle_ts, exc)

    result = (binance_open, coinbase_open, kraken_open)
    _candle_open_cache[candle_ts] = result
    return result


def _fetch_gamma_resolution(candle_open: pd.Timestamp) -> bool | None:
    """Return True=YES, False=NO, None=unresolved/error. Results are cached."""
    candle_ts = int(candle_open.timestamp())
    if candle_ts in _resolution_cache:
        return _resolution_cache[candle_ts]

    slug = f"{_SLUG_PREFIX}-{candle_ts}"
    url = f"{_GAMMA_URL}/{slug}"
    result: bool | None = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        outcomes = json.loads(data.get("outcomes", "[]"))
        prices = [float(p) for p in json.loads(data.get("outcomePrices", "[]"))]
        if outcomes and prices:
            yes_idx = next(
                (i for i, o in enumerate(outcomes) if o.lower() in ("yes", "up")),
                None,
            )
            if yes_idx is not None:
                yes_price = prices[yes_idx]
                if yes_price >= 0.99:
                    result = True
                elif yes_price <= 0.01:
                    result = False
    except Exception:
        pass

    _resolution_cache[candle_ts] = result
    return result

# Columns required to keep a training row; computed features that are always present
_REQUIRED_FEATURES = ["pct_change_binance", "time_remaining", "yes_price", "no_price", "resolved_yes"]


def load_features(raw_dir: Path, candle_interval_s: int = 300) -> pd.DataFrame:
    """Load all parquet files and return a training DataFrame."""
    files = sorted(raw_dir.glob("ticks_*.parquet"))
    if not files:
        return pd.DataFrame()

    rows: list[dict] = []
    for f in files:
        try:
            rows.extend(_rows_from_file(f, candle_interval_s))
        except Exception as exc:
            logger.warning("Skipping %s: %s", f.name, exc)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    return df.dropna(subset=_REQUIRED_FEATURES).reset_index(drop=True)


def _pct_change_series(price_series: pd.Series, open_price: float | None = None) -> pd.Series:
    """Compute per-row pct change from open_price (or first non-null tick if not given)."""
    filled = price_series.ffill()
    if open_price is None:
        first_valid = filled.dropna()
        if first_valid.empty or float(first_valid.iloc[0]) == 0:
            return pd.Series([float("nan")] * len(price_series), index=price_series.index)
        open_price = float(first_valid.iloc[0])
    if open_price == 0:
        return pd.Series([float("nan")] * len(price_series), index=price_series.index)
    return filled.apply(
        lambda v: (float(v) - open_price) / open_price if pd.notna(v) else float("nan")
    )


def _rows_from_file(file: Path, candle_interval_s: int = 300) -> list[dict]:
    df = pd.read_parquet(file).sort_values("datetime").reset_index(drop=True)
    if df.empty:
        return []

    yes_series = df["yes_price"].dropna()
    if len(yes_series) == 0:
        return []

    btc = df["btc_usd"].ffill()
    has_btc = btc.notna().any()

    if has_btc:
        btc_open = float(btc.dropna().iloc[0])
        btc_close = float(btc.dropna().iloc[-1])
        btc_resolved = btc_close > btc_open
    else:
        btc_resolved = float(yes_series.iloc[-1]) >= 0.5
        btc_open = None

    # Resolve candle boundary timestamp for REST lookups.
    first_ts_utc = pd.Timestamp(df["datetime"].iloc[0]).tz_localize("UTC") \
        if df["datetime"].iloc[0].tzinfo is None \
        else pd.Timestamp(df["datetime"].iloc[0]).tz_convert("UTC")
    candle_open = first_ts_utc.floor("5min")
    candle_ts = int(candle_open.timestamp())

    # Prefer Gamma API resolution (authoritative) over BTC tick derivation.
    gamma = _fetch_gamma_resolution(candle_open)
    resolved_yes = int(gamma if gamma is not None else btc_resolved)

    # Fetch authoritative candle open prices from exchange REST APIs.
    rest_binance_open, rest_coinbase_open, rest_kraken_open = _fetch_candle_opens(candle_ts)

    pct_binance = _pct_change_series(df["btc_usd"], open_price=rest_binance_open)

    # Coinbase / Kraken: present in new parquets, missing or all-null in old ones.
    # Use NaN (not 0.0) when unavailable so trainer.dropna correctly excludes these rows.
    pct_coinbase = (
        _pct_change_series(df["btc_coinbase"], open_price=rest_coinbase_open)
        if "btc_coinbase" in df.columns and df["btc_coinbase"].notna().any()
        else pd.Series([float("nan")] * len(df), index=df.index)
    )
    pct_kraken = (
        _pct_change_series(df["btc_kraken"], open_price=rest_kraken_open)
        if "btc_kraken" in df.columns and df["btc_kraken"].notna().any()
        else pd.Series([float("nan")] * len(df), index=df.index)
    )

    market_id = str(df["market_id"].iloc[0])
    t_start = pd.Timestamp(df["datetime"].iloc[0])

    rows = []
    for row in df.itertuples():
        elapsed_s = (pd.Timestamp(row.datetime) - t_start).total_seconds()
        time_remaining = max(0, int(candle_interval_s - elapsed_s))

        yes_f = float(row.yes_price) if pd.notna(row.yes_price) else None
        no_f = float(row.no_price) if pd.notna(row.no_price) else None
        if yes_f is None or no_f is None:
            continue

        rows.append({
            "pct_change_binance": float(pct_binance.iloc[row.Index]),
            "pct_change_coinbase": float(pct_coinbase.iloc[row.Index]),
            "pct_change_kraken": float(pct_kraken.iloc[row.Index]),
            "time_remaining": time_remaining,
            "yes_price": yes_f,
            "no_price": no_f,
            "spread": yes_f + no_f - 1.0,
            "resolved_yes": resolved_yes,
            "market_id": market_id,
        })

    return rows


def resolve_outcome(file: Path) -> tuple[str, bool] | None:
    """Return (market_id, resolved_yes) for a completed candle file."""
    df = pd.read_parquet(file).sort_values("datetime").reset_index(drop=True)
    if df.empty:
        return None
    market_id = str(df["market_id"].iloc[0])

    # Try Gamma API first (authoritative resolution source).
    first_ts = pd.Timestamp(df["datetime"].iloc[0])
    if first_ts.tzinfo is None:
        first_ts = first_ts.tz_localize("UTC")
    else:
        first_ts = first_ts.tz_convert("UTC")
    candle_open = first_ts.floor("5min")
    gamma = _fetch_gamma_resolution(candle_open)
    if gamma is not None:
        return market_id, gamma

    # Fall back to BTC tick derivation.
    btc = df["btc_usd"].dropna()
    if len(btc) >= 2:
        return market_id, bool(btc.iloc[-1] > btc.iloc[0])
    yes = df["yes_price"].dropna()
    if len(yes) == 0:
        return None
    return market_id, bool(float(yes.iloc[-1]) >= 0.5)


def extract_current_snapshot(file: Path, candle_interval_s: int = 300) -> dict | None:
    """
    Extract a start-of-new-candle snapshot from the last row of a completed candle file.
    Used to make a prediction for the candle that just opened.
    """
    df = pd.read_parquet(file)
    if df.empty:
        return None

    df = df.sort_values("datetime").reset_index(drop=True)
    last_row = df.iloc[-1]

    yes = last_row["yes_price"]
    no = last_row["no_price"]
    yes_f = float(yes) if pd.notna(yes) else 0.5
    no_f = float(no) if pd.notna(no) else 0.5

    btc = df["btc_usd"].dropna()
    btc_val = float(btc.iloc[-1]) if len(btc) > 0 else 0.0

    return {
        "market_id": str(last_row["market_id"]),
        "yes_price": yes_f,
        "no_price": no_f,
        "btc_usd": btc_val,
        "pct_change_binance": 0.0,
        "pct_change_coinbase": 0.0,
        "pct_change_kraken": 0.0,
        "time_remaining": candle_interval_s,
    }
