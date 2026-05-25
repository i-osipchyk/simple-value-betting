"""
Real-time BTC/USD price feed from Kraken WebSocket v2.

Connects to wss://ws.kraken.com/v2, subscribes to the BTC/USD ticker channel,
and updates _current_kraken on every ticker update or snapshot.
"""

import asyncio
import json
import logging

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

_KRAKEN_WS_URL = "wss://ws.kraken.com/v2"
_SUBSCRIBE_MSG = json.dumps({
    "method": "subscribe",
    "params": {"channel": "ticker", "symbol": ["BTC/USD"]},
})
_current_kraken: float | None = None


def get_kraken_price() -> float | None:
    return _current_kraken


async def kraken_price_loop() -> None:
    global _current_kraken
    delay = 1.0
    while True:
        try:
            async with websockets.connect(_KRAKEN_WS_URL, ping_interval=30, ping_timeout=10) as ws:
                await ws.send(_SUBSCRIBE_MSG)
                logger.info("Connected to Kraken BTC/USD feed")
                delay = 1.0
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("channel") == "ticker":
                        data = msg.get("data", [])
                        if data and "last" in data[0]:
                            _current_kraken = float(data[0]["last"])
        except ConnectionClosed as exc:
            logger.warning("Kraken feed closed: %s — reconnecting in %.0fs", exc, delay)
        except OSError as exc:
            logger.warning("Kraken feed network error: %s — reconnecting in %.0fs", exc, delay)
        except Exception as exc:
            logger.error("Kraken feed error: %s — reconnecting in %.0fs", exc, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60.0)
