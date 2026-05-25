"""
Write-to-DB / export-to-parquet storage pattern.

Each data source creates a TableStore pointed at its own DuckDB file and
parquet output directory. Data is written to the DB table on every event;
completed rows are exported to a timestamped parquet file and deleted from
the DB so the table only ever holds live/open rows.

    store = TableStore(
        db_path="/data/model_trades.db",
        table_name="trades",
        create_sql="CREATE TABLE IF NOT EXISTS trades (...)",
        parquet_dir=Path("/data/trades/model"),
    )
    store.insert({"trade_id": "...", ...})
    exported = store.export_and_delete("market_id = ?", [market_id])
"""

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq


class TableStore:
    def __init__(
        self,
        db_path: str,
        table_name: str,
        create_sql: str,
        parquet_dir: Path,
    ) -> None:
        self._table = table_name
        self._parquet_dir = Path(parquet_dir)
        self._parquet_dir.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(db_path)
        self._conn.execute(create_sql)
        self._lock = threading.RLock()

    def insert(self, row: dict) -> None:
        cols = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        with self._lock:
            self._conn.execute(
                f"INSERT INTO {self._table} ({cols}) VALUES ({placeholders})",
                list(row.values()),
            )

    def execute(self, sql: str, params: list | None = None) -> Any:
        """Run arbitrary SQL under the store lock. Use for UPDATE/DELETE."""
        with self._lock:
            return self._conn.execute(sql, params or [])

    def select(self, where_sql: str = "TRUE", params: list | None = None) -> list[dict]:
        with self._lock:
            rel = self._conn.execute(
                f"SELECT * FROM {self._table} WHERE {where_sql}",
                params or [],
            )
            cols = [d[0] for d in rel.description]
            return [dict(zip(cols, row)) for row in rel.fetchall()]

    def export_and_delete(self, where_sql: str = "TRUE", params: list | None = None) -> list[dict]:
        """
        Export matching rows to a timestamped parquet file, delete them from the DB.
        Returns the exported rows as a list of dicts.
        """
        with self._lock:
            arrow_tbl = self._conn.execute(
                f"SELECT * FROM {self._table} WHERE {where_sql}",
                params or [],
            ).fetch_arrow_table()

            if arrow_tbl.num_rows == 0:
                return []

            ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            out_path = self._parquet_dir / f"{self._table}_{ts}.parquet"
            pq.write_table(arrow_tbl, out_path, compression="snappy")

            self._conn.execute(
                f"DELETE FROM {self._table} WHERE {where_sql}",
                params or [],
            )

            return arrow_tbl.to_pylist()

    def aggregate_exported(self, select_cols: str) -> dict | None:
        """
        Run a SELECT aggregate over all previously exported parquet files.
        select_cols is a comma-separated list of expressions with aliases,
        e.g. "COUNT(*) AS n, SUM(pnl) AS total_pnl".
        Returns None if no exported files exist yet.
        """
        glob = str(self._parquet_dir / f"{self._table}_*.parquet")
        if not any(self._parquet_dir.glob(f"{self._table}_*.parquet")):
            return None
        conn = duckdb.connect()
        try:
            rel = conn.execute(f"SELECT {select_cols} FROM read_parquet('{glob}')")
            cols = [d[0] for d in rel.description]
            row = rel.fetchone()
            return dict(zip(cols, row)) if row else None
        finally:
            conn.close()

    def close(self) -> None:
        self._conn.close()
