import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from config import settings

logger = logging.getLogger(__name__)

# DuckDB tick buffer schema (live prices only — open prices and resolution added at export).
_CREATE_TICKS = """
CREATE TABLE IF NOT EXISTS ticks (
    datetime      TIMESTAMPTZ NOT NULL,
    market_id     VARCHAR     NOT NULL,
    yes_price     FLOAT,
    no_price      FLOAT,
    btc_usd       FLOAT,
    btc_coinbase  FLOAT,
    btc_kraken    FLOAT
)
"""

_MIGRATIONS = [
    "ALTER TABLE ticks ADD COLUMN IF NOT EXISTS btc_coinbase FLOAT",
    "ALTER TABLE ticks ADD COLUMN IF NOT EXISTS btc_kraken FLOAT",
]

# Parquet export schema — one file per completed candle.
_EXPORT_SCHEMA = pa.schema(
    [
        pa.field("market_id", pa.string()),
        pa.field("datetime", pa.timestamp("us", tz="UTC")),
        pa.field("yes_price", pa.float32()),
        pa.field("no_price", pa.float32()),
        pa.field("btc_usd", pa.float32()),
        pa.field("btc_coinbase", pa.float32()),
        pa.field("btc_kraken", pa.float32()),
        pa.field("open_btc_binance", pa.float32()),
        pa.field("open_btc_coinbase", pa.float32()),
        pa.field("open_btc_kraken", pa.float32()),
        pa.field("resolved_yes_gamma", pa.bool_()),
        pa.field("resolved_yes_binance", pa.bool_()),
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
        conn.execute(sql)
    logger.info("DuckDB initialised at %s", _db_path())
    return conn


def insert_tick(
    conn: duckdb.DuckDBPyConnection,
    dt: datetime,
    market_id: str,
    yes_price: float | None,
    no_price: float | None,
    btc_usd: float | None,
    btc_coinbase: float | None,
    btc_kraken: float | None,
) -> None:
    conn.execute(
        "INSERT INTO ticks VALUES (?, ?, ?, ?, ?, ?, ?)",
        [dt, market_id, yes_price, no_price, btc_usd, btc_coinbase, btc_kraken],
    )


def write_latest_tick(
    dt: datetime,
    market_id: str,
    yes_price: float | None,
    no_price: float | None,
    btc_usd: float | None,
    btc_coinbase: float | None,
    btc_kraken: float | None,
    open_btc_binance: float | None,
    open_btc_coinbase: float | None,
    open_btc_kraken: float | None,
) -> None:
    """Atomically overwrite /data/latest_tick.json so the model container can read it."""
    payload = {
        "datetime": dt.isoformat(),
        "market_id": market_id,
        "yes_price": yes_price,
        "no_price": no_price,
        "btc_usd": btc_usd,
        "btc_coinbase": btc_coinbase,
        "btc_kraken": btc_kraken,
        "open_btc_binance": open_btc_binance,
        "open_btc_coinbase": open_btc_coinbase,
        "open_btc_kraken": open_btc_kraken,
    }
    dest = Path(settings.local_data_dir) / "latest_tick.json"
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.rename(dest)  # atomic on POSIX


def export_batch(
    conn: duckdb.DuckDBPyConnection,
    candle_start: datetime,
    candle_end: datetime,
    market_id: str,
    open_btc: tuple[float | None, float | None, float | None],
    resolved_yes_gamma: bool | None,
    resolved_yes_binance: bool | None,
) -> Path | None:
    """Export ticks for exactly one candle window to parquet, enriched with open prices and resolution."""
    rows = conn.execute(
        "SELECT datetime, market_id, yes_price, no_price, btc_usd, btc_coinbase, btc_kraken "
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
            "btc_usd": [r[4] for r in rows],
            "btc_coinbase": [r[5] for r in rows],
            "btc_kraken": [r[6] for r in rows],
            "open_btc_binance": [open_binance] * n,
            "open_btc_coinbase": [open_coinbase] * n,
            "open_btc_kraken": [open_kraken] * n,
            "resolved_yes_gamma": [resolved_yes_gamma] * n,
            "resolved_yes_binance": [resolved_yes_binance] * n,
        },
        schema=_EXPORT_SCHEMA,
    )
    tmp_path = out_path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp_path, compression="snappy")
    tmp_path.rename(out_path)  # atomic on POSIX — watcher only sees complete files
    logger.info("Exported %d rows → %s", n, out_path)
    return out_path
