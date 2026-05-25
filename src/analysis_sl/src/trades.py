"""
Trade ledger with stop-loss support for the empirical lookup container.

Open trades live in /data/analysis_sl_trades.db. Stop-loss exits are written
immediately. On market resolution the remaining open trades are resolved and
all closed trades are exported to /data/trades/analysis_sl/ then deleted from DB.
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
    pct_change_open DOUBLE,
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
    exit_reason     VARCHAR,
    exit_price      DOUBLE
)
"""

_store = TableStore(
    db_path=str(Path(settings.local_data_dir) / "analysis_sl_trades.db"),
    table_name="trades",
    create_sql=_CREATE_SQL,
    parquet_dir=Path(settings.local_data_dir) / "trades" / "analysis_sl",
)


def open_trade(
    market_id: str,
    yes_price: float,
    no_price: float,
    btc_usd: float,
    pct_change_open: float,
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
        "pct_change_open": pct_change_open,
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
        "exit_price": None,
    })


def get_open_positions(market_id: str) -> list[dict]:
    return _store.select("market_id = ? AND exit_reason IS NULL", [market_id])


def stop_loss_exit(trade_id: str, exit_price: float) -> None:
    _store.execute(
        """UPDATE trades SET
               exit_reason = 'stop_loss',
               exit_price  = ?,
               resolved_at = ?,
               pnl = stake * (? / yes_price) - stake
           WHERE trade_id = ? AND exit_reason IS NULL""",
        [exit_price, datetime.now(tz=timezone.utc), exit_price, trade_id],
    )


def resolve_market(market_id: str, resolved_yes: bool) -> None:
    fee = settings.pm_fee
    now = datetime.now(tz=timezone.utc)

    if not _store.select("market_id = ?", [market_id]):
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
    stopped = sum(1 for r in rows if r.get("exit_reason") == "stop_loss")
    logger.info(
        "RESOLVED  market=%s  outcome=%s  trades=%d  stopped=%d  wins=%d  pnl=%+.2f",
        market_id[:20],
        "YES" if resolved_yes else "NO",
        len(rows),
        stopped,
        wins,
        pnl,
    )

    stats = _store.aggregate_exported(
        "COUNT(*) AS n, "
        "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN exit_reason = 'stop_loss' THEN 1 ELSE 0 END) AS stopped, "
        "SUM(pnl) AS total_pnl"
    )
    if stats and stats["n"]:
        roi = 100.0 * stats["total_pnl"] / (stats["n"] * STAKE)
        logger.info(
            "OVERALL   trades=%d  stopped=%d  wins=%d  pnl=%+.2f  roi=%+.1f%%",
            stats["n"], stats["stopped"], stats["wins"], stats["total_pnl"], roi,
        )
