"""
Polymarket CLOB WebSocket client.

Protocol (wss://ws-subscriptions-clob.polymarket.com/ws/market):
  Subscribe to market prices:
      {"type": "market", "assets_ids": ["<token_id>", ...], "custom_feature_enabled": True}
  Subscribe to Chainlink crypto prices:
      {"type": "crypto_prices_chainlink"}

  Relevant inbound event_types:
    "best_bid_ask"     — top-of-book update; best_ask is the implied probability
    "last_trade_price" — last executed trade price (fallback)
    "price_change"     — mid-price update (fallback)
    "crypto_price"     — BTC/USD from Chainlink oracle
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from config import settings
from market import MarketInfo

logger = logging.getLogger(__name__)


def _parse_price(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@dataclass
class MarketState:
    """Latest prices seen over the WebSocket. None until first message arrives."""

    yes_price: float | None = None
    no_price: float | None = None
    btc_usd: float | None = None
    events_received: int = field(default=0, repr=False)


class PolymarketWSClient:
    def __init__(self, market_info: MarketInfo) -> None:
        self.market_info = market_info
        self.state = MarketState()
        self._stop_event = asyncio.Event()
        # Set when the first YES or NO price is received — tick loop waits on this.
        self.ready = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        """Connect and maintain the WebSocket until stop() is called."""
        delay = settings.reconnect_base_delay
        while not self._stop_event.is_set():
            try:
                await self._connect_and_consume()
                delay = settings.reconnect_base_delay
            except ConnectionClosed as exc:
                logger.warning("WebSocket closed: %s — reconnecting in %.1fs", exc, delay)
            except OSError as exc:
                logger.warning("Network error: %s — reconnecting in %.1fs", exc, delay)
            except Exception as exc:  # noqa: BLE001
                logger.error("Unexpected WS error: %s — reconnecting in %.1fs", exc, delay)

            if self._stop_event.is_set():
                break

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

            delay = min(delay * 2, settings.reconnect_max_delay)

    async def _connect_and_consume(self) -> None:
        ws_url = f"{settings.pm_ws_url}/ws/market"
        logger.info("Connecting to %s", ws_url)
        async with websockets.connect(
            ws_url,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=2,
        ) as ws:
            logger.info("Connected — market=%s", self.market_info.market_id)
            await self._subscribe(ws)
            async for raw in ws:
                if self._stop_event.is_set():
                    break
                self._handle_raw(raw)

    async def _subscribe(self, ws: Any) -> None:
        market_sub = json.dumps(
            {
                "type": "market",
                "assets_ids": list(self.market_info.asset_id_map.keys()),
                "custom_feature_enabled": True,  # enables best_bid_ask events
            }
        )
        crypto_sub = json.dumps({"type": "crypto_prices_chainlink"})
        await ws.send(market_sub)
        logger.debug("Subscribed to market channel with %d tokens", len(self.market_info.asset_id_map))
        await ws.send(crypto_sub)
        logger.debug("Subscribed to crypto_prices_chainlink")

    def _handle_raw(self, raw: str | bytes) -> None:
        if raw == "PONG":
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Non-JSON message ignored: %r", raw)
            return

        events: list[dict[str, Any]] = payload if isinstance(payload, list) else [payload]
        for event in events:
            self._handle_event(event)

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("event_type", "")
        asset_id = event.get("asset_id", "")
        outcome = self.market_info.asset_id_map.get(asset_id, "")

        if event_type == "best_bid_ask":
            # best_ask = the implied probability (price to buy the outcome)
            price = _parse_price(event.get("best_ask"))
            if price is None:
                return
            self.state.events_received += 1
            if outcome == "yes":
                self.state.yes_price = price
                self.ready.set()
            elif outcome == "no":
                self.state.no_price = price
                self.ready.set()

        elif event_type in ("last_trade_price", "price_change"):
            price = _parse_price(event.get("price"))
            if price is None:
                return
            self.state.events_received += 1
            if outcome == "yes":
                self.state.yes_price = price
                self.ready.set()
            elif outcome == "no":
                self.state.no_price = price
                self.ready.set()

        elif event_type == "crypto_price":
            if "BTC" in asset_id.upper():
                btc = _parse_price(event.get("price"))
                if btc is not None:
                    self.state.btc_usd = btc

    def snapshot(self) -> tuple[datetime, str, float | None, float | None, float | None]:
        """Return (utc_now, market_id, yes_price, no_price, btc_usd)."""
        return (
            datetime.now(tz=timezone.utc),
            self.market_info.market_id,
            self.state.yes_price,
            self.state.no_price,
            self.state.btc_usd,
        )
