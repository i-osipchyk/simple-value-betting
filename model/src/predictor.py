"""
Prediction logic: call the latest model, compute edge, log in green if edge exceeds threshold.
"""

import logging
from datetime import datetime, timezone

import numpy as np

import registry
import storage
import trades
from config import settings

logger = logging.getLogger(__name__)

_GREEN = "\033[32m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def predict(
    market_id: str,
    yes_price: float,
    no_price: float,
    btc_usd: float,
    pct_change_open: float,
    time_remaining: int,
    algorithm: str = "logistic_regression",
) -> dict:
    model, metadata = registry.load_model(algorithm)
    if model is None or metadata is None:
        raise RuntimeError(f"No trained model available for algorithm '{algorithm}'")

    spread = yes_price + no_price - 1.0
    feature_values = {
        "pct_change_open": pct_change_open,
        "time_remaining": time_remaining,
        "yes_price": yes_price,
        "no_price": no_price,
        "spread": spread,
    }
    X = np.array([[feature_values[f] for f in settings.feature_names]])
    predicted_prob = float(model.predict_proba(X)[0, 1])
    market_prob = yes_price
    edge = predicted_prob - market_prob

    model_id = metadata.get("model_id", "unknown")
    pred_id = storage.write_prediction(
        market_id=market_id,
        yes_price=yes_price,
        no_price=no_price,
        btc_usd=btc_usd,
        pct_change_open=pct_change_open,
        time_remaining=time_remaining,
        predicted_prob=predicted_prob,
        market_prob=market_prob,
        edge=edge,
        model_id=model_id,
        algorithm=algorithm,
    )

    result = {
        "id": pred_id,
        "predicted_prob": round(predicted_prob, 4),
        "market_prob": round(market_prob, 4),
        "edge": round(edge, 4),
        "model_id": model_id,
        "algorithm": algorithm,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }

    _log_result(result, market_id, edge)
    return result


def infer(
    market_id: str,
    yes_price: float,
    no_price: float,
    btc_usd: float,
    pct_change_open: float,
    time_remaining: int,
    algorithm: str = "logistic_regression",
) -> None:
    """Run inference every second — logs in green if edge found, silent otherwise. No DB write."""
    model, metadata = registry.load_model(algorithm)
    if model is None or metadata is None:
        return

    spread = yes_price + no_price - 1.0
    feature_values = {
        "pct_change_open": pct_change_open,
        "time_remaining": time_remaining,
        "yes_price": yes_price,
        "no_price": no_price,
        "spread": spread,
    }
    X = np.array([[feature_values[f] for f in settings.feature_names]])
    predicted_prob = float(model.predict_proba(X)[0, 1])
    market_prob = yes_price
    edge = predicted_prob - market_prob

    model_id = metadata.get("model_id", "unknown")
    result = {
        "predicted_prob": predicted_prob,
        "market_prob": market_prob,
        "edge": edge,
        "model_id": model_id,
    }

    interval_s = settings.candle_interval_minutes * 60
    if (
        yes_price <= 0.04 or yes_price >= 0.97
        or time_remaining > interval_s - 15
        or yes_price < 0.40
        or time_remaining < 100
        or edge > 0.15
        or pct_change_open == 0.0
        or edge < settings.min_edge_threshold
    ):
        logger.info(
            "SKIP  market=%-20s  t=%3ds  yes=%.3f  edge=%+.4f",
            market_id[:20], time_remaining, yes_price, edge,
        )
        return

    trades.open_trade(
        market_id=market_id,
        yes_price=yes_price,
        no_price=no_price,
        btc_usd=btc_usd,
        pct_change_open=pct_change_open,
        time_remaining=time_remaining,
        side="YES",
        predicted_prob=predicted_prob,
        edge=edge,
        model_id=model_id,
    )

    _log_result(result, market_id, edge)


def _log_result(result: dict, market_id: str, edge: float) -> None:
    market_prob = result["market_prob"]
    predicted_prob = result["predicted_prob"]
    no_edge = (1.0 - predicted_prob) - (1.0 - market_prob - (result.get("spread", 0) or 0))

    if edge >= settings.min_edge_threshold:
        msg = (
            f"EDGE YES  market={market_id[:20]:<20}  "
            f"pred={predicted_prob:.3f}  mkt={market_prob:.3f}  edge={edge:+.4f}  model={result['model_id']}"
        )
        logger.info("%s%s%s%s", _GREEN, _BOLD, msg, _RESET)
    else:
        logger.info(
            "pred  market=%s  pred=%.3f  mkt=%.3f  edge=%+.4f",
            market_id[:20],
            predicted_prob,
            market_prob,
            edge,
        )
