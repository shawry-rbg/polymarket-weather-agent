"""
Time & Slope Agent.

Provides time-of-day awareness (peak temperature windows) and
temperature trend estimation via Redis history.

Peak windows define the hours when temperature markets are most active
and when bucket crossings are most likely.

Usage:
    from polybot.agents.time_agent import is_peak_window, estimate_temperature_in_one_hour
    if is_peak_window("london"):
        pred = estimate_temperature_in_one_hour("london", redis_url)
"""

import os
from datetime import datetime, timezone, timedelta

# Peak trading windows (local hour start, end) by region
PEAK_WINDOWS = {
    "default": (13, 17),   # 1 PM – 5 PM local
    "asia":    (14, 18),   # 2 PM – 6 PM for Asia
}

# Number of history points to use for slope estimation
HISTORY_LEN = 6

# City slug -> IANA timezone name
CITY_TIMEZONES = {
    "london":      "Europe/London",
    "nyc":         "America/New_York",
    "chongqing":   "Asia/Shanghai",
    "bangkok":     "Asia/Bangkok",
    "jakarta":     "Asia/Jakarta",
    "mumbai":      "Asia/Kolkata",
    "hong_kong":   "Asia/Hong_Kong",
    "shanghai":    "Asia/Shanghai",
    "beijing":     "Asia/Shanghai",
    "seoul":       "Asia/Seoul",
    "taipei":      "Asia/Taipei",
    "manila":      "Asia/Manila",
    "kuala_lumpur": "Asia/Kuala_Lumpur",
    "ho_chi_minh_city": "Asia/Ho_Chi_Minh",
    "istanbul":    "Europe/Istanbul",
    "mexico_city": "America/Mexico_City",
    "shenzhen":    "Asia/Shanghai",
    "guangzhou":   "Asia/Shanghai",
    "singapore":   "Asia/Singapore",
    "dubai":       "Asia/Dubai",
    "cape_town":   "Africa/Johannesburg",
    "lagos":       "Africa/Lagos",
    "buenos_aires": "America/Argentina/Buenos_Aires",
}

# City slugs that use the Asia peak window
ASIA_CITIES = {
    "chongqing", "bangkok", "jakarta", "mumbai", "shanghai", "hong_kong",
    "beijing", "seoul", "taipei", "manila", "kuala_lumpur", "ho_chi_minh_city",
    "shenzhen", "guangzhou", "singapore", "dubai",
}


def local_hour(city_slug: str) -> int:
    """
    Get the current local hour for a city.

    Wraps pytz in try/except; falls back to UTC if timezone not found
    or pytz is unavailable.

    Args:
        city_slug: City identifier

    Returns:
        Current hour (0-23) in the city's local timezone (or UTC as fallback)
    """
    tz_name = CITY_TIMEZONES.get(city_slug, "UTC")
    try:
        import pytz
        tz = pytz.timezone(tz_name)
        hour = datetime.now(tz).hour
        return hour
    except ImportError:
        print(f"[TIME] pytz not available, defaulting to UTC for {city_slug}")
        return datetime.now(timezone.utc).hour
    except Exception as e:
        print(f"[TIME] Timezone error for {city_slug} ({tz_name}): {e}, defaulting to UTC")
        return datetime.now(timezone.utc).hour


def is_peak_window(city_slug: str) -> bool:
    """
    Check if the current local time is within the peak trading window.

    Peak windows are when temperature markets are most active and
    bucket crossings are most likely to occur.

    Args:
        city_slug: City identifier

    Returns:
        True if current local hour is within the peak window
    """
    hour = local_hour(city_slug)
    if city_slug in ASIA_CITIES:
        start, end = PEAK_WINDOWS["asia"]
    else:
        start, end = PEAK_WINDOWS["default"]
    in_window = start <= hour < end
    print(f"[TIME] {city_slug}: local_hour={hour}, peak=({start}-{end}), in_window={in_window}")
    return in_window


def estimate_temperature_in_one_hour(city_slug: str, redis_url: str = None) -> float | None:
    """
    Estimate the temperature in one hour based on recent history slope.

    Uses the last HISTORY_LEN temperature readings from Redis to compute
    a linear slope (°F per 5-min interval), then extrapolates 12 intervals
    (1 hour) forward.

    Args:
        city_slug: City identifier
        redis_url: Redis connection URL (defaults to REDIS_URL env var)

    Returns:
        Predicted temperature in Fahrenheit in one hour, or None if
        insufficient history data
    """
    if redis_url is None:
        redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        print(f"[SLOPE] {city_slug}: No Redis URL available")
        return None

    try:
        import redis as _redis
        r = _redis.from_url(redis_url)
        history = r.lrange(f"live_temp_history:{city_slug}", 0, HISTORY_LEN - 1)
    except Exception as e:
        print(f"[SLOPE] {city_slug}: Redis error: {e}")
        return None

    if not history or len(history) < 2:
        print(f"[SLOPE] {city_slug}: Insufficient history ({len(history) if history else 0} points)")
        return None

    try:
        temps = [float(x.decode() if isinstance(x, bytes) else x) for x in history]
    except (ValueError, TypeError) as e:
        print(f"[SLOPE] {city_slug}: Parse error: {e}")
        return None

    # temps[0] is most recent, temps[-1] is oldest
    # Slope per 5-min interval: (newest - oldest) / (n-1)
    slope_per_5min = (temps[0] - temps[-1]) / (len(temps) - 1)
    # 12 intervals = 1 hour
    slope_per_hour = slope_per_5min * 12
    predicted = temps[0] + slope_per_hour

    print(f"[SLOPE] {city_slug}: history={temps}, slope={slope_per_5min:.3f}F/5min "
          f"({slope_per_hour:.2f}F/hr), current={temps[0]:.1f}F, predicted_1h={predicted:.1f}F")

    return predicted
