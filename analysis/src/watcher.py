"""
File watcher: rebuilds the lookup table whenever a new parquet batch arrives.
Also resolves open trades for the completed market before rebuilding.
"""

import asyncio
import logging
from pathlib import Path

import pandas as pd
from watchfiles import Change, awatch

import trades
from config import settings
from lookup import Table, build_table

logger = logging.getLogger(__name__)

_table: Table = {}
_rebuild_lock = asyncio.Lock()


def get_table() -> Table:
    return _table


async def watch_and_rebuild() -> None:
    raw_dir = Path(settings.local_data_dir) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    existing = sorted(raw_dir.glob("ticks_*.parquet"))
    if existing:
        logger.info("Found %d existing parquet files — building initial lookup table", len(existing))
        await _rebuild(raw_dir, existing[-1])

    logger.info("Watching %s for new candle batches", raw_dir)
    async for changes in awatch(str(raw_dir)):
        new_files = [
            Path(path)
            for change, path in changes
            if change == Change.added and path.endswith(".parquet") and Path(path).exists()
        ]
        if not new_files:
            continue

        logger.info("New batch(es): %s", [f.name for f in new_files])
        latest = max(new_files, key=lambda f: f.stat().st_mtime)
        await _rebuild(raw_dir, latest)


async def _rebuild(raw_dir: Path, latest_file: Path) -> None:
    async with _rebuild_lock:
        global _table

        outcome = await asyncio.to_thread(_resolve_outcome, latest_file)
        if outcome is not None:
            market_id, resolved_yes = outcome
            await asyncio.to_thread(trades.resolve_market, market_id, resolved_yes)

        _table = await asyncio.to_thread(
            build_table,
            raw_dir,
            settings.time_bucket_seconds,
            settings.pct_change_bucket_size,
            settings.candle_interval_minutes * 60,
        )


def _resolve_outcome(file: Path) -> tuple[str, bool] | None:
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
