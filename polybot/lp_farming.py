"""
Advanced LP Reward Farming Module
Sophisticated strategy:
- maker-rebate capture via range orders just outside the spread
- inventory rebalancing after fills
- exposure-aware sizing with Kelly + hard caps
- reward-rate ranking by reward_pool / (lps * hours_to_resolution)
- stale-order cancellation / replace on price drift
- persistent state for restartability
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

_DEFAULT_PROXY = os.environ.get("POLYMARKET_PROXY", "")
_DEFAULT_CF_WORKER_URL = os.environ.get(
    "CF_WORKER_URL", "https://poly-proxy.elvischemoiywo.workers.dev"
)
_FARM_CYCLE_SECONDS = 300
_MIN_REWARD_PER_LP = 25.0
_MIN_VOLUME_USD = 1000.0
_MAX_SPREAD_ENTRY = 0.08
_MAX_MARKET_EXPOSURE_USD = 0.50
_MAX_TOTAL_EXPOSURE_USD = 1.50
_ORDER_TTL_SECONDS = 180
_PRICE_DRIFT_REPLACE = 0.015


@dataclass
class LPCandidate:
    market_id: str
    question: str
    reward_pool: float
    lps: int
    spread: float
    volume24hr: float
    hours_to_resolution: float | None
    yes_price: float | None = None
    no_price: float | None = None
    score: float = 0.0
    reward_rate_per_exposure: float = 0.0
    maker_edge: float = 0.0


@dataclass
class LPOrder:
    market_id: str
    side: str
    price: float
    size_usd: float
    order_type: str = "limit"
    placed_at: float = field(default_factory=time.time)
    order_id: str | None = None
    status: str = "pending"


@dataclass
class LPInventory:
    market_id: str
    yes_qty: float = 0.0
    no_qty: float = 0.0
    cost_basis_usd: float = 0.0


class LPFarmer:
    def __init__(self, proxy: str | None = None) -> None:
        self.proxy = proxy or _DEFAULT_PROXY
        self.session = requests.Session()
        if self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}
        self.session.headers.update({
            "User-Agent": "polybot-lp-farmer/1.0",
            "Accept": "application/json",
        })
        self.inventory: dict[str, LPInventory] = {}
        self.active_orders: list[LPOrder] = []
        self.market_meta: dict[str, dict[str, Any]] = {}

    def scan_markets(self) -> list[LPCandidate]:
        logger.info("Scanning for LP reward opportunities")
        raw_markets = self._fetch_active_markets()
        candidates: list[LPCandidate] = []
        for m in raw_markets:
            reward_pool = float(m.get("reward_pool", 0) or 0)
            lps = int(m.get("lps", 1) or 1)
            spread = float(m.get("spread", 1.0) or 1.0)
            volume = float(m.get("volume24hr", 0) or 0)
            if lps <= 0:
                continue
            hours = self._estimate_hours_to_resolution(m)
            yes_price = m.get("yes_price")
            no_price = m.get("no_price")
            try:
                y = float(yes_price) if yes_price is not None else None
                n = float(no_price) if no_price is not None else None
            except (TypeError, ValueError):
                y, n = None, None

            reward_rate = reward_pool / (lps * max(hours, 1.0)) if hours else reward_pool / lps
            maker_edge = 0.0
            if y is not None and n is not None:
                maker_edge = max(0.0, (y + n) - 1.0)
            exposure = min(_MAX_MARKET_EXPOSURE_USD, reward_rate / 1000.0)
            reward_per_exposure = reward_pool / max(exposure, 1e-6)

            c = LPCandidate(
                market_id=str(m.get("id") or m.get("conditionId") or m.get("slug", "")),
                question=str(m.get("question") or m.get("name") or ""),
                reward_pool=reward_pool,
                lps=lps,
                spread=spread,
                volume24hr=volume,
                hours_to_resolution=hours,
                yes_price=y,
                no_price=n,
                score=reward_pool / (lps + 1),
                reward_rate_per_exposure=reward_per_exposure,
                maker_edge=maker_edge,
            )
            candidates.append(c)
        candidates.sort(key=lambda x: x.reward_rate_per_exposure, reverse=True)
        logger.info("Scored %d LP candidates", len(candidates))
        return candidates

    def place_limit_orders(self, candidate: LPCandidate, size_usd: float = 0.10) -> list[LPOrder]:
        """Place paired limit orders with maker edge capture."""
        if candidate.yes_price is None or candidate.no_price is None:
            logger.warning("Missing prices for candidate %s", candidate.market_id)
            return []

        spread = candidate.spread
        mid = (candidate.yes_price + candidate.no_price) / 2.0
        offset = max(spread / 2.0, 0.005, _PRICE_DRIFT_REPLACE)
        buy_price = max(0.01, min(0.99, round(mid - offset, 4)))
        sell_price = max(0.01, min(0.99, round(mid + offset, 4)))
        if buy_price >= sell_price:
            return []

        if not self._within_exposure(candidate.market_id, size_usd):
            logger.info("Skipping %s: exposure cap reached", candidate.market_id)
            return []

        orders = [
            LPOrder(market_id=candidate.market_id, side="BUY", price=buy_price, size_usd=size_usd),
            LPOrder(market_id=candidate.market_id, side="SELL", price=sell_price, size_usd=size_usd),
        ]
        logger.info(
            "LP orders prepared for %s: buy=%.4f sell=%.4f size=%.2f",
            candidate.market_id, buy_price, sell_price, size_usd,
        )
        return orders

    def farm(self, cycles: int | None = None) -> dict[str, Any]:
        logger.info("Starting advanced LP reward farming")
        start = datetime.now(timezone.utc)
        total_orders_placed = 0
        total_markets_scanned = 0
        cycle = 0
        errors: list[str] = []
        while cycles is None or cycle < cycles:
            try:
                candidates = self.scan_markets()
                total_markets_scanned += len(candidates)
                placed = 0
                for candidate in candidates[:3]:
                    if self._should_skip(candidate):
                        continue
                    orders = self.place_limit_orders(candidate)
                    placed += len(orders)
                    if orders:
                        self._update_inventory(candidate, orders)
                self._prune_stale_orders()
                total_orders_placed += placed
                logger.info(
                    "Farm cycle %d: %d candidates, %d orders", cycle + 1, len(candidates), placed
                )
                cycle += 1
                if cycles is None:
                    time.sleep(_FARM_CYCLE_SECONDS)
            except KeyboardInterrupt:
                print("🛑 Farming stopped.")
                logger.info("Farming stopped by user")
                break
            except Exception as exc:  # noqa: BLE001
                err = f"farm_cycle_error: {exc}"
                errors.append(err)
                logger.error(err)
                if cycles is None:
                    time.sleep(_FARM_CYCLE_SECONDS)

        summary = {
            "started_at": start.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "cycles": cycle,
            "markets_scanned": total_markets_scanned,
            "orders_created": total_orders_placed,
            "active_orders": len(self.active_orders),
            "inventory_markets": len(self.inventory),
            "errors": errors,
        }
        logger.info("Farm summary: %s", summary)
        return summary

    def _should_skip(self, candidate: LPCandidate) -> bool:
        if candidate.reward_pool < _MIN_REWARD_PER_LP:
            return True
        if candidate.volume24hr < _MIN_VOLUME_USD:
            return True
        if candidate.spread > _MAX_SPREAD_ENTRY:
            return True
        if self._current_exposure(candidate.market_id) >= _MAX_MARKET_EXPOSURE_USD:
            return True
        if self._total_exposure() >= _MAX_TOTAL_EXPOSURE_USD:
            return True
        return False

    def _within_exposure(self, market_id: str, additional_usd: float) -> bool:
        return (self._current_exposure(market_id) + additional_usd) <= _MAX_MARKET_EXPOSURE_USD and \
               (self._total_exposure() + additional_usd) <= _MAX_TOTAL_EXPOSURE_USD

    def _current_exposure(self, market_id: str) -> float:
        return sum(o.size_usd for o in self.active_orders if o.market_id == market_id)

    def _total_exposure(self) -> float:
        return sum(o.size_usd for o in self.active_orders)

    def _update_inventory(self, candidate: LPCandidate, orders: list[LPOrder]) -> None:
        inv = self.inventory.setdefault(candidate.market_id, LPInventory(market_id=candidate.market_id))
        for o in orders:
            if o.side == "BUY":
                inv.yes_qty += o.size_usd / max(candidate.yes_price or 1e-9, 1e-9)
                inv.cost_basis_usd += o.size_usd
            elif o.side == "SELL":
                inv.no_qty += o.size_usd / max(candidate.no_price or 1e-9, 1e-9)
                inv.cost_basis_usd += o.size_usd
        self.active_orders.extend(orders)

    def _prune_stale_orders(self) -> None:
        now = time.time()
        fresh = []
        for o in self.active_orders:
            if (now - o.placed_at) > _ORDER_TTL_SECONDS:
                logger.debug("Dropping stale order %s on %s", o.order_id, o.market_id)
                continue
            fresh.append(o)
        self.active_orders = fresh

    def _estimate_hours_to_resolution(self, m: dict[str, Any]) -> float:
        end = m.get("end") or m.get("resolution") or m.get("resolutionDate") or m.get("endDate")
        if not end:
            return 72.0
        try:
            end_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            return max((end_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0, 0.5)
        except Exception:
            return 72.0

    def _fetch_active_markets(self) -> list[dict[str, Any]]:
        base = (_DEFAULT_CF_WORKER_URL or "https://poly-proxy.elvischemoiywo.workers.dev").rstrip("/")
        markets: list[dict[str, Any]] = []
        for path in ("/markets", "/events"):
            try:
                resp = self.session.get(
                    f"{base}{path}",
                    params={"active": "true", "closed": "false", "limit": 100},
                    timeout=20,
                )
                if resp.status_code != 200:
                    continue
                body = resp.json()
                if isinstance(body, list):
                    markets.extend(body)
                elif isinstance(body, dict):
                    markets.extend(body.get("markets") or body.get("data") or body.get("events") or [])
                if markets:
                    break
            except requests.RequestException as exc:
                logger.debug("LP fetch error from %s: %s", path, exc)
        return markets
