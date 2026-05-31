"""
File watcher: resolves open trades on resolution signals from the collector,
and retrains models whenever a new raw parquet candle arrives.

Resolution signals arrive as JSON files in /data/resolutions/ written by the
collector after confirming the market outcome via Gamma API or Binance.
New parquet files in /data/raw/ trigger model retraining only.
"""

import asyncio
import json
import logging
from pathlib import Path

from watchfiles import Change, awatch

import history
import lookup
import trades
from config import MODELS, settings
from features import load_features
from trainer import train

logger = logging.getLogger(__name__)

_train_lock = asyncio.Lock()
_candle_counters: dict[str, int] = {m["id"]: 0 for m in MODELS}


async def watch_and_resolve() -> None:
    raw_dir = Path(settings.local_data_dir) / "raw"
    resolutions_dir = Path(settings.local_data_dir) / "resolutions"
    raw_dir.mkdir(parents=True, exist_ok=True)
    resolutions_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Watching %s for resolution signals and %s for new candles",
        resolutions_dir, raw_dir,
    )
    async for changes in awatch(str(raw_dir), str(resolutions_dir)):
        resolution_files = [
            Path(path)
            for change, path in changes
            if change in (Change.added, Change.modified)
            and path.endswith(".json")
            and not path.endswith(".json.tmp")
            and Path(path).exists()
        ]
        parquet_files = [
            Path(path)
            for change, path in changes
            if change in (Change.added, Change.modified)
            and path.endswith(".parquet")
            and Path(path).exists()
        ]

        for res_file in resolution_files:
            try:
                payload = json.loads(res_file.read_text())
                market_id: str = payload["market_id"]
                resolved_yes = payload.get("resolved_yes_gamma")
                if resolved_yes is None:
                    resolved_yes = payload.get("resolved_yes_binance")
                if resolved_yes is None:
                    logger.warning("No resolution value in %s — skipping", res_file.name)
                    continue
                logger.info(
                    "Resolution signal: market=%s  outcome=%s",
                    market_id[:20], "YES" if resolved_yes else "NO",
                )
                await asyncio.to_thread(trades.resolve_market, market_id, bool(resolved_yes))
            except Exception:
                logger.exception("Failed to process resolution file %s", res_file.name)

        if parquet_files:
            await _run_training(parquet_files)


async def _run_training(new_parquet_files: list[Path]) -> None:
    async with _train_lock:
        for path in new_parquet_files:
            await asyncio.to_thread(history.append_parquet, path)

        conn = history.get_connection()
        table = await asyncio.to_thread(
            lookup.build_from_db, conn,
            30, 0.001, settings.candle_interval_minutes * 60,
        )
        lookup.set_table(table)

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
