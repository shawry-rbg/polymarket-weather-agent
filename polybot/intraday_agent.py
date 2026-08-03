"""
Intraday Agent - Real-time trading agent with event-driven architecture.

Combines live probability tracking, rebalancing, and ladder execution
into a single continuous loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class IntradayAgent:
    """
    Real-time intraday trading agent.

    Responsibilities:
    - Poll live temperatures every 5 minutes
    - Compute live probabilities
    - Trigger rebalancing when drift > 5%
    - Execute ladder strategy during GFS windows
    - Manage risk (stop-loss, take-profit, position limits)
    """

    def __init__(
        self,
        cities: list[dict],
        bankroll: float = 2.30,
        trade_func=None,
        prob_func=None,
        position_manager=None,
    ) -> None:
        self.cities = cities
        self.bankroll = bankroll
        self.trade_func = trade_func
        self.prob_func = prob_func
        self.position_manager = position_manager
        self._running = False
        self._paper_mode = False  # LIVE mode by default

    def set_paper_mode(self, paper: bool = True) -> None:
        """Set paper mode. Pass False for live trading."""
        self._paper_mode = paper

    async def _fetch_all_live_probs(self) -> dict:
        """Fetch live probabilities for all cities concurrently."""
        from polybot.live_prob import compute_live_probabilities

        async def _one(city_config):
            name = city_config["name"]
            try:
                return name, await compute_live_probabilities(
                    city=name,
                    lat=city_config["lat"],
                    lon=city_config["lon"],
                    buckets=city_config.get("buckets", list(range(20, 40))),
                )
            except Exception as e:
                logger.error(f"[{name}] Live prob error: {e}")
                return name, {"error": str(e), "city": name}

        tasks = [_one(c) for c in self.cities]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        probs = {}
        for r in results:
            if isinstance(r, tuple) and len(r) == 2:
                name, data = r
                probs[name] = data
        return probs

    async def _fetch_all_markets(self) -> dict:
        """Fetch Polymarket market data for all cities."""
        from polybot.polymarket import find_markets

        async def _one(city_config):
            name = city_config["name"]
            try:
                markets = await find_markets(city_name=name, date_str="")
                return name, {str(m.get("threshold_f", 0)): m for m in markets}
            except Exception as e:
                logger.debug(f"[{name}] Market fetch error: {e}")
                return name, {}

        tasks = [_one(c) for c in self.cities]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_markets = {}
        for r in results:
            if isinstance(r, tuple) and len(r) == 2:
                name, data = r
                if data:
                    all_markets[name] = data
        return all_markets

    async def _rebalance_step(self, live_probs: dict, all_markets: dict) -> list:
        """Execute one rebalancing step."""
        from polybot.rebalancer import rebalance_all, PositionManager

        if not self.trade_func:
            logger.debug("No trade function, skipping rebalance")
            return []

        pm = self.position_manager or PositionManager()

        if self._paper_mode:
            logger.info("[PAPER] Would rebalance, skipping execution")
            return []

        return await rebalance_all(
            live_results=live_probs,
            all_markets=all_markets,
            bankroll=self.bankroll,
            trade_func=self.trade_func,
            position_manager=pm,
        )

    async def _ladder_step(self, all_markets: dict) -> list:
        """Execute ladder strategy during GFS windows."""
        from polybot.ladder import is_gfs_window, get_tail_buckets, compute_ladder_allocation

        if not is_gfs_window():
            return []

        logger.info("[LADDER] In GFS window - scanning for tail opportunities")

        from polybot.prediction_engine import bayesian_temperature_probability

        trades = []
        for city_config in self.cities:
            name = city_config["name"]
            city_markets = all_markets.get(name, {})
            if not city_markets:
                continue

            # Estimate probs for each bucket
            edge_data = []
            for bucket_str, market in city_markets.items():
                yes_price = market.get("yes_price", 0.5)
                edge_data.append({
                    "bucket": bucket_str,
                    "prob": 0.5,  # Would be from live_probs
                    "price": yes_price,
                    "edge": abs(0.5 - yes_price),
                    "conditionId": market.get("conditionId", ""),
                })

            tail = get_tail_buckets([{"price": d["price"], "prob": d["prob"]} for d in edge_data])
            if not tail:
                continue

            allocation = compute_ladder_allocation(edge_data, self.bankroll)
            for alloc in allocation:
                if alloc["amount_usd"] > 0 and not self._paper_mode and self.trade_func:
                    try:
                        result = await self.trade_func(
                            alloc.get("conditionId", ""),
                            "YES",
                            alloc["price"],
                            alloc["amount_usd"],
                        )
                        trades.append({"city": name, "result": result})
                    except Exception as e:
                        logger.warning(f"[LADDER] Trade error: {e}")

        return trades

    async def run_once(self) -> dict:
        """Execute a single intraday scan cycle."""
        now = datetime.now(timezone.utc).isoformat()
        logger.info(f"[INTRADAY] Starting scan cycle at {now}")

        # 1. Fetch live probabilities
        live_probs = await self._fetch_all_live_probs()

        # 2. Fetch market data
        all_markets = await self._fetch_all_markets()

        # 3. Rebalance
        rebalance_trades = await self._rebalance_step(live_probs, all_markets)

        # 4. Ladder (only in GFS windows)
        ladder_trades = await self._ladder_step(all_markets)

        summary = {
            "timestamp": now,
            "n_cities_scanned": len(live_probs),
            "n_live_errors": sum(1 for v in live_probs.values() if "error" in v),
            "n_markets_found": sum(len(m) for m in all_markets.values()),
            "rebalance_trades": len(rebalance_trades),
            "ladder_trades": len(ladder_trades),
        }
        logger.info(f"[INTRADAY] Scan complete: {json.dumps(summary)}")
        return summary

    async def run_continuous(self, interval_seconds: int = 300) -> None:
        """
        Run continuous intraday trading loop.

        Args:
            interval_seconds: seconds between scans (default 5 min)
        """
        self._running = True
        logger.info(f"[INTRADAY] Starting continuous loop, interval={interval_seconds}s")
        logger.info(f"[INTRADAY] Paper mode: {self._paper_mode}")
        logger.info(f"[INTRADAY] Cities: {[c['name'] for c in self.cities]}")

        while self._running:
            try:
                await self.run_once()
            except Exception as e:
                logger.error(f"[INTRADAY] Cycle error: {e}", exc_info=True)

            await asyncio.sleep(interval_seconds)

    def stop(self) -> None:
        self._running = False
        logger.info("[INTRADAY] Stopped")
