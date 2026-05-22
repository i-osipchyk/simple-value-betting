"""
Empirical probability lookup table.

Groups all tick rows into (time_remaining_bucket, pct_change_bucket) cells and
computes the observed YES resolution rate per cell. A cell must have at least
min_bucket_count samples before its probability estimate is trusted.

Bucketing:
  time_bucket  = (time_remaining // time_bucket_seconds) * time_bucket_seconds
  pct_index    = round(pct_change_open / pct_change_bucket_size)   [integer]

Using an integer pct_index avoids float comparison issues in the lookup dict.
"""

import logging
from collections import defaultdict
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

# {(time_bucket: int, pct_index: int): (n: int, prob_yes: float)}
Table = dict[tuple[int, int], tuple[int, float]]


def build_table(raw_dir: Path, time_bucket_s: int, pct_bucket_size: float) -> Table:
    files = sorted(raw_dir.glob("ticks_*.parquet"))
    if not files:
        return {}

    counts: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0])  # [n, sum_yes]
    total_rows = 0

    for f in files:
        try:
            for time_remaining, pct_change, resolved_yes in _rows_from_file(f):
                t_key = (time_remaining // time_bucket_s) * time_bucket_s
                p_index = round(pct_change / pct_bucket_size)
                counts[(t_key, p_index)][0] += 1
                counts[(t_key, p_index)][1] += resolved_yes
                total_rows += 1
        except Exception as exc:
            logger.warning("Skipping %s: %s", f.name, exc)

    table: Table = {k: (v[0], v[1] / v[0]) for k, v in counts.items()}
    logger.info(
        "Lookup table built: %d files  %d rows  %d cells",
        len(files),
        total_rows,
        len(table),
    )
    return table


def lookup(
    table: Table,
    time_remaining: int,
    pct_change_open: float,
    time_bucket_s: int,
    pct_bucket_size: float,
    min_count: int,
) -> tuple[float, int] | None:
    """Return (prob_yes, n) for the matching bucket, or None if below min_count."""
    result = lookup_raw(table, time_remaining, pct_change_open, time_bucket_s, pct_bucket_size)
    if result is None or result[0] < min_count:
        return None
    n, prob_yes = result
    return prob_yes, n


def lookup_raw(
    table: Table,
    time_remaining: int,
    pct_change_open: float,
    time_bucket_s: int,
    pct_bucket_size: float,
) -> tuple[int, float] | None:
    """Return (n, prob_yes) for the bucket regardless of min_count, or None if unseen."""
    t_key = (time_remaining // time_bucket_s) * time_bucket_s
    p_index = round(pct_change_open / pct_bucket_size)
    return table.get((t_key, p_index))


def _rows_from_file(file: Path) -> list[tuple[int, float, int]]:
    """Yield (time_remaining, pct_change_open, resolved_yes) for each tick row."""
    df = pd.read_parquet(file).sort_values("datetime").reset_index(drop=True)
    if df.empty:
        return []

    btc = df["btc_usd"].ffill()
    has_btc = btc.notna().any()

    if has_btc:
        btc_open = float(btc.dropna().iloc[0])
        btc_close = float(btc.dropna().iloc[-1])
        resolved_yes = 1 if btc_close > btc_open else 0
    else:
        yes_series = df["yes_price"].dropna()
        if len(yes_series) == 0:
            return []
        resolved_yes = 1 if float(yes_series.iloc[-1]) >= 0.5 else 0
        btc_open = None

    t_start = pd.Timestamp(df["datetime"].iloc[0])
    t_end = pd.Timestamp(df["datetime"].iloc[-1])
    candle_duration_s = max(1.0, (t_end - t_start).total_seconds())

    rows = []
    for row in df.itertuples():
        elapsed_s = (pd.Timestamp(row.datetime) - t_start).total_seconds()
        time_remaining = max(0, int(candle_duration_s - elapsed_s))

        if has_btc and btc_open is not None and btc_open != 0:
            btc_now = btc.iloc[row.Index]
            pct_change = (float(btc_now) - btc_open) / btc_open if pd.notna(btc_now) else 0.0
        else:
            pct_change = 0.0

        rows.append((time_remaining, pct_change, resolved_yes))

    return rows
