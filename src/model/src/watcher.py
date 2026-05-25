"""
File watcher: resolves open trades and retrains whenever a new parquet batch lands in /data/raw/.
"""

import asyncio
import logging
from pathlib import Path

from watchfiles import Change, awatch

import trades
from config import MODELS, settings
from features import load_features, resolve_outcome
from trainer import train

logger = logging.getLogger(__name__)

_train_lock = asyncio.Lock()
_candle_counters: dict[str, int] = {m["id"]: 0 for m in MODELS}


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
        models_due = []
        for model_cfg in MODELS:
            mid = model_cfg["id"]
            _candle_counters[mid] += 1
            every_n = model_cfg.get("retrain_every_n_candles", 1)
            if _candle_counters[mid] < every_n:
                logger.info(
                    "Model %s: candle %d/%d — skipping retrain",
                    mid, _candle_counters[mid], every_n,
                )
                continue
            _candle_counters[mid] = 0
            models_due.append(model_cfg)

        if not models_due:
            return

        raw_dir = Path(settings.local_data_dir) / "raw"
        df = await asyncio.to_thread(
            load_features, raw_dir, settings.candle_interval_minutes * 60
        )

        for model_cfg in models_due:
            try:
                metadata = await asyncio.to_thread(train, model_cfg, df)
                logger.info(
                    "Model ready: %s  rows=%d  metrics=%s",
                    metadata["model_id"],
                    metadata["training_rows"],
                    metadata["metrics"],
                )
            except ValueError as exc:
                logger.warning("Training skipped for %s: %s", model_cfg["id"], exc)
            except Exception:
                logger.exception("Training error for %s", model_cfg["id"])
