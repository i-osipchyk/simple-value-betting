"""
Model registry: load the latest trained model for a given algorithm.
Results are cached in-process; cache is invalidated when registry.json changes.
"""

import json
import logging
from pathlib import Path

import joblib

from config import settings

logger = logging.getLogger(__name__)

# algorithm -> (model_object, metadata_dict)
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


def load_model(algorithm: str) -> tuple[object | None, dict | None]:
    """Return (model, metadata) for the latest registered version of *algorithm*."""
    registry = _read_registry()
    model_id = registry.get(algorithm)
    if not model_id:
        return None, None

    cached = _cache.get(algorithm)
    if cached and cached[1].get("model_id") == model_id:
        return cached

    model_dir = _models_dir() / model_id
    model_path = model_dir / "model.joblib"
    meta_path = model_dir / "metadata.json"

    if not model_path.exists():
        logger.warning("Model artifact missing: %s", model_path)
        return None, None

    metadata: dict = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    if metadata.get("feature_names") != settings.feature_names:
        logger.info(
            "Model %s has features %s but config requires %s — skipping (will retrain)",
            model_id,
            metadata.get("feature_names"),
            settings.feature_names,
        )
        return None, None

    model = joblib.load(model_path)
    _cache[algorithm] = (model, metadata)
    logger.info("Loaded model %s from disk", model_id)
    return model, metadata


def list_models() -> dict[str, dict]:
    """Return metadata for all registered algorithms."""
    result: dict[str, dict] = {}
    for algo, model_id in _read_registry().items():
        meta_path = _models_dir() / model_id / "metadata.json"
        if meta_path.exists():
            try:
                result[algo] = json.loads(meta_path.read_text())
            except Exception:
                pass
    return result
