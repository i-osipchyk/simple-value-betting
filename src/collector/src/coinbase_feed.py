"""
Real-time BTC/USD price feed from Coinbase WebSocket.

Connects to wss://ws-feed.exchange.coinbase.com and subscribes to the BTC-USD
ticker channel. Updates _current_coinbase on every ticker message.
"""

import asyncio
import json
import logging

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

_COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
_SUBSCRIBE_MSG = json.dumps({
    "type": "subscribe",
    "channels": [{"name": "ticker", "product_ids": ["BTC-USD"]}],
})
_current_coinbase: float | None = None


def get_coinbase_price() -> float | None:
    return _current_coinbase


async def coinbase_price_loop() -> None:
    global _current_coinbase
    delay = 1.0
    while True:
        try:
            async with websockets.connect(_COINBASE_WS_URL, ping_interval=30, ping_timeout=10) as ws:
                await ws.send(_SUBSCRIBE_MSG)
                logger.info("Connected to Coinbase BTC-USD feed")
                delay = 1.0
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "ticker" and "price" in msg:
                        _current_coinbase = float(msg["price"])
        except ConnectionClosed as exc:
            logger.warning("Coinbase feed closed: %s — reconnecting in %.0fs", exc, delay)
        except OSError as exc:
            logger.warning("Coinbase feed network error: %s — reconnecting in %.0fs", exc, delay)
        except Exception as exc:
            logger.error("Coinbase feed error: %s — reconnecting in %.0fs", exc, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60.0)
