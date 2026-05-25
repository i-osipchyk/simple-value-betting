"""
Trade ledger for the empirical probability container, backed by DuckDB.

Open trades live in /data/analysis_trades.db until the market resolves,
then exported to /data/trades/analysis/ as timestamped parquet files and
deleted from the DB so only live positions remain in the database.
"""

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from table_store import TableStore

logger = logging.getLogger(__name__)

STAKE = 1.0

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id        VARCHAR     NOT NULL,
    opened_at       TIMESTAMPTZ NOT NULL,
    market_id       VARCHAR     NOT NULL,
    yes_price       DOUBLE,
    no_price        DOUBLE,
    btc_usd         DOUBLE,
    pct_change_binance DOUBLE,
    time_remaining  INTEGER,
    spread          DOUBLE,
    side            VARCHAR,
    predicted_prob  DOUBLE,
    edge            DOUBLE,
    model_id        VARCHAR,
    stake           DOUBLE,
    resolved_yes    BOOLEAN,
    resolved_at     TIMESTAMPTZ,
    pnl             DOUBLE,
    exit_reason     VARCHAR
)
"""

_store = TableStore(
    db_path=str(Path(settings.local_data_dir) / "analysis_trades.db"),
    table_name="trades",
    create_sql=_CREATE_SQL,
    parquet_dir=Path(settings.local_data_dir) / "trades" / "analysis",
)


def open_trade(
    market_id: str,
    yes_price: float,
    no_price: float,
    btc_usd: float,
    pct_change_binance: float,
    time_remaining: int,
    side: str,
    predicted_prob: float,
    edge: float,
    model_id: str,
) -> None:
    _store.insert({
        "trade_id": str(uuid.uuid4()),
        "opened_at": datetime.now(tz=timezone.utc),
        "market_id": market_id,
        "yes_price": yes_price,
        "no_price": no_price,
        "btc_usd": btc_usd,
        "pct_change_binance": pct_change_binance,
        "time_remaining": time_remaining,
        "spread": yes_price + no_price - 1.0,
        "side": side,
        "predicted_prob": predicted_prob,
        "edge": edge,
        "model_id": model_id,
        "stake": STAKE,
        "resolved_yes": None,
        "resolved_at": None,
        "pnl": None,
        "exit_reason": None,
    })


def resolve_market(market_id: str, resolved_yes: bool) -> None:
    fee = settings.pm_fee
    now = datetime.now(tz=timezone.utc)

    if not _store.select("market_id = ? AND exit_reason IS NULL", [market_id]):
        return

    win_side = "YES" if resolved_yes else "NO"
    win_price_col = "yes_price" if resolved_yes else "no_price"

    _store.execute(
        f"""UPDATE trades SET resolved_yes=?, resolved_at=?, exit_reason='resolved',
                pnl = stake * (1.0 / {win_price_col}) * (1.0 - ?) - stake
            WHERE market_id=? AND side=? AND exit_reason IS NULL""",
        [resolved_yes, now, fee, market_id, win_side],
    )
    _store.execute(
        """UPDATE trades SET resolved_yes=?, resolved_at=?, exit_reason='resolved',
               pnl = -stake
           WHERE market_id=? AND side != ? AND exit_reason IS NULL""",
        [resolved_yes, now, market_id, win_side],
    )

    exported = _store.export_and_delete("market_id = ?", [market_id])
    _log_summary(exported, market_id, resolved_yes)


def _log_summary(rows: list[dict], market_id: str, resolved_yes: bool) -> None:
    if not rows:
        return
    pnl = sum(r["pnl"] for r in rows if r["pnl"] is not None)
    wins = sum(1 for r in rows if r.get("pnl", 0) > 0)
    logger.info(
        "RESOLVED  market=%s  outcome=%s  trades=%d  wins=%d  pnl=%+.2f",
        market_id[:20],
        "YES" if resolved_yes else "NO",
        len(rows),
        wins,
        pnl,
    )

    stats = _store.aggregate_exported(
        "COUNT(*) AS n, "
        "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins, "
        "SUM(pnl) AS total_pnl"
    )
    if stats and stats["n"]:
        roi = 100.0 * stats["total_pnl"] / (stats["n"] * STAKE)
        logger.info(
            "OVERALL   trades=%d  wins=%d  pnl=%+.2f  roi=%+.1f%%",
            stats["n"], stats["wins"], stats["total_pnl"], roi,
        )
