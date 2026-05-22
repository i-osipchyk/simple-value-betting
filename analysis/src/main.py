import asyncio
import logging

from inference import inference_loop
from watcher import watch_and_rebuild

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


async def main() -> None:
    logger.info("Starting analysis container")
    await asyncio.gather(
        watch_and_rebuild(),
        inference_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
