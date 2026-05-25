"""FastAPI application — model inference, training, and MLOps endpoints."""

import asyncio
import logging
import time

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

import predictor
import registry
import storage
import watcher
from config import MODELS, settings
from features import load_features
from pathlib import Path
from trainer import train

logger = logging.getLogger(__name__)

app = FastAPI(title="Polymarket ML Model")
_started_at = time.time()

# Shared with watcher._train_lock to expose training status
_training_in_progress = False


# ── Request/response models ────────────────────────────────────────────────────

class TrainRequest(BaseModel):
    algorithm: str = "logistic_regression"


class PredictRequest(BaseModel):
    market_id: str
    yes_price: float
    no_price: float
    btc_usd: float
    pct_change_binance: float
    time_remaining: int
    model_id: str = "logistic_regression"
    pct_change_coinbase: float = 0.0
    pct_change_kraken: float = 0.0


# ── Legacy endpoints (kept for backward compat) ────────────────────────────────

@app.post("/train")
async def train_endpoint(req: TrainRequest):
    model_cfg = next((m for m in MODELS if m["id"] == req.algorithm), None)
    if model_cfg is None:
        raise HTTPException(status_code=404, detail=f"No model config for id '{req.algorithm}'")
    try:
        raw_dir = Path(settings.local_data_dir) / "raw"
        df = await asyncio.to_thread(load_features, raw_dir, settings.candle_interval_minutes * 60)
        metadata = await asyncio.to_thread(train, model_cfg, df)
        return metadata
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/predict")
async def predict_endpoint(req: PredictRequest):
    try:
        result = await asyncio.to_thread(
            predictor.predict,
            req.market_id,
            req.yes_price,
            req.no_price,
            req.btc_usd,
            req.pct_change_binance,
            req.time_remaining,
            req.model_id,
            req.pct_change_coinbase,
            req.pct_change_kraken,
        )
        return result
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/status")
def status_endpoint():
    return {
        "models": registry.list_models(),
        "predictions_count": storage.count_predictions(),
        "uptime_seconds": int(time.time() - _started_at),
    }


# ── MLOps endpoints ────────────────────────────────────────────────────────────

@app.get("/models")
def list_models_endpoint():
    """All configured models with their current version metadata."""
    current = registry.list_models()
    return [
        {
            "config": m,
            "current_version": current.get(m["id"]),
        }
        for m in MODELS
    ]


@app.get("/models/{model_id}")
def get_model_endpoint(model_id: str):
    """Config + current version metadata for one model."""
    model_cfg = next((m for m in MODELS if m["id"] == model_id), None)
    if model_cfg is None:
        raise HTTPException(status_code=404, detail=f"No model config for id '{model_id}'")
    current = registry.list_models()
    return {
        "config": model_cfg,
        "current_version": current.get(model_id),
    }


@app.get("/models/{model_id}/history")
def model_history_endpoint(model_id: str):
    """All past trained versions for a model, newest first."""
    model_cfg = next((m for m in MODELS if m["id"] == model_id), None)
    if model_cfg is None:
        raise HTTPException(status_code=404, detail=f"No model config for id '{model_id}'")
    return registry.get_model_history(model_id)


@app.get("/models/{model_id}/feature-importance")
def feature_importance_endpoint(model_id: str):
    """Feature importance/coefficients from the current model version."""
    model_cfg = next((m for m in MODELS if m["id"] == model_id), None)
    if model_cfg is None:
        raise HTTPException(status_code=404, detail=f"No model config for id '{model_id}'")
    _, metadata = registry.load_model(model_id, expected_features=model_cfg["features"])
    if metadata is None:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' has not been trained yet")
    return {
        "model_id": metadata.get("model_id"),
        "algorithm": metadata.get("algorithm"),
        "feature_importance": metadata.get("feature_importance", {}),
    }


@app.post("/models/{model_id}/retrain")
async def retrain_endpoint(model_id: str, background_tasks: BackgroundTasks):
    """Force retrain a specific model in the background."""
    model_cfg = next((m for m in MODELS if m["id"] == model_id), None)
    if model_cfg is None:
        raise HTTPException(status_code=404, detail=f"No model config for id '{model_id}'")

    async def _do_retrain():
        try:
            raw_dir = Path(settings.local_data_dir) / "raw"
            df = await asyncio.to_thread(load_features, raw_dir, settings.candle_interval_minutes * 60)
            metadata = await asyncio.to_thread(train, model_cfg, df)
            logger.info("Forced retrain complete: %s  metrics=%s", metadata["model_id"], metadata["metrics"])
        except Exception:
            logger.exception("Forced retrain failed for %s", model_id)

    background_tasks.add_task(_do_retrain)
    return {"status": "started", "model_id": model_id}


@app.get("/health")
def health_endpoint():
    """System health: uptime, training status, per-model info."""
    current = registry.list_models()
    model_status = {}
    for m in MODELS:
        mid = m["id"]
        meta = current.get(mid)
        model_status[mid] = {
            "last_trained_at": meta.get("trained_at") if meta else None,
            "current_version": meta.get("model_id") if meta else None,
            "candles_since_retrain": watcher._candle_counters.get(mid, 0),
            "metrics": meta.get("metrics") if meta else None,
        }
    return {
        "uptime_seconds": int(time.time() - _started_at),
        "training_in_progress": watcher._train_lock.locked(),
        "models": model_status,
    }
