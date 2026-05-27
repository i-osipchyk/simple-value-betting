"""
Collector entry point.

Lifecycle per candle (one coroutine per market in markets.yaml):
  1. Fetch market info (market_id + token IDs) from Polymarket Gamma API.
  2. Open WebSocket and subscribe to that candle's YES/NO tokens + BTC/USD.
  3. Write one tick row to DuckDB every second (carry-forward prices).
  4. At the next candle boundary, stop the tick loop and export a parquet batch.
  5. Repeat from step 1 with the new candle's token IDs.

On SIGINT/SIGTERM: finish the current tick, do a final parquet flush, close DuckDB.
"""

import asyncio
import logging
import signal
from datetime import datetime, timezone

import btc_feed
import coinbase_feed
import kraken_feed
import s3_sync
import storage
from config import MARKETS, settings
from market import MarketInfo, fetch_market_info, seconds_until_reconnect
from websocket_client import PolymarketWSClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def tick_loop(
    client: PolymarketWSClient,
    conn,
    stop: asyncio.Event,
) -> None:
    """Write one row per second into DuckDB using the latest prices from WS state."""
    while not stop.is_set():
        dt, market_id, yes_price, no_price, _ = client.snapshot()
        btc_usd = btc_feed.get_btc_price()
        btc_cb = coinbase_feed.get_coinbase_price()
        btc_kr = kraken_feed.get_kraken_price()
        storage.insert_tick(conn, dt, market_id, yes_price, no_price, btc_usd, btc_cb, btc_kr)
        storage.write_latest_tick(dt, market_id, yes_price, no_price, btc_usd, btc_cb, btc_kr)
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.tick_interval_seconds)
        except asyncio.TimeoutError:
            pass


def export_candle(conn, since: datetime, market_id: str) -> datetime:
    """Export ticks since `since` for `market_id` to parquet, upload if needed."""
    now = datetime.now(tz=timezone.utc)
    try:
        path = storage.export_batch(conn, since, market_id)
        if path:
            s3_sync.save(path)
    except Exception:
        logger.exception("Parquet export failed")
    return now


async def candle_loop(conn, global_stop: asyncio.Event, market: dict) -> None:
    """Outer loop: collect one candle, export, reconnect with fresh token IDs."""
    slug_prefix: str = market["slug_prefix"]
    # Align to the current candle boundary so a restart mid-candle picks up all
    # DuckDB rows from the candle start rather than only post-restart rows.
    _now = datetime.now(tz=timezone.utc)
    _interval_s = settings.candle_interval_minutes * 60
    _epoch = int(_now.timestamp())
    batch_start = datetime.fromtimestamp(_epoch - (_epoch % _interval_s), tz=timezone.utc)
    last_market_id: str | None = None

    while not global_stop.is_set():
        try:
            market_info: MarketInfo = await asyncio.to_thread(fetch_market_info, slug_prefix)
        except Exception:
            logger.exception("Could not fetch market info for %s — retrying in 10s", slug_prefix)
            try:
                await asyncio.wait_for(global_stop.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
            continue

        last_market_id = market_info.market_id
        client = PolymarketWSClient(market_info)
        iter_stop = asyncio.Event()

        ws_task = asyncio.create_task(client.run(), name=f"ws_{market['name']}")
        tick_task = asyncio.create_task(tick_loop(client, conn, iter_stop), name=f"tick_{market['name']}")

        sleep_s = seconds_until_reconnect()
        logger.info(
            "Candle %s active (%s) — sleeping %ds until next boundary",
            market_info.market_id,
            market["name"],
            sleep_s,
        )

        try:
            await asyncio.wait_for(global_stop.wait(), timeout=sleep_s)
        except asyncio.TimeoutError:
            pass  # Normal: boundary reached

        iter_stop.set()
        client.stop()
        await asyncio.gather(ws_task, tick_task, return_exceptions=True)
        logger.info("Candle %s closed — exporting", market_info.market_id)

        batch_start = export_candle(conn, batch_start, market_info.market_id)

        if global_stop.is_set():
            break

    # Final flush for ticks collected in the current partial candle
    if last_market_id:
        export_candle(conn, batch_start, last_market_id)


async def main() -> None:
    conn = storage.init_db()
    global_stop = asyncio.Event()

    def _shutdown(sig: signal.Signals) -> None:
        logger.info("Received %s — shutting down", sig.name)
        global_stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    btc_task = asyncio.create_task(btc_feed.btc_price_loop(), name="btc_feed")
    cb_task = asyncio.create_task(coinbase_feed.coinbase_price_loop(), name="coinbase_feed")
    kr_task = asyncio.create_task(kraken_feed.kraken_price_loop(), name="kraken_feed")

    market_tasks = [
        asyncio.create_task(candle_loop(conn, global_stop, m), name=f"candle_{m['name']}")
        for m in MARKETS
    ]

    try:
        await asyncio.gather(*market_tasks, return_exceptions=True)
    finally:
        for task in (btc_task, cb_task, kr_task):
            task.cancel()
        await asyncio.gather(btc_task, cb_task, kr_task, return_exceptions=True)

    conn.close()
    logger.info("Collector stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
