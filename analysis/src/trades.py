"""
Trade ledger for the empirical probability container.
Stored at /data/analysis_trades/trades.parquet.
Same schema and P&L logic as the model container trades.
"""

import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from config import settings

logger = logging.getLogger(__name__)

STAKE = 1.0
_lock = threading.Lock()

_SCHEMA = pa.schema(
    [
        pa.field("trade_id", pa.string()),
        pa.field("opened_at", pa.timestamp("us", tz="UTC")),
        pa.field("market_id", pa.string()),
        pa.field("yes_price", pa.float64()),
        pa.field("no_price", pa.float64()),
        pa.field("btc_usd", pa.float64()),
        pa.field("pct_change_open", pa.float64()),
        pa.field("time_remaining", pa.int32()),
        pa.field("spread", pa.float64()),
        pa.field("side", pa.string()),  # "YES" or "NO"
        pa.field("predicted_prob", pa.float64()),
        pa.field("edge", pa.float64()),
        pa.field("model_id", pa.string()),
        pa.field("stake", pa.float64()),
        pa.field("resolved_yes", pa.bool_()),
        pa.field("resolved_at", pa.timestamp("us", tz="UTC")),
        pa.field("pnl", pa.float64()),
    ]
)


def _path() -> Path:
    p = Path(settings.local_data_dir) / "trades"
    p.mkdir(parents=True, exist_ok=True)
    return p / "analysis_trades.parquet"


def _read() -> list[dict]:
    path = _path()
    if not path.exists():
        return []
    return pq.read_table(path, schema=_SCHEMA).to_pylist()


def _write(rows: list[dict]) -> None:
    if not rows:
        tbl = pa.table({f.name: pa.array([], type=f.type) for f in _SCHEMA}, schema=_SCHEMA)
    else:
        tbl = pa.Table.from_pylist(rows, schema=_SCHEMA)
    tmp = _path().with_suffix(".parquet.tmp")
    pq.write_table(tbl, tmp, compression="snappy")
    tmp.rename(_path())


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
    row = {
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
    }
    with _lock:
        rows = _read()
        rows.append(row)
        _write(rows)


def resolve_market(market_id: str, resolved_yes: bool) -> None:
    fee = settings.pm_fee
    now = datetime.now(tz=timezone.utc)

    with _lock:
        rows = _read()
        open_trades = [r for r in rows if r["market_id"] == market_id and r["resolved_yes"] is None]
        if not open_trades:
            return

        for r in rows:
            if r["market_id"] != market_id or r["resolved_yes"] is not None:
                continue
            r["resolved_yes"] = resolved_yes
            r["resolved_at"] = now
            side = r.get("side", "YES")
            won = (side == "YES" and resolved_yes) or (side == "NO" and not resolved_yes)
            if won:
                price = r["yes_price"] if side == "YES" else r["no_price"]
                r["pnl"] = r["stake"] * (1.0 / price) * (1.0 - fee) - r["stake"]
            else:
                r["pnl"] = -r["stake"]

        _write(rows)

    _log_summary(rows, market_id, resolved_yes)


def _log_summary(rows: list[dict], market_id: str, resolved_yes: bool) -> None:
    market_trades = [r for r in rows if r["market_id"] == market_id and r["pnl"] is not None]
    if market_trades:
        m_pnl = sum(r["pnl"] for r in market_trades)
        m_wins = sum(1 for r in market_trades if r["pnl"] > 0)
        logger.info(
            "RESOLVED  market=%s  outcome=%s  trades=%d  wins=%d  pnl=%+.2f",
            market_id[:20],
            "YES" if resolved_yes else "NO",
            len(market_trades),
            m_wins,
            m_pnl,
        )

    closed = [r for r in rows if r["pnl"] is not None]
    if closed:
        total_pnl = sum(r["pnl"] for r in closed)
        total_wins = sum(1 for r in closed if r["pnl"] > 0)
        roi = 100.0 * total_pnl / (len(closed) * STAKE)
        logger.info(
            "OVERALL   trades=%d  wins=%d  pnl=%+.2f  roi=%+.1f%%",
            len(closed),
            total_wins,
            total_pnl,
            roi,
        )
