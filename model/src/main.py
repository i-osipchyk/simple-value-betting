"""
Model container entry point.

Runs three concurrent tasks:
  - uvicorn FastAPI server on port 8000
  - file watcher that retrains on each new parquet batch
  - inference loop that runs every second against latest_tick.json
"""

import asyncio
import logging
import signal

import uvicorn

from api import app
from inference import inference_loop
from watcher import watch_and_resolve

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
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
    infer_task = asyncio.create_task(inference_loop(), name="inference")
    server_task = asyncio.create_task(server.serve(), name="uvicorn")

    await global_stop.wait()

    server.should_exit = True
    watcher_task.cancel()
    infer_task.cancel()
    await asyncio.gather(watcher_task, infer_task, server_task, return_exceptions=True)
    logger.info("Model container stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
