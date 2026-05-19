"""
Collector entry point.

Lifecycle per 15-minute candle:
  1. Fetch market info (market_id + token IDs) from Polymarket Gamma API.
  2. Open WebSocket and subscribe to that candle's YES/NO tokens + BTC/USD.
  3. Write one tick row to DuckDB every second (carry-forward prices).
  4. At the next 15-minute boundary, stop the tick loop and export a parquet batch.
  5. Repeat from step 1 with the new candle's token IDs.

On SIGINT/SIGTERM: finish the current tick, do a final parquet flush, close DuckDB.
"""

import asyncio
import logging
import signal
from datetime import datetime, timezone

import s3_sync
import storage
from config import settings
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
        dt, market_id, yes_price, no_price, btc_usd = client.snapshot()
        storage.insert_tick(conn, dt, market_id, yes_price, no_price, btc_usd)
        storage.write_latest_tick(dt, market_id, yes_price, no_price, btc_usd)
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.tick_interval_seconds)
        except asyncio.TimeoutError:
            pass


def export_candle(conn, since: datetime) -> datetime:
    """Export ticks since `since` to parquet, upload if needed, return new batch start."""
    now = datetime.now(tz=timezone.utc)
    try:
        path = storage.export_batch(conn, since)
        if path:
            s3_sync.save(path)
    except Exception:
        logger.exception("Parquet export failed")
    return now


async def candle_loop(conn, global_stop: asyncio.Event) -> None:
    """Outer loop: collect one candle, export, reconnect with fresh token IDs."""
    batch_start = datetime.now(tz=timezone.utc)

    while not global_stop.is_set():
        try:
            market_info: MarketInfo = await asyncio.to_thread(fetch_market_info)
        except Exception:
            logger.exception("Could not fetch market info — retrying in 10s")
            try:
                await asyncio.wait_for(global_stop.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
            continue

        client = PolymarketWSClient(market_info)
        iter_stop = asyncio.Event()

        ws_task = asyncio.create_task(client.run(), name="ws_client")
        tick_task = asyncio.create_task(tick_loop(client, conn, iter_stop), name="tick_loop")

        sleep_s = seconds_until_reconnect()
        logger.info(
            "Candle %s active — sleeping %ds until next boundary",
            market_info.market_id,
            sleep_s,
        )

        try:
            await asyncio.wait_for(global_stop.wait(), timeout=sleep_s)
        except asyncio.TimeoutError:
            pass  # Normal: boundary reached

        # Stop collecting ticks for this candle
        iter_stop.set()
        client.stop()
        await asyncio.gather(ws_task, tick_task, return_exceptions=True)
        logger.info("Candle %s closed — exporting", market_info.market_id)

        # Export immediately at the boundary, before the next candle starts
        batch_start = export_candle(conn, batch_start)

        if global_stop.is_set():
            break

    return batch_start


async def main() -> None:
    conn = storage.init_db()
    global_stop = asyncio.Event()

    def _shutdown(sig: signal.Signals) -> None:
        logger.info("Received %s — shutting down", sig.name)
        global_stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    batch_start = await candle_loop(conn, global_stop)

    # Final flush for any ticks collected since the last candle export
    export_candle(conn, batch_start)
    conn.close()
    logger.info("Collector stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
