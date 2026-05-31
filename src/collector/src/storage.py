import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

import s3_sync
from config import settings

logger = logging.getLogger(__name__)

# DuckDB tick buffer schema (live prices only — open prices and resolution added at export).
_CREATE_TICKS = """
CREATE TABLE IF NOT EXISTS ticks (
    datetime      TIMESTAMPTZ NOT NULL,
    market_id     VARCHAR     NOT NULL,
    yes_price     FLOAT,
    no_price      FLOAT,
    btc_binance   FLOAT,
    btc_coinbase  FLOAT,
    btc_kraken    FLOAT
)
"""

_MIGRATIONS = [
    "ALTER TABLE ticks ADD COLUMN IF NOT EXISTS btc_coinbase FLOAT",
    "ALTER TABLE ticks ADD COLUMN IF NOT EXISTS btc_kraken FLOAT",
    "ALTER TABLE ticks RENAME COLUMN btc_usd TO btc_binance",
]

# Parquet export schema — one file per completed candle.
_EXPORT_SCHEMA = pa.schema(
    [
        pa.field("market_id", pa.string()),
        pa.field("datetime", pa.timestamp("us", tz="UTC")),
        pa.field("yes_price", pa.float32()),
        pa.field("no_price", pa.float32()),
        pa.field("btc_binance", pa.float32()),
        pa.field("btc_coinbase", pa.float32()),
        pa.field("btc_kraken", pa.float32()),
        pa.field("open_btc_binance", pa.float32()),
        pa.field("open_btc_coinbase", pa.float32()),
        pa.field("open_btc_kraken", pa.float32()),
        pa.field("resolved_yes_gamma", pa.bool_()),
        pa.field("resolved_yes_binance", pa.bool_()),
        pa.field("above_ema9", pa.bool_()),
        pa.field("above_ema20", pa.bool_()),
        pa.field("above_ema34", pa.bool_()),
        pa.field("above_all_emas", pa.bool_()),
        pa.field("below_all_emas", pa.bool_()),
        pa.field("ema9_value", pa.float32()),
        pa.field("ema20_value", pa.float32()),
        pa.field("ema34_value", pa.float32()),
        pa.field("prev_body_pct", pa.float32()),
        pa.field("prev_wick_ratio", pa.float32()),
        pa.field("prev_rel_volume", pa.float32()),
        pa.field("prev_green", pa.bool_()),
    ]
)


def _db_path() -> str:
    return os.path.join(settings.local_data_dir, "raw_data.db")


def _raw_dir() -> Path:
    p = Path(settings.local_data_dir) / "raw"
    p.mkdir(parents=True, exist_ok=True)
    return p


def init_db() -> duckdb.DuckDBPyConnection:
    os.makedirs(settings.local_data_dir, exist_ok=True)
    conn = duckdb.connect(_db_path())
    conn.execute(_CREATE_TICKS)
    for sql in _MIGRATIONS:
        try:
            conn.execute(sql)
        except Exception as exc:
            logger.debug("Migration skipped (%s): %s", exc, sql)
    logger.info("DuckDB initialised at %s", _db_path())
    return conn


def insert_tick(
    conn: duckdb.DuckDBPyConnection,
    dt: datetime,
    market_id: str,
    yes_price: float | None,
    no_price: float | None,
    btc_binance: float | None,
    btc_coinbase: float | None,
    btc_kraken: float | None,
) -> None:
    conn.execute(
        "INSERT INTO ticks VALUES (?, ?, ?, ?, ?, ?, ?)",
        [dt, market_id, yes_price, no_price, btc_binance, btc_coinbase, btc_kraken],
    )


def write_latest_tick(
    dt: datetime,
    market_id: str,
    yes_price: float | None,
    no_price: float | None,
    btc_binance: float | None,
    btc_coinbase: float | None,
    btc_kraken: float | None,
    open_btc_binance: float | None,
    open_btc_coinbase: float | None,
    open_btc_kraken: float | None,
    ema_flags: dict | None = None,
) -> None:
    """Atomically overwrite /data/latest_tick.json so the model container can read it."""
    payload = {
        "datetime": dt.isoformat(),
        "market_id": market_id,
        "yes_price": yes_price,
        "no_price": no_price,
        "btc_binance": btc_binance,
        "btc_coinbase": btc_coinbase,
        "btc_kraken": btc_kraken,
        "open_btc_binance": open_btc_binance,
        "open_btc_coinbase": open_btc_coinbase,
        "open_btc_kraken": open_btc_kraken,
        **(ema_flags or {}),
    }
    dest = Path(settings.local_data_dir) / "latest_tick.json"
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.rename(dest)  # atomic on POSIX


def write_resolution(
    market_id: str,
    candle_ts: int,
    resolved_yes_gamma: bool | None,
    resolved_yes_binance: bool | None,
) -> Path:
    """Write an atomic resolution signal file for the trade engine to consume."""
    resolutions_dir = Path(settings.local_data_dir) / "resolutions"
    resolutions_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "market_id": market_id,
        "candle_ts": candle_ts,
        "resolved_yes_gamma": resolved_yes_gamma,
        "resolved_yes_binance": resolved_yes_binance,
    }
    path = resolutions_dir / f"resolved_{market_id}_{candle_ts}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.rename(path)
    return path


def export_batch(
    conn: duckdb.DuckDBPyConnection,
    candle_start: datetime,
    candle_end: datetime,
    market_id: str,
    open_btc: tuple[float | None, float | None, float | None],
    resolved_yes_gamma: bool | None,
    resolved_yes_binance: bool | None,
    ema_flags: dict | None = None,
) -> Path | None:
    """Export ticks for exactly one candle window to parquet, enriched with open prices and resolution."""
    rows = conn.execute(
        "SELECT datetime, market_id, yes_price, no_price, btc_binance, btc_coinbase, btc_kraken "
        "FROM ticks "
        "WHERE datetime >= ? AND datetime < ? AND market_id = ? "
        "ORDER BY datetime",
        [candle_start, candle_end, market_id],
    ).fetchall()

    if not rows:
        logger.info(
            "No rows for market %s in [%s, %s) — skipping parquet export",
            market_id, candle_start, candle_end,
        )
        return None

    open_binance, open_coinbase, open_kraken = open_btc
    flags = ema_flags or {}
    n = len(rows)

    ts = int(candle_start.timestamp())
    filename = f"ticks_{market_id}_{ts}.parquet"
    out_path = _raw_dir() / filename

    table = pa.table(
        {
            "market_id": [r[1] for r in rows],
            "datetime": [r[0] for r in rows],
            "yes_price": [r[2] for r in rows],
            "no_price": [r[3] for r in rows],
            "btc_binance": [r[4] for r in rows],
            "btc_coinbase": [r[5] for r in rows],
            "btc_kraken": [r[6] for r in rows],
            "open_btc_binance": [open_binance] * n,
            "open_btc_coinbase": [open_coinbase] * n,
            "open_btc_kraken": [open_kraken] * n,
            "resolved_yes_gamma": [resolved_yes_gamma] * n,
            "resolved_yes_binance": [resolved_yes_binance] * n,
            "above_ema9": [flags.get("above_ema9")] * n,
            "above_ema20": [flags.get("above_ema20")] * n,
            "above_ema34": [flags.get("above_ema34")] * n,
            "above_all_emas": [flags.get("above_all_emas")] * n,
            "below_all_emas": [flags.get("below_all_emas")] * n,
            "ema9_value": [flags.get("ema9_value")] * n,
            "ema20_value": [flags.get("ema20_value")] * n,
            "ema34_value": [flags.get("ema34_value")] * n,
            "prev_body_pct": [flags.get("prev_body_pct")] * n,
            "prev_wick_ratio": [flags.get("prev_wick_ratio")] * n,
            "prev_rel_volume": [flags.get("prev_rel_volume")] * n,
            "prev_green": [flags.get("prev_green")] * n,
        },
        schema=_EXPORT_SCHEMA,
    )
    tmp_path = out_path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp_path, compression="snappy")
    tmp_path.rename(out_path)  # atomic on POSIX — watcher only sees complete files
    logger.info("Exported %d rows → %s", n, out_path)
    s3_sync.save(out_path)
    return out_path
