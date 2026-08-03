"""
ICON (Icosahedral Nonhydrostatic) model fetch.

ICON is DWD's (German Weather Service) global model.
Often more accurate than GFS for European and tropical regions.

API: https://api.open-meteo.com/v1/forecast with models=icon_global
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

ICON_API_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 15


async def fetch_icon_forecast(
    lat: float,
    lon: float,
    date_str: str | None = None,
) -> Optional[dict]:
    """
    Fetch ICON deterministic forecast.

    Returns:
        dict with daily_max_temp_f, date, etc. or None on error.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "models": "icon_global",
        "timezone": "auto",
        "forecast_days": 1,
        "temperature_unit": "fahrenheit",
    }
    if date_str:
        params["start_date"] = date_str
        params["end_date"] = date_str

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(ICON_API_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"[ICON] Fetch error for ({lat:.4f}, {lon:.4f}): {e}")
        return None

    try:
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        temps = daily.get("temperature_2m_max", [])
        if not dates or not temps:
            return None
        return {
            "date": dates[0],
            "daily_max_temp_f": float(temps[0]),
            "model": "icon",
        }
    except (IndexError, TypeError, ValueError) as e:
        logger.warning(f"[ICON] Parse error: {e}")
        return None


def icon_probability(forecast_temp_f: float, threshold_f: float, spread_f: float = 3.0) -> float:
    """
    Compute P(T >= threshold) using ICON deterministic forecast.
    ICON has ~3.0°F RMSE for day-1 forecasts.
    """
    if forecast_temp_f is None:
        return 0.5
    z = (threshold_f - forecast_temp_f) / spread_f
    cdf = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return round(max(0.01, min(0.99, 1.0 - cdf)), 4)
