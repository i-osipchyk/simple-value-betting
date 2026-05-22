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

_SCHEMA = pa.schema(
    [
        pa.field("datetime", pa.timestamp("us", tz="UTC")),
        pa.field("market_id", pa.string()),
        pa.field("yes_price", pa.float32()),
        pa.field("no_price", pa.float32()),
        pa.field("btc_usd", pa.float32()),
    ]
)

_CREATE_TICKS = """
CREATE TABLE IF NOT EXISTS ticks (
    datetime   TIMESTAMPTZ NOT NULL,
    market_id  VARCHAR     NOT NULL,
    yes_price  FLOAT,
    no_price   FLOAT,
    btc_usd    FLOAT
)
"""


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
    logger.info("DuckDB initialised at %s", _db_path())
    return conn


def insert_tick(
    conn: duckdb.DuckDBPyConnection,
    dt: datetime,
    market_id: str,
    yes_price: float | None,
    no_price: float | None,
    btc_usd: float | None,
) -> None:
    conn.execute(
        "INSERT INTO ticks VALUES (?, ?, ?, ?, ?)",
        [dt, market_id, yes_price, no_price, btc_usd],
    )


def write_latest_tick(
    dt: datetime,
    market_id: str,
    yes_price: float | None,
    no_price: float | None,
    btc_usd: float | None,
) -> None:
    """Atomically overwrite /data/latest_tick.json so the model container can read it."""
    payload = {
        "datetime": dt.isoformat(),
        "market_id": market_id,
        "yes_price": yes_price,
        "no_price": no_price,
        "btc_usd": btc_usd,
    }
    dest = Path(settings.local_data_dir) / "latest_tick.json"
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.rename(dest)  # atomic on POSIX


def export_batch(conn: duckdb.DuckDBPyConnection, since: datetime) -> Path | None:
    rows = conn.execute(
        "SELECT datetime, market_id, yes_price, no_price, btc_usd "
        "FROM ticks WHERE datetime >= ? ORDER BY datetime",
        [since],
    ).fetchall()

    if not rows:
        logger.info("No rows since %s — skipping parquet export", since)
        return None

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    market_id = rows[0][1]
    filename = f"ticks_{market_id}_{ts}.parquet"
    out_path = _raw_dir() / filename

    table = pa.table(
        {
            "datetime": [r[0] for r in rows],
            "market_id": [r[1] for r in rows],
            "yes_price": [r[2] for r in rows],
            "no_price": [r[3] for r in rows],
            "btc_usd": [r[4] for r in rows],
        },
        schema=_SCHEMA,
    )
    pq.write_table(table, out_path, compression="snappy")
    logger.info("Exported %d rows → %s", len(rows), out_path)
    return out_path
