"""
Trade engine entry point.

Runs three concurrent tasks:
  - uvicorn FastAPI server on port 8000
  - file watcher that retrains on each new parquet batch
  - inference loop that runs every second against latest_tick.json
"""

import asyncio
import logging
import signal
from pathlib import Path

import uvicorn

import history
import lookup
from api import app
from config import settings
from inference import inference_loop
from watcher import watch_and_resolve

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    raw_dir = Path(settings.local_data_dir) / "raw"
    await asyncio.to_thread(history.init, raw_dir)
    conn = history.get_connection()
    initial_table = await asyncio.to_thread(
        lookup.build_from_db, conn,
        30, 0.001, settings.candle_interval_minutes * 60,
    )
    lookup.set_table(initial_table)

    global_stop = asyncio.Event()

    def _shutdown(sig: signal.Signals) -> None:
        logger.info("Received %s — shutting down", sig.name)
        global_stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="warning")
    server = uvicorn.Server(config)

    watcher_task = asyncio.create_task(watch_and_resolve(), name="watcher")
    server_task = asyncio.create_task(server.serve(), name="uvicorn")

    infer_task: asyncio.Task | None = None

    async def _run_inference() -> None:
        nonlocal infer_task
        while not global_stop.is_set():
            infer_task = asyncio.create_task(inference_loop(), name="inference")
            try:
                await infer_task
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Inference loop crashed — restarting in 2s")
                await asyncio.sleep(2)

    supervisor_task = asyncio.create_task(_run_inference(), name="inference_supervisor")

    await global_stop.wait()

    server.should_exit = True
    watcher_task.cancel()
    supervisor_task.cancel()
    if infer_task and not infer_task.done():
        infer_task.cancel()
    await asyncio.gather(watcher_task, supervisor_task, server_task, return_exceptions=True)
    logger.info("Trade engine stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
