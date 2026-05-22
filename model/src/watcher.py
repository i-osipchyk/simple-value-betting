"""
File watcher: resolves open trades and retrains whenever a new parquet batch lands in /data/raw/.
"""

import asyncio
import logging
from pathlib import Path

from watchfiles import Change, awatch

import trades
from config import settings
from features import resolve_outcome
from trainer import train

logger = logging.getLogger(__name__)

_DEFAULT_ALGORITHMS = ["logistic_regression"]
_train_lock = asyncio.Lock()


async def watch_and_resolve() -> None:
    """Resolve open trades whenever a completed candle parquet arrives."""
    raw_dir = Path(settings.local_data_dir) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Watching %s for completed candles", raw_dir)
    async for changes in awatch(str(raw_dir)):
        new_files = [
            Path(path)
            for change, path in changes
            if change == Change.added and path.endswith(".parquet") and Path(path).exists()
        ]
        if not new_files:
            continue

        latest = max(new_files, key=lambda f: f.stat().st_mtime)
        logger.info("New candle: %s — resolving trades", latest.name)

        outcome = await asyncio.to_thread(resolve_outcome, latest)
        if outcome is not None:
            market_id, resolved_yes = outcome
            await asyncio.to_thread(trades.resolve_market, market_id, resolved_yes)

        await _run_training()


async def _run_training() -> None:
    async with _train_lock:
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
                logger.warning("Training skipped: %s", exc)
            except Exception:
                logger.exception("Training error for %s", algorithm)
