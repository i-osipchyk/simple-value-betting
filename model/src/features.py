"""
Feature extraction from raw tick parquet files.

Each parquet file is one completed 5-minute candle. Every row is used as a
training example — each second of the candle is a distinct snapshot with its
own yes_price, no_price, pct_change_open, and time_remaining. All rows within
the same candle share the same label (resolved_yes).
"""

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

FEATURE_NAMES = ["pct_change_open", "time_remaining", "yes_price", "no_price", "spread"]


def load_features(raw_dir: Path) -> pd.DataFrame:
    """Load all parquet files and return a training DataFrame."""
    files = sorted(raw_dir.glob("ticks_*.parquet"))
    if not files:
        return pd.DataFrame(columns=FEATURE_NAMES + ["resolved_yes", "market_id"])

    rows: list[dict] = []
    for f in files:
        try:
            rows.extend(_rows_from_file(f))
        except Exception as exc:
            logger.warning("Skipping %s: %s", f.name, exc)

    if not rows:
        return pd.DataFrame(columns=FEATURE_NAMES + ["resolved_yes", "market_id"])

    df = pd.DataFrame(rows)
    return df.dropna(subset=FEATURE_NAMES + ["resolved_yes"]).reset_index(drop=True)


def _rows_from_file(file: Path) -> list[dict]:
    df = pd.read_parquet(file).sort_values("datetime").reset_index(drop=True)
    if df.empty:
        return []

    yes_series = df["yes_price"].dropna()
    if len(yes_series) == 0:
        return []

    btc = df["btc_usd"].ffill()  # forward-fill so each row has the last known BTC price
    has_btc = btc.notna().any()

    if has_btc:
        btc_open = float(btc.dropna().iloc[0])
        btc_close = float(btc.dropna().iloc[-1])
        resolved_yes = 1 if btc_close > btc_open else 0
    else:
        resolved_yes = 1 if float(yes_series.iloc[-1]) >= 0.5 else 0
        btc_open = None

    market_id = str(df["market_id"].iloc[0])
    t_start = pd.Timestamp(df["datetime"].iloc[0])
    t_end = pd.Timestamp(df["datetime"].iloc[-1])
    candle_duration_s = max(1.0, (t_end - t_start).total_seconds())

    rows = []
    for row in df.itertuples():
        elapsed_s = (pd.Timestamp(row.datetime) - t_start).total_seconds()
        time_remaining = max(0, int(candle_duration_s - elapsed_s))

        if has_btc and btc_open is not None:
            btc_now = btc.iloc[row.Index]
            pct_change = (
                (float(btc_now) - btc_open) / btc_open
                if pd.notna(btc_now) and btc_open != 0
                else 0.0
            )
        else:
            pct_change = 0.0

        yes_f = float(row.yes_price) if pd.notna(row.yes_price) else None
        no_f = float(row.no_price) if pd.notna(row.no_price) else None
        if yes_f is None or no_f is None:
            continue

        rows.append(
            {
                "pct_change_open": pct_change,
                "time_remaining": time_remaining,
                "yes_price": yes_f,
                "no_price": no_f,
                "spread": yes_f + no_f - 1.0,
                "resolved_yes": resolved_yes,
                "market_id": market_id,
            }
        )

    return rows


def resolve_outcome(file: Path) -> tuple[str, bool] | None:
    """Return (market_id, resolved_yes) for a completed candle file."""
    df = pd.read_parquet(file).sort_values("datetime").reset_index(drop=True)
    if df.empty:
        return None
    market_id = str(df["market_id"].iloc[0])
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
        "pct_change_open": 0.0,  # new candle just opened
        "time_remaining": candle_interval_s,
    }
