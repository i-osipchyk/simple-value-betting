"""
Model training: logistic regression (default) and LightGBM.

Each training run produces a versioned directory under /data/models/ and updates
registry.json to point to the latest version for that algorithm.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from config import settings

logger = logging.getLogger(__name__)


def _models_dir() -> Path:
    p = Path(settings.local_data_dir) / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def train(model_cfg: dict, df: pd.DataFrame) -> dict:
    """Train a model from a config dict (from models.yaml). Returns metadata dict."""
    model_id_key: str = model_cfg["id"]
    algorithm: str = model_cfg["algorithm"]
    features: list[str] = model_cfg["features"]
    min_rows: int = model_cfg.get("min_training_rows", 500)
    max_rows: int | None = model_cfg.get("max_training_rows")
    params: dict = model_cfg.get("params", {})

    # Cap to most-recent N rows to limit memory and focus on recent patterns
    if max_rows and len(df) > max_rows:
        df = df.iloc[-max_rows:]

    df = df[features + ["resolved_yes"]].dropna()
    n = len(df)
    if n < min_rows:
        raise ValueError(
            f"Not enough training data: {n} rows (min_training_rows={min_rows})"
        )

    X = df[features]
    y = df["resolved_yes"].to_numpy(dtype=int)

    if n >= 10:
        split = int(n * 0.8)
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]
    else:
        X_train, X_test = X, X
        y_train, y_test = y, y
        logger.warning("Too few rows for train/test split — evaluating on training data")

    model = _build_model(algorithm, params)
    model.fit(X_train, y_train)
    metrics = _evaluate(model, X_test, y_test)
    feature_importance = _extract_importance(model, algorithm, features)

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    model_id = f"{model_id_key}_{ts}"
    model_dir = _models_dir() / model_id
    model_dir.mkdir()

    joblib.dump(model, model_dir / "model.joblib")

    metadata = {
        "model_id": model_id,
        "config_id": model_id_key,
        "trained_at": datetime.now(tz=timezone.utc).isoformat(),
        "algorithm": algorithm,
        "feature_names": features,
        "training_rows": int(len(X_train)),
        "test_rows": int(len(X_test)),
        "metrics": metrics,
        "feature_importance": feature_importance,
        "model_config": model_cfg,
    }
    (model_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))

    _update_registry(model_id_key, model_id)

    logger.info(
        "Trained %s — rows=%d  auc=%.3f  brier=%.3f",
        model_id,
        len(X_train),
        metrics.get("auc_roc", float("nan")),
        metrics.get("brier_score", float("nan")),
    )
    return metadata


def _build_model(algorithm: str, params: dict):
    if algorithm == "logistic_regression":
        return Pipeline([
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(
                max_iter=params.get("max_iter", 1000),
                C=params.get("C", 1.0),
                random_state=params.get("random_state", 42),
            )),
        ])
    if algorithm == "lightgbm":
        from lightgbm import LGBMClassifier
        return LGBMClassifier(
            n_estimators=params.get("n_estimators", 100),
            learning_rate=params.get("learning_rate", 0.1),
            max_depth=params.get("max_depth", -1),
            num_leaves=params.get("num_leaves", 31),
            random_state=params.get("random_state", 42),
            verbose=-1,
        )
    if algorithm == "xgboost":
        from xgboost import XGBClassifier
        return XGBClassifier(
            n_estimators=params.get("n_estimators", 100),
            max_depth=params.get("max_depth", 6),
            learning_rate=params.get("learning_rate", 0.1),
            random_state=params.get("random_state", 42),
            eval_metric="logloss",
            verbosity=0,
        )
    raise ValueError(f"Unknown algorithm: {algorithm!r}")


def _evaluate(model, X_test: np.ndarray, y_test: np.ndarray) -> dict:
    metrics: dict = {}
    if len(X_test) == 0:
        return metrics
    try:
        y_prob = model.predict_proba(X_test)[:, 1]
        y_pred = model.predict(X_test)
        if len(np.unique(y_test)) > 1:
            metrics["auc_roc"] = round(float(roc_auc_score(y_test, y_prob)), 4)
        metrics["brier_score"] = round(float(brier_score_loss(y_test, y_prob)), 4)
        metrics["accuracy"] = round(float(np.mean(y_pred == y_test)), 4)
    except Exception as exc:
        logger.warning("Evaluation failed: %s", exc)
    return metrics


def _extract_importance(model, algorithm: str, features: list[str]) -> dict:
    try:
        if algorithm == "logistic_regression":
            coef = model.named_steps["clf"].coef_[0]
            return {f: round(float(c), 6) for f, c in zip(features, coef)}
        if algorithm == "lightgbm":
            imp = model.feature_importances_
            return {f: int(i) for f, i in zip(features, imp)}
    except Exception:
        pass
    return {}


def _update_registry(config_id: str, model_id: str) -> None:
    registry_path = _models_dir() / "registry.json"
    registry: dict = {}
    if registry_path.exists():
        try:
            registry = json.loads(registry_path.read_text())
        except Exception:
            pass
    registry[config_id] = model_id
    registry_path.write_text(json.dumps(registry, indent=2))
