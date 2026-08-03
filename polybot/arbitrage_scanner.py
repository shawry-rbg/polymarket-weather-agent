"""
Arbitrage Scanner for Polymarket Temperature Markets.

Detects risk-free arbitrage opportunities across temperature buckets.
If sum of YES prices across ALL buckets < $1.00, buying all buckets
guarantees a $1.00 payout regardless of outcome.

    ArbScanner:
        - run_continuous(): scan every N seconds
        - run_once(): single scan

    Standalone functions:
        scan_arbitrage_opportunities(city, date_str)
        find_arbitrage_gaps(cities)
        execute_arb_bundle(opportunity, trade_func)
        log_arb(result, log_path)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from polybot.polymarket import (
    find_markets,
    get_market_price,
    parse_outcome_prices,
    BASE_URL,
    REQUEST_TIMEOUT,
)
from polybot.cities import global_cities

logger = logging.getLogger(__name__)

# Threshold for full-bundle arbitrage detection
# If sum of YES prices < ARB_THRESHOLD, we can guarantee profit
ARB_THRESHOLD = 0.98

# Threshold for near-certain outcome (single bucket)
NEAR_CERTAIN_THRESHOLD = 0.98

# Default log path
DEFAULT_ARB_LOG = "/polybot-data/arb_log.jsonl"


# ---------------------------------------------------------------------------
# Standalone async functions
# ---------------------------------------------------------------------------


async def scan_arbitrage_opportunities(
    city: str,
    date_str: str = "",
) -> list[dict]:
    """
    Fetch all temperature buckets for a city from Polymarket Gamma API
    and detect arbitrage opportunities.

    Two types of arbitrage detected:
    1. FULL_BUNDLE: Sum of YES prices across ALL buckets < ARB_THRESHOLD.
       Buying all buckets = guaranteed $1.00 payout for < $ARB_THRESHOLD cost.
    2. NEAR_CERTAIN: Any single bucket has YES price > NEAR_CERTAIN_THRESHOLD.
       Outcome is near-certain at a favorable price.

    Args:
        city: City name (e.g. "London", "New York")
        date_str: Optional date string to filter markets (e.g. "May 30")

    Returns:
        List of arb opportunity dicts with keys:
            - city: city name
            - arb_type: "FULL_BUNDLE" or "NEAR_CERTAIN"
            - buckets: list of bucket info {question, threshold_f, yes_price}
            - total_cost: sum of YES prices (for FULL_BUNDLE)
            - guaranteed_payout: always 1.00
            - profit_margin: guaranteed_payout - total_cost
            - profit_margin_pct: profit as percentage of cost
            - timestamp: ISO timestamp of scan
    """
    logger.info(f"[ARB_SCANNER] Scanning {city} for arb opportunities (date={date_str or 'any'})")
    opportunities: list[dict] = []

    # Step 1: Fetch all temperature markets for this city
    markets = await find_markets(city_name=city, date_str=date_str or None)

    if not markets:
        logger.info(f"[ARB_SCANNER] No temperature markets found for {city}")
        return []

    # Step 2: Extract prices for each bucket
    buckets: list[dict] = []
    for market in markets:
        question = market.get("question", "")
        try:
            yes_price, no_price = parse_outcome_prices(market)
        except (ValueError, json.JSONDecodeError) as e:
            logger.debug(f"Could not parse prices for '{question[:60]}': {e}")
            continue

        threshold_f = market.get("threshold_f")
        if threshold_f is None:
            # Try to extract from question
            from polybot.polymarket import _extract_threshold
            threshold_f = _extract_threshold(question)

        buckets.append(
            {
                "question": question,
                "threshold_f": threshold_f,
                "yes_price": yes_price,
                "no_price": no_price,
                "volume24hr": float(market.get("volume24hr", 0) or 0),
                "conditionId": market.get("conditionId", ""),
                "market_id": str(market.get("id", "")),
            }
        )

    if len(buckets) < 2:
        logger.info(f"[ARB_SCANNER] Only {len(buckets)} bucket(s) for {city}, need 2+")
        return []

    # Step 3: Sort by threshold
    buckets.sort(key=lambda b: b.get("threshold_f", 0))

    logger.info(f"[ARB_SCANNER] Found {len(buckets)} buckets for {city}")
    for b in buckets:
        logger.debug(
            f"  threshold={b['threshold_f']}F YES={b['yes_price']:.4f} "
            f"NO={b['no_price']:.4f} vol={b['volume24hr']:.0f}"
        )

    # Step 4: Check for FULL_BUNDLE arbitrage
    total_yes_cost = sum(b["yes_price"] for b in buckets)
    if total_yes_cost < ARB_THRESHOLD:
        profit = 1.0 - total_yes_cost  # $1.00 guaranteed payout
        opportunities.append(
            {
                "city": city,
                "arb_type": "FULL_BUNDLE",
                "date_str": date_str,
                "buckets": buckets,
                "num_buckets": len(buckets),
                "total_cost": round(total_yes_cost, 4),
                "guaranteed_payout": 1.0,
                "profit_margin": round(profit, 4),
                "profit_margin_pct": round(profit / total_yes_cost * 100, 2),
                "arb_threshold": ARB_THRESHOLD,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        logger.warning(
            f"[ARB_SCANNER] *** FULL_BUNDLE ARBITRAGE: {city} *** "
            f"total_cost=${total_yes_cost:.4f} profit=${profit:.4f} ({profit / total_yes_cost * 100:.1f}%)"
        )

    # Step 5: Check for NEAR_CERTAIN opportunities
    for b in buckets:
        if b["yes_price"] > NEAR_CERTAIN_THRESHOLD:
            profit = 1.0 - b["yes_price"]
            opportunities.append(
                {
                    "city": city,
                    "arb_type": "NEAR_CERTAIN",
                    "date_str": date_str,
                    "buckets": [b],
                    "num_buckets": 1,
                    "total_cost": round(b["yes_price"], 4),
                    "guaranteed_payout": 1.0,
                    "profit_margin": round(profit, 4),
                    "profit_margin_pct": round(profit / b["yes_price"] * 100, 2),
                    "arb_threshold": NEAR_CERTAIN_THRESHOLD,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            logger.warning(
                f"[ARB_SCANNER] *** NEAR_CERTAIN OPPORTUNITY: {city} *** "
                f"threshold={b['threshold_f']}F price={b['yes_price']:.4f} profit=${profit:.4f}"
            )

    return opportunities


async def find_arbitrage_gaps(
    cities: list[str] | None = None,
    date_str: str = "",
) -> list[dict]:
    """
    Scan multiple cities for arbitrage opportunities.

    Args:
        cities: List of city names. If None, uses all active cities from config.
        date_str: Optional date string to filter markets.

    Returns:
        List of arb opportunity dicts, sorted by profit_margin descending.
    """
    if cities is None:
        # Use all active (non-reserve) cities from config
        cities = [
            city_info["name"]
            for city_info in global_cities.values()
            if not city_info.get("reserve", False)
        ]

    logger.info(f"[ARB_SCANNER] Scanning {len(cities)} cities for arbitrage gaps")

    # Scan all cities concurrently
    tasks = [scan_arbitrage_opportunities(city, date_str) for city in cities]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Flatten results, skipping errors
    all_opportunities: list[dict] = []
    for city, result in zip(cities, results):
        if isinstance(result, Exception):
            logger.error(f"[ARB_SCANNER] Error scanning {city}: {result}")
        elif isinstance(result, list):
            all_opportunities.extend(result)

    # Sort by profit_margin descending
    all_opportunities.sort(key=lambda opp: opp.get("profit_margin", 0), reverse=True)

    logger.info(
        f"[ARB_SCANNER] Found {len(all_opportunities)} total arb opportunity/ies "
        f"across {len(cities)} cities"
    )

    return all_opportunities


async def execute_arb_bundle(
    opportunity: dict,
    trade_func: Callable,
    max_cost: float = 1.0,
) -> dict:
    """
    Execute trades for an arbitrage opportunity by buying all buckets.

    Args:
        opportunity: Arb opportunity dict from scan_arbitrage_opportunities()
        trade_func: Async callable with signature:
            async def trade_func(condition_id: str, side: str, price: float, size: float) -> dict
        max_cost: Maximum total cost to execute. Skip if total_cost > max_cost.

    Returns:
        Execution result dict with:
            - opportunity: the original opportunity dict
            - executed_trades: list of trade results
            - total_spent: actual total spent
            - status: "executed" | "skipped" | "partial" | "failed"
            - timestamp: ISO timestamp
            - error: error message if failed
    """
    city = opportunity.get("city", "unknown")
    arb_type = opportunity.get("arb_type", "FULL_BUNDLE")
    buckets = opportunity.get("buckets", [])
    total_cost = opportunity.get("total_cost", 0)

    logger.info(
        f"[ARB_EXECUTOR] Executing {arb_type} arb for {city}: "
        f"{len(buckets)} buckets, expected_cost=${total_cost:.4f}"
    )

    # Check max_cost
    if total_cost > max_cost:
        result = {
            "opportunity": opportunity,
            "executed_trades": [],
            "total_spent": 0.0,
            "status": "skipped",
            "reason": f"total_cost ${total_cost:.4f} > max_cost ${max_cost:.4f}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        log_arb(result)
        logger.info(f"[ARB_EXECUTOR] Skipped (cost ${total_cost:.4f} > max ${max_cost:.4f})")
        return result

    # Execute trades for each bucket
    executed_trades: list[dict] = []
    total_spent = 0.0
    errors: list[str] = []

    for bucket in buckets:
        condition_id = bucket.get("conditionId") or bucket.get("market_id", "")
        yes_price = bucket.get("yes_price", 0)

        if not condition_id:
            errors.append(f"No conditionId for {bucket.get('question', 'unknown')[:60]}")
            continue

        if yes_price <= 0 or yes_price > 1:
            errors.append(f"Invalid price {yes_price} for {condition_id}")
            continue

        try:
            trade_result = await trade_func(
                condition_id=condition_id,
                side="YES",
                price=yes_price,
                size=1.0,  # $1.00 per bucket for guaranteed payout
            )

            trade_record = {
                "condition_id": condition_id,
                "question": bucket.get("question", "")[:80],
                "threshold_f": bucket.get("threshold_f"),
                "side": "YES",
                "price": yes_price,
                "size": 1.0,
                "result": trade_result,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            executed_trades.append(trade_record)

            if trade_result and trade_result.get("status") not in ("failed", None):
                total_spent += yes_price
                logger.info(
                    f"[ARB_EXECUTOR] ✓ Bought YES @ {yes_price:.4f} for "
                    f"{bucket.get('question', '')[:60]} (id={condition_id[:20]}...)"
                )
            else:
                errors.append(
                    f"Trade failed for {condition_id}: {trade_result}"
                )
                logger.warning(
                    f"[ARB_EXECUTOR] ✗ Trade failed for "
                    f"{bucket.get('question', '')[:60]}: {trade_result}"
                )

        except Exception as e:
            errors.append(f"Exception for {condition_id}: {e}")
            logger.error(
                f"[ARB_EXECUTOR] ✗ Exception executing trade for "
                f"{bucket.get('question', '')[:60]}: {e}"
            )

    # Determine status
    if not executed_trades:
        status = "failed"
    elif len(executed_trades) < len(buckets):
        status = "partial"
    elif errors:
        status = "partial"
    else:
        status = "executed"

    profit = 1.0 - total_spent if status in ("executed", "partial") else 0.0

    result = {
        "opportunity": opportunity,
        "executed_trades": executed_trades,
        "num_executed": len(executed_trades),
        "num_failed": len(buckets) - len(executed_trades),
        "total_spent": round(total_spent, 4),
        "guaranteed_payout": 1.0 if status == "executed" else 0.0,
        "expected_profit": round(profit, 4) if status == "executed" else 0.0,
        "status": status,
        "errors": errors if errors else None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Log the execution
    log_arb(result)

    logger.info(
        f"[ARB_EXECUTOR] {arb_type} execution for {city}: status={status} "
        f"spent=${total_spent:.4f} trades={len(executed_trades)}/{len(buckets)}"
    )

    return result


def log_arb(result: dict, log_path: str = DEFAULT_ARB_LOG) -> None:
    """
    Append an arbitrage result to the JSONL arbitrage log.

    Args:
        result: Execution result dict from execute_arb_bundle()
        log_path: Path to JSONL log file
    """
    try:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(result, default=str) + "\n")
        logger.debug(f"[ARB_LOG] Wrote arb result to {log_path}")
    except Exception as e:
        logger.error(f"[ARB_LOG] Failed to log arb result: {e}")


# ---------------------------------------------------------------------------
# ArbScanner class
# ---------------------------------------------------------------------------


class ArbScanner:
    """
    Continuous arbitrage scanner for Polymarket temperature markets.

    Runs periodic scans and optionally executes trades via a provided
    trade function.

    Usage:
        scanner = ArbScanner(scan_interval=300)
        await scanner.run_continuous(trade_func=my_trader, cities=["London", "NYC"])

        # Or single scan:
        results = await scanner.run_once(cities=["London"])
    """

    def __init__(self, scan_interval: int = 300) -> None:
        """
        Initialize the arb scanner.

        Args:
            scan_interval: Seconds between scans (default: 300 = 5 minutes)
        """
        self.scan_interval = scan_interval
        self._running = False
        self._scan_count = 0
        self._total_opportunities_found = 0
        self._total_trades_executed = 0
        self._last_scan_time: datetime | None = None
        self._last_results: list[dict] = []

    @property
    def stats(self) -> dict:
        """Return scanner statistics."""
        return {
            "scan_interval": self.scan_interval,
            "is_running": self._running,
            "scan_count": self._scan_count,
            "total_opportunities_found": self._total_opportunities_found,
            "total_trades_executed": self._total_trades_executed,
            "last_scan_time": self._last_scan_time.isoformat() if self._last_scan_time else None,
            "last_results_count": len(self._last_results),
        }

    async def run_once(
        self,
        trade_func: Callable | None = None,
        cities: list[str] | None = None,
        date_str: str = "",
        max_cost: float = 1.0,
        auto_execute: bool = False,
    ) -> list[dict]:
        """
        Run a single arbitrage scan across all specified cities.

        Args:
            trade_func: Optional trade execution function.
                Signature: async def trade_func(condition_id, side, price, size) -> dict
            cities: List of city names. Defaults to all active cities.
            date_str: Optional date filter string.
            max_cost: Max cost per arb bundle if auto-executing.
            auto_execute: If True and trade_func provided, execute arb opportunities.

        Returns:
            List of all execution results / opportunity dicts.
        """
        self._scan_count += 1
        self._last_scan_time = datetime.now(timezone.utc)

        logger.info(
            f"[ARB_SCANNER] Starting scan #{self._scan_count} "
            f"({cities or 'all active cities'})"
        )

        # Find arbitrage gaps
        opportunities = await find_arbitrage_gaps(cities=cities, date_str=date_str)
        self._total_opportunities_found += len(opportunities)
        self._last_results = opportunities

        if not opportunities:
            logger.info(f"[ARB_SCANNER] Scan #{self._scan_count}: No arb opportunities found")
            return []

        logger.info(
            f"[ARB_SCANNER] Scan #{self._scan_count}: "
            f"Found {len(opportunities)} arb opportunity/ies"
        )

        for opp in opportunities:
            logger.info(
                f"  [{opp['arb_type']}] {opp['city']}: "
                f"cost=${opp['total_cost']:.4f} profit=${opp['profit_margin']:.4f} "
                f"({opp['profit_margin_pct']:.1f}%)"
            )

        # Auto-execute if requested
        if auto_execute and trade_func:
            execution_results: list[dict] = []
            for opp in opportunities:
                if opp.get("profit_margin", 0) <= 0:
                    continue
                try:
                    result = await execute_arb_bundle(
                        opportunity=opp,
                        trade_func=trade_func,
                        max_cost=max_cost,
                    )
                    execution_results.append(result)
                    if result.get("status") in ("executed", "partial"):
                        self._total_trades_executed += result.get("num_executed", 0)
                except Exception as e:
                    logger.error(f"[ARB_SCANNER] Auto-execute error: {e}")
            return execution_results

        return opportunities

    async def run_continuous(
        self,
        trade_func: Callable | None = None,
        cities: list[str] | None = None,
        date_str: str = "",
        max_cost: float = 1.0,
        auto_execute: bool = False,
        max_scans: int = 0,
    ) -> None:
        """
        Run continuous arbitrage scanning in a loop.

        Args:
            trade_func: Optional trade execution function.
            cities: List of city names. Defaults to all active cities.
            date_str: Optional date filter string.
            max_cost: Max cost per arb bundle if auto-executing.
            auto_execute: If True and trade_func provided, execute arb opportunities.
            max_scans: Maximum number of scans (0 = unlimited).
        """
        self._running = True
        scan_num = 0

        logger.info(
            f"[ARB_SCANNER] Starting continuous scan: interval={self.scan_interval}s "
            f"cities={cities or 'all active'} auto_execute={auto_execute}"
        )

        try:
            while self._running:
                scan_num += 1

                if max_scans > 0 and scan_num > max_scans:
                    logger.info(f"[ARB_SCANNER] Reached max_scans={max_scans}, stopping")
                    break

                logger.info(f"[ARB_SCAN #{scan_num}] Starting...")
                t0 = time.monotonic()

                try:
                    await self.run_once(
                        trade_func=trade_func,
                        cities=cities,
                        date_str=date_str,
                        max_cost=max_cost,
                        auto_execute=auto_execute,
                    )
                except Exception as e:
                    logger.error(f"[ARB_SCANNER] Scan #{scan_num} error: {e}")

                elapsed = time.monotonic() - t0
                logger.info(
                    f"[ARB_SCAN #{scan_num}] Complete in {elapsed:.1f}s "
                    f"(total_opps={self._total_opportunities_found})"
                )

                # Sleep until next scan
                if self._running:
                    sleep_time = max(1, self.scan_interval - elapsed)
                    logger.debug(f"[ARB_SCANNER] Sleeping {sleep_time:.0f}s until next scan")
                    await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            logger.info("[ARB_SCANNER] Scan loop cancelled")
        finally:
            self._running = False
            logger.info(
                f"[ARB_SCANNER] Stopped after {scan_num} scans. "
                f"Total opps found: {self._total_opportunities_found} "
                f"Total trades executed: {self._total_trades_executed}"
            )

    def stop(self) -> None:
        """Signal the scanner to stop running continuously."""
        logger.info("[ARB_SCANNER] Stop requested")
        self._running = False


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

async def _main():
    """Quick test: scan all active cities for arb opportunities."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)7s] %(name)s: %(message)s",
    )

    print("=== Arbitrage Scanner Test ===\n")

    # Test single city
    print("--- Single city scan: London ---")
    london_opps = await scan_arbitrage_opportunities("London")
    print(f"Found {len(london_opps)} opportunities in London\n")

    for opp in london_opps:
        print(
            f"  Type: {opp['arb_type']} | Cost: ${opp['total_cost']:.4f} | "
            f"Profit: ${opp['profit_margin']:.4f} ({opp['profit_margin_pct']:.1f}%)"
        )
        print(f"  Buckets: {opp['num_buckets']}")
        for b in opp.get("buckets", [])[:5]:
            print(f"    - {b['threshold_f']}F: YES={b['yes_price']:.4f} NO={b['no_price']:.4f}")

    # Test multi-city scan
    print("\n--- Multi-city scan (top 5 active cities) ---")
    test_cities = ["London", "New York", "Seoul", "Hong Kong", "Shanghai"]
    all_opps = await find_arbitrage_gaps(test_cities)
    print(f"Found {len(all_opps)} total opportunities\n")

    for opp in all_opps[:5]:
        print(
            f"  [{opp['arb_type']}] {opp['city']}: "
            f"${opp['total_cost']:.4f} -> ${opp['guaranteed_payout']:.2f} "
            f"(+${opp['profit_margin']:.4f})"
        )

    # Test ArbScanner class
    print("\n--- ArbScanner test (single scan) ---")
    scanner = ArbScanner(scan_interval=60)
    results = await scanner.run_once(cities=["London"])
    print(f"Scanner stats: {scanner.stats}")
    print(f"Results: {len(results)} opportunities")


if __name__ == "__main__":
    asyncio.run(_main())
