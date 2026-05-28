"""
Collector entry point.

Lifecycle per candle:
  1. Pre-fetch (20s before boundary): next market info from Gamma API only.
     Open prices are NOT pre-fetched — they don't exist in exchange APIs until the candle starts.
  2. At exact boundary: stop old tick loop, kick off finalization as background task.
  3. Immediately after boundary: fetch open prices + connect to new WS in parallel.
  4. Start tick loop once both WS is ready and open prices are fetched.
  5. Finalization (background): poll Gamma ≤30s, derive Binance resolution, export parquet.
"""

import asyncio
import logging
import signal
import time
from datetime import datetime, timezone

import btc_feed
import coinbase_feed
import kraken_feed
import s3_sync
import storage
from config import MARKETS, settings
from market import MarketInfo, fetch_binance_resolution, fetch_gamma_resolution, fetch_market_info, fetch_open_prices
from websocket_client import PolymarketWSClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Start pre-fetching next market info this many seconds before the boundary.
_PREFETCH_BEFORE_S = 20


async def tick_loop(
    client: PolymarketWSClient,
    conn,
    stop: asyncio.Event,
    open_btc_binance: float | None,
    open_btc_coinbase: float | None,
    open_btc_kraken: float | None,
    ema_flags: dict,
) -> None:
    """Write one row per second into DuckDB using the latest prices from WS state."""
    while not stop.is_set():
        dt, market_id, yes_price, no_price, _ = client.snapshot()
        btc_binance = btc_feed.get_btc_price()
        btc_cb = coinbase_feed.get_coinbase_price()
        btc_kr = kraken_feed.get_kraken_price()
        storage.insert_tick(conn, dt, market_id, yes_price, no_price, btc_binance, btc_cb, btc_kr)
        storage.write_latest_tick(
            dt, market_id, yes_price, no_price, btc_binance, btc_cb, btc_kr,
            open_btc_binance, open_btc_coinbase, open_btc_kraken, ema_flags,
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.tick_interval_seconds)
        except asyncio.TimeoutError:
            pass


async def _fetch_market_info_with_retry(
    slug_prefix: str, candle_ts: int, max_attempts: int = 3
) -> MarketInfo:
    last_exc: Exception = RuntimeError("no attempts")
    for attempt in range(1, max_attempts + 1):
        try:
            return await asyncio.to_thread(fetch_market_info, slug_prefix, candle_ts)
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "Market info fetch attempt %d/%d failed for ts=%d: %s",
                attempt, max_attempts, candle_ts, exc,
            )
            if attempt < max_attempts:
                await asyncio.sleep(5)
    raise RuntimeError(f"Failed to fetch market info for ts={candle_ts}") from last_exc


async def _fetch_open_prices_post_boundary(
    candle_ts: int, max_attempts: int = 5
) -> tuple[float | None, float | None, float | None, dict]:
    """Fetch open prices + EMA flags for a candle that has just started. Retries until at least
    one exchange returns a value (exchanges need a second or two to register the new candle)."""
    from market import _EMA_FLAGS_NONE
    result: tuple[float | None, float | None, float | None, dict] = (None, None, None, dict(_EMA_FLAGS_NONE))
    for attempt in range(max_attempts):
        if attempt > 0:
            await asyncio.sleep(1)
        result = await asyncio.to_thread(fetch_open_prices, candle_ts)
        binance, coinbase, kraken, _flags = result
        if binance is not None:
            return result
        logger.debug("Binance open not yet available for ts=%d (attempt %d/%d)", candle_ts, attempt + 1, max_attempts)
    logger.warning("Binance open still N/A after %d attempts for ts=%d — using partial result", max_attempts, candle_ts)
    return result


async def _await_gamma_resolution(
    candle_ts: int, slug_prefix: str, candle_end: datetime
) -> bool | None:
    """Poll Gamma API at 1, 2, 3, 4, and 4:50 minutes after candle_end."""
    poll_offsets_s = [60, 120, 180, 240, 290]
    for offset in poll_offsets_s:
        sleep_s = (candle_end.timestamp() + offset) - time.time()
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)
        result = await asyncio.to_thread(fetch_gamma_resolution, candle_ts, slug_prefix)
        logger.info("Gamma poll +%ds: %s", offset, result)
        if result is not None:
            return result
    logger.warning("Gamma unresolved after all polls for candle_ts=%d", candle_ts)
    return None


async def _finalize_candle(
    conn,
    candle_ts: int,
    slug_prefix: str,
    market_id: str,
    candle_start: datetime,
    candle_end: datetime,
    open_btc: tuple[float | None, float | None, float | None],
    ema_flags: dict,
) -> None:
    """Fetch resolution and write parquet for a completed candle. Runs as a background task.
    Exports only once resolution is confirmed; falls back to Binance REST if gamma never resolves."""
    try:
        gamma_res = await _await_gamma_resolution(candle_ts, slug_prefix, candle_end)

        if gamma_res is None:
            binance_res = await asyncio.to_thread(fetch_binance_resolution, candle_ts)
            if binance_res is None:
                logger.error(
                    "No resolution available for market %s — skipping export", market_id
                )
                return
        else:
            binance_res = await asyncio.to_thread(fetch_binance_resolution, candle_ts)

        logger.info("Resolution — gamma=%s  binance=%s", gamma_res, binance_res)

        path = storage.export_batch(
            conn, candle_start, candle_end, market_id,
            open_btc, gamma_res, binance_res, ema_flags,
        )
        if path:
            s3_sync.save(path)
    except Exception:
        logger.exception("Finalization failed for market %s candle_ts=%d", market_id, candle_ts)


async def candle_loop(conn, global_stop: asyncio.Event, market: dict) -> None:
    slug_prefix: str = market["slug_prefix"]
    interval_s = settings.candle_interval_minutes * 60

    # Bootstrap: fetch market info for the currently active candle.
    now_ts = time.time()
    candle_ts = int(now_ts) - (int(now_ts) % interval_s)
    next_market_info: MarketInfo | None = None  # set by end-of-candle pre-fetch

    while not global_stop.is_set():
        # Use pre-fetched market info if available, otherwise fetch now (bootstrap or after error).
        if next_market_info is not None:
            market_info = next_market_info
            next_market_info = None
        else:
            try:
                market_info = await _fetch_market_info_with_retry(slug_prefix, candle_ts)
            except Exception:
                logger.exception("Market info fetch failed for ts=%d — retrying in 10s", candle_ts)
                try:
                    await asyncio.wait_for(global_stop.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass
                continue

        candle_start = datetime.fromtimestamp(candle_ts, tz=timezone.utc)
        candle_end = datetime.fromtimestamp(candle_ts + interval_s, tz=timezone.utc)
        next_candle_ts = candle_ts + interval_s

        # Connect to WS immediately — market info already fetched.
        client = PolymarketWSClient(market_info)
        ws_task = asyncio.create_task(client.run(), name=f"ws_{market['name']}")

        # Fetch open prices in parallel with WS connection.
        # Both happen concurrently; candle is active so REST APIs have the data.
        open_btc_task = asyncio.create_task(
            _fetch_open_prices_post_boundary(candle_ts),
            name=f"open_prices_{market['name']}",
        )

        # Wait for first price event and open prices before recording ticks.
        try:
            await asyncio.wait_for(client.ready.wait(), timeout=30)
            logger.info("WS ready for market %s", market_info.market_id)
        except asyncio.TimeoutError:
            logger.warning(
                "WS not ready within 30s for market %s — starting tick loop anyway",
                market_info.market_id,
            )

        open_btc_binance, open_btc_coinbase, open_btc_kraken, ema_flags = await open_btc_task
        open_btc = (open_btc_binance, open_btc_coinbase, open_btc_kraken)

        if global_stop.is_set():
            client.stop()
            await asyncio.gather(ws_task, open_btc_task, return_exceptions=True)
            break

        # Tick loop.
        iter_stop = asyncio.Event()
        tick_task = asyncio.create_task(
            tick_loop(client, conn, iter_stop, open_btc_binance, open_btc_coinbase, open_btc_kraken, ema_flags),
            name=f"tick_{market['name']}",
        )

        sleep_s = (candle_end - datetime.now(tz=timezone.utc)).total_seconds()
        logger.info(
            "Candle %s active — %.0fs remaining until %s",
            market_info.market_id, max(0.0, sleep_s), candle_end.strftime("%H:%M:%S"),
        )

        # Phase 1: tick until _PREFETCH_BEFORE_S seconds before boundary.
        sleep_before_prefetch = max(0.0, sleep_s - _PREFETCH_BEFORE_S)
        if sleep_before_prefetch > 0:
            try:
                await asyncio.wait_for(global_stop.wait(), timeout=sleep_before_prefetch)
            except asyncio.TimeoutError:
                pass

        if global_stop.is_set():
            iter_stop.set()
            ws_task.cancel()
            await asyncio.gather(ws_task, tick_task, return_exceptions=True)
            break

        # Phase 2: pre-fetch NEXT market info while last _PREFETCH_BEFORE_S seconds tick.
        # Open prices are NOT fetched here — the next candle hasn't started yet.
        logger.info("Pre-fetching next market info (ts=%d)", next_candle_ts)
        prefetch_task = asyncio.create_task(
            _fetch_market_info_with_retry(slug_prefix, next_candle_ts),
            name=f"prefetch_{market['name']}",
        )

        remaining = (candle_end - datetime.now(tz=timezone.utc)).total_seconds()
        if remaining > 0:
            try:
                await asyncio.wait_for(global_stop.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass  # Normal: boundary reached

        # --- BOUNDARY: stop old candle ---
        # Cancel ws_task instead of waiting for it to drain — the async-for-in-ws loop
        # blocks until the next message arrives (up to ~10s), so cancellation is the only
        # way to exit immediately. tick_task stops on its own within tick_interval_seconds.
        iter_stop.set()
        ws_task.cancel()
        await asyncio.gather(ws_task, tick_task, return_exceptions=True)

        if global_stop.is_set():
            prefetch_task.cancel()
            await asyncio.gather(prefetch_task, return_exceptions=True)
            break

        # Get pre-fetched next market info.
        try:
            next_market_info = await asyncio.wait_for(prefetch_task, timeout=30)
            logger.info("Boundary — connecting immediately to %s", next_market_info.market_id)
        except Exception:
            logger.exception("Market info pre-fetch failed for ts=%d — will re-fetch", next_candle_ts)
            next_market_info = None

        # Kick off finalization for the OLD candle in the background so the new market
        # connection starts immediately rather than waiting 30s for gamma resolution.
        asyncio.create_task(
            _finalize_candle(
                conn, candle_ts, slug_prefix, market_info.market_id,
                candle_start, candle_end, open_btc, ema_flags,
            ),
            name=f"finalize_{market['name']}",
        )

        candle_ts = next_candle_ts
        # Loop continues: open prices fetched at top (candle now active, REST APIs have data).


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
