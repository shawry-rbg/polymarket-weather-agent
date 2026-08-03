"""
Trade Summary Report Generator for Polybot.

Fetches all paper trades from Redis, resolves them against Polymarket Gamma API
closed-market data, and produces a comprehensive P&L report.

Usage:
    from polybot.trade_summary import generate_report
    print(generate_report())

    # Or from Modal:
    modal run polybot/modal_deploy.py --summary
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

def _get_redis():
    """Get Redis connection from REDIS_URL env var."""
    try:
        import redis as _r
        url = os.environ.get("REDIS_URL", "")
        if url:
            return _r.from_url(url)
    except Exception:
        pass
    return None


def _fetch_all_trades(r) -> list[dict]:
    """Fetch all paper trades from Redis list."""
    raw = r.lrange("paper_trades", 0, 499)
    trades = []
    for item in raw:
        try:
            if isinstance(item, bytes):
                item = item.decode()
            t = json.loads(item)
            trades.append(t)
        except Exception:
            pass
    return trades


# ---------------------------------------------------------------------------
# Polymarket Gamma API helpers
# ---------------------------------------------------------------------------

def _extract_threshold(question: str) -> float:
    """Extract temperature threshold (F) from a market question string."""
    import re
    q = question.lower()
    patterns = [
        r"(\d+)\s*[°]?\s*f(?:ahrenheit)?\b",
        r"(\d+)\s*degrees?\s*f",
        r"exceed\s+(?:or\s+equal\s+(?:to|)\s+)?(\d+)",
        r"above\s+(?:or\s+equal\s+(?:to|)\s+)?(\d+)",
        r"over\s+(?:or\s+equal\s+(?:to|)\s+)?(\d+)",
        r"higher\s+than\s+(?:or\s+equal\s+(?:to|)\s+)?(\d+)",
        r"at\s+least\s+(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, q)
        if m:
            return float(m.group(1))
    return 90.0


async def _fetch_closed_markets(city: str, date_str: str) -> list[dict]:
    """Fetch closed/resolved markets for a city+date from Gamma API."""
    import httpx

    BASE_URL = "https://poly-proxy.elvischemoiywo.workers.dev/gamma"
    temp_keywords = [
        "temperature", "temp", "°f", "fahrenheit", "degrees",
        "high temp", "highest temp", "hot", "heat",
    ]

    closed = []
    city_lower = city.lower().strip()

    # Normalize date patterns
    date_patterns = [date_str.lower()]
    date_normalized = date_str.lower().replace(" ", "-")
    date_patterns.append(date_normalized)
    date_normalized2 = date_str.lower().replace(" ", "")
    date_patterns.append(date_normalized2)

    import re as _re
    month_day = _re.search(r"(\w+)\s*(\d{1,2})", date_str)
    if month_day:
        month = month_day.group(1).lower()
        day = month_day.group(2)
        date_patterns.extend([f"{month}-{day}", f"{month}{day}", f"{month} {day}"])

    now = datetime.now(timezone.utc)

    try:
        for offset in range(0, 2000, 100):
            params = {
                "active": "true",
                "closed": "true",
                "limit": 100,
                "offset": offset,
                "order": "volume24hr",
                "ascending": "false",
            }
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{BASE_URL}/events", params=params)
                if resp.status_code != 200:
                    break
                events = resp.json()
                if not events:
                    break

                for event in events:
                    event_title = (event.get("title", "") or "").lower()
                    markets_in_event = event.get("markets", [])

                    for market in markets_in_event:
                        question = (market.get("question", "") or "").lower()

                        is_temp = any(
                            kw in question or kw in event_title
                            for kw in temp_keywords
                        )
                        if not is_temp:
                            continue

                        if city_lower not in question and city_lower not in event_title:
                            continue

                        # Check if this market is closed (endDate < now)
                        end_date_str = market.get("endDate", "") or event.get("endDate", "")
                        if end_date_str:
                            try:
                                end_dt = datetime.fromisoformat(
                                    end_date_str.replace("Z", "+00:00")
                                )
                                if end_dt >= now:
                                    continue  # Market still open
                            except Exception:
                                continue

                        # Date filter
                        if date_patterns:
                            date_match = any(
                                dp in question or dp in event_title
                                for dp in date_patterns
                            )
                            if not date_match:
                                date_match = any(dp in end_date_str.lower() for dp in date_patterns)
                            if not date_match:
                                continue

                        threshold = _extract_threshold(market.get("question", ""))
                        closed.append({
                            "question": market.get("question", ""),
                            "outcomePrices": market.get("outcomePrices", "[]"),
                            "endDate": end_date_str,
                            "threshold_f": threshold,
                            "eventTitle": event.get("title", ""),
                        })

                    # If we found markets for this city, stop scanning
                    if any(city_lower in m.get("question", "").lower() for m in closed):
                        break

                if any(city_lower in m.get("question", "").lower() for m in closed):
                    break

    except Exception as e:
        logger.error(f"Error fetching closed markets for {city}/{date_str}: {e}")

    # Deduplicate
    seen = set()
    unique = []
    for m in closed:
        key = m.get("question", "")
        if key not in seen:
            seen.add(key)
            unique.append(m)
    return unique


def _determine_winning_bucket(closed_markets: list[dict]) -> Optional[str]:
    """
    Determine which bucket won from closed market data.
    The winning outcome has yes_price ≈ 1.0.
    Returns bucket string like '>84F' or None.
    """
    if not closed_markets:
        return None

    best_price = -1.0
    winning_bucket = None

    for m in closed_markets:
        try:
            raw = m.get("outcomePrices", "[]")
            prices = json.loads(raw) if isinstance(raw, str) else raw
            if len(prices) < 2:
                continue
            yes_price = float(prices[0])
            threshold = m.get("threshold_f", 0)
            bucket = f">{threshold}F"

            if yes_price >= 0.95:
                # This bucket resolved YES — it's the winner
                return bucket

            if yes_price > best_price:
                best_price = yes_price
                winning_bucket = bucket
        except Exception:
            pass

    return winning_bucket


# ---------------------------------------------------------------------------
# P&L calculation
# ---------------------------------------------------------------------------

def _calculate_trade_pnl(trade: dict, winning_bucket: Optional[str]) -> dict:
    """
    Calculate P&L for a single trade given the winning bucket.

    BUY: profit = (1 - entry_price) * size if bucket matches, else -entry_price * size
    SELL: profit = entry_price * size if bucket does NOT match, else -(1 - entry_price) * size
    """
    side = trade.get("side", trade.get("action", "BUY")).upper()
    entry_price = float(trade.get("entry_price", trade.get("price", 0)))
    size = float(trade.get("size", 0))
    trade_bucket = trade.get("bucket", "")

    if entry_price <= 0 or size <= 0:
        return {
            "profit_usd": 0.0,
            "won": False,
            "reason": "invalid_price_or_size",
        }

    if winning_bucket is None:
        return {
            "profit_usd": 0.0,
            "won": False,
            "reason": "no_winning_bucket",
        }

    bucket_matches = (trade_bucket == winning_bucket)

    if side == "BUY":
        won = bucket_matches
        if won:
            profit = (1.0 - entry_price) * size
        else:
            profit = -entry_price * size
    elif side == "SELL":
        won = not bucket_matches
        if won:
            profit = entry_price * size
        else:
            profit = -(1.0 - entry_price) * size
    else:
        return {
            "profit_usd": 0.0,
            "won": False,
            "reason": f"unknown_side_{side}",
        }

    return {
        "profit_usd": round(profit, 4),
        "won": won,
        "reason": "resolved",
    }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _parse_market_date(trade: dict) -> str:
    """Extract a market date string from a trade record."""
    # Try explicit date field
    for key in ("market_date", "date", "target_date"):
        val = trade.get(key, "")
        if val:
            return str(val)

    # Try to parse from timestamp
    ts = trade.get("timestamp", "")
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    return "unknown"


def generate_report() -> str:
    """
    Generate a complete trade summary report.

    Returns a formatted string with:
    - Per-city, per-date aggregated P&L
    - Individual trade details
    - Overall summary statistics
    """
    import asyncio

    lines = []
    sep = "=" * 72
    thin_sep = "-" * 72

    lines.append("")
    lines.append(sep)
    lines.append("  POLYBOT TRADE SUMMARY REPORT")
    lines.append(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(sep)
    lines.append("")

    # --- Connect to Redis ---
    r = _get_redis()
    if not r:
        lines.append("ERROR: Cannot connect to Redis. Set REDIS_URL env var.")
        return "\n".join(lines)

    # --- Fetch all trades ---
    all_trades = _fetch_all_trades(r)
    if not all_trades:
        lines.append("No paper trades found in Redis.")
        return "\n".join(lines)

    lines.append(f"Total trades in Redis: {len(all_trades)}")

    # Count by status
    open_trades = [t for t in all_trades if t.get("status") == "open"]
    resolved_trades = [t for t in all_trades if t.get("status") == "resolved"]
    lines.append(f"  Open:     {len(open_trades)}")
    lines.append(f"  Resolved: {len(resolved_trades)}")
    lines.append("")

    # --- Cumulative P&L from Redis counters ---
    try:
        cum_pnl = float(r.get("paper_pnl_total") or 0)
        cum_wins = int(r.get("paper_win_count") or 0)
        cum_total = int(r.get("paper_trade_count") or 0)
        lines.append(f"Cumulative P&L (from Redis): ${cum_pnl:+.4f}  ({cum_wins}W / {cum_total}T)")
        lines.append("")
    except Exception:
        pass

    # --- Group trades by city + date ---
    grouped: dict[str, list[dict]] = {}
    for t in all_trades:
        city = t.get("city", "unknown")
        mkt_date = _parse_market_date(t)
        key = f"{city}|{mkt_date}"
        grouped.setdefault(key, []).append(t)

    # --- Resolve closed markets and calculate P&L ---
    # We need to query Gamma API for each city+date combination
    resolution_cache: dict[str, Optional[str]] = {}  # "city|date" -> winning_bucket

    async def resolve_all():
        tasks = {}
        for key, trades_list in grouped.items():
            city, mkt_date = key.split("|", 1)
            if mkt_date != "unknown":
                tasks[key] = _fetch_closed_markets(city, mkt_date)

        results = {}
        for key, coro in tasks.items():
            try:
                markets = await coro
                results[key] = _determine_winning_bucket(markets)
            except Exception as e:
                logger.error(f"Resolution error for {key}: {e}")
                results[key] = None
        return results

    # Run async resolution
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import nest_asyncio
        nest_asyncio.apply()
        resolution_cache = asyncio.get_event_loop().run_until_complete(resolve_all())
    else:
        resolution_cache = asyncio.run(resolve_all())

    # --- Process each trade ---
    enriched_trades = []
    for t in all_trades:
        city = t.get("city", "unknown")
        mkt_date = _parse_market_date(t)
        key = f"{city}|{mkt_date}"
        winning_bucket = resolution_cache.get(key)

        pnl_info = _calculate_trade_pnl(t, winning_bucket)

        enriched = {
            "city": city,
            "market_date": mkt_date,
            "bucket": t.get("bucket", ""),
            "side": t.get("side", t.get("action", "")),
            "entry_price": float(t.get("entry_price", t.get("price", 0))),
            "size": float(t.get("size", 0)),
            "timestamp": t.get("timestamp", ""),
            "status": t.get("status", "unknown"),
            "reason": t.get("reason", ""),
            "winning_bucket": winning_bucket or "N/A",
            "profit_usd": pnl_info["profit_usd"],
            "won": pnl_info["won"],
            "resolution_reason": pnl_info.get("reason", ""),
        }

        # Override with actual resolved data if already in Redis
        if t.get("status") == "resolved":
            existing_profit = t.get("profit_usd", "")
            if existing_profit:
                try:
                    enriched["profit_usd"] = float(existing_profit)
                except (ValueError, TypeError):
                    pass
            existing_won = t.get("won", "")
            if existing_won:
                enriched["won"] = existing_won in ("1", "True", "true", True)
            existing_wb = t.get("winning_bucket", "")
            if existing_wb:
                enriched["winning_bucket"] = existing_wb

        enriched_trades.append(enriched)

    # --- Aggregate by city + date ---
    agg: dict[str, dict] = {}
    for et in enriched_trades:
        key = f"{et['city']}|{et['market_date']}"
        if key not in agg:
            agg[key] = {
                "city": et["city"],
                "market_date": et["market_date"],
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "net_pnl": 0.0,
                "winning_bucket": et["winning_bucket"],
                "open_count": 0,
                "resolved_count": 0,
            }
        agg[key]["total_trades"] += 1
        agg[key]["net_pnl"] += et["profit_usd"]
        if et["status"] == "open":
            agg[key]["open_count"] += 1
        else:
            agg[key]["resolved_count"] += 1
            if et["won"]:
                agg[key]["wins"] += 1
            else:
                agg[key]["losses"] += 1

    # --- Sort: resolved first, then by date, then by city ---
    sorted_agg = sorted(
        agg.values(),
        key=lambda x: (
            0 if x["resolved_count"] > 0 else 1,
            x["market_date"],
            x["city"],
        ),
    )

    # --- Print aggregated summary table ---
    lines.append(sep)
    lines.append("  AGGREGATED SUMMARY BY CITY & DATE")
    lines.append(sep)
    lines.append("")
    header = (
        f"  {'City':<18} {'Date':<12} {'Trades':>6} {'Resolved':>8} "
        f"{'Open':>5} {'Net P&L':>10} {'W/L':>7} {'Outcome':<12}"
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))

    total_net_pnl = 0.0
    total_wins = 0
    total_losses = 0
    total_resolved = 0

    for row in sorted_agg:
        wl_str = f"{row['wins']}W/{row['losses']}L" if row["resolved_count"] > 0 else "-"
        pnl_str = f"${row['net_pnl']:+.4f}"
        outcome = row["winning_bucket"] if row["winning_bucket"] != "N/A" else "?"

        lines.append(
            f"  {row['city']:<18} {row['market_date']:<12} {row['total_trades']:>6} "
            f"{row['resolved_count']:>8} {row['open_count']:>5} "
            f"{pnl_str:>10} {wl_str:>7} {outcome:<12}"
        )

        total_net_pnl += row["net_pnl"]
        total_wins += row["wins"]
        total_losses += row["losses"]
        total_resolved += row["resolved_count"]

    lines.append("  " + "-" * (len(header) - 2))
    total_wl = f"{total_wins}W/{total_losses}L" if total_resolved > 0 else "-"
    lines.append(
        f"  {'TOTAL':<18} {'':<12} {sum(a['total_trades'] for a in sorted_agg):>6} "
        f"{total_resolved:>8} {sum(a['open_count'] for a in sorted_agg):>5} "
        f"${total_net_pnl:+.4f}      {total_wl:>7}"
    )
    lines.append("")

    # --- Print individual trade details ---
    lines.append(sep)
    lines.append("  INDIVIDUAL TRADE DETAILS")
    lines.append(sep)
    lines.append("")

    # Sort: by city, then date, then timestamp
    sorted_trades = sorted(
        enriched_trades,
        key=lambda x: (x["city"], x["market_date"], x["timestamp"]),
    )

    current_city = ""
    for et in sorted_trades:
        if et["city"] != current_city:
            current_city = et["city"]
            lines.append(f"  --- {current_city} ({et['market_date']}) ---")
            lines.append(
                f"  {'Bucket':<16} {'Side':<5} {'Entry':>6} {'Size':>8} "
                f"{'Status':<10} {'PnL':>10} {'Won':>4} {'Winner':<12}"
            )
            lines.append("  " + "-" * 70)

        won_str = "YES" if et["won"] else "NO" if et["status"] == "resolved" else "-"
        pnl_str = f"${et['profit_usd']:+.4f}" if et["status"] == "resolved" else "-"

        lines.append(
            f"  {et['bucket']:<16} {et['side']:<5} {et['entry_price']:>6.4f} "
            f"{et['size']:>8.4f} {et['status']:<10} {pnl_str:>10} "
            f"{won_str:>4} {et['winning_bucket']:<12}"
        )

    lines.append("")

    # --- Overall statistics ---
    lines.append(sep)
    lines.append("  OVERALL STATISTICS")
    lines.append(sep)
    lines.append("")

    all_resolved = [t for t in enriched_trades if t["status"] == "resolved"]
    all_open = [t for t in enriched_trades if t["status"] == "open"]

    if all_resolved:
        win_count = sum(1 for t in all_resolved if t["won"])
        loss_count = sum(1 for t in all_resolved if not t["won"])
        win_rate = win_count / len(all_resolved) * 100 if all_resolved else 0
        avg_pnl = sum(t["profit_usd"] for t in all_resolved) / len(all_resolved)
        best_trade = max(all_resolved, key=lambda t: t["profit_usd"])
        worst_trade = min(all_resolved, key=lambda t: t["profit_usd"])

        lines.append(f"  Total trades:        {len(enriched_trades)}")
        lines.append(f"  Resolved:            {len(all_resolved)}")
        lines.append(f"  Open:                {len(all_open)}")
        lines.append(f"  Wins:                {win_count}")
        lines.append(f"  Losses:              {loss_count}")
        lines.append(f"  Win rate:            {win_rate:.1f}%")
        lines.append(f"  Net P&L:             ${total_net_pnl:+.4f}")
        lines.append(f"  Avg P&L per trade:   ${avg_pnl:+.4f}")
        lines.append(f"  Best trade:          {best_trade['city']}/{best_trade['bucket']} "
                      f"${best_trade['profit_usd']:+.4f}")
        lines.append(f"  Worst trade:         {worst_trade['city']}/{worst_trade['bucket']} "
                      f"${worst_trade['profit_usd']:+.4f}")
    else:
        lines.append(f"  Total trades: {len(enriched_trades)}")
        lines.append(f"  Open: {len(all_open)}")
        lines.append(f"  Resolved: 0 (no closed markets to evaluate yet)")

    # --- Open trades summary ---
    if all_open:
        lines.append("")
        lines.append(thin_sep)
        lines.append("  OPEN TRADES (awaiting resolution)")
        lines.append(thin_sep)
        open_by_city: dict[str, list] = {}
        for t in all_open:
            open_by_city.setdefault(t["city"], []).append(t)

        for city, ctrades in sorted(open_by_city.items()):
            lines.append(f"  {city}: {len(ctrades)} open trade(s)")
            for t in ctrades:
                lines.append(
                    f"    {t['bucket']:<16} {t['side']:<5} @ {t['entry_price']:.4f} "
                    f"size={t['size']:.4f}  ({t['reason']})"
                )

    lines.append("")
    lines.append(sep)
    lines.append("  END OF REPORT")
    lines.append(sep)
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    print(generate_report())
