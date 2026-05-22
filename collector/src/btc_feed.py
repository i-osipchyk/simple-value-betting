"""
Real-time BTC/USD price feed from Binance WebSocket.

Connects to wss://stream.binance.com:9443/ws/btcusdt@aggTrade and updates
_current_btc on every aggregate trade. The collector tick loop calls
get_btc_price() each second to write an accurate BTC/USD value.
"""

import asyncio
import json
import logging

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

_BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
_current_btc: float | None = None


def get_btc_price() -> float | None:
    return _current_btc


async def btc_price_loop() -> None:
    global _current_btc
    delay = 1.0
    while True:
        try:
            async with websockets.connect(_BINANCE_WS_URL, ping_interval=30, ping_timeout=10) as ws:
                logger.info("Connected to Binance BTC/USDT feed")
                delay = 1.0
                async for raw in ws:
                    data = json.loads(raw)
                    _current_btc = float(data["p"])
        except ConnectionClosed as exc:
            logger.warning("Binance feed closed: %s — reconnecting in %.0fs", exc, delay)
        except OSError as exc:
            logger.warning("Binance feed network error: %s — reconnecting in %.0fs", exc, delay)
        except Exception as exc:
            logger.error("Binance feed error: %s — reconnecting in %.0fs", exc, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60.0)
