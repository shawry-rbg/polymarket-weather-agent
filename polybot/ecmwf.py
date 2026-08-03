"""
ECMWF deterministic model fetch from Open-Meteo.

ECMWF (European Centre for Medium-Range Weather Forecasts) provides a
high-resolution deterministic forecast that complements the GFS ensemble.
We use it to compute a blended probability:
    blended_prob = 0.4 * GFS_prob + 0.6 * ECMWF_prob

ECMWF is generally more accurate for day-1 to day-3 forecasts, so we
weight it higher in the blend.

Usage:
    from polybot.ecmwf import fetch_ecmwf_forecast, ecmwf_probability
    forecast = await fetch_ecmwf_forecast(lat, lon, date_str="2026-06-03")
    prob = ecmwf_probability(forecast, threshold_f=75.0)
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

ECMWF_API_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 15


async def fetch_ecmwf_forecast(
    lat: float,
    lon: float,
    date_str: str | None = None,
    unit: str = "C",
) -> Optional[dict]:
    """
    Fetch ECMWF deterministic forecast from Open-Meteo.

    Args:
        lat, lon: Coordinates
        date_str: Target date "YYYY-MM-DD" (None = today)
        unit: "C" or "F"

    Returns:
        dict with daily_max_temp, date, etc. or None on error.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "models": "ecmwf_ifs04",
        "timezone": "auto",
        "forecast_days": 1,
        "temperature_unit": "fahrenheit",  # Force Fahrenheit
    }
    if date_str:
        params["start_date"] = date_str
        params["end_date"] = date_str

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(ECMWF_API_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"[ECMWF] Fetch error for ({lat:.4f}, {lon:.4f}): {e}")
        return None

    try:
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        temps = daily.get("temperature_2m_max", [])
        if not dates or not temps:
            return None
        return {
            "date": dates[0],
            "daily_max_temp": float(temps[0]),
            "unit": unit,
        }
    except (IndexError, TypeError, ValueError) as e:
        logger.warning(f"[ECMWF] Parse error: {e}")
        return None


def ecmwf_probability(
    ecmwf_forecast: dict,
    threshold_f: float,
    spread_f: float = 3.0,
) -> float:
    """
    Compute probability that temp >= threshold using ECMWF deterministic forecast.

    Uses a normal CDF centered on the ECMWF forecast with a fixed spread
    (ECMWF has ~3°F RMSE for day-1 forecasts).

    Args:
        ecmwf_forecast: Dict from fetch_ecmwf_forecast with "daily_max_temp"
        threshold_f: Temperature threshold in Fahrenheit
        spread_f: Standard deviation for the normal CDF (default 3.0°F)

    Returns:
        Probability (0.0 to 1.0)
    """
    if not ecmwf_forecast:
        return 0.5
    forecast_temp = ecmwf_forecast.get("daily_max_temp")
    if forecast_temp is None:
        return 0.5

    # Data is already in Fahrenheit (temperature_unit="fahrenheit" in API call)
    z = (threshold_f - forecast_temp) / spread_f
    cdf = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    prob = 1.0 - cdf
    return round(max(0.01, min(0.99, prob)), 4)


def blended_probability(
    gfs_prob: float,
    ecmwf_prob: float,
    gfs_weight: float = 0.4,
    ecmwf_weight: float = 0.6,
) -> float:
    """
    Compute blended probability from GFS ensemble and ECMWF deterministic.

    Default weights: 40% GFS, 60% ECMWF (ECMWF is more accurate for short-range).

    Args:
        gfs_prob: GFS ensemble probability
        ecmwf_prob: ECMWF deterministic probability
        gfs_weight: Weight for GFS (default 0.4)
        ecmwf_weight: Weight for ECMWF (default 0.6)

    Returns:
        Blended probability (0.0 to 1.0)
    """
    blended = gfs_weight * gfs_prob + ecmwf_weight * ecmwf_prob
    return round(max(0.01, min(0.99, blended)), 4)
