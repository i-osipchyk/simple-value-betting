"""Predictions DuckDB — separate from the collector's raw_data.db."""

import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from config import settings

logger = logging.getLogger(__name__)

_CREATE_PREDICTIONS = """
CREATE TABLE IF NOT EXISTS predictions (
    id              VARCHAR PRIMARY KEY,
    predicted_at    TIMESTAMPTZ,
    market_id       VARCHAR,
    yes_price       FLOAT,
    no_price        FLOAT,
    btc_usd         FLOAT,
    pct_change_open FLOAT,
    time_remaining  INTEGER,
    predicted_prob  FLOAT,
    market_prob     FLOAT,
    edge            FLOAT,
    model_id        VARCHAR,
    algorithm       VARCHAR,
    resolved_yes    BOOLEAN,
    resolved_at     TIMESTAMPTZ
)
"""

_conn: duckdb.DuckDBPyConnection | None = None
_lock = threading.Lock()


def _db_path() -> str:
    return os.path.join(settings.local_data_dir, "predictions.db")


def _get_conn() -> duckdb.DuckDBPyConnection:
    global _conn
    if _conn is None:
        os.makedirs(settings.local_data_dir, exist_ok=True)
        _conn = duckdb.connect(_db_path())
        _conn.execute(_CREATE_PREDICTIONS)
        logger.info("Predictions DB initialised at %s", _db_path())
    return _conn


def write_prediction(
    market_id: str,
    yes_price: float,
    no_price: float,
    btc_usd: float,
    pct_change_open: float,
    time_remaining: int,
    predicted_prob: float,
    market_prob: float,
    edge: float,
    model_id: str,
    algorithm: str,
) -> str:
    pred_id = str(uuid.uuid4())
    now = datetime.now(tz=timezone.utc)
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO predictions
              (id, predicted_at, market_id, yes_price, no_price, btc_usd,
               pct_change_open, time_remaining, predicted_prob, market_prob,
               edge, model_id, algorithm, resolved_yes, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            [
                pred_id, now, market_id, yes_price, no_price, btc_usd,
                pct_change_open, time_remaining, predicted_prob, market_prob,
                edge, model_id, algorithm,
            ],
        )
    return pred_id


def count_predictions() -> int:
    with _lock:
        return int(_get_conn().execute("SELECT COUNT(*) FROM predictions").fetchone()[0])


def predictions_dir() -> Path:
    p = Path(settings.local_data_dir) / "predictions"
    p.mkdir(parents=True, exist_ok=True)
    return p
