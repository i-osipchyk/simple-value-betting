"""
Model registry: load the latest trained model for a given config id.
Results are cached in-process; cache is invalidated when registry.json changes.
"""

import json
import logging
from pathlib import Path

import joblib

from config import settings

logger = logging.getLogger(__name__)

# config_id -> (model_object, metadata_dict)
_cache: dict[str, tuple[object, dict]] = {}


def _models_dir() -> Path:
    return Path(settings.local_data_dir) / "models"


def _read_registry() -> dict[str, str]:
    p = _models_dir() / "registry.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def load_model(model_id: str, expected_features: list[str] | None = None) -> tuple[object | None, dict | None]:
    """Return (model, metadata) for the latest registered version of *model_id*.

    If expected_features is provided and the stored model was trained on different
    features, returns (None, None) to trigger retraining.
    """
    registry = _read_registry()
    version_id = registry.get(model_id)
    if not version_id:
        return None, None

    cached = _cache.get(model_id)
    if cached and cached[1].get("model_id") == version_id:
        return cached

    model_dir = _models_dir() / version_id
    model_path = model_dir / "model.joblib"
    meta_path = model_dir / "metadata.json"

    if not model_path.exists():
        logger.warning("Model artifact missing: %s", model_path)
        return None, None

    metadata: dict = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    if expected_features is not None and metadata.get("feature_names") != expected_features:
        logger.info(
            "Model %s has features %s but config requires %s — skipping (will retrain)",
            version_id,
            metadata.get("feature_names"),
            expected_features,
        )
        return None, None

    model = joblib.load(model_path)
    _cache[model_id] = (model, metadata)
    logger.info("Loaded model %s from disk", version_id)
    return model, metadata


def list_models() -> dict[str, dict]:
    """Return metadata for all registered model ids."""
    result: dict[str, dict] = {}
    for config_id, version_id in _read_registry().items():
        meta_path = _models_dir() / version_id / "metadata.json"
        if meta_path.exists():
            try:
                result[config_id] = json.loads(meta_path.read_text())
            except Exception:
                pass
    return result


def get_model_history(model_id: str) -> list[dict]:
    """Return all past version metadata for a config id, newest first."""
    pattern = f"{model_id}_*"
    history: list[dict] = []
    for meta_path in (_models_dir()).glob(f"{pattern}/metadata.json"):
        try:
            history.append(json.loads(meta_path.read_text()))
        except Exception:
            pass
    history.sort(key=lambda m: m.get("trained_at", ""), reverse=True)
    return history
