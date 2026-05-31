"""
Trade ledger backed by a single DuckDB table with parquet export on resolution.

All strategies write to the same table; the strategy_id column identifies which
model or heuristic produced each trade.

  /data/trades.db                         ← live open trades
  /data/trades/trades_{ts}.parquet        ← resolved trades, one file per market close
"""

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import s3_sync
from config import MODELS, settings
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
    strategy_id     VARCHAR,
    stake           DOUBLE,
    resolved_yes    BOOLEAN,
    resolved_at     TIMESTAMPTZ,
    pnl             DOUBLE,
    exit_reason     VARCHAR,
    exit_price      DOUBLE
)
"""

_store: TableStore | None = None


def _get_store() -> TableStore:
    global _store
    if _store is None:
        _store = TableStore(
            db_path=str(Path(settings.local_data_dir) / "trades.db"),
            table_name="trades",
            create_sql=_CREATE_SQL,
            parquet_dir=Path(settings.local_data_dir) / "trades",
        )
    return _store


def open_trade(
    config_id: str,
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
    _get_store().insert({
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
        "strategy_id": config_id,
        "stake": STAKE,
        "resolved_yes": None,
        "resolved_at": None,
        "pnl": None,
        "exit_reason": None,
        "exit_price": None,
    })


def get_open_positions(config_id: str, market_id: str) -> list[dict]:
    """Return all open (unresolved, not stop-loss exited) trades for a strategy + market."""
    return _get_store().select(
        "strategy_id = ? AND market_id = ? AND exit_reason IS NULL",
        [config_id, market_id],
    )


def stop_loss_exit(config_id: str, trade_id: str, exit_price: float) -> None:
    """Close a trade at stop-loss price before market resolution."""
    _get_store().execute(
        """UPDATE trades SET
               exit_reason = 'stop_loss',
               exit_price  = ?,
               resolved_at = ?,
               pnl = stake * (? / yes_price) - stake
           WHERE trade_id = ? AND exit_reason IS NULL""",
        [exit_price, datetime.now(tz=timezone.utc), exit_price, trade_id],
    )


def resolve_market(market_id: str, resolved_yes: bool) -> None:
    """Resolve all open trades for this market across every strategy."""
    store = _get_store()
    fee = settings.pm_fee
    now = datetime.now(tz=timezone.utc)

    if not store.select("market_id = ? AND exit_reason IS NULL", [market_id]):
        return

    win_side = "YES" if resolved_yes else "NO"
    win_price_col = "yes_price" if resolved_yes else "no_price"

    store.execute(
        f"""UPDATE trades SET resolved_yes=?, resolved_at=?, exit_reason='resolved',
                pnl = stake * (1.0 / {win_price_col}) * (1.0 - ?) - stake
            WHERE market_id=? AND side=? AND exit_reason IS NULL""",
        [resolved_yes, now, fee, market_id, win_side],
    )
    store.execute(
        """UPDATE trades SET resolved_yes=?, resolved_at=?, exit_reason='resolved',
               pnl = -stake
           WHERE market_id=? AND side != ? AND exit_reason IS NULL""",
        [resolved_yes, now, market_id, win_side],
    )

    exported, parquet_path = store.export_and_delete("market_id = ?", [market_id])
    _log_summary(store, exported, market_id, resolved_yes)

    if parquet_path:
        s3_sync.save(parquet_path)


def _log_summary(
    store: TableStore, rows: list[dict], market_id: str, resolved_yes: bool
) -> None:
    if not rows:
        return

    by_strategy: dict[str, list[dict]] = {}
    for r in rows:
        sid = r.get("strategy_id") or "unknown"
        by_strategy.setdefault(sid, []).append(r)

    for strategy_id, strategy_rows in sorted(by_strategy.items()):
        pnl = sum(r["pnl"] for r in strategy_rows if r["pnl"] is not None)
        wins = sum(1 for r in strategy_rows if (r.get("pnl") or 0) > 0)
        logger.info(
            "RESOLVED  strategy=%-30s  market=%s  outcome=%s  trades=%d  wins=%d  pnl=%+.2f",
            strategy_id, market_id[:20], "YES" if resolved_yes else "NO",
            len(strategy_rows), wins, pnl,
        )

    stats = store.aggregate_exported(
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
