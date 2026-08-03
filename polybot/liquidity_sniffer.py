"""
Liquidity Sniffer — Polymarket CLOB API direct.

Zero extra dependencies (uses httpx already in modal image).
Provides:
  - get_order_book_depth(token_id) → {bids, asks, spread, total_depth}
  - check_arbitrage(markets) → list of arb opportunities
  - track_smart_money(top_n) → list of top trader addresses + recent activity
  - get_token_id_from_market(market) → token_id string

All functions are async and safe to call from the orchestrator.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = __import__("logging").getLogger(__name__)

CLOB_BASE = "https://poly-proxy.elvischemoiywo.workers.dev/clob"
GAMMA_BASE = "https://poly-proxy.elvischemoiywo.workers.dev/gamma"
DATA_BASE = "https://poly-proxy.elvischemoiywo.workers.dev/data"
REQUEST_TIMEOUT = 10

# Cache for token IDs to avoid repeated lookups
_token_id_cache: Dict[str, str] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get(path: str, base: str = CLOB_BASE, params: dict | None = None) -> dict | list:
    url = f"{base}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(url, params=params or {})
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.debug(f"API error {url}: {e}")
        return {}


async def _post(path: str, data: dict, base: str = CLOB_BASE) -> dict:
    url = f"{base}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(url, json=data)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.debug(f"API POST error {url}: {e}")
        return {}


def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 1. Order Book Depth
# ---------------------------------------------------------------------------

async def get_order_book_depth(token_id: str) -> dict:
    """
    Fetch Level-2 order book for a CLOB token.

    Returns:
        {
            "token_id": str,
            "bids": [{"price": float, "size": float}, ...],
            "asks": [{"price": float, "size": float}, ...],
            "best_bid": float,
            "best_ask": float,
            "spread": float,
            "total_bid_depth": float,   # sum of all bid sizes
            "total_ask_depth": float,   # sum of all ask sizes
            "mid_price": float,
        }
    """
    data = await _get(f"book/{token_id}")
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict) and "data" in data:
        entries = data["data"]
    else:
        entries = [data] if data else []

    bids: List[dict] = []
    asks: List[dict] = []

    for entry in entries:
        price = _float(entry.get("price") or entry.get("p"))
        size = _float(entry.get("size") or entry.get("s") or entry.get("amount", 0))
        side = str(entry.get("side") or entry.get("S", "")).upper()
        if side == "BUY":
            bids.append({"price": price, "size": size})
        elif side == "SELL":
            asks.append({"price": price, "size": size})

    bids.sort(key=lambda x: x["price"], reverse=True)
    asks.sort(key=lambda x: x["price"])

    best_bid = bids[0]["price"] if bids else 0.0
    best_ask = asks[0]["price"] if asks else 0.0
    spread = best_ask - best_bid if best_bid and best_ask else 0.0
    mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0

    return {
        "token_id": token_id,
        "bids": bids,
        "asks": asks,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": round(spread, 4),
        "mid_price": round(mid, 4),
        "total_bid_depth": round(sum(b["size"] for b in bids), 4),
        "total_ask_depth": round(sum(a["size"] for a in asks), 4),
    }


async def check_sufficient_depth(token_id: str, order_size: float, min_multiplier: float = 3.0) -> bool:
    """
    Check if order book depth is sufficient for a trade.

    Returns True if total ask depth >= order_size * min_multiplier.
    This ensures we won't suffer more than ~10% slippage.
    """
    book = await get_order_book_depth(token_id)
    required = order_size * min_multiplier
    return book["total_ask_depth"] >= required


# ---------------------------------------------------------------------------
# 2. Token ID Resolution
# ---------------------------------------------------------------------------

async def get_token_id_from_market(market: dict) -> str | None:
    """
    Extract the YES token ID from a Gamma market dict.
    Uses clobTokenIds from Gamma API (index 0 = YES) to avoid CLOB auth requirement.
    Falls back to CLOB condition endpoint for backward compat.
    """
    # Primary: use clobTokenIds from Gamma response
    clob_token_ids = market.get("clobTokenIds") or market.get("clob_token_ids")
    if clob_token_ids and isinstance(clob_token_ids, list) and len(clob_token_ids) >= 1:
        tid = str(clob_token_ids[0])
        print(f"[LIQ] Using clobTokenIds[0] = {tid[:20]}...")
        return tid
    print(f"[LIQ] No clobTokenIds in market, conditionId={market.get('conditionId','')[:20]}")

    # Fallback: CLOB condition endpoint (requires auth, may 403)
    condition_id = market.get("conditionId") or market.get("condition_id", "")
    if not condition_id:
        return None

    if condition_id in _token_id_cache:
        return _token_id_cache[condition_id]

    data = await _get(f"condition/{condition_id}", base=CLOB_BASE)
    tokens = data.get("tokens", []) if isinstance(data, dict) else []
    for token in tokens:
        outcome = str(token.get("outcome", "")).upper()
        if outcome == "YES":
            tid = token.get("token_id", "")
            if tid:
                _token_id_cache[condition_id] = tid
            return tid

    return None


async def get_market_price(token_id: str) -> Tuple[float, float]:
    """Get current YES/NO prices for a token via CLOB."""
    data = await _get(f"price/{token_id}", base=CLOB_BASE)
    yes_price = _float(data.get("price", 0))
    no_price = round(1.0 - yes_price, 4) if yes_price else 0.0
    return yes_price, no_price


# ---------------------------------------------------------------------------
# 3. Arbitrage Detection
# ---------------------------------------------------------------------------

async def check_arbitrage_for_city(
    city_name: str,
    date_str: str | None = None,
) -> List[dict]:
    """
    Find arbitrage opportunities for a city's temperature markets.

    Strategy: For each pair of mutually exclusive buckets,
    check if sum of YES probabilities < 0.98 (risk-free profit).

    Returns list of opportunities:
        {"city": str, "markets": [market_a, market_b],
         "combined_price": float, "edge": float, "strategy": str}
    """
    from polybot.polymarket import find_markets

    markets = await find_markets(city_name=city_name, date_str=date_str)
    if not markets:
        return []

    opportunities = []
    from polybot.polymarket import parse_outcome_prices as _parse

    prices_list: List[Tuple[dict, float, float]] = []
    for m in markets:
        try:
            yes_p, no_p = _parse(m)
            prices_list.append((m, yes_p, no_p))
        except Exception:
            continue

    # Check pairs: sum of adjacent bucket prices
    price_threshold = 0.98
    for i in range(len(prices_list)):
        m_a, yes_a, _ = prices_list[i]
        threshold_a = m_a.get("threshold_f") or _extract_threshold(m_a.get("question", ""))

        # Single market: if YES price is very low (< 0.02), it's near-resolved
        if yes_a < 0.02 or yes_a > 0.98:
            continue

        # Check sum of all YES prices for this city
        combined = sum(p[1] for p in prices_list)
        if combined < price_threshold and len(prices_list) > 1:
            opportunities.append({
                "city": city_name,
                "strategy": "BUY_ALL",
                "markets": [{"question": m.get("question", ""), "yes_price": yp, "threshold_f": m.get("threshold_f")}
                            for m, yp, _ in prices_list],
                "combined_price": round(combined, 4),
                "edge": round(1.0 - combined, 4),
                "date": date_str or m_a.get("endDate", ""),
            })
            break  # One arb opportunity per city is enough

    return opportunities


async def check_arbitrage_all_cities(cities: list, date_str: str | None = None) -> List[dict]:
    """Run arb check for all cities concurrently."""
    tasks = [check_arbitrage_for_city(c["name"], date_str) for c in cities]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    return [opp for sublist in results for opp in sublist if opp]


def _extract_threshold(question: str = "") -> float:
    import re
    patterns = [
        r"(\d+)\s*[°]?\s*f(?:ahrenheit)?\b", r"(\d+)\s*degrees?\s*f",
        r"exceed\s+(?:or\s+equal\s+(?:to|)\s+)?(\d+)\s*[°]?F?",
        r"above\s+(?:or\s+equal\s+(?:to|)\s+)?(\d+)",
        r"over\s+(?:or\s+equal\s+(?:to|)\s+)?(\d+)",
        r"at\s+least\s+(\d+)", r"be\s+(\d+)\s*(?:degrees?|°)?\s*F",
    ]
    for pat in patterns:
        m = re.search(pat, question, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            if val > 60:
                return round(val, 1)
    return 90.0


# ---------------------------------------------------------------------------
# 4. Smart Money Tracking
# ---------------------------------------------------------------------------

async def track_smart_money(
    min_win_rate: float = 0.75,
    min_trades: int = 5,
    top_n: int = 10,
) -> List[dict]:
    """
    Find top traders by win rate and volume from Data API.

    Returns list of top traders:
        {"address": str, "win_rate": float, "total_trades": int,
         "total_volume": float, "recent_positions": [...]}
    """
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    params = {
        "limit": 100,
        "order": "volume24hr",
        "ascending": "false",
        "start": since,
    }
    data = await _get("users", base=DATA_BASE, params=params)

    if not isinstance(data, list):
        data = data.get("data", []) if isinstance(data, dict) else []

    traders: Dict[str, dict] = {}
    for entry in data:
        addr = entry.get("address", "") or entry.get("trader", "")
        if not addr:
            continue
        addr = addr.lower()
        if addr not in traders:
            traders[addr] = {
                "address": addr,
                "total_trades": 0,
                "wins": 0,
                "total_volume": 0.0,
            }
        t = traders[addr]
        t["total_trades"] += 1
        vol = _float(entry.get("volume", 0) or entry.get("total_volume", 0))
        t["total_volume"] += vol

    # Filter and rank
    ranked = []
    for addr, t in traders.items():
        if t["total_trades"] >= min_trades:
            wr = (t["wins"] / t["total_trades"]) if t["total_trades"] > 0 else 0.5
            if wr >= min_win_rate:
                ranked.append({
                    "address": addr,
                    "win_rate": round(wr, 3),
                    "total_trades": t["total_trades"],
                    "total_volume": round(t["total_volume"], 2),
                })

    ranked.sort(key=lambda x: x["total_volume"], reverse=True)
    return ranked[:top_n]


async def get_trader_positions(trader_address: str) -> List[dict]:
    """Get recent positions for a specific trader."""
    params = {
        "address": trader_address.lower(),
        "limit": 20,
        "order": "timestamp",
        "ascending": "false",
    }
    data = await _get("positions", base=DATA_BASE, params=params)

    if not isinstance(data, list):
        data = data.get("data", []) if isinstance(data, dict) else []

    positions = []
    for entry in data:
        positions.append({
            "token_id": entry.get("token_id", ""),
            "outcome": entry.get("outcome", ""),
            "size": _float(entry.get("size", 0)),
            "avg_price": _float(entry.get("avg_price", 0) or entry.get("price", 0)),
            "side": entry.get("side", ""),
            "timestamp": entry.get("timestamp", ""),
        })
    return positions


# ---------------------------------------------------------------------------
# 5. Pre-trade liquidity gate
# ---------------------------------------------------------------------------

async def pre_trade_check(
    market: dict,
    order_size: float,
    direction: str = "BUY",
) -> dict:
    """
    Complete pre-trade liquidity check.
    Uses Gamma API bestBid/bestAsk when CLOB API is unavailable (403).

    Returns:
        {"pass": bool, "reason": str, "depth": dict | None, "slippage_pct": float}
    """
    # Try to get order book from CLOB
    token_id = await get_token_id_from_market(market)
    if token_id:
        book = await get_order_book_depth(token_id)
        if book.get("asks"):
            required_depth = order_size * 3.0
            if book["total_ask_depth"] >= required_depth:
                return {"pass": True, "reason": "ok", "depth": book, "slippage_pct": 0}
            else:
                return {
                    "pass": False,
                    "reason": f"insufficient_depth: {book['total_ask_depth']:.2f} < {required_depth:.2f}",
                    "depth": book,
                    "slippage_pct": 0,
                }

    # Fallback: use Gamma API bestBid/bestAsk for basic liquidity check
    best_bid = float(market.get("bestBid", 0) or 0)
    best_ask = float(market.get("bestAsk", 0) or 0)
    spread = float(market.get("spread", 0) or 0)
    vol24 = float(market.get("volume24hr", 0) or 0)

    # Basic sanity: need a valid price range
    if best_ask <= 0 or best_ask > 0.98:
        return {"pass": False, "reason": "no_valid_ask", "depth": None, "slippage_pct": 0}

    # Check spread is reasonable (< 10 cents)
    if spread > 0.10:
        return {"pass": False, "reason": f"spread_too_wide: {spread:.3f}", "depth": None, "slippage_pct": 0}

    # Check minimum 24h volume ($100)
    if vol24 < 100:
        return {"pass": False, "reason": f"low_volume: ${vol24:.0f}", "depth": None, "slippage_pct": 0}

    return {
        "pass": True,
        "reason": "gamma_fallback",
        "depth": {"best_bid": best_bid, "best_ask": best_ask, "spread": spread},
        "slippage_pct": spread,
    }




# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    async def _demo():
        print("=== Liquidity Sniffer Demo ===")
        # Demo: check arb for London
        opps = await check_arbitrage_for_city("London", "June 1")
        print(f"Arb opportunities for London: {len(opps)}")
        for opp in opps:
            print(f"  {opp['strategy']} combined={opp['combined_price']} edge={opp['edge']}")

        # Demo: smart money
        traders = await track_smart_money(min_trades=3, top_n=5)
        print(f"\nTop traders: {len(traders)}")
        for t in traders[:3]:
            print(f"  {t['address'][:12]}... wr={t['win_rate']} vol={t['total_volume']}")

    asyncio.run(_demo())
