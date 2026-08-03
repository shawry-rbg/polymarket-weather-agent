"""
HRRR hourly model for US cities.

HRRR (High-Resolution Rapid Refresh) is a NOAA convection-allowing model
with 3km resolution, updated hourly. It provides the most accurate
short-term temperature forecasts for the continental United States.

Uses Open-Meteo's HRRR model endpoint (free, no key required).

Usage:
    result = await fetch_hrrr(lat, lon)
    # result["hourly_temps_f"] = [..., 72.1, 73.5, 74.2, ...]  # next 24h
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 10  # seconds

# US city coords for HRRR scans (Atlanta, Dallas, Miami — NYC is rotated out)
US_CITIES_HRRR = {
    "atlanta": {"name": "Atlanta", "lat": 33.7490, "lon": -84.3880},
    "dallas": {"name": "Dallas", "lat": 32.7767, "lon": -96.7970},
    "miami": {"name": "Miami", "lat": 25.7617, "lon": -80.1918},
}


async def fetch_hrrr(lat: float, lon: float, hours: int = 18) -> Optional[dict]:
    """
    Fetch HRRR hourly temperature forecast for a US location.

    HRRR provides hourly data for ~18 hours ahead (0-18h forecast).
    We use the max temperature over the forecast period as our daily high estimate.

    Args:
        lat, lon: City coordinates (must be within CONUS for best accuracy)
        hours: Number of forecast hours (max ~18 for HRRR)

    Returns:
        dict with keys:
            hourly_temps_f: list[float] — hourly temperature in Fahrenheit
            hourly_times: list[str] — ISO timestamps for each hour
            temp_max_f: float — maximum hourly temperature in forecast window
            temp_min_f: float — minimum hourly temperature
            temp_mean_f: float — mean temperature
            model: str = "hrrr"
        Or None on error.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "models": "hrrr",
        "timezone": "auto",
        "forecast_days": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("HRRR fetch error for (%.4f, %.4f): %s", lat, lon, exc)
        return None
    except Exception as exc:
        logger.warning("HRRR unexpected error for (%.4f, %.4f): %s", lat, lon, exc)
        return None

    try:
        hourly = data["hourly"]
        temps_c = hourly["temperature_2m"]
        times = hourly["time"]

        # Limit to requested hours
        temps_c = temps_c[:hours]
        times = times[:hours]

        if not temps_c:
            logger.warning("HRRR returned empty hourly data")
            return None

        temps_f = [t * 9 / 5 + 32 for t in temps_c]

        import statistics
        return {
            "hourly_temps_f": temps_f,
            "hourly_times": times,
            "temp_max_f": round(max(temps_f), 1),
            "temp_min_f": round(min(temps_f), 1),
            "temp_mean_f": round(statistics.mean(temps_f), 1),
            "model": "hrrr",
        }

    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("HRRR malformed response for (%.4f, %.4f): %s", lat, lon, exc)
        return None


async def fetch_hrrr_for_cities(cities: dict[str, dict] = None) -> dict[str, dict]:
    """
    Fetch HRRR for all configured US cities concurrently.

    Returns:
        {city_slug: result_dict_or_None}
    """
    if cities is None:
        cities = US_CITIES_HRRR

    import asyncio
    tasks = {
        slug: fetch_hrrr(city["lat"], city["lon"])
        for slug, city in cities.items()
    }
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    output = {}
    for (slug, _), result in zip(tasks.items(), results):
        if isinstance(result, Exception):
            logger.warning("HRRR error for %s: %s", slug, result)
            output[slug] = None
        else:
            output[slug] = result
    return output


def hrrr_tiebreaker(
    gfs_temp_f: float,
    ecmwf_temp_f: float,
    hrrr_temp_f: float,
    disagreement_threshold_f: float = 2.0,
) -> float:
    """
    Use HRRR as tiebreaker when GFS and ECMWF disagree significantly.

    Logic:
    - If |GFS - ECMWF| <= disagreement_threshold: return GFS (consensus is fine)
    - If they disagree: return HRRR (it's the highest-resolution short-term model)

    Args:
        gfs_temp_f: GFS deterministic forecast temperature (F)
        ecmwf_temp_f: ECMWF forecast temperature (F)
        hrrr_temp_f: HRRR forecast temperature (F)
        disagreement_threshold_f: Threshold for model disagreement (default 2.0F)

    Returns:
        float — the selected temperature to use
    """
    if abs(gfs_temp_f - ecmwf_temp_f) <= disagreement_threshold_f:
        # Models agree — use GFS as it's our primary model
        return gfs_temp_f
    else:
        # Models disagree — trust HRRR as tiebreaker
        logger.info(
            "HRRR tiebreaker: GFS=%.1fF vs ECMWF=%.1fF (spread=%.1fF), using HRRR=%.1fF",
            gfs_temp_f, ecmwf_temp_f,
            abs(gfs_temp_f - ecmwf_temp_f),
            hrrr_temp_f,
        )
        return hrrr_temp_f
