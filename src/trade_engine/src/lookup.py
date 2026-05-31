"""
Empirical probability lookup table.

Groups all tick rows into (time_remaining_bucket, pct_change_bucket) cells and
computes the observed YES resolution rate per cell. A cell must have at least
min_bucket_count samples before its probability estimate is trusted.

Bucketing:
  time_bucket  = (time_remaining // time_bucket_seconds) * time_bucket_seconds
  pct_index    = round(pct_change_binance / pct_change_bucket_size)   [integer]

Using an integer pct_index avoids float comparison issues in the lookup dict.
"""

import logging
from collections import defaultdict
from pathlib import Path

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)

# {(time_bucket: int, pct_index: int): (n: int, prob_yes: float)}
Table = dict[tuple[int, int], tuple[int, float]]

# Module-level table rebuilt after each new candle.
_table: Table = {}


def get_table() -> Table:
    return _table


def set_table(t: Table) -> None:
    global _table
    _table = t


def build_from_db(
    conn: duckdb.DuckDBPyConnection,
    time_bucket_s: int,
    pct_bucket_size: float,
    candle_interval_s: int = 300,
) -> Table:
    """Build lookup table from the history DB raw_ticks table."""
    try:
        n = conn.execute("SELECT COUNT(*) FROM raw_ticks").fetchone()[0]
    except Exception:
        return {}
    if n == 0:
        return {}

    df = conn.execute(
        "SELECT datetime, market_id, btc_binance, open_btc_binance, "
        "resolved_yes_gamma, resolved_yes_binance FROM raw_ticks ORDER BY market_id, datetime"
    ).df()

    return _build_from_df(df, time_bucket_s, pct_bucket_size, candle_interval_s)


def build_table(raw_dir: Path, time_bucket_s: int, pct_bucket_size: float, candle_interval_s: int = 300) -> Table:
    files = sorted(raw_dir.rglob("minute_*.parquet"))
    if not files:
        return {}

    rows: list[tuple[int, float, int]] = []
    for f in files:
        try:
            rows.extend(_rows_from_file(f, candle_interval_s))
        except Exception as exc:
            logger.warning("Skipping %s: %s", f.name, exc)

    return _bucket(rows, time_bucket_s, pct_bucket_size, source=f"{len(files)} files")


def _build_from_df(
    df: pd.DataFrame,
    time_bucket_s: int,
    pct_bucket_size: float,
    candle_interval_s: int,
) -> Table:
    rows: list[tuple[int, float, int]] = []
    for market_id, group in df.groupby("market_id"):
        group = group.sort_values("datetime").reset_index(drop=True)
        btc = group["btc_binance"].ffill()
        open_btc = group["open_btc_binance"].iloc[0]
        res_gamma = group["resolved_yes_gamma"].iloc[-1]
        res_binance = group["resolved_yes_binance"].iloc[-1]
        if res_gamma is not None and not pd.isna(res_gamma):
            resolved_yes = int(bool(res_gamma))
        elif res_binance is not None and not pd.isna(res_binance):
            resolved_yes = int(bool(res_binance))
        else:
            continue
        t_start = pd.Timestamp(group["datetime"].iloc[0])
        for i, row in group.iterrows():
            elapsed = (pd.Timestamp(row["datetime"]) - t_start).total_seconds()
            time_remaining = max(0, int(candle_interval_s - elapsed))
            btc_now = btc.iloc[i]
            if pd.notna(btc_now) and open_btc and open_btc != 0:
                pct = (float(btc_now) - float(open_btc)) / float(open_btc)
            else:
                pct = 0.0
            rows.append((time_remaining, pct, resolved_yes))
    return _bucket(rows, time_bucket_s, pct_bucket_size, source="history.db")


def _bucket(rows: list[tuple[int, float, int]], time_bucket_s: int, pct_bucket_size: float, source: str) -> Table:
    counts: dict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0])
    for time_remaining, pct_change, resolved_yes in rows:
        t_key = (time_remaining // time_bucket_s) * time_bucket_s
        p_index = round(pct_change / pct_bucket_size)
        counts[(t_key, p_index)][0] += 1
        counts[(t_key, p_index)][1] += resolved_yes
    table: Table = {k: (v[0], v[1] / v[0]) for k, v in counts.items()}
    logger.info("Lookup table built from %s: %d rows  %d cells", source, len(rows), len(table))
    return table


def lookup(
    table: Table,
    time_remaining: int,
    pct_change_binance: float,
    time_bucket_s: int,
    pct_bucket_size: float,
    min_count: int,
) -> tuple[float, int] | None:
    """Return (prob_yes, n) for the matching bucket, or None if below min_count."""
    result = lookup_raw(table, time_remaining, pct_change_binance, time_bucket_s, pct_bucket_size)
    if result is None or result[0] < min_count:
        return None
    n, prob_yes = result
    return prob_yes, n


def lookup_raw(
    table: Table,
    time_remaining: int,
    pct_change_binance: float,
    time_bucket_s: int,
    pct_bucket_size: float,
) -> tuple[int, float] | None:
    """Return (n, prob_yes) for the bucket regardless of min_count, or None if unseen."""
    t_key = (time_remaining // time_bucket_s) * time_bucket_s
    p_index = round(pct_change_binance / pct_bucket_size)
    return table.get((t_key, p_index))


def _rows_from_file(file: Path, candle_interval_s: int = 300) -> list[tuple[int, float, int]]:
    """Yield (time_remaining, pct_change_binance, resolved_yes) for each tick row."""
    df = pd.read_parquet(file).sort_values("datetime").reset_index(drop=True)
    if df.empty:
        return []

    btc_col = "btc_binance" if "btc_binance" in df.columns else "btc_usd"
    btc = df[btc_col].ffill()
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

    rows = []
    for row in df.itertuples():
        elapsed_s = (pd.Timestamp(row.datetime) - t_start).total_seconds()
        time_remaining = max(0, int(candle_interval_s - elapsed_s))

        if has_btc and btc_open is not None and btc_open != 0:
            btc_now = btc.iloc[row.Index]
            pct_change = (float(btc_now) - btc_open) / btc_open if pd.notna(btc_now) else 0.0
        else:
            pct_change = 0.0

        rows.append((time_remaining, pct_change, resolved_yes))

    return rows
