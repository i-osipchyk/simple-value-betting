"""
DuckDB-backed history of all raw tick data.

At startup, all parquet files in the partitioned raw/ directory are loaded
into a single raw_ticks table. New candles are appended incrementally as
the watcher detects them. The lookup table is rebuilt from this DB after
each append.

File structure expected:  raw/{year}/{month}/{day}/{hour}/minute_{mm}_{market_id}.parquet
"""

import logging
from pathlib import Path

import duckdb

from config import settings

logger = logging.getLogger(__name__)

_conn: duckdb.DuckDBPyConnection | None = None

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS raw_ticks (
    market_id          VARCHAR,
    datetime           TIMESTAMPTZ,
    yes_price          FLOAT,
    no_price           FLOAT,
    btc_binance        FLOAT,
    btc_coinbase       FLOAT,
    btc_kraken         FLOAT,
    open_btc_binance   FLOAT,
    open_btc_coinbase  FLOAT,
    open_btc_kraken    FLOAT,
    resolved_yes_gamma   BOOLEAN,
    resolved_yes_binance BOOLEAN,
    above_ema9         BOOLEAN,
    above_ema20        BOOLEAN,
    above_ema34        BOOLEAN,
    above_all_emas     BOOLEAN,
    below_all_emas     BOOLEAN,
    ema9_value         FLOAT,
    ema20_value        FLOAT,
    ema34_value        FLOAT,
    prev_body_pct      FLOAT,
    prev_wick_ratio    FLOAT,
    prev_rel_volume    FLOAT,
    prev_green         BOOLEAN
)
"""


def get_connection() -> duckdb.DuckDBPyConnection:
    global _conn
    if _conn is None:
        _conn = duckdb.connect(str(Path(settings.local_data_dir) / "history.db"))
        _conn.execute(_CREATE_SQL)
    return _conn


def init(raw_dir: Path) -> int:
    """
    Rebuild raw_ticks from all parquet files in raw_dir at startup.
    Returns the total row count.
    """
    conn = get_connection()
    files = sorted(raw_dir.rglob("minute_*.parquet"))
    if not files:
        logger.info("history.db: no parquet files found — table is empty")
        return 0

    glob = str(raw_dir / "**" / "minute_*.parquet")
    conn.execute("DROP TABLE IF EXISTS raw_ticks")
    conn.execute(
        f"CREATE TABLE raw_ticks AS "
        f"SELECT * FROM read_parquet('{glob}', union_by_name=true)"
    )
    n: int = conn.execute("SELECT COUNT(*) FROM raw_ticks").fetchone()[0]
    logger.info("history.db: loaded %d rows from %d candles", n, len(files))
    return n


def append_parquet(path: Path) -> None:
    """Append a single new candle parquet to raw_ticks."""
    conn = get_connection()
    try:
        conn.execute(
            f"INSERT INTO raw_ticks "
            f"SELECT * FROM read_parquet('{path}', union_by_name=true)"
        )
        logger.debug("history.db: appended %s", path.name)
    except Exception:
        logger.exception("history.db: failed to append %s", path.name)
