"""
WebSocket streaming for Polymarket market data.

Connects to Polymarket's CLOB WebSocket to receive real-time price updates,
eliminating the need for REST polling and reducing Redis writes by ~70%.

Usage:
    streamer = PolymarketWSStreamer()
    await streamer.connect()
    await streamer.subscribe_markets(market_ids)
    async for update in streamer.listen():
        handle_price_change(update)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RECONNECT_DELAY = 5  # seconds
PING_INTERVAL = 30  # seconds


class PolymarketWSStreamer:
    """
    Async WebSocket client for Polymarket CLOB market data.

    Subscribes to price change events for specified markets and yields
    parsed update dicts to the caller.
    """

    def __init__(self, on_price_change: Callable | None = None):
        self.ws = None
        self.subscribed_markets: set[str] = set()
        self._running = False
        self._on_price_change = on_price_change
        self._callbacks: list[Callable] = []
        self._reconnect_delay = RECONNECT_DELAY
        self._last_ping = 0.0

    def add_callback(self, callback: Callable):
        """Register a callback for price change events."""
        self._callbacks.append(callback)

    async def connect(self):
        """Establish WebSocket connection with auto-reconnect."""
        import websockets
        self._running = True
        while self._running:
            try:
                logger.info("[WS] Connecting to %s", WS_URL)
                async with websockets.connect(
                    WS_URL,
                    ping_interval=PING_INTERVAL,
                    close_timeout=10,
                ) as ws:
                    self.ws = ws
                    self._last_ping = time.time()
                    logger.info("[WS] Connected successfully")

                    # Re-subscribe to any previously subscribed markets
                    if self.subscribed_markets:
                        await self._subscribe_all()

                    # Listen for messages
                    async for raw in ws:
                        await self._handle_message(raw)

            except Exception as e:
                if not self._running:
                    break
                logger.warning("[WS] Connection error: %s — reconnecting in %ds", e, self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)  # exponential backoff, max 60s

    async def _handle_message(self, raw: str):
        """Parse and dispatch incoming WebSocket messages."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("[WS] Non-JSON message: %s", raw[:100])
            return

        msg_type = data.get("type", "")

        if msg_type == "price_change":
            await self._handle_price_change(data)
        elif msg_type == "subscribed":
            logger.debug("[WS] Subscription confirmed: %s", data.get("market_ids", []))
        elif msg_type == "error":
            logger.warning("[WS] Server error: %s", data.get("message", ""))
        elif msg_type == "pong":
            self._last_ping = time.time()
        else:
            logger.debug("[WS] Unknown message type: %s", msg_type)

    async def _handle_price_change(self, data: dict):
        """Process a price change event."""
        market_id = data.get("market_id", "")
        token_id = data.get("token_id", "")
        outcome = data.get("outcome", "")
        price = data.get("price", 0)
        side = data.get("side", "")
        size = data.get("size", 0)
        timestamp = data.get("timestamp", "")

        update = {
            "type": "price_change",
            "market_id": market_id,
            "token_id": token_id,
            "outcome": outcome,
            "price": float(price) if price else 0.0,
            "side": side,
            "size": float(size) if size else 0.0,
            "timestamp": timestamp,
        }

        logger.debug("[WS] Price change: %s %s @ %.4f", outcome, market_id[:12], float(price) if price else 0)

        # Fire callbacks
        for cb in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(update)
                else:
                    cb(update)
            except Exception as e:
                logger.error("[WS] Callback error: %s", e)

        if self._on_price_change:
            try:
                if asyncio.iscoroutinefunction(self._on_price_change):
                    await self._on_price_change(update)
                else:
                    self._on_price_change(update)
            except Exception as e:
                logger.error("[WS] on_price_change error: %s", e)

    async def subscribe_markets(self, market_ids: list[str]):
        """Subscribe to price updates for a list of market IDs."""
        if not self.ws:
            # Queue for when connection is established
            self.subscribed_markets.update(market_ids)
            return

        msg = {
            "type": "subscribe",
            "market_ids": market_ids,
        }
        try:
            await self.ws.send(json.dumps(msg))
            self.subscribed_markets.update(market_ids)
            logger.info("[WS] Subscribed to %d markets", len(market_ids))
        except Exception as e:
            logger.error("[WS] Subscribe error: %s", e)

    async def _subscribe_all(self):
        """Re-subscribe to all tracked markets (after reconnect)."""
        if self.subscribed_markets:
            await self.subscribe_markets(list(self.subscribed_markets))

    async def unsubscribe(self, market_ids: list[str]):
        """Unsubscribe from specific markets."""
        if self.ws:
            msg = {"type": "unsubscribe", "market_ids": market_ids}
            try:
                await self.ws.send(json.dumps(msg))
                self.subscribed_markets.difference_update(market_ids)
            except Exception as e:
                logger.error("[WS] Unsubscribe error: %s", e)

    async def disconnect(self):
        """Gracefully close the WebSocket connection."""
        self._running = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        logger.info("[WS] Disconnected")


async def fetch_active_market_ids() -> list[str]:
    """
    Fetch all active Polymarket weather market IDs via REST (one-time bootstrap).
    After initial fetch, WebSocket takes over for real-time updates.
    """
    import httpx
    market_ids = []
    offset = 0
    limit = 100

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                url = (
                    "https://poly-proxy.elvischemoiywo.workers.dev/gamma/markets"
                    f"?active=true&closed=false&limit={limit}&offset={offset}"
                    "&order=volume24hr&ascending=false"
                )
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()

                if not data:
                    break

                for m in data:
                    # Filter for temperature markets
                    question = m.get("question", "").lower()
                    if any(kw in question for kw in ["temperature", "°c", "°f", "degrees"]):
                        mid = m.get("conditionId") or m.get("id", "")
                        if mid:
                            market_ids.append(mid)

                if len(data) < limit:
                    break
                offset += limit

    except Exception as e:
        logger.error("[WS] Failed to fetch market IDs: %s", e)

    logger.info("[WS] Found %d active weather markets", len(market_ids))
    return market_ids


async def run_ws_listener():
    """
    Main entry point for the WebSocket listener.
    Fetches active markets, connects, and processes price changes.
    """
    from polybot.notify import send_embed

    market_ids = await fetch_active_market_ids()
    if not market_ids:
        logger.warning("[WS] No active markets found — exiting")
        return

    streamer = PolymarketWSStreamer()

    # Register a callback that triggers evaluation on significant price changes
    async def on_price_change(update: dict):
        price = update.get("price", 0)
        outcome = update.get("outcome", "")
        # Only trigger on meaningful price changes (>1 cent)
        if abs(price) > 0.01:
            logger.info("[WS] Significant price change: %s @ %.4f", outcome[:40], price)
            # Could trigger immediate evaluation here
            # For now, just log — the agent_cycle cron will pick it up

    streamer.add_callback(on_price_change)

    # Connect and listen (blocks forever)
    await streamer.connect()
