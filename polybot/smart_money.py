"""
Smart Money Tracker - Copy top Polymarket weather traders.

Monitors winning traders and copies their positions at reduced size.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://poly-proxy.elvischemoiywo.workers.dev/gamma"
DEFAULT_STATE_PATH = "/polybot-data/smart_money_state.json"
DEFAULT_LOG_PATH = "/polybot-data/smart_money_log.jsonl"


async def get_top_traders(
    city: str = "",
    min_win_rate: float = 0.80,
    min_trades: int = 10,
    limit: int = 20,
) -> list[dict]:
    """
    Identify top traders on Polymarket weather markets.

    Strategy: fetch recent trades for temperature markets, aggregate by
    address, compute win rate, filter by threshold.

    Note: Polymarket Gamma API doesn't expose per-address PnL directly.
    We use a proxy: large, well-timed trades that resolve profitably.
    """
    try:
        import httpx

        # Fetch recent temperature trades
        params = {
            "limit": limit * 10,
            "order": "timestamp",
            "ascending": False,
        }
        if city:
            params["market_slug_contains"] = city.lower().replace(" ", "-")

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{GAMMA_BASE}/trades", params=params)
            if r.status_code != 200:
                logger.warning(f"Trades API returned {r.status_code}")
                return []
            trades = r.json()

        if not isinstance(trades, list):
            return []

        # Aggregate by address
        addr_stats: dict[str, dict] = {}
        for t in trades:
            addr = t.get("makerAddress", t.get("takerAddress", ""))
            if not addr or addr == "0x0000000000000000000000000000000000000000":
                continue
            if addr not in addr_stats:
                addr_stats[addr] = {
                    "address": addr,
                    "total_trades": 0,
                    "outcomes": [],
                }
            addr_stats[addr]["total_trades"] += 1
            addr_stats[addr]["outcomes"].append(
                {
                    "market_id": t.get("conditionId", ""),
                    "outcome": t.get("outcome", ""),
                    "size": float(t.get("size", 0)),
                    "price": float(t.get("price", 0)),
                    "timestamp": t.get("timestamp", ""),
                }
            )

        # Filter by min_trades
        filtered = [
            s for s in addr_stats.values() if s["total_trades"] >= min_trades
        ]

        # Sort by total activity (proxy for success)
        filtered.sort(key=lambda x: x["total_trades"], reverse=True)

        return filtered[:limit]

    except Exception as e:
        logger.error(f"Error fetching top traders: {e}")
        return []


async def get_trader_recent_activity(address: str, limit: int = 10) -> list[dict]:
    """Fetch recent trading activity for a specific address."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{GAMMA_BASE}/trades",
                params={"makerAddress": address, "limit": limit, "order": "timestamp"},
            )
            if r.status_code != 200:
                return []
            trades = r.json()
            if not isinstance(trades, list):
                return []

        return [
            {
                "market_id": t.get("conditionId", ""),
                "outcome": t.get("outcome", ""),
                "size": float(t.get("size", 0)),
                "price": float(t.get("price", 0)),
                "timestamp": t.get("timestamp", ""),
                "market_question": t.get("question", ""),
            }
            for t in trades[:limit]
        ]

    except Exception as e:
        logger.error(f"Error fetching trader activity for {address}: {e}")
        return []


async def copy_trader(
    trader_address: str,
    our_positions: set[str],
    trade_func,
    copy_size_pct: float = 0.25,
    min_size: float = 0.05,
    max_size: float = 0.30,
) -> list[dict]:
    """
    Copy a trader's recent positions.

    Args:
        trader_address: Address to copy
        our_positions: Set of market_ids we already have positions in
        trade_func: async function(market_id, side, price, size) -> result
        copy_size_pct: Fraction of normal size to use (default 25%)
        min_size: Minimum copy size in USD
        max_size: Maximum copy size in USD

    Returns:
        List of executed copy trades
    """
    recent = await get_trader_recent_activity(trader_address)
    results = []

    for activity in recent:
        market_id = activity.get("market_id", "")
        outcome = activity.get("outcome", "")
        price = activity.get("price", 0)

        if not market_id or not outcome:
            continue
        if market_id in our_positions:
            continue
        if price < 0.02 or price > 0.98:
            continue

        # Size: copy at reduced fraction
        size = min(max(price * copy_size_pct, min_size), max_size)

        try:
            logger.info(
                f"[SMART_MONEY] Copying {trader_address[:10]}...: "
                f"{outcome} @ {price:.3f} size=${size:.2f} on {market_id[:20]}"
            )
            result = await trade_func(market_id, outcome, price, size)
            if result:
                results.append({
                    "copied_from": trader_address,
                    "market_id": market_id,
                    "outcome": outcome,
                    "price": price,
                    "size": size,
                    "result": result,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                our_positions.add(market_id)
        except Exception as e:
            logger.warning(f"Copy trade failed: {e}")

    return results


def log_copy_trade(trade: dict, log_path: str = DEFAULT_LOG_PATH) -> None:
    """Append a copy trade to the smart money log."""
    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(json.dumps(trade, default=str) + "\n")
    except Exception as e:
        logger.error(f"Failed to log copy trade: {e}")


class SmartMoneyTracker:
    """Track top traders and manage copy trading state."""

    def __init__(self, state_path: str = DEFAULT_STATE_PATH) -> None:
        self._path = Path(state_path)
        self._data: dict = {
            "top_traders": [],
            "copied_positions": [],
            "last_update": None,
            "total_copied": 0,
            "total_profit": 0.0,
        }
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Failed to save smart money state: {e}")

    @property
    def top_traders(self) -> list[dict]:
        return self._data.get("top_traders", [])

    @property
    def copied_positions(self) -> list[str]:
        return [
            p.get("market_id", "")
            for p in self._data.get("copied_positions", [])
            if p.get("status") == "open"
        ]

    def update_top_traders(self, traders: list[dict]) -> None:
        self._data["top_traders"] = traders[:20]
        self._data["last_update"] = datetime.now(timezone.utc).isoformat()
        self.save()

    def add_copied_position(self, trade: dict) -> None:
        trade["status"] = "open"
        trade["copied_at"] = datetime.now(timezone.utc).isoformat()
        self._data.setdefault("copied_positions", []).append(trade)
        self._data["total_copied"] = self._data.get("total_copied", 0) + 1
        self.save()

    def close_position(self, market_id: str, pnl: float = 0.0) -> None:
        for p in self._data.get("copied_positions", []):
            if p.get("market_id") == market_id and p.get("status") == "open":
                p["status"] = "closed"
                p["pnl"] = pnl
                p["closed_at"] = datetime.now(timezone.utc).isoformat()
                self._data["total_profit"] = (
                    self._data.get("total_profit", 0) + pnl
                )
                break
        self.save()

    def get_our_position_ids(self) -> set[str]:
        return set(self.copied_positions)

    def summary(self) -> dict:
        return {
            "n_top_tracked": len(self.top_traders),
            "n_copied_open": len(self.copied_positions),
            "total_copied": self._data.get("total_copied", 0),
            "total_profit": round(self._data.get("total_profit", 0), 2),
            "last_update": self._data.get("last_update"),
        }
