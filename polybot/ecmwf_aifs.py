"""
ECMWF AIFS (Artificial Intelligence Forecasting System) model fetch.

AIFS is ECMWF's ML-based weather model. Often more accurate than
traditional NWP for temperature forecasts.

API: https://api.open-meteo.com/v1/forecast with models=ecmwf_aifs025
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

AIFS_API_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 15


async def fetch_aifs_forecast(
    lat: float,
    lon: float,
    date_str: str | None = None,
) -> Optional[dict]:
    """
    Fetch ECMWF AIFS deterministic forecast.

    Returns:
        dict with daily_max_temp_f, date, etc. or None on error.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "models": "ecmwf_aifs025",
        "timezone": "auto",
        "forecast_days": 1,
        "temperature_unit": "fahrenheit",
    }
    if date_str:
        params["start_date"] = date_str
        params["end_date"] = date_str

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(AIFS_API_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"[AIFS] Fetch error for ({lat:.4f}, {lon:.4f}): {e}")
        return None

    try:
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        temps = daily.get("temperature_2m_max", [])
        if not dates or not temps:
            return None
        if temps[0] is None:
            logger.debug(f"[AIFS] Null temp for ({lat:.4f}, {lon:.4f}) — skipping")
            return None
        return {
            "date": dates[0],
            "daily_max_temp_f": float(temps[0]),
            "model": "aifs",
        }
    except (IndexError, TypeError, ValueError) as e:
        logger.warning(f"[AIFS] Parse error: {e}")
        return None


def aifs_probability(forecast_temp_f: float, threshold_f: float, spread_f: float = 2.5) -> float:
    """
    Compute P(T >= threshold) using AIFS deterministic forecast.
    AIFS has ~2.5°F RMSE for day-1 forecasts.
    """
    if forecast_temp_f is None:
        return 0.5
    z = (threshold_f - forecast_temp_f) / spread_f
    cdf = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return round(max(0.01, min(0.99, 1.0 - cdf)), 4)
