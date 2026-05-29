"""
Feature extraction from raw tick parquet files.

Each parquet file is one completed 5-minute candle written by the collector.
Open prices and market resolution are embedded in the parquet; no REST calls needed.

Old parquet files (without open_btc_* or resolved_yes_* columns) are handled with
fallback logic so retraining on historical data continues to work.
"""

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

# Columns required to keep a training row
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


def _read_resolution(df: pd.DataFrame) -> int | None:
    """Read resolved_yes from parquet columns; prefer gamma, fall back to binance, then ticks."""
    # New-format parquet: resolution columns embedded by collector.
    for col in ("resolved_yes_gamma", "resolved_yes_binance"):
        if col in df.columns:
            vals = df[col].dropna()
            if not vals.empty:
                return int(bool(vals.iloc[0]))

    # Old-format fallback: derive from btc_usd tick close > open.
    btc = df["btc_usd"].ffill().dropna() if "btc_usd" in df.columns else pd.Series()
    if len(btc) >= 2:
        return int(bool(float(btc.iloc[-1]) > float(btc.iloc[0])))

    yes = df["yes_price"].dropna() if "yes_price" in df.columns else pd.Series()
    if not yes.empty:
        return int(float(yes.iloc[-1]) >= 0.5)

    return None


def _read_open_prices(df: pd.DataFrame) -> tuple[float | None, float | None, float | None]:
    """Read candle open prices from parquet columns; fall back to first tick for old files."""
    def _col_val(col: str) -> float | None:
        if col in df.columns:
            vals = df[col].dropna()
            if not vals.empty:
                return float(vals.iloc[0])
        return None

    binance = _col_val("open_btc_binance")
    coinbase = _col_val("open_btc_coinbase")
    kraken = _col_val("open_btc_kraken")

    # Fallback: use first btc_binance (or old btc_usd) tick as open when open_btc_binance is null.
    if binance is None:
        for col in ("btc_binance", "btc_usd"):
            if col in df.columns:
                first = df[col].dropna()
                if not first.empty:
                    binance = float(first.iloc[0])
                    break

    return binance, coinbase, kraken


def _rows_from_file(file: Path, candle_interval_s: int = 300) -> list[dict]:
    df = pd.read_parquet(file).sort_values("datetime").reset_index(drop=True)
    if df.empty:
        return []

    yes_series = df["yes_price"].dropna()
    if yes_series.empty:
        return []

    resolved_yes = _read_resolution(df)
    if resolved_yes is None:
        return []

    open_binance, open_coinbase, open_kraken = _read_open_prices(df)

    # Support old files that used btc_usd before the rename.
    btc_binance_col = "btc_binance" if "btc_binance" in df.columns else "btc_usd"
    pct_binance = _pct_change_series(df[btc_binance_col], open_price=open_binance)

    pct_coinbase = (
        _pct_change_series(df["btc_coinbase"], open_price=open_coinbase)
        if "btc_coinbase" in df.columns and df["btc_coinbase"].notna().any()
        else pd.Series([float("nan")] * len(df), index=df.index)
    )
    pct_kraken = (
        _pct_change_series(df["btc_kraken"], open_price=open_kraken)
        if "btc_kraken" in df.columns and df["btc_kraken"].notna().any()
        else pd.Series([float("nan")] * len(df), index=df.index)
    )

    market_id = str(df["market_id"].iloc[0])
    t_start = pd.Timestamp(df["datetime"].iloc[0])

    def _bool_col(col: str) -> float:
        if col in df.columns:
            vals = df[col].dropna()
            if not vals.empty:
                return 1.0 if bool(vals.iloc[0]) else 0.0
        return float("nan")

    def _float_col(col: str) -> float:
        if col in df.columns:
            vals = df[col].dropna()
            if not vals.empty:
                return float(vals.iloc[0])
        return float("nan")

    above_ema9 = _bool_col("above_ema9")
    above_ema20 = _bool_col("above_ema20")
    above_ema34 = _bool_col("above_ema34")
    above_all_emas = _bool_col("above_all_emas")
    below_all_emas = _bool_col("below_all_emas")

    ema9_value = _float_col("ema9_value")
    ema20_value = _float_col("ema20_value")
    ema34_value = _float_col("ema34_value")

    # % distance of open price from each EMA — scale-invariant signal for the model.
    def _ema_dist(ema_val: float) -> float:
        if open_binance and open_binance != 0 and not pd.isna(ema_val):
            return (open_binance - ema_val) / open_binance
        return float("nan")

    ema9_dist  = _ema_dist(ema9_value)
    ema20_dist = _ema_dist(ema20_value)
    ema34_dist = _ema_dist(ema34_value)

    prev_body_pct   = _float_col("prev_body_pct")
    prev_wick_ratio = _float_col("prev_wick_ratio")
    prev_rel_volume = _float_col("prev_rel_volume")
    prev_green      = _bool_col("prev_green")

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
            "above_ema9": above_ema9,
            "above_ema20": above_ema20,
            "above_ema34": above_ema34,
            "above_all_emas": above_all_emas,
            "below_all_emas": below_all_emas,
            "ema9_value": ema9_value,
            "ema20_value": ema20_value,
            "ema34_value": ema34_value,
            "ema9_dist": ema9_dist,
            "ema20_dist": ema20_dist,
            "ema34_dist": ema34_dist,
            "prev_body_pct": prev_body_pct,
            "prev_wick_ratio": prev_wick_ratio,
            "prev_rel_volume": prev_rel_volume,
            "prev_green": prev_green,
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
    resolved = _read_resolution(df)
    if resolved is None:
        return None
    return market_id, bool(resolved)


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

    btc_col = "btc_binance" if "btc_binance" in df.columns else "btc_usd"
    btc = df[btc_col].dropna()
    btc_val = float(btc.iloc[-1]) if len(btc) > 0 else 0.0

    return {
        "market_id": str(last_row["market_id"]),
        "yes_price": yes_f,
        "no_price": no_f,
        "btc_binance": btc_val,
        "pct_change_binance": 0.0,
        "pct_change_coinbase": 0.0,
        "pct_change_kraken": 0.0,
        "time_remaining": candle_interval_s,
        "ema9_value": 0.0,
        "ema20_value": 0.0,
        "ema34_value": 0.0,
        "ema9_dist": 0.0,
        "ema20_dist": 0.0,
        "ema34_dist": 0.0,
        "prev_body_pct": 0.0,
        "prev_wick_ratio": 0.0,
        "prev_rel_volume": 1.0,
        "prev_green": 0.0,
    }
