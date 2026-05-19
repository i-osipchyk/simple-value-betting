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
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

from config import settings
from features import load_features

logger = logging.getLogger(__name__)


def _models_dir() -> Path:
    p = Path(settings.local_data_dir) / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def train(algorithm: str = "logistic_regression") -> dict:
    """Train a model on all available parquet data. Returns metadata dict."""
    raw_dir = Path(settings.local_data_dir) / "raw"
    df = load_features(raw_dir)

    n = len(df)
    if n < 2:
        raise ValueError(f"Not enough training data: {n} rows (need at least 2)")

    if n < settings.min_training_rows:
        logger.warning(
            "Training with only %d rows (MIN_TRAINING_ROWS=%d) — model will have high variance",
            n,
            settings.min_training_rows,
        )

    X = df[settings.feature_names].to_numpy(dtype=float)
    y = df["resolved_yes"].to_numpy(dtype=int)

    if n >= 10:
        split = int(n * 0.8)
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]
    else:
        X_train, X_test = X, X
        y_train, y_test = y, y
        logger.warning("Too few rows for train/test split — evaluating on training data")

    model = _build_model(algorithm)
    model.fit(X_train, y_train)
    metrics = _evaluate(model, X_test, y_test)

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    model_id = f"{algorithm}_{ts}"
    model_dir = _models_dir() / model_id
    model_dir.mkdir()

    joblib.dump(model, model_dir / "model.joblib")

    metadata = {
        "model_id": model_id,
        "trained_at": datetime.now(tz=timezone.utc).isoformat(),
        "algorithm": algorithm,
        "feature_names": settings.feature_names,
        "training_rows": int(len(X_train)),
        "test_rows": int(len(X_test)),
        "metrics": metrics,
    }
    (model_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))

    _update_registry(algorithm, model_id)

    logger.info(
        "Trained %s — rows=%d  auc=%.3f  brier=%.3f",
        model_id,
        len(X_train),
        metrics.get("auc_roc", float("nan")),
        metrics.get("brier_score", float("nan")),
    )
    return metadata


def _build_model(algorithm: str):
    if algorithm == "logistic_regression":
        return Pipeline(
            [
                ("scaler", StandardScaler()),
                ("clf", LogisticRegression(max_iter=1000, random_state=42)),
            ]
        )
    if algorithm == "lightgbm":
        from lightgbm import LGBMClassifier

        return LGBMClassifier(n_estimators=100, random_state=42, verbose=-1)
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


def _update_registry(algorithm: str, model_id: str) -> None:
    registry_path = _models_dir() / "registry.json"
    registry: dict = {}
    if registry_path.exists():
        try:
            registry = json.loads(registry_path.read_text())
        except Exception:
            pass
    registry[algorithm] = model_id
    registry_path.write_text(json.dumps(registry, indent=2))
