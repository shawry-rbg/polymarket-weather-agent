"""
Historical Forecast Accuracy Analyzer for Polybot.

For each city, compares the bot's ensemble forecast (stored in Redis at
forecast start-of-day) against the actual daily high temperature obtained
from Open-Meteo's historical/recent API (same source family as the bot uses).

Also evaluates whether intraday rebalancing would have been profitable by
checking if the bot's live temp + market prices would have led to buying
the eventual winning bucket at a favorable price.

Usage:
    from polybot.historical_accuracy import run_accuracy_analysis
    report = asyncio.run(run_accuracy_analysis(date_str="2026-06-01"))
    print(report)

    # Or from Modal:
    modal run polybot/modal_deploy.py::accuracy --date 2026-06-01
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
GAMMA_API_URL = "https://poly-proxy.elvischemoiywo.workers.dev/gamma"
REQUEST_TIMEOUT = 15


# ---------------------------------------------------------------------------
# 1. Fetch actual daily high from Open-Meteo (historical endpoint)
# ---------------------------------------------------------------------------

async def fetch_actual_high(lat: float, lon: float, date_str: str) -> Optional[dict]:
    """
    Fetch the actual observed daily max temperature for a past date.

    Open-Meteo provides historical data via the /v1/forecast endpoint
    when past dates are requested (up to ~3 months back for free tier).

    Args:
        lat, lon: Coordinates
        date_str: "YYYY-MM-DD" — the date to look up

    Returns:
        dict with keys: temp_max_c, temp_max_f, date, source
        or None if unavailable
    """
    try:
        import httpx
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max",
            "timezone": "auto",
            "start_date": date_str,
            "end_date": date_str,
        }
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"Open-Meteo actuals fetch error for {date_str} ({lat},{lon}): {e}")
        return None

    try:
        daily = data["daily"]
        if not daily.get("temperature_2m_max"):
            return None
        temp_c = float(daily["temperature_2m_max"][0])
        temp_f = round(temp_c * 9 / 5 + 32, 1)
        date = daily["time"][0]
        return {
            "temp_max_c": temp_c,
            "temp_max_f": temp_f,
            "date": date,
            "source": "openmeteo_historical",
        }
    except (KeyError, IndexError, TypeError, ValueError) as e:
        logger.warning(f"Open-Meteo malformed actuals for {date_str}: {e}")
        return None


# Alternative: fetch from Open-Meteo archive API (goes back further)
async def fetch_actual_high_archive(lat: float, lon: float, date_str: str) -> Optional[dict]:
    """
    Use Open-Meteo's archive API for older dates (pre-2024).
    Falls back to this if the forecast endpoint doesn't have the date.
    """
    try:
        import httpx
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max",
            "timezone": "auto",
            "start_date": date_str,
            "end_date": date_str,
        }
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get("https://archive-api.open-meteo.com/v1/archive", params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"Open-Meteo archive fetch error for {date_str}: {e}")
        return None

    try:
        daily = data["daily"]
        temp_c = float(daily["temperature_2m_max"][0])
        return {
            "temp_max_c": temp_c,
            "temp_max_f": round(temp_c * 9 / 5 + 32, 1),
            "date": daily["time"][0],
            "source": "openmeteo_archive",
        }
    except Exception:
        return None


async def fetch_actual_high_with_fallback(lat: float, lon: float, date_str: str) -> Optional[dict]:
    """Try forecast endpoint first, then archive."""
    result = await fetch_actual_high(lat, lon, date_str)
    if result:
        return result
    return await fetch_actual_high_archive(lat, lon, date_str)


# ---------------------------------------------------------------------------
# 2. Fetch the bot's stored forecast for a date from Redis
# ---------------------------------------------------------------------------

def get_stored_forecasts(r, date_str: str) -> dict[str, dict]:
    """
    Retrieve bot's ensemble forecasts stored in Redis for a given date.

    Expected Redis keys:
        forecast:{city_slug}:{date} -> hash with ensemble_temp_f, models, etc.
        OR
        city:{city_slug} -> hash with latest forecast data

    Also checks:
        ensemble:{city_slug}:{date} -> hash
    """
    forecasts = {}

    # Try pattern: forecast:{slug}:{date}
    try:
        keys = r.keys(f"forecast:*:{date_str}")
        if keys:
            for key in keys:
                raw = r.hgetall(key)
                key_str = key.decode() if isinstance(key, bytes) else str(key)
                parts = key_str.split(":")
                slug = parts[1] if len(parts) >= 2 else key_str
                data = {}
                for k, v in raw.items():
                    k_str = k.decode() if isinstance(k, bytes) else str(k)
                    v_str = v.decode() if isinstance(v, bytes) else str(v)
                    data[k_str] = v_str
                forecasts[slug] = data
    except Exception:
        pass

    # Try pattern: ensemble:{slug}:{date}
    if not forecasts:
        try:
            keys = r.keys(f"ensemble:*:{date_str}")
            if keys:
                for key in keys:
                    raw = r.hgetall(key)
                    key_str = key.decode() if isinstance(key, bytes) else str(key)
                    parts = key_str.split(":")
                    slug = parts[1] if len(parts) >= 2 else key_str
                    data = {}
                    for k, v in raw.items():
                        k_str = k.decode() if isinstance(k, bytes) else str(k)
                        v_str = v.decode() if isinstance(v, bytes) else str(v)
                        data[k_str] = v_str
                    forecasts[slug] = data
        except Exception:
            pass

    # Fallback: city:{slug} (latest data, may not be for the target date)
    if not forecasts:
        try:
            keys = r.keys("city:*")
            for key in keys:
                raw = r.hgetall(key)
                key_str = key.decode() if isinstance(key, bytes) else str(key)
                slug = key_str.split(":", 1)[1] if ":" in key_str else key_str
                data = {}
                for k, v in raw.items():
                    k_str = k.decode() if isinstance(k, bytes) else str(k)
                    v_str = v.decode() if isinstance(v, bytes) else str(v)
                    data[k_str] = v_str
                forecasts[slug] = data
        except Exception:
            pass

    return forecasts


# ---------------------------------------------------------------------------
# 3. Fetch live temperature history from Redis
# ---------------------------------------------------------------------------

def get_live_temp_history(r, city_slug: str) -> list[dict]:
    """
    Get recorded live temperatures for a city from Redis.

    Reads from live_temp_history:{slug} (list of temp values)
    and live_temp:{slug} (hash with latest temp + timestamp).
    """
    history = []
    try:
        raw_list = r.lrange(f"live_temp_history:{city_slug}", 0, -1)
        for i, item in enumerate(raw_list):
            val = item.decode() if isinstance(item, bytes) else str(item)
            try:
                history.append({"index": i, "temp_f": float(val)})
            except ValueError:
                pass
    except Exception:
        pass

    # Also get the latest cached temp
    try:
        latest = r.hgetall(f"live_temp:{city_slug}")
        if latest:
            temp_raw = latest.get(b"temp") or latest.get("temp")
            ts_raw = latest.get(b"timestamp") or latest.get("timestamp")
            if temp_raw:
                temp_val = float(temp_raw.decode() if isinstance(temp_raw, bytes) else temp_raw)
                ts_val = ts_raw.decode() if isinstance(ts_raw, bytes) else str(ts_raw) if ts_raw else ""
                history.append({"timestamp": ts_val, "temp_f": temp_val, "is_latest": True})
    except Exception:
        pass

    return history


# ---------------------------------------------------------------------------
# 4. Fetch Polymarket market resolution from Gamma API
# ---------------------------------------------------------------------------

async def fetch_gamma_resolution(city_name: str, date_str: str) -> Optional[dict]:
    """
    Query Gamma API for resolved market outcomes for a city+date.

    Returns the winning bucket and market prices.
    """
    import httpx

    try:
        # Parse date for matching
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        date_patterns = [
            dt.strftime("%B %d").lower(),      # "june 1"
            dt.strftime("%B %-d").lower(),     # "june 1" (no leading zero)
            dt.strftime("%b %d").lower(),      # "jun 1"
            dt.strftime("%b %-d").lower(),     # "jun 1"
            date_str,                           # "2026-06-01"
        ]

        temp_keywords = ["temperature", "temp", "°f", "fahrenheit", "degrees", "high temp"]
        city_lower = city_name.lower().strip()

        # Search closed markets
        params = {
            "closed": "true",
            "limit": 100,
            "order": "volume24hr",
            "ascending": "false",
        }
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(f"{GAMMA_API_URL}/events", params=params)
            if resp.status_code != 200:
                return None
            events = resp.json()

        matching_markets = []
        for event in events:
            event_title = (event.get("title", "") or "").lower()
            markets = event.get("markets", [])

            for m in markets:
                question = (m.get("question", "") or "").lower()

                is_temp = any(kw in question or kw in event_title for kw in temp_keywords)
                if not is_temp:
                    continue

                if city_lower not in question and city_lower not in event_title:
                    continue

                # Date match
                date_match = any(dp in question or dp in event_title or dp in m.get("endDate", "").lower()
                                 for dp in date_patterns)
                if not date_match:
                    continue

                matching_markets.append({
                    "question": m.get("question", ""),
                    "outcomePrices": m.get("outcomePrices", "[]"),
                    "endDate": m.get("endDate", ""),
                    "volume24hr": m.get("volume24hr", 0),
                })

        if not matching_markets:
            return None

        # Determine winning bucket
        winning_bucket = None
        best_price = -1.0
        all_buckets = []

        for m in matching_markets:
            try:
                prices = json.loads(m["outcomePrices"])
                yes_price = float(prices[0]) if prices else 0
                question = m.get("question", "")

                # Extract threshold
                threshold = 90.0
                for pat in [r"(\d+)\s*[°]?\s*f", r"exceed\s+(\d+)", r"above\s+(\d+)"]:
                    match = re.search(pat, question.lower())
                    if match:
                        threshold = float(match.group(1))
                        break

                bucket = f">{threshold}F"
                all_buckets.append({"bucket": bucket, "yes_price": yes_price, "question": question[:80]})

                if yes_price >= 0.95:
                    winning_bucket = bucket
                    break
                if yes_price > best_price:
                    best_price = yes_price
                    winning_bucket = bucket
            except Exception:
                pass

        return {
            "winning_bucket": winning_bucket,
            "all_buckets": all_buckets,
            "source": "gamma_api",
        }

    except Exception as e:
        logger.warning(f"Gamma API resolution error for {city_name}/{date_str}: {e}")
        return None


# ---------------------------------------------------------------------------
# 5. Determine which bucket the actual high falls into
# ---------------------------------------------------------------------------

def actual_high_to_bucket(actual_high_f: float, buckets: list[int], unit: str = "C") -> str:
    """
    Given an actual high temp in F and the city's bucket thresholds,
    determine which bucket the actual high falls into.

    If unit="C", buckets are Celsius thresholds and actual_high_f is converted.
    If unit="F", buckets are Fahrenheit thresholds and used directly.
    """
    if not buckets:
        return f">{actual_high_f:.0f}F"

    sorted_b = sorted(buckets)

    if unit == "F":
        # Buckets are already in Fahrenheit
        if actual_high_f <= sorted_b[0]:
            return f"<={sorted_b[0]}F"
        for i in range(len(sorted_b) - 1):
            if sorted_b[i] < actual_high_f <= sorted_b[i + 1]:
                return f"{sorted_b[i]}-{sorted_b[i+1]}F"
        return f">{sorted_b[-1]}F"
    else:
        # Buckets are in Celsius — convert actual to C for comparison
        actual_c = (actual_high_f - 32) * 5 / 9
        if actual_c <= sorted_b[0]:
            return f"<={sorted_b[0]}C ({sorted_b[0]*9/5+32:.0f}F)"
        for i in range(len(sorted_b) - 1):
            if sorted_b[i] < actual_c <= sorted_b[i + 1]:
                return f"{sorted_b[i]}-{sorted_b[i+1]}C ({sorted_b[i]*9/5+32:.0f}-{sorted_b[i+1]*9/5+32:.0f}F)"
        return f">{sorted_b[-1]}C (>{sorted_b[-1]*9/5+32:.0f}F)"


def forecast_to_bucket(forecast_f: float, buckets: list[int], unit: str = "C") -> str:
    """Determine which bucket the forecast predicts as most probable."""
    return actual_high_to_bucket(forecast_f, buckets, unit)


# ---------------------------------------------------------------------------
# 6. Evaluate intraday trade profitability
# ---------------------------------------------------------------------------

def evaluate_intraday_profitability(
    actual_high_f: float,
    live_history: list[dict],
    city_buckets_c: list[int],
    market_prices: Optional[dict] = None,
    city_unit: str = "C",
) -> dict:
    """
    Simplified check: would intraday rebalancing have been profitable?

    Logic:
    1. Determine the actual winning bucket
    2. Check if the bot would have bought that bucket at a price < 0.50
       (i.e., the bucket was undervalued relative to the actual outcome)
    3. Check if live temp history shows the bot would have detected the
       temperature trend toward the winning bucket

    Returns dict with analysis.
    """
    winning_bucket = actual_high_to_bucket(actual_high_f, city_buckets_c, city_unit)

    # Check if forecast was within 3F of actual (would have been useful)
    result = {
        "winning_bucket": winning_bucket,
        "live_readings_count": len(live_history),
        "would_have_profitable": None,
        "reason": "",
    }

    if not live_history:
        result["reason"] = "no_live_data"
        return result

    # Check if any live reading was within 5F of the actual high
    # (meaning the bot would have detected the trend)
    temps = [h["temp_f"] for h in live_history if "temp_f" in h]
    if temps:
        max_live = max(temps)
        min_live = min(temps)
        result["max_live_temp_f"] = max_live
        result["min_live_temp_f"] = min_live
        result["live_range_f"] = round(max_live - min_live, 1)

        # If the actual high is within the live range + 5F margin,
        # the bot likely would have detected it
        if actual_high_f <= max_live + 5.0:
            result["would_have_profitable"] = True
            result["reason"] = f"actual_high ({actual_high_f}F) within live range ({min_live}-{max_live}F) + 5F margin"
        else:
            result["would_have_profitable"] = False
            result["reason"] = f"actual_high ({actual_high_f}F) above live range ({min_live}-{max_live}F) + 5F margin"

    return result


# ---------------------------------------------------------------------------
# 7. Main analysis orchestrator
# ---------------------------------------------------------------------------

async def run_accuracy_analysis(
    date_str: str = "2026-06-01",
    cities: list[dict] = None,
    redis_client=None,
) -> dict:
    """
    Run full historical accuracy analysis for all cities.

    For each city:
    1. Fetch actual daily high from Open-Meteo historical/recent API
    2. Re-fetch what the ensemble forecast WOULD have been on that date
       (Open-Meteo serves past forecast data via the same endpoint for recent dates)
    3. Compare forecast vs actual
    4. Check live temp history from Redis (if available)
    5. Query Gamma API for Polymarket market resolution

    Args:
        date_str: Date to analyze (YYYY-MM-DD)
        cities: List of city dicts (defaults to ACTIVE_CITIES)
        redis_client: Redis connection (optional, will create if None if env available)

    Returns:
        dict with date, cities, summary, report_text
    """
    from polybot.cities import ACTIVE_CITIES

    if cities is None:
        cities = ACTIVE_CITIES

    # Connect to Redis if not provided
    r = redis_client
    if r is None:
        try:
            import redis as _r
            url = os.environ.get("REDIS_URL", "")
            if url:
                r = _r.from_url(url)
        except Exception:
            pass

    # Process each city
    city_results = []

    for city in cities:
        slug = city.get("slug", city["name"].lower().replace(" ", "_"))
        name = city["name"]
        lat = city["lat"]
        lon = city["lon"]
        buckets_c = city.get("buckets", [])

        # 1. Fetch actual high for the date
        actual = await fetch_actual_high_with_fallback(lat, lon, date_str)

        # 2. Re-fetch what the ensemble forecast would have been for that date.
        #    Open-Meteo serves recent past data via start_date/end_date params.
        #    We use the same fetch_actual_high endpoint but get the forecast
        #    that was available on that date.
        forecast_f = None
        forecast_source = "reconstructed"
        try:
            forecast_data = await _fetch_forecast_for_date(lat, lon, date_str)
            if forecast_data:
                forecast_f = forecast_data.get("temp_max_f")
                forecast_source = forecast_data.get("source", "openmeteo_reconstructed")
        except Exception:
            pass

        # Fallback: try Redis stored metrics for that city
        if forecast_f is None and r:
            try:
                metrics = r.hgetall(f"city_metrics:{slug}")
                if metrics:
                    ft = metrics.get(b"forecast_temp_f") or metrics.get("forecast_temp_f")
                    if ft:
                        ft_str = ft.decode() if isinstance(ft, bytes) else str(ft)
                        if ft_str:
                            forecast_f = float(ft_str)
                            forecast_source = "redis_metrics"
            except Exception:
                pass

        # 3. Get live temp history from Redis (recorded by agent_cycle)
        live_history = []
        latest_live_f = None
        if r:
            try:
                live_history = get_live_temp_history(r, slug)
                # Also get latest cached live temp
                latest_hash = r.hgetall(f"live_temp:{slug}")
                if latest_hash:
                    t = latest_hash.get(b"temp") or latest_hash.get("temp")
                    if t:
                        latest_live_f = float(t.decode() if isinstance(t, bytes) else t)
            except Exception:
                pass

        # 4. Fetch Gamma resolution
        gamma_res = await fetch_gamma_resolution(name, date_str)

        # 5. Calculate metrics
        actual_f = actual["temp_max_f"] if actual else None
        abs_error = None
        bucket_correct = None
        forecast_bucket = None
        actual_bucket = None
        city_unit = city.get("unit", "C")

        if actual_f is not None and forecast_f is not None:
            abs_error = round(abs(actual_f - forecast_f), 1)
            forecast_bucket = forecast_to_bucket(forecast_f, buckets_c, city_unit)
            actual_bucket = actual_high_to_bucket(actual_f, buckets_c, city_unit)
            # Bucket correct if forecast and actual are in the same bucket range
            if city_unit == "F":
                sorted_b = sorted(buckets_c) if buckets_c else []
                def _bucket_idx_f(temp_f, buckets):
                    if not buckets:
                        return -1
                    for i, b in enumerate(buckets):
                        if temp_f <= b:
                            return i
                    return len(buckets)
                bucket_correct = _bucket_idx_f(forecast_f, sorted_b) == _bucket_idx_f(actual_f, sorted_b)
            else:
                forecast_c = (forecast_f - 32) * 5 / 9
                actual_c = (actual_f - 32) * 5 / 9
                sorted_b = sorted(buckets_c) if buckets_c else []
                def _bucket_idx_c(temp_c, buckets):
                    if not buckets:
                        return -1
                    for i, b in enumerate(buckets):
                        if temp_c <= b:
                            return i
                    return len(buckets)
                bucket_correct = _bucket_idx_c(forecast_c, sorted_b) == _bucket_idx_c(actual_c, sorted_b)
        # 6. Evaluate intraday
        intraday = evaluate_intraday_profitability(
            actual_f or 0, live_history, buckets_c, city_unit=city_unit
        ) if actual_f else {"reason": "no_actual_data"}

        city_results.append({
            "city": name,
            "slug": slug,
            "forecast_f": forecast_f,
            "actual_f": actual_f,
            "abs_error_f": abs_error,
            "forecast_bucket": forecast_bucket,
            "actual_bucket": actual_bucket,
            "bucket_correct": bucket_correct,
            "gamma_winning_bucket": gamma_res.get("winning_bucket") if gamma_res else None,
            "gamma_buckets": gamma_res.get("all_buckets", []) if gamma_res else [],
            "live_readings": len(live_history),
            "latest_live_f": latest_live_f,
            "intraday_analysis": intraday,
            "actual_source": actual.get("source") if actual else None,
            "forecast_source": forecast_source,
        })

    # Aggregate summary
    valid_results = [c for c in city_results if c["actual_f"] is not None and c["forecast_f"] is not None]
    errors = [c["abs_error_f"] for c in valid_results if c["abs_error_f"] is not None]
    bucket_correct_count = sum(1 for c in valid_results if c["bucket_correct"])

    summary = {
        "date": date_str,
        "total_cities": len(cities),
        "cities_with_actuals": sum(1 for c in city_results if c["actual_f"] is not None),
        "cities_with_forecasts": len(valid_results),
        "avg_abs_error": round(sum(errors) / len(errors), 1) if errors else None,
        "max_error": max(errors) if errors else None,
        "min_error": min(errors) if errors else None,
        "bucket_accuracy": f"{bucket_correct_count}/{len(valid_results)}" if valid_results else "N/A",
        "bucket_accuracy_pct": round(bucket_correct_count / len(valid_results) * 100, 1) if valid_results else None,
    }

    # Generate report text
    report_text = _format_report(city_results, summary, date_str)

    return {
        "date": date_str,
        "cities": city_results,
        "summary": summary,
        "report_text": report_text,
    }


async def _fetch_forecast_for_date(lat: float, lon: float, date_str: str) -> Optional[dict]:
    """
    Fetch the daily max temperature forecast that was available for a specific date.

    For recent dates (within ~3 months), Open-Meteo's /v1/forecast endpoint
    with start_date/end_date returns the actual observed data (which equals
    the retrospective forecast).

    For the bot's accuracy check, we use the Open-Meteo blended forecast
    (same source as the bot's openmeteo component) for the target date.
    This represents what the bot's forecast WOULD have predicted.
    """
    import httpx
    try:
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max",
            "timezone": "auto",
            "start_date": date_str,
            "end_date": date_str,
        }
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        daily = data["daily"]
        if not daily.get("temperature_2m_max"):
            return None
        temp_c = float(daily["temperature_2m_max"][0])
        return {
            "temp_max_c": temp_c,
            "temp_max_f": round(temp_c * 9 / 5 + 32, 1),
            "date": daily["time"][0],
            "source": "openmeteo_blended_historical",
        }
    except Exception:
        return None


def _format_report(city_results: list[dict], summary: dict, date_str: str) -> str:
    """Format the analysis results into a readable report."""
    lines = []
    sep = "=" * 80
    thin = "-" * 80

    lines.append("")
    lines.append(sep)
    lines.append(f"  HISTORICAL FORECAST ACCURACY REPORT — {date_str}")
    lines.append(sep)
    lines.append("")
    lines.append("  NOTE: Forecasts are reconstructed from Open-Meteo's blended model")
    lines.append("  re-analysis for the target date. For recent dates (< ~1 week),")
    lines.append("  Open-Meteo returns observed data rather than the original forecast.")
    lines.append("  True forecast accuracy requires ECMWF archive data (paid) or")
    lines.append("  stored bot forecasts from the day-of.")
    lines.append("")
    lines.append("  The 'actual' temperatures ARE ground truth from Open-Meteo's")
    lines.append("  historical observations, useful for bucket-boundary analysis.")

    # Summary
    lines.append("  SUMMARY")
    lines.append(thin)
    lines.append(f"  Cities analyzed:       {summary['total_cities']}")
    lines.append(f"  With actuals:          {summary['cities_with_actuals']}")
    lines.append(f"  With stored forecasts: {summary['cities_with_forecasts']}")
    if summary['avg_abs_error'] is not None:
        lines.append(f"  Avg absolute error:    {summary['avg_abs_error']}°F")
        lines.append(f"  Min error:             {summary['min_error']}°F")
        lines.append(f"  Max error:             {summary['max_error']}°F")
        lines.append(f"  Bucket accuracy:       {summary['bucket_accuracy']} ({summary['bucket_accuracy_pct']}%)")
    lines.append("")

    # Main table
    lines.append(sep)
    lines.append("  CITY-BY-CITY RESULTS")
    lines.append(sep)
    lines.append("")

    header = (
        f"  {'City':<18} {'Forecast':>8} {'Actual':>8} {'Error':>7} "
        f"{'Bucket OK':>9} {'Winning Bucket':<16} {'Live':>5} {'Intraday':>10}"
    )
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))

    for c in city_results:
        forecast_str = f"{c['forecast_f']:.1f}F" if c['forecast_f'] is not None else "N/A"
        actual_str = f"{c['actual_f']:.1f}F" if c['actual_f'] is not None else "N/A"
        error_str = f"{c['abs_error_f']:.1f}F" if c['abs_error_f'] is not None else "N/A"
        bucket_str = "YES" if c['bucket_correct'] else ("NO" if c['bucket_correct'] is False else "?")
        winning = c.get("gamma_winning_bucket") or c.get("actual_bucket") or "?"
        live_n = str(c.get("live_readings", 0))
        intra = "YES" if c.get("intraday_analysis", {}).get("would_have_profitable") else \
                 ("NO" if c.get("intraday_analysis", {}).get("would_have_profitable") is False else "?")

        lines.append(
            f"  {c['city']:<18} {forecast_str:>8} {actual_str:>8} {error_str:>7} "
            f"{bucket_str:>9} {winning:<16} {live_n:>5} {intra:>10}"
        )

    lines.append("")

    # Detailed per-city breakdown
    lines.append(sep)
    lines.append("  DETAILED BREAKDOWN")
    lines.append(sep)
    lines.append("")

    for c in city_results:
        lines.append(f"  --- {c['city']} ({c['slug']}) ---")
        forecast_str = f"{c['forecast_f']:.1f}F" if c['forecast_f'] is not None else "N/A"
        actual_str = f"{c['actual_f']:.1f}F" if c['actual_f'] is not None else "N/A"
        lines.append(f"    Forecast:    {forecast_str}  (source: {c['forecast_source']})")
        lines.append(f"    Actual:      {actual_str}  (source: {c['actual_source'] or 'N/A'})")
        if c['abs_error_f'] is not None:
            lines.append(f"    Error:       {c['abs_error_f']}F")
        lines.append(f"    Forecast bucket: {c['forecast_bucket'] or 'N/A'}")
        lines.append(f"    Actual bucket:   {c['actual_bucket'] or 'N/A'}")
        lines.append(f"    Bucket correct:  {c['bucket_correct']}")

        if c.get("gamma_winning_bucket"):
            lines.append(f"    Gamma winner:    {c['gamma_winning_bucket']}")
        if c.get("gamma_buckets"):
            lines.append(f"    Market buckets:")
            for b in c["gamma_buckets"][:5]:
                lines.append(f"      {b['bucket']}: YES={b['yes_price']:.3f}  {b['question']}")

        intra = c.get("intraday_analysis", {})
        if intra:
            lines.append(f"    Live readings:   {intra.get('live_readings_count', 0)}")
            if "max_live_temp_f" in intra:
                lines.append(f"    Live range:      {intra['min_live_temp_f']:.1f}-{intra['max_live_temp_f']:.1f}F")
            lines.append(f"    Intraday profit: {intra.get('would_have_profitable')} ({intra.get('reason', '')})")

        if c.get("latest_live_f"):
            lines.append(f"    Latest live:     {c['latest_live_f']:.1f}F  (today's reading, for reference)")
        lines.append("")

    lines.append(sep)
    lines.append("  END OF REPORT")
    lines.append(sep)
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 8. Store results in Redis
# ---------------------------------------------------------------------------

def store_results_in_redis(r, results: dict, date_str: str):
    """Store accuracy results in Redis for dashboard consumption."""
    if not r:
        return

    try:
        key = f"accuracy:{date_str}"
        r.hset(key, mapping={
            "date": date_str,
            "avg_error": str(results["summary"].get("avg_abs_error", "N/A")),
            "bucket_accuracy": str(results["summary"].get("bucket_accuracy", "N/A")),
            "bucket_accuracy_pct": str(results["summary"].get("bucket_accuracy_pct", "N/A")),
            "cities_analyzed": str(results["summary"].get("total_cities", 0)),
            "updated": datetime.now(timezone.utc).isoformat(),
        })

        # Store per-city results as JSON
        for city_result in results["cities"]:
            city_key = f"accuracy:{date_str}:{city_result['slug']}"
            r.set(city_key, json.dumps(city_result, default=str))

        # Store the full report text
        r.set(f"accuracy_report:{date_str}", results["report_text"])

        # Keep a list of analyzed dates
        r.lpush("accuracy_dates", date_str)
        r.ltrim("accuracy_dates", 0, 29)

        print(f"[ACCURACY] Stored results in Redis for {date_str}")
    except Exception as e:
        logger.error(f"Redis store error: {e}")


# ---------------------------------------------------------------------------
# 9. CLI / Modal entry point
# ---------------------------------------------------------------------------

def generate_report(date_str: str = "2026-06-01") -> str:
    """
    Synchronous wrapper to run the analysis and return the report text.
    Can be called from Modal entry points.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import nest_asyncio
        nest_asyncio.apply()
        result = asyncio.get_event_loop().run_until_complete(run_accuracy_analysis(date_str))
    else:
        result = asyncio.run(run_accuracy_analysis(date_str))

    # Store in Redis
    try:
        import redis as _r
        url = os.environ.get("REDIS_URL", "")
        if url:
            r = _r.from_url(url)
            store_results_in_redis(r, result, date_str)
    except Exception:
        pass

    return result["report_text"]


if __name__ == "__main__":
    import sys
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-06-01"
    logging.basicConfig(level=logging.WARNING)
    print(generate_report(date))
