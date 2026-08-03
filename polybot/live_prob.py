"""
Live Probability Engine - Intraday temperature-based probability estimation.

Fetches live temperature every 5 minutes, maintains circular buffer,
computes time-weighted slope, estimates final daily high, and converts
to bucket probabilities.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Circular buffer of temp readings: (timestamp_utc, temp_c, temp_f)
READING_BUFFER_SIZE = 288  # 24h * 12 readings/hour (every 5 min)
DEFAULT_STD_DEV_C = 1.2  # Standard deviation for bucket probability distribution


class TempReadingBuffer:
    """Circular buffer of recent temperature readings."""

    def __init__(self, max_size: int = READING_BUFFER_SIZE) -> None:
        self.max_size = max_size
        self._buffer: deque[tuple[datetime, float, float]] = deque(maxlen=max_size)

    def add(self, temp_c: float, temp_f: float, timestamp: datetime | None = None) -> None:
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        self._buffer.append((timestamp, temp_c, temp_f))

    @property
    def size(self) -> int:
        return len(self._buffer)

    @property
    def latest(self) -> Optional[tuple[datetime, float, float]]:
        return self._buffer[-1] if self._buffer else None

    def get_slope(self, hours: float = 1.0) -> Optional[float]:
        """
        Compute temperature slope (°C/hour) over the last N hours.
        Uses simple linear regression on the buffered readings.
        """
        if len(self._buffer) < 2:
            return None

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        recent = [(t, tc) for t, tc, tf in self._buffer if t >= cutoff]
        if len(recent) < 2:
            return None

        # Convert timestamps to hours since first reading
        t0 = recent[0][0]
        xs = [(t - t0).total_seconds() / 3600.0 for t, _ in recent]
        ys = [tc for _, tc in recent]

        n = len(xs)
        sum_x = sum(xs)
        sum_y = sum(ys)
        sum_xy = sum(x * y for x, y in zip(xs, ys))
        sum_xx = sum(x * x for x in xs)

        denom = n * sum_xx - sum_x * sum_x
        if abs(denom) < 1e-10:
            return 0.0

        slope = (n * sum_xy - sum_x * sum_y) / denom
        return slope  # °C/hour

    def get_readings(self, hours: float = 24.0) -> list[tuple[datetime, float, float]]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return [(t, tc, tf) for t, tc, tf in self._buffer if t >= cutoff]


# Per-city buffers
_city_buffers: dict[str, TempReadingBuffer] = {}


def get_buffer(city: str) -> TempReadingBuffer:
    """Get or create the temp buffer for a city."""
    if city not in _city_buffers:
        _city_buffers[city] = TempReadingBuffer()
    return _city_buffers[city]


async def fetch_live_temp(lat: float, lon: float) -> Optional[tuple[float, float]]:
    """
    Fetch current live temperature.
    Returns (temp_c, temp_f) or None.
    """
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m",
                    "timezone": "auto",
                },
            )
            if r.status_code == 200:
                data = r.json()
                temp_c = float(data.get("current", {}).get("temperature_2m", 0))
                temp_f = temp_c * 9 / 5 + 32
                return temp_c, temp_f
    except Exception as e:
        logger.debug(f"Live temp fetch error: {e}")
    return None


async def fetch_forecast_max(lat: float, lon: float) -> Optional[float]:
    """Fetch forecast daily max temp (°C)."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": "temperature_2m_max",
                    "timezone": "auto",
                    "forecast_days": 1,
                },
            )
            if r.status_code == 200:
                data = r.json()
                return float(data["daily"]["temperature_2m_max"][0])
    except Exception:
        pass
    return None


def estimate_daily_high(
    current_temp_c: float,
    slope_1h: Optional[float],
    slope_3h: Optional[float],
    forecast_max_c: Optional[float],
    now: Optional[datetime] = None,
) -> float:
    """
    Estimate the final daily high temperature.

    Uses: current_temp + blended_slope * time_remaining, capped by forecast_max + 2°C.

    Args:
        current_temp_c: Current temperature in °C
        slope_1h: Temperature slope over last 1 hour (°C/hr)
        slope_3h: Temperature slope over last 3 hours (°C/hr)
        forecast_max_c: Forecast daily max in °C
        now: Current UTC time

    Returns:
        Estimated daily high in °C
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Compute hours remaining until end of UTC day
    end_of_day = now.replace(hour=23, minute=59, second=59)
    hours_remaining = max(0, (end_of_day - now).total_seconds() / 3600)

    # Blend slopes: prefer 1h if available, else 3h
    if slope_1h is not None and slope_3h is not None:
        # Weight 1h slope more heavily since it's more recent
        blended_slope = 0.7 * slope_1h + 0.3 * slope_3h
    elif slope_1h is not None:
        blended_slope = slope_1h
    elif slope_3h is not None:
        blended_slope = slope_3h
    else:
        blended_slope = 0.0

    # Decay slope over time (temps don't rise linearly all day)
    # Use a damping factor based on hours remaining
    if hours_remaining > 6:
        damping = 0.3  # Far from end of day, low confidence in extrapolation
    elif hours_remaining > 3:
        damping = 0.6
    elif hours_remaining > 1:
        damping = 0.85
    else:
        damping = 1.0

    estimated_rise = blended_slope * hours_remaining * damping
    estimated_high = current_temp_c + estimated_rise

    # Cap by forecast max + 2°C (don't exceed physical limits)
    if forecast_max_c is not None:
        cap = forecast_max_c + 2.0
        estimated_high = min(estimated_high, cap)

    # Floor: at minimum, the current temp
    estimated_high = max(estimated_high, current_temp_c)

    return round(estimated_high, 1)


def temp_to_bucket_probs(
    estimated_high_c: float,
    buckets: list[int],
    std_dev_c: float = DEFAULT_STD_DEV_C,
) -> dict[int, float]:
    """
    Convert estimated daily high to bucket probabilities.

    For each bucket threshold, compute P(T > threshold) using
    a normal distribution centered on estimated_high_c.

    Returns dict: {threshold: probability}
    """
    probs = {}
    for threshold in buckets:
        if std_dev_c <= 0:
            std_dev_c = DEFAULT_STD_DEV_C
        z = (estimated_high_c - threshold) / (std_dev_c * math.sqrt(2))
        prob = 0.5 * math.erfc(-z)
        probs[threshold] = round(max(0.01, min(0.99, prob)), 4)
    return probs


async def compute_live_probabilities(
    city: str,
    lat: float,
    lon: float,
    buckets: list[int],
) -> dict:
    """
    Full live probability computation for a city.

    1. Fetch live temp, add to buffer
    2. Compute 1h and 3h slopes
    3. Fetch forecast max
    4. Estimate daily high
    5. Convert to bucket probabilities

    Returns dict with all intermediate + final results.
    """
    buf = get_buffer(city)
    now = datetime.now(timezone.utc)

    # Fetch live temp
    live = await fetch_live_temp(lat, lon)
    if live:
        temp_c, temp_f = live
        buf.add(temp_c, temp_f, now)
    else:
        latest = buf.latest
        if latest:
            temp_c, temp_f = latest[1], latest[2]
        else:
            # No data at all, use forecast
            forecast_c = await fetch_forecast_max(lat, lon)
            if forecast_c is None:
                return {"error": "no_data", "city": city, "buckets": {}}
            temp_c = forecast_c
            temp_f = temp_c * 9 / 5 + 32

    # Slopes
    slope_1h = buf.get_slope(hours=1.0)
    slope_3h = buf.get_slope(hours=3.0)

    # Forecast max
    forecast_max_c = await fetch_forecast_max(lat, lon)

    # Estimate daily high
    estimated_high_c = estimate_daily_high(
        current_temp_c=temp_c,
        slope_1h=slope_1h,
        slope_3h=slope_3h,
        forecast_max_c=forecast_max_c,
        now=now,
    )
    estimated_high_f = estimated_high_c * 9 / 5 + 32

    # Bucket probabilities
    bucket_probs = temp_to_bucket_probs(estimated_high_c, buckets)

    return {
        "city": city,
        "current_temp_c": round(temp_c, 1),
        "current_temp_f": round(temp_f, 1),
        "slope_1h_c_per_hr": round(slope_1h, 2) if slope_1h else None,
        "slope_3h_c_per_hr": round(slope_3h, 2) if slope_3h else None,
        "forecast_max_c": forecast_max_c,
        "estimated_high_c": estimated_high_c,
        "estimated_high_f": round(estimated_high_f, 1),
        "bucket_probs": bucket_probs,
        "buffer_size": buf.size,
        "timestamp": now.isoformat(),
    }


async def run_live_scanner(
    cities: list[dict],
    interval_seconds: int = 300,
    callback=None,
) -> None:
    """
    Continuous live scanner: fetch temps and compute probs every interval.

    Args:
        cities: list of {name, lat, lon, buckets}
        interval_seconds: scan interval (default 5 min)
        callback: async function(results_dict) called after each scan
    """
    logger.info(f"[LIVE_SCANNER] Starting live scanner for {len(cities)} cities, interval={interval_seconds}s")
    while True:
        results = {}
        for city_config in cities:
            name = city_config["name"]
            try:
                probs = await compute_live_probabilities(
                    city=name,
                    lat=city_config["lat"],
                    lon=city_config["lon"],
                    buckets=city_config.get("buckets", list(range(20, 40))),
                )
                results[name] = probs
            except Exception as e:
                logger.error(f"[LIVE_SCANNER] Error scanning {name}: {e}")
                results[name] = {"error": str(e), "city": name}

        if callback:
            try:
                await callback(results)
            except Exception as e:
                logger.error(f"[LIVE_SCANNER] Callback error: {e}")

        await asyncio.sleep(interval_seconds)
