"""FastAPI application — /train, /predict, /models, /status."""

import asyncio
import logging
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import predictor
import registry
import storage
from trainer import train

logger = logging.getLogger(__name__)

app = FastAPI(title="Polymarket ML Model")
_started_at = time.time()


class TrainRequest(BaseModel):
    algorithm: str = "logistic_regression"


class PredictRequest(BaseModel):
    market_id: str
    yes_price: float
    no_price: float
    btc_usd: float
    pct_change_open: float
    time_remaining: int
    algorithm: str = "logistic_regression"


@app.post("/train")
async def train_endpoint(req: TrainRequest):
    try:
        metadata = await asyncio.to_thread(train, req.algorithm)
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
            req.pct_change_open,
            req.time_remaining,
            req.algorithm,
        )
        return result
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/models")
def models_endpoint():
    return registry.list_models()


@app.get("/status")
def status_endpoint():
    return {
        "models": registry.list_models(),
        "predictions_count": storage.count_predictions(),
        "uptime_seconds": int(time.time() - _started_at),
    }
