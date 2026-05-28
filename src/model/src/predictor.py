"""
Prediction logic: call the latest model, compute edge, log in green if edge exceeds threshold.
"""

import logging
from datetime import datetime, timezone

import pandas as pd

import registry
import storage
import trades
from config import MODELS, settings

logger = logging.getLogger(__name__)

_GREEN = "\033[32m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _build_feature_vector(
    model_cfg: dict,
    yes_price: float,
    no_price: float,
    pct_change_binance: float,
    time_remaining: int,
    pct_change_coinbase: float,
    pct_change_kraken: float,
    above_ema9: float = 0.0,
    above_ema20: float = 0.0,
    above_ema34: float = 0.0,
    above_all_emas: float = 0.0,
    below_all_emas: float = 0.0,
    ema9_value: float = 0.0,
    ema20_value: float = 0.0,
    ema34_value: float = 0.0,
    ema9_dist: float = 0.0,
    ema20_dist: float = 0.0,
    ema34_dist: float = 0.0,
    prev_body_pct: float = 0.0,
    prev_wick_ratio: float = 0.0,
    prev_rel_volume: float = 1.0,
    prev_green: float = 0.0,
) -> pd.DataFrame:
    all_values = {
        "pct_change_binance": pct_change_binance,
        "pct_change_coinbase": pct_change_coinbase,
        "pct_change_kraken": pct_change_kraken,
        "time_remaining": time_remaining,
        "yes_price": yes_price,
        "no_price": no_price,
        "spread": yes_price + no_price - 1.0,
        "above_ema9": above_ema9,
        "above_ema20": above_ema20,
        "above_ema34": above_ema34,
        "above_all_emas": above_all_emas,
        "below_all_emas": below_all_emas,
        "ema9_value": ema9_value,
        "ema20_value": ema20_value,
        "ema34_value": ema34_value,
        "ema9_dist": ema9_dist,
        "ema20_dist": ema20_dist,
        "ema34_dist": ema34_dist,
        "prev_body_pct": prev_body_pct,
        "prev_wick_ratio": prev_wick_ratio,
        "prev_rel_volume": prev_rel_volume,
        "prev_green": prev_green,
    }
    features = model_cfg["features"]
    return pd.DataFrame([[all_values[f] for f in features]], columns=features)


def predict(
    market_id: str,
    yes_price: float,
    no_price: float,
    btc_binance: float,
    pct_change_binance: float,
    time_remaining: int,
    model_id: str = "logistic_regression",
    pct_change_coinbase: float = 0.0,
    pct_change_kraken: float = 0.0,
) -> dict:
    model_cfg = next((m for m in MODELS if m["id"] == model_id), None)
    if model_cfg is None:
        raise RuntimeError(f"No model config found for id '{model_id}'")

    model, metadata = registry.load_model(model_id, expected_features=model_cfg["features"])
    if model is None or metadata is None:
        raise RuntimeError(f"No trained model available for '{model_id}'")

    X = _build_feature_vector(
        model_cfg, yes_price, no_price, pct_change_binance, time_remaining,
        pct_change_coinbase, pct_change_kraken,
    )
    predicted_prob = float(model.predict_proba(X)[0, 1])
    market_prob = yes_price
    edge = predicted_prob - market_prob

    version_id = metadata.get("model_id", "unknown")
    pred_id = storage.write_prediction(
        market_id=market_id,
        yes_price=yes_price,
        no_price=no_price,
        btc_usd=btc_binance,
        pct_change_binance=pct_change_binance,
        time_remaining=time_remaining,
        predicted_prob=predicted_prob,
        market_prob=market_prob,
        edge=edge,
        model_id=version_id,
        algorithm=model_cfg["algorithm"],
    )

    result = {
        "id": pred_id,
        "predicted_prob": round(predicted_prob, 4),
        "market_prob": round(market_prob, 4),
        "edge": round(edge, 4),
        "model_id": version_id,
        "config_id": model_id,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }

    _log_result(result, market_id, edge)
    return result


def infer(
    market_id: str,
    yes_price: float,
    no_price: float,
    btc_binance: float,
    pct_change_binance: float,
    time_remaining: int,
    pct_change_coinbase: float = 0.0,
    pct_change_kraken: float = 0.0,
    above_ema9: float = 0.0,
    above_ema20: float = 0.0,
    above_ema34: float = 0.0,
    above_all_emas: float = 0.0,
    below_all_emas: float = 0.0,
    ema9_value: float = 0.0,
    ema20_value: float = 0.0,
    ema34_value: float = 0.0,
    ema9_dist: float = 0.0,
    ema20_dist: float = 0.0,
    ema34_dist: float = 0.0,
    prev_body_pct: float = 0.0,
    prev_wick_ratio: float = 0.0,
    prev_rel_volume: float = 1.0,
    prev_green: float = 0.0,
) -> None:
    """Run inference for all configured models and log one summary line per tick."""
    interval_s = settings.candle_interval_minutes * 60
    results: list[tuple[dict, float, float, bool]] = []  # (model_cfg, predicted_prob, edge, tradeable)

    for model_cfg in MODELS:
        model, metadata = registry.load_model(model_cfg["id"], expected_features=model_cfg["features"])
        if model is None or metadata is None:
            continue

        X = _build_feature_vector(
            model_cfg, yes_price, no_price, pct_change_binance, time_remaining,
            pct_change_coinbase, pct_change_kraken,
            above_ema9, above_ema20, above_ema34, above_all_emas, below_all_emas,
            ema9_value, ema20_value, ema34_value,
            ema9_dist, ema20_dist, ema34_dist,
            prev_body_pct, prev_wick_ratio, prev_rel_volume, prev_green,
        )
        predicted_prob = float(model.predict_proba(X)[0, 1])
        edge = predicted_prob - yes_price

        rules = model_cfg.get("entry_rules", {})
        tradeable = (
            rules.get("min_edge", 0.0) <= edge <= rules.get("max_edge", float("inf"))
            and rules.get("min_time", 0) <= time_remaining <= rules.get("max_time", interval_s)
            and rules.get("min_price", 0.0) < yes_price < rules.get("max_price", 1.0)
        )
        results.append((model_cfg, metadata, predicted_prob, edge, tradeable))

    if not results:
        return

    _log_tick(market_id, yes_price, time_remaining, results)

    for model_cfg, metadata, predicted_prob, edge, tradeable in results:
        if not tradeable:
            continue
        version_id = metadata.get("model_id", "unknown")
        trades.open_trade(
            config_id=model_cfg["id"],
            market_id=market_id,
            yes_price=yes_price,
            no_price=no_price,
            btc_usd=btc_binance,
            pct_change_binance=pct_change_binance,
            time_remaining=time_remaining,
            side="YES",
            predicted_prob=predicted_prob,
            edge=edge,
            model_id=version_id,
        )
        msg = (
            f"EDGE YES  market={market_id[:20]:<20}  model={model_cfg['id']}  "
            f"pred={predicted_prob:.3f}  mkt={yes_price:.3f}  edge={edge:+.4f}"
        )
        logger.info("%s%s%s%s", _GREEN, _BOLD, msg, _RESET)


def _log_tick(
    market_id: str,
    yes_price: float,
    time_remaining: int,
    results: list,
) -> None:
    model_parts = "  ".join(
        f"{cfg['id']}={prob:.3f}({edge:+.3f})"
        for cfg, _meta, prob, edge, _tradeable in results
    )
    logger.info(
        "tick  market=%-20s  t=%3ds  mkt=%.3f  %s",
        market_id[:20], time_remaining, yes_price, model_parts,
    )
