"""
Live Temperature Agent - Open-Meteo (free, no API key required).

Fetches current weather temperature and caches it in Redis.
Provides rebalancing trigger detection by comparing live temp
against forecast temperature. Also detects bucket-crossing events
and maintains a rolling temperature history for slope estimation.

Usage:
    from polybot.agents.live_temp_agent import update_live_temp, check_rebalancing_trigger
    from polybot.agents.live_temp_agent import crossing_imminent
    temp = update_live_temp("london", 51.5074, -0.1278)
    should_rebalance = check_rebalancing_trigger("london", forecast_temp_f=72.0, threshold=1.5)
    out, inn = crossing_imminent("london", 72.1, [71.6, 73.4, 75.2])
"""

import os
from datetime import datetime

import redis
import requests

# Module-level Redis connection (lazy, reused across calls)
_r: redis.Redis | None = None

# Number of history entries to keep (12 x 5min = 1 hour of history)
HISTORY_LEN = 12


def _get_redis() -> redis.Redis | None:
    global _r
    if _r is not None:
        return _r
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            _r = redis.from_url(redis_url)
            return _r
        except Exception as e:
            print(f"[LIVE_TEMP] Redis connect error: {e}")
    return None


def fetch_live_temp_openmeteo(lat: float, lon: float) -> float | None:
    """
    Fetch current temperature from Open-Meteo (free, no key needed).

    Args:
        lat: Latitude
        lon: Longitude

    Returns:
        Temperature in Fahrenheit, or None on error.
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current_weather": "true",
        "temperature_unit": "fahrenheit",
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return float(data["current_weather"]["temperature"])
    except Exception as e:
        print(f"[LIVE_TEMP] Open-Meteo error: {e}")
        return None


# Maximum age (seconds) before a cached live temp is considered stale
LIVE_TEMP_MAX_AGE_S = 7200  # 2 hours


def fetch_live_temp_cached(
    city_slug: str, lat: float, lon: float, max_age_s: int = LIVE_TEMP_MAX_AGE_S
) -> tuple[float | None, str]:
    """
    Fetch live temperature with Redis caching and freshness check.

    1. Check Redis for cached live_temp:{city_slug} (hash with temp + timestamp).
    2. If cached value exists and is newer than max_age_s, return it (source="cache").
    3. Otherwise, fetch fresh from Open-Meteo, cache it, and return (source="openmeteo").
    4. If Open-Meteo fails but a cached value exists (even stale), return the stale
       value with source="stale_cache" as last resort.

    Args:
        city_slug: City identifier for Redis key.
        lat: Latitude (for Open-Meteo fallback).
        lon: Longitude (for Open-Meteo fallback).
        max_age_s: Maximum acceptable age in seconds (default 7200 = 2h).

    Returns:
        (temp_f, source) tuple. temp_f is None only if no data at all.
        source is one of: "cache", "openmeteo", "stale_cache".
    """
    r = _get_redis()
    now = datetime.now()

    # Try Redis cache first
    if r:
        try:
            data = r.hgetall(f"live_temp:{city_slug}")
            if data:
                temp_raw = data.get(b"temp") or data.get("temp")
                ts_raw = data.get(b"timestamp") or data.get("timestamp")
                if temp_raw is not None and ts_raw is not None:
                    cached_temp = float(temp_raw)
                    ts_str = ts_raw.decode() if isinstance(ts_raw, bytes) else ts_raw
                    try:
                        cached_ts = datetime.fromisoformat(ts_str)
                        age_s = (now - cached_ts).total_seconds()
                        if 0 <= age_s <= max_age_s:
                            # Fresh cache hit
                            return cached_temp, "cache"
                        else:
                            print(
                                f"[LIVE_TEMP] {city_slug}: cached temp {cached_temp}F "
                                f"is {age_s:.0f}s old (> {max_age_s}s max) — fetching fresh"
                            )
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            print(f"[LIVE_TEMP] Redis read error for {city_slug}: {e}")

    # Cache miss or stale — fetch fresh from Open-Meteo
    fresh_temp = fetch_live_temp_openmeteo(lat, lon)
    if fresh_temp is not None:
        # Cache the fresh value
        if r:
            try:
                r.hset(f"live_temp:{city_slug}", mapping={
                    "temp": str(fresh_temp),
                    "timestamp": now.isoformat(),
                })
            except Exception:
                pass
        return fresh_temp, "openmeteo"

    # Open-Meteo failed — fall back to stale cache if available
    if r:
        try:
            data = r.hgetall(f"live_temp:{city_slug}")
            if data:
                temp_raw = data.get(b"temp") or data.get("temp")
                if temp_raw is not None:
                    stale_temp = float(temp_raw)
                    print(
                        f"[LIVE_TEMP] {city_slug}: Open-Meteo failed, "
                        f"using stale cached temp {stale_temp}F"
                    )
                    return stale_temp, "stale_cache"
        except Exception:
            pass

    return None, "none"


# Backward-compatible alias
def fetch_live_temp(lat: float, lon: float) -> float | None:
    """Backward-compatible alias for fetch_live_temp_openmeteo."""
    return fetch_live_temp_openmeteo(lat, lon)


def update_live_temp(city_slug: str, lat: float, lon: float) -> float | None:
    """
    Fetch live temperature and cache it in Redis.

    Stores:
      - Hash live_temp:{city_slug} with current temp + timestamp
      - List live_temp_history:{city_slug} with last 12 readings (for slope estimation)

    Args:
        city_slug: City identifier (e.g. "london", "nyc")
        lat: Latitude
        lon: Longitude

    Returns:
        Temperature in Fahrenheit, or None on error.
    """
    temp = fetch_live_temp_openmeteo(lat, lon)
    r = _get_redis()
    if temp is not None and r:
        try:
            r.hset(f"live_temp:{city_slug}", mapping={
                "temp": str(temp),
                "timestamp": datetime.now().isoformat(),
            })
            # Push to rolling history list for slope estimation
            r.lpush(f"live_temp_history:{city_slug}", str(temp))
            r.ltrim(f"live_temp_history:{city_slug}", 0, HISTORY_LEN - 1)
            print(f"[LIVE_TEMP] {city_slug}: {temp}F cached to Redis (history updated)")
        except Exception as e:
            print(f"[LIVE_TEMP] Redis write error: {e}")
    return temp


def check_rebalancing_trigger(city_slug: str, forecast_temp_f: float, threshold: float = 1.5) -> bool:
    """
    Check if live temperature deviates from forecast enough to trigger rebalancing.

    Args:
        city_slug: City identifier
        forecast_temp_f: Forecasted temperature in Fahrenheit
        threshold: Deviation threshold in Fahrenheit (default 1.5)

    Returns:
        True if |live_temp - forecast_temp| > threshold
    """
    r = _get_redis()
    if not r:
        print(f"[LIVE_TEMP] No Redis connection for trigger check: {city_slug}")
        return False
    try:
        data = r.hgetall(f"live_temp:{city_slug}")
        if not data:
            print(f"[LIVE_TEMP] No cached temp for {city_slug}")
            return False
        # Redis returns bytes
        live_temp_raw = data.get(b"temp") or data.get("temp")
        if live_temp_raw is None:
            return False
        live_temp = float(live_temp_raw)
        deviation = abs(live_temp - forecast_temp_f)
        triggered = deviation > threshold
        if triggered:
            print(f"[LIVE_TEMP] REBALANCE TRIGGER: {city_slug} "
                  f"live={live_temp}F forecast={forecast_temp_f}F "
                  f"dev={deviation:.1f}F > threshold={threshold}F")
        return triggered
    except Exception as e:
        print(f"[LIVE_TEMP] Trigger check error: {e}")
        return False


def crossing_imminent(city_slug: str, live_temp_f: float, city_buckets_f: list[float]) -> tuple:
    """
    Detect if live temperature is within 0.9°F of any bucket threshold.

    When a temperature boundary is about to be crossed, the bot should
    exit the current bucket and enter the adjacent one.

    Args:
        city_slug: City identifier (for logging)
        live_temp_f: Current live temperature in Fahrenheit
        city_buckets_f: List of bucket threshold boundaries in Fahrenheit
                        (e.g. [71.6, 73.4, 75.2] for London's 22C/23C/24C boundaries)

    Returns:
        (bucket_out, bucket_in) tuple if within 0.9°F of a threshold, else (None, None).
        bucket_out is the bucket being exited, bucket_in is the bucket being entered.
    """
    for threshold in city_buckets_f:
        if abs(live_temp_f - threshold) < 0.9:
            # Determine the two buckets around this threshold.
            # The "out" bucket is the one below the threshold, "in" is above.
            lower_center = threshold - 0.9  # midpoint of lower bucket
            upper_center = threshold + 0.9  # midpoint of upper bucket
            bucket_out = f"{threshold - 1.8:.1f}-{threshold:.1f}F"
            bucket_in = f"{threshold:.1f}-{threshold + 1.8:.1f}F"
            print(f"[SPEED] {city_slug}: CROSSING IMMINENT at threshold={threshold:.1f}F "
                  f"(live={live_temp_f:.1f}F, dist={abs(live_temp_f - threshold):.2f}F) "
                  f"OUT={bucket_out} IN={bucket_in}")
            return bucket_out, bucket_in
    return None, None
