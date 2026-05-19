"""
File watcher: triggers train → predict whenever a new parquet batch lands in /data/raw/.

On startup: scans for existing parquet files and trains an initial model if any exist.
Then watches for new files using watchfiles and repeats train → predict on each arrival.
"""

import asyncio
import logging
from pathlib import Path

from watchfiles import Change, awatch

import predictor
import trades
from config import settings
from features import extract_current_snapshot, resolve_outcome
from trainer import train

logger = logging.getLogger(__name__)

_DEFAULT_ALGORITHMS = ["logistic_regression"]
_train_lock = asyncio.Lock()


async def watch_and_train() -> None:
    raw_dir = Path(settings.local_data_dir) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Train on any data that already exists before we start watching
    existing = sorted(raw_dir.glob("ticks_*.parquet"))
    if existing:
        logger.info("Found %d existing parquet file(s) — running initial training", len(existing))
        await _train_and_predict(existing[-1])

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
        await _train_and_predict(latest)


async def _train_and_predict(snapshot_file: Path) -> None:
    async with _train_lock:
        # Resolve open trades for the market whose candle just completed
        outcome = await asyncio.to_thread(resolve_outcome, snapshot_file)
        if outcome is not None:
            market_id, resolved_yes = outcome
            await asyncio.to_thread(trades.resolve_market, market_id, resolved_yes)

        for algorithm in _DEFAULT_ALGORITHMS:
            try:
                metadata = await asyncio.to_thread(train, algorithm)
                logger.info(
                    "Model ready: %s  rows=%d  metrics=%s",
                    metadata["model_id"],
                    metadata["training_rows"],
                    metadata["metrics"],
                )
            except ValueError as exc:
                logger.warning("Skipping prediction — training failed: %s", exc)
                continue
            except Exception:
                logger.exception("Training error for %s", algorithm)
                continue

            snapshot = await asyncio.to_thread(
                extract_current_snapshot,
                snapshot_file,
                settings.candle_interval_minutes * 60,
            )
            if snapshot is None:
                logger.warning("Could not extract snapshot from %s", snapshot_file.name)
                continue

            try:
                await asyncio.to_thread(
                    predictor.predict,
                    snapshot["market_id"],
                    snapshot["yes_price"],
                    snapshot["no_price"],
                    snapshot["btc_usd"],
                    snapshot["pct_change_open"],
                    snapshot["time_remaining"],
                    algorithm,
                )
            except Exception:
                logger.exception("Prediction failed for %s", algorithm)
