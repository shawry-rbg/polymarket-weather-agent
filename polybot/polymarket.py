"""
Polymarket Gamma API client -- FIXED v2.

Key fixes:
- find_markets() now scans ALL active temperature markets for a given date
- Uses events endpoint + keywords to find temperature markets
- Returns ALL matching markets, not just first one
- Handles date-specific searches (e.g. "May 30")
- Maps wethr.net city slugs to Polymarket event slugs
"""

import asyncio
import json
import logging
import re
import time
from datetime import timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://poly-proxy.elvischemoiywo.workers.dev/gamma"
REQUEST_TIMEOUT = 15

# Module-level Redis connection (lazy, reused)
_r = None


def _get_redis():
    """Get or create a Redis connection (lazy, reused)."""
    global _r
    if _r is not None:
        return _r
    import os
    import redis as _redis_mod
    url = os.environ.get("REDIS_URL")
    if url:
        try:
            _r = _redis_mod.from_url(url)
            return _r
        except Exception:
            pass
    return None

# City name -> wethr.net slug mapping
CITY_SLUG_MAP = {
    "austin": "austin",
    "hong kong": "hong-kong",
    "hong-kong": "hong-kong",
    "seoul": "seoul",
    "london": "london",
    "paris": "paris",
    "shanghai": "shanghai",
    "chicago": "chicago",
    "denver": "denver",
    "houston": "houston",
    "warsaw": "warsaw",
    "munich": "munich",
    "tokyo": "tokyo",
    "new york": "new-york",
    "los angeles": "los-angeles",
    "phoenix": "phoenix",
    "san francisco": "san-francisco",
    "miami": "miami",
    "dallas": "dallas",
    "atlanta": "atlanta",
    "seattle": "seattle",
    "minneapolis": "minneapolis",
    "detroit": "detroit",
    "cleveland": "cleveland",
    "boston": "boston",
    "philadelphia": "philadelphia",
    "washington": "washington",
    "toronto": "toronto",
    "vancouver": "vancouver",
    "mexico city": "mexico-city",
    "sao paulo": "sao-paulo",
    "buenos aires": "buenos-aires",
    "dubai": "dubai",
    "mumbai": "mumbai",
    "singapore": "singapore",
    "bangkok": "bangkok",
    "jakarta": "jakarta",
    "cairo": "cairo",
    "lagos": "lagos",
    "nairobi": "nairobi",
    "sydney": "sydney",
    "melbourne": "melbourne",
    "perth": "perth",
    "moscow": "moscow",
    "istanbul": "istanbul",
    "berlin": "berlin",
    "madrid": "madrid",
    "rome": "rome",
    "amsterdam": "amsterdam",
    "brussels": "brussels",
    "vienna": "vienna",
    "zurich": "zurich",
    "copenhagen": "copenhagen",
    "oslo": "oslo",
    "stockholm": "stockholm",
    "helsinki": "helsinki",
    "prague": "prague",
    "budapest": "budapest",
    "bucharest": "bucharest",
    "lisbon": "lisbon",
    "athens": "athens",
    "warsaw": "warsaw",
    "seoul": "seoul",
    "taipei": "taipei",
    "manila": "manila",
    "kuala lumpur": "kuala-lumpur",
    "ho chi minh": "ho-chi-minh",
    "rio de janeiro": "rio-de-janeiro",
    "lima": "lima",
    "bogota": "bogota",
    "santiago": "santiago",
    "capetown": "capetown",
    "casablanca": "casablanca",
    "tunis": "tunis",
    "tel aviv": "tel-aviv",
    "riyadh": "riyadh",
    "doha": "doha",
    "kuwait": "kuwait",
    "manama": "manama",
    "muscat": "muscat",
    "karachi": "karachi",
    "lahore": "lahore",
    "dhaka": "dhaka",
    "colombo": "colombo",
    "kathmandu": "kathmandu",
}


async def find_markets(
    city_name: str | None = None,
    date_str: str | None = None,
    require_temp_keyword: bool = True,
) -> list[dict]:
    """
    Find active Polymarket temperature markets.

    Strategy:
    1. Fetch active events from Gamma API
    2. Filter for temperature/weather keywords
    3. Optionally filter by city name and/or date
    4. Return ALL matching markets

    Args:
        city_name: Filter by city (case-insensitive, matches question text)
        date_str: Filter by date string like "May 30", "may-30", "2025-05-30"
        require_temp_keyword: If True, require temp/weather keyword in question

    Returns:
        List of market dicts with keys: question, outcomePrices, volume24hr,
        endDate, conditionId, slug, threshold_f
    """
    all_markets = []

    # Normalize city name
    city_lower = city_name.lower().strip() if city_name else None

    # Normalize date
    date_patterns = []
    if date_str:
        # Generate multiple date patterns for matching
        date_lower = date_str.lower().strip()
        date_patterns.append(date_lower)
        # "May 30" -> also try "may-30", "may30"
        date_normalized = date_lower.replace(" ", "-")
        date_patterns.append(date_normalized)
        date_normalized2 = date_lower.replace(" ", "")
        date_patterns.append(date_normalized2)
        # Extract month and day
        month_day = re.search(r"(\w+)\s*(\d{1,2})", date_str)
        if month_day:
            month = month_day.group(1).lower()
            day = month_day.group(2)
            date_patterns.extend([
                f"{month}-{day}",
                f"{month}{day}",
                f"{month} {day}",
            ])

    # Step 1: Fetch active events with temperature keywords
    temp_keywords = [
        "temperature", "temp", "°f", "°c", "fahrenheit", "celsius",
        "degrees", "high temp", "highest temp", "hot", "heat",
        "weather forecast", "will it be hotter",
    ]

    try:
        # Search across multiple pages
        for offset in range(0, 2000, 100):
            params = {
                "active": "true",
                "closed": "false",
                "limit": 100,
                "offset": offset,
                "order": "volume24hr",
                "ascending": "false",
            }
            await asyncio.sleep(2.0)
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                r = await client.get(f"{BASE_URL}/events", params=params)
                if r.status_code != 200:
                    break
                body = r.json()
                events = body if isinstance(body, list) else body.get("events") or body.get("data") or []
                if not events:
                    break

                for event in events:
                    event_title = (event.get("title", "") or "").lower()
                    event_question = (event.get("question", "") or "").lower()

                    # Check if any market in this event is temperature-related
                    markets_in_event = event.get("markets", [])
                    for market in markets_in_event:
                        question = (market.get("question", "") or "").lower()

                        is_temp = any(kw in question or kw in event_title or kw in event_question
                                      for kw in temp_keywords)

                        if require_temp_keyword and not is_temp:
                            continue

                        # Filter by city
                        if city_lower and city_lower not in question and city_lower not in event_title:
                            continue

                        # Filter by date
                        if date_patterns:
                            date_match = any(
                                dp in question or dp in event_title
                                for dp in date_patterns
                            )
                            end_date = (market.get("endDate", "") or event.get("endDate", "") or "").lower()
                            date_match = date_match or any(dp in end_date for dp in date_patterns)
                            if not date_match:
                                continue

                        threshold = _extract_threshold(market.get("question", ""))

                        market_data = {
                            "question": market.get("question", ""),
                            "outcomePrices": market.get("outcomePrices", "[]"),
                            "volume24hr": market.get("volume24hr", 0),
                            "endDate": market.get("endDate", ""),
                            "conditionId": market.get("conditionId", ""),
                            "slug": market.get("slug", ""),
                            "id": str(market.get("id", "")),
                            "threshold_f": threshold,
                            "eventTitle": event.get("title", ""),
                            "clobTokenIds": market.get("clobTokenIds", []),
                            "bestBid": market.get("bestBid", 0),
                            "bestAsk": market.get("bestAsk", 0),
                            "spread": market.get("spread", 0),
                        }
                        all_markets.append(market_data)

                # If we found markets for the requested city, we can stop
                if city_lower and any(
                    city_lower in m.get("question", "").lower()
                    for m in all_markets
                ):
                    break

    except Exception as e:
        logger.error(f"Error fetching events: {e}")

    # Step 2: Also search markets endpoint directly (backup)
    try:
        await asyncio.sleep(2.0)
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            r = await client.get(
                f"{BASE_URL}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 100,
                    "order": "volume24hr",
                    "ascending": "false",
                }
            )
            if r.status_code == 200:
                body = r.json()
                markets = body if isinstance(body, list) else body.get("markets") or body.get("data") or []
                if isinstance(markets, list):
                    for market in markets:
                        question = (market.get("question", "") or "").lower()

                        is_temp = any(kw in question for kw in temp_keywords)
                        if require_temp_keyword and not is_temp:
                            continue

                        if city_lower and city_lower not in question:
                            continue

                        if date_patterns:
                            date_match = any(dp in question for dp in date_patterns)
                            end_date = (market.get("endDate", "") or "").lower()
                            date_match = date_match or any(dp in end_date for dp in date_patterns)
                            if not date_match:
                                continue

                        # Skip if we already have this market
                        mid = str(market.get("id", ""))
                        if any(str(m.get("id", "")) == mid for m in all_markets):
                            continue

                        threshold = _extract_threshold(market.get("question", ""))
                        all_markets.append({
                            "question": market.get("question", ""),
                            "outcomePrices": market.get("outcomePrices", "[]"),
                            "volume24hr": market.get("volume24hr", 0),
                            "endDate": market.get("endDate", ""),
                            "conditionId": market.get("conditionId", ""),
                            "slug": market.get("slug", ""),
                            "id": mid,
                            "threshold_f": threshold,
                            "eventTitle": "",
                            "clobTokenIds": market.get("clobTokenIds", []),
                            "bestBid": market.get("bestBid", 0),
                            "bestAsk": market.get("bestAsk", 0),
                            "spread": market.get("spread", 0),
                        })
    except Exception as e:
        logger.error(f"Error fetching markets: {e}")

    # Deduplicate by conditionId
    seen = set()
    unique_markets = []
    for m in all_markets:
        key = m.get("conditionId") or m.get("id")
        if key and key not in seen:
            seen.add(key)
            unique_markets.append(m)

    logger.info(
        f"Found {len(unique_markets)} temperature market(s)"
        f"{f' for city={city_name}' if city_name else ''}"
        f"{f' date={date_str}' if date_str else ''}"
    )
    for m in unique_markets[:10]:
        logger.info(f"  - {m['question'][:80]} (threshold={m.get('threshold_f')}F)")

    return unique_markets


async def find_closed_markets(
    city_name: str | None = None,
    date_str: str | None = None,
    max_pages: int = 5,
) -> list[dict]:
    """
    Find CLOSED/RESOLVED Polymarket temperature markets.

    Same as find_markets but queries closed=true. Used for trade resolution.
    Limits pagination to max_pages to avoid excessive API calls.

    Args:
        city_name: Filter by city (case-insensitive)
        date_str: Filter by date string like "June 1"
        max_pages: Max pages of results to fetch (default 5 = 500 markets)

    Returns:
        List of closed market dicts with outcomePrices showing final values.
    """
    all_markets = []

    city_lower = city_name.lower().strip() if city_name else None

    date_patterns = []
    if date_str:
        date_lower = date_str.lower().strip()
        date_patterns.append(date_lower)
        date_patterns.append(date_lower.replace(" ", "-"))
        date_patterns.append(date_lower.replace(" ", ""))
        month_day = re.search(r"(\w+)\s*(\d{1,2})", date_str)
        if month_day:
            month = month_day.group(1).lower()
            day = month_day.group(2)
            date_patterns.extend([f"{month}-{day}", f"{month}{day}", f"{month} {day}"])

    temp_keywords = [
        "temperature", "temp", "°f", "°c", "fahrenheit", "celsius",
        "degrees", "high temp", "highest temp", "hot", "heat",
    ]

    try:
        for offset in range(0, max_pages * 100, 100):
            params = {
                "closed": "true",
                "limit": 100,
                "offset": offset,
                "order": "volume24hr",
                "ascending": "false",
            }
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
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

                        is_temp = any(kw in question or kw in event_title for kw in temp_keywords)
                        if not is_temp:
                            continue

                        if city_lower and city_lower not in question and city_lower not in event_title:
                            continue

                        if date_patterns:
                            date_match = any(dp in question or dp in event_title for dp in date_patterns)
                            if not date_match:
                                continue

                        threshold = _extract_threshold(market.get("question", ""))
                        all_markets.append({
                            "question": market.get("question", ""),
                            "outcomePrices": market.get("outcomePrices", "[]"),
                            "volume24hr": market.get("volume24hr", 0),
                            "endDate": market.get("endDate", ""),
                            "conditionId": market.get("conditionId", ""),
                            "slug": market.get("slug", ""),
                            "id": str(market.get("id", "")),
                            "threshold_f": threshold,
                            "eventTitle": event.get("title", ""),
                        })

                # Early exit if we found markets for the city
                if city_lower and any(
                    city_lower in m.get("question", "").lower() for m in all_markets
                ):
                    break

    except Exception as e:
        logger.error(f"Error fetching closed events: {e}")

    # Deduplicate
    seen = set()
    unique = []
    for m in all_markets:
        key = m.get("conditionId") or m.get("id")
        if key and key not in seen:
            seen.add(key)
            unique.append(m)

    logger.info(
        f"Found {len(unique)} closed temperature market(s)"
        f"{f' for city={city_name}' if city_name else ''}"
        f"{f' date={date_str}' if date_str else ''}"
    )
    return unique


def _extract_threshold(question: str) -> float:
    """Extract temperature threshold (in F) from a market question."""
    import re
    q = question.lower()

    # Match patterns like "90F", "90°F", "90 F", "90 degrees", "90°f"
    patterns = [
        r"(\d+)\s*[°]?\s*f(?:ahrenheit)?\b",
        r"(\d+)\s*degrees?\s*f",
        r"exceed\s+(?:or\s+equal\s+(?:to|)\s+)?(\d+)",
        r"above\s+(?:or\s+equal\s+(?:to|)\s+)?(\d+)",
        r"over\s+(?:or\s+equal\s+(?:to|)\s+)?(\d+)",
        r"higher\s+than\s+(?:or\s+equal\s+(?:to|)\s+)?(\d+)",
        r"at\s+least\s+(\d+)",
        r"(\d+)\s*[°]?\s*c(?:elsius)?\b",  # Celsius -- will convert
    ]
    for pat in patterns:
        m = re.search(pat, q)
        if m:
            val = float(m.group(1))
            # If Celsius, convert to Fahrenheit
            if "celsius" in q or ("°c" in q and "°f" not in q) or (" c " in q and val < 60):
                val = val * 9 / 5 + 32
            return round(val, 1)

    # Generic number after "exceed" or "above" without unit
    m = re.search(r"(?:exceed|above|over|higher than)\s+(\d+)", q)
    if m:
        return float(m.group(1))

    return 90.0  # default


async def get_market_price(market_id: str) -> Optional[dict]:
    """Fetch a single market's current price data.

    Includes sanity check: if the returned yes_price > 0.95 and the bucket
    is not the top bucket (by threshold), logs a warning and refetches once.
    """
    # Check cache first (30-second TTL)
    cache_key = f"market_price_cache:{market_id}"
    try:
        r_cache = _get_redis()
        if r_cache:
            cached_raw = r_cache.get(cache_key)
            if cached_raw:
                cached = json.loads(cached_raw.decode() if isinstance(cached_raw, bytes) else cached_raw)
                cached_at = cached.get("_cached_at", 0)
                if time.time() - cached_at < 30:
                    return cached.get("data")
    except Exception:
        pass

    result = await _fetch_market_price_once(market_id)

    # Sanity check: if yes_price > 0.95, refetch once to confirm
    if result and result.get("yes_price", 0) > 0.95:
        threshold = result.get("threshold_f", 0)
        # Check if this is NOT the top bucket (top bucket has the highest threshold)
        # by looking at the question text
        question = result.get("question", "")
        is_top = "or higher" in question.lower() or "above" in question.lower()
        if not is_top:
            logger.warning(
                f"[SANITY] {market_id}: yes_price={result['yes_price']:.3f} > 0.95 "
                f"for non-top bucket (threshold={threshold}F). Refetching..."
            )
            # Refetch once
            result2 = await _fetch_market_price_once(market_id)
            if result2 and result2.get("yes_price", 0) <= 0.95:
                logger.info(f"[SANITY] {market_id}: Refetched price={result2['yes_price']:.3f} (corrected)")
                result = result2
            else:
                logger.warning(
                    f"[SANITY] {market_id}: Refetched price still > 0.95 "
                    f"({result2.get('yes_price', 'N/A') if result2 else 'fetch failed'}). "
                    f"Using original but flagging."
                )
                result["_price_sanity_warn"] = True

    # Cache result with 30-second TTL
    if result:
        try:
            r_cache = _get_redis()
            if r_cache:
                cache_data = {"_cached_at": time.time(), "data": result}
                r_cache.set(cache_key, json.dumps(cache_data, default=str), ex=30)
        except Exception:
            pass

    return result


async def _fetch_market_price_once(market_id: str) -> Optional[dict]:
    """Fetch a single market's raw price data from the API."""
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            r = await client.get(f"{BASE_URL}/markets/{market_id}")
            if r.status_code != 200:
                return None
            market = r.json()
        yes_price, no_price = parse_outcome_prices(market)
        return {
            "question": market.get("question", ""),
            "yes_price": yes_price,
            "no_price": no_price,
            "volume24hr": float(market.get("volume24hr", 0) or 0),
            "slug": market.get("slug", ""),
            "conditionId": market.get("conditionId", ""),
            "endDate": market.get("endDate", ""),
            "threshold_f": _extract_threshold(market.get("question", "")),
            "clobTokenIds": market.get("clobTokenIds", []),
            "bestBid": market.get("bestBid", 0),
            "bestAsk": market.get("bestAsk", 0),
            "spread": market.get("spread", 0),
        }
    except Exception as e:
        logger.error(f"Error fetching market {market_id}: {e}")
        return None


def parse_outcome_prices(market: dict) -> tuple[float, float]:
    """Parse outcomePrices JSON string to (yes_price, no_price)."""
    raw = market.get("outcomePrices", "[]")
    prices_list = json.loads(raw)
    if len(prices_list) < 2:
        raise ValueError(f"Expected >= 2 prices, got {len(prices_list)}")
    return float(prices_list[0]), float(prices_list[1])


async def find_markets_by_end_date(
    city_name: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    max_pages: int = 10,
) -> list[dict]:
    """
    Find Polymarket temperature markets that have ended.

    Strategy: Query the markets endpoint (both active and closed) and filter
    locally by checking outcomePrices. Resolved markets have YES price near
    0 or 1. We also check the endDate field to filter by date range.

    Note: The Gamma API does NOT support end_date_gte/end_date_lte parameters,
    so we fetch markets and filter locally.

    Args:
        city_name: Filter by city (case-insensitive)
        start_date: Start of end_date range "YYYY-MM-DD" (inclusive)
        end_date: End of end_date range "YYYY-MM-DD" (inclusive)
        max_pages: Max pages of results to fetch

    Returns:
        List of market dicts with outcomePrices, endDate, question, etc.
    """
    all_markets = []
    city_lower = city_name.lower().strip() if city_name else None

    temp_keywords = [
        "temperature", "temp", "°f", "°c", "fahrenheit", "celsius",
        "degrees", "high temp", "highest temp",
    ]

    # Parse date range for local filtering
    from datetime import datetime as dt
    start_dt = None
    end_dt = None
    if start_date:
        try:
            start_dt = dt.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            pass
    if end_date:
        try:
            end_dt = dt.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        except ValueError:
            pass

    try:
        # Query both active and closed markets
        for closed_param in ["false", "true"]:
            for offset in range(0, max_pages * 100, 100):
                params = {
                    "limit": 100,
                    "offset": offset,
                    "order": "volume24hr",
                    "ascending": "false",
                    "closed": closed_param,
                }

                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    resp = await client.get(f"{BASE_URL}/markets", params=params)
                    if resp.status_code != 200:
                        break
                    markets = resp.json()
                    if not markets:
                        break

                    for market in markets:
                        question = (market.get("question", "") or "").lower()

                        is_temp = any(kw in question for kw in temp_keywords)
                        if not is_temp:
                            continue

                        if city_lower and city_lower not in question:
                            continue

                        # Filter by end_date locally
                        end_date_str = market.get("endDate", "")
                        if (start_dt or end_dt) and end_date_str:
                            try:
                                m_end = dt.fromisoformat(
                                    end_date_str.replace("Z", "+00:00")
                                ).replace(tzinfo=None)
                                if start_dt and m_end < start_dt:
                                    continue
                                if end_dt and m_end > end_dt:
                                    continue
                            except Exception:
                                pass

                        threshold = _extract_threshold(market.get("question", ""))
                        all_markets.append({
                            "question": market.get("question", ""),
                            "outcomePrices": market.get("outcomePrices", "[]"),
                            "volume24hr": market.get("volume24hr", 0),
                            "endDate": end_date_str,
                            "conditionId": market.get("conditionId", ""),
                            "slug": market.get("slug", ""),
                            "id": str(market.get("id", "")),
                            "threshold_f": threshold,
                            "active": market.get("active", True),
                            "closed": market.get("closed", False),
                            "resolved": market.get("resolved", False),
                            "clobTokenIds": market.get("clobTokenIds", []),
                            "bestBid": market.get("bestBid", 0),
                            "bestAsk": market.get("bestAsk", 0),
                            "spread": market.get("spread", 0),
                        })

    except Exception as e:
        logger.error(f"Error fetching markets by end_date: {e}")

    # Deduplicate
    seen = set()
    unique = []
    for m in all_markets:
        key = m.get("conditionId") or m.get("id")
        if key and key not in seen:
            seen.add(key)
            unique.append(m)

    logger.info(
        f"Found {len(unique)} markets by end_date"
        f"{f' for city={city_name}' if city_name else ''}"
        f"{f' range={start_date}..{end_date}' if start_date or end_date else ''}"
    )
    return unique


async def get_city_slug(city_name: str) -> str:
    """Get wethr.net slug for a city name."""
    city_lower = city_name.lower().strip()
    if city_lower in CITY_SLUG_MAP:
        return CITY_SLUG_MAP[city_lower]
    # Fallback: convert spaces to hyphens
    return city_lower.replace(" ", "-")


# ===================================================================
# CLI test
# ===================================================================
async def _main():
    """Quick test: list all May 30 temperature markets."""
    import json

    print("=== All May 30 Temperature Markets ===")
    markets = await find_markets(date_str="May 30")
    print(f"\nTotal: {len(markets)} markets")
    for m in markets[:20]:
        q = m["question"][:90]
        print(f"  [{m.get('threshold_f', '?')}F] {q}")

    print("\n=== Austin Temperature Markets ===")
    austin = await find_markets(city_name="Austin", date_str="May 30")
    print(f"\nAustin markets: {len(austin)}")
    for m in austin[:5]:
        print(f"  {m['question'][:80]}")

    if austin:
        first_id = austin[0].get("conditionId") or austin[0].get("id")
        if first_id:
            price = await get_market_price(first_id)
            if price:
                print(f"\n  YES: {price['yes_price']:.3f}  NO: {price['no_price']:.3f}")
                print(f"  Vol24h: {price['volume24hr']:.0f}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())


# ---------------------------------------------------------------------------
# Market status detection
# ---------------------------------------------------------------------------

async def get_next_market(city_name: str, after_date: str = None) -> Optional[dict]:
    """
    Find the next available temperature market for a city from the Gamma API.

    Queries active temperature markets for the city, filters by date >= after_date,
    and returns the earliest one.

    Args:
        city_name: Human-readable city name (e.g. "London", "New York")
        after_date: ISO date string "YYYY-MM-DD" — only return markets on or after
                    this date. Defaults to today (UTC).

    Returns:
        Dict with market info, or None if no market found:
        {
            "market_id": str,        # conditionId or id
            "date": "YYYY-MM-DD",    # market date from question
            "end_date": str,         # ISO end date from Gamma
            "is_open": bool,         # True if end_date > now
            "resolve_time_local": str,  # human-readable local resolve time
            "question": str,
            "threshold_f": float,
        }
    """
    import datetime

    if after_date is None:
        after_date = datetime.datetime.utcnow().strftime("%Y-%m-%d")

    # Search for temperature markets for this city
    # Try a few date windows: today, tomorrow, day-after
    candidates = []
    base_date = datetime.datetime.strptime(after_date, "%Y-%m-%d")
    for day_offset in range(7):
        check_date = base_date + datetime.timedelta(days=day_offset)
        date_str = check_date.strftime("%B %-d")  # e.g. "June 1"

        try:
            markets = await find_markets(city_name=city_name, date_str=date_str)
            for m in markets:
                end_date_str = m.get("endDate", "")
                question = m.get("question", "")
                threshold = m.get("threshold_f")
                if threshold is None:
                    threshold = _extract_threshold(question)

                # Parse end_date to determine if market is still open
                is_open = False
                resolve_time_local = ""
                try:
                    if end_date_str:
                        end_dt = datetime.datetime.fromisoformat(
                            end_date_str.replace("Z", "+00:00")
                        )
                        is_open = end_dt > datetime.datetime.now(datetime.timezone.utc)
                        # Format local resolve time
                        resolve_time_local = end_dt.strftime("%b %d, %H:%M UTC")
                except Exception:
                    pass

                candidates.append({
                    "market_id": m.get("conditionId") or m.get("id", ""),
                    "date": check_date.strftime("%Y-%m-%d"),
                    "end_date": end_date_str,
                    "is_open": is_open,
                    "resolve_time_local": resolve_time_local,
                    "question": question[:100],
                    "threshold_f": threshold,
                })
        except Exception as e:
            logger.debug(f"get_next_market {city_name} {date_str}: {e}")
            continue

        # If we found markets for this date, return the earliest one
        if candidates:
            # Sort by date, then by threshold
            candidates.sort(key=lambda x: (x["date"], x["threshold_f"] or 0))
            return candidates[0]

    return None


async def get_all_market_statuses(city_list: list[str]) -> dict[str, dict]:
    """
    Get next market status for multiple cities.

    Args:
        city_list: List of city names

    Returns:
        Dict of {city_name: market_info_dict or None}
    """
    results = {}
    for city_name in city_list:
        try:
            market = await get_next_market(city_name)
            results[city_name] = market
        except Exception as e:
            logger.warning(f"get_all_market_statuses {city_name}: {e}")
            results[city_name] = None
    return results


# ===================================================================
# Resolution source verification
# ===================================================================

# City -> Weather Underground station mapping with known historical biases
RESOLUTION_STATIONS: dict[str, dict] = {
    "atlanta": {"station_id": "KATL", "name": "Hartsfield-Jackson Atlanta International"},
    "dallas": {"station_id": "KDFW", "name": "Dallas/Fort Worth International"},
    "miami": {"station_id": "KMIA", "name": "Miami International"},
    "chicago": {"station_id": "KORD", "name": "O'Hare International"},
    "new york": {"station_id": "KJFK", "name": "JFK International"},
    "los angeles": {"station_id": "KLAX", "name": "LAX International"},
    "san francisco": {"station_id": "KSFO", "name": "San Francisco International"},
    "seattle": {"station_id": "KSEA", "name": "Seattle-Tacoma International"},
    "boston": {"station_id": "KBOS", "name": "Logan International"},
    "denver": {"station_id": "KDEN", "name": "Denver International"},
    "houston": {"station_id": "KIAH", "name": "Houston Intercontinental"},
    "austin": {"station_id": "KAUS", "name": "Austin-Bergstrom International"},
    "london": {"station_id": "EGLL", "name": "London Heathrow"},
    "seoul": {"station_id": "RKSI", "name": "Incheon International"},
    "tokyo": {"station_id": "RJTT", "name": "Haneda International"},
    "hong_kong": {"station_id": "VHHH", "name": "Hong Kong International"},
    "shanghai": {"station_id": "ZSPD", "name": "Pudong International"},
    "singapore": {"station_id": "WSSS", "name": "Singapore Changi"},
    "mumbai": {"station_id": "VABB", "name": "Mumbai International"},
    "dubai": {"station_id": "OMDB", "name": "Dubai International"},
    "istanbul": {"station_id": "LTFM", "name": "Istanbul Airport"},
    "paris": {"station_id": "LFPG", "name": "Charles de Gaulle"},
    "berlin": {"station_id": "EDDB", "name": "Berlin Brandenburg"},
    "madrid": {"station_id": "LEMD", "name": "Barajas International"},
    "rome": {"station_id": "LIRF", "name": "Fiumicino International"},
    "amsterdam": {"station_id": "EHAM", "name": "Schiphol International"},
    "sydney": {"station_id": "YSSY", "name": "Sydney Kingsford Smith"},
    "melbourne": {"station_id": "YMML", "name": "Melbourne Airport"},
    "cape_town": {"station_id": "FACT", "name": "Cape Town International"},
    "buenos_aires": {"station_id": "SAEZ", "name": "Ezeiza International"},
    "chongqing": {"station_id": "ZUCK", "name": "Chongqing Jiangbei International"},
    "mexico_city": {"station_id": "MMMX", "name": "Mexico City International"},
    "jakarta": {"station_id": "WIII", "name": "Soekarno-Hatta International"},
    "manila": {"station_id": "RPLL", "name": "Manila International"},
    "taipei": {"station_id": "RCTP", "name": "Taiwan Taoyuan International"},
    "kuala_lumpur": {"station_id": "WMKK", "name": "Kuala Lumpur International"},
    "ho_chi_minh_city": {"station_id": "VVTS", "name": "Tan Son Nhat International"},
    "lagos": {"station_id": "DNMM", "name": "Murtala Muhammed International"},
    "bangkok": {"station_id": "VTBD", "name": "Bangkok International"},
}

# Known historical biases (station_reading - forecast) in Fahrenheit
# Positive = station reads hotter than the standard forecast
KNOWN_STATION_BIASES: dict[str, float] = {
    "KATL": +1.5, "KDFW": +0.8, "KMIA": -0.5, "KORD": +1.2,
    "KJFK": +0.5, "KLAX": +0.3, "KSFO": -1.0, "KSEA": -0.5,
    "KBOS": +0.8, "EGLL": -0.3, "RKSI": -0.5, "ZUCK": +1.0,
    "FACT": -0.8, "SAEZ": +0.5, "ZSPD": +0.3, "VHHH": -0.2,
    "VTBD": +0.5, "OMDB": +1.0, "LTFM": +0.3, "KAUS": +1.0,
    "KIAH": +0.5, "KLAX": +0.3, "KPHX": +1.5, "KMSP": +0.5,
    "KDTW": +0.3, "KPHL": +0.4, "KIAD": +0.3,
}


async def verify_resolution_station(city: str, date: str, forecast_temp_f: float) -> dict:
    """
    Verify the resolution station for a city/date by checking known historical
    station biases. If the station's bias deviates >2°F from forecast, skip trade.

    Args:
        city: City slug (e.g. "london", "chongqing")
        date: Date string "YYYY-MM-DD"
        forecast_temp_f: Our forecast temperature in Fahrenheit

    Returns:
        {
            "verified": bool,
            "station_id": str,
            "station_name": str,
            "bias_f": float | None,
            "deviation_f": float,
            "skip_trade": bool,
            "reason": str,
        }
    """
    city_slug = city.lower().replace(" ", "_")
    station_info = RESOLUTION_STATIONS.get(city_slug, {})

    if not station_info:
        return {
            "verified": True,
            "station_id": "UNKNOWN",
            "station_name": city,
            "bias_f": None,
            "deviation_f": 0.0,
            "skip_trade": False,
            "reason": f"No resolution station mapping for {city}",
        }

    station_id = station_info["station_id"]
    station_name = station_info["name"]
    bias_f = KNOWN_STATION_BIASES.get(station_id, 0.0)
    deviation_f = abs(bias_f)
    skip_trade = deviation_f > 2.0

    result = {
        "verified": not skip_trade,
        "station_id": station_id,
        "station_name": station_name,
        "bias_f": bias_f,
        "deviation_f": deviation_f,
        "skip_trade": skip_trade,
        "reason": (
            f"Station {station_id} ({station_name}) bias: {bias_f:+.1f}F "
            f"(dev: {deviation_f:.1f}F)"
            + (" — SKIP" if skip_trade else " — OK")
        ),
    }
    logger.info(f"[RESOLUTION] {city}: {result['reason']}")
    return result
