"""Fetches daily max temperature forecasts from the Open-Meteo API.

Open-Meteo is free and requires no API key.
Docs: https://open-meteo.com/en/docs
"""

import asyncio
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 10  # seconds

# Reasonable temperature ranges per city (Fahrenheit) — used for unit-bug detection
CITY_TEMP_RANGES_F = {
    "Atlanta":      (10, 110),
    "Dallas":       (5,  115),
    "Houston":      (20, 110),
    "Miami":        (40, 100),
    "New York":     (-5, 105),
    "Mexico City":  (30, 95),
    "Buenos Aires": (25, 105),
    "London":       (15, 95),
    "Istanbul":     (15, 105),
    "Seoul":        (5,  105),
    "Beijing":      (0,  110),
    "Shanghai":     (20, 108),
    "Chongqing":    (25, 110),
    "Guangzhou":    (30, 108),
    "Shenzhen":     (30, 108),
    "Hong Kong":    (45, 102),
    "Taipei":       (35, 105),
    "Tokyo":        (15, 105),
    "Bangkok":      (60, 108),
    "Singapore":    (68, 100),
    "Kuala Lumpur": (65, 102),
    "Jakarta":      (65, 102),
    "Manila":       (65, 105),
    "Ho Chi Minh":  (60, 105),
    "Mumbai":       (60, 115),
    "Cape Town":    (35, 105),
    "Lagos":        (60, 108),
}


def sanity_check_temp(city: str, temp_f: float, source: str = "unknown") -> float:
    """
    Raise ValueError if temperature is outside reasonable range for the city.
    This catches unit bugs (Celsius treated as Fahrenheit) immediately.
    """
    low, high = CITY_TEMP_RANGES_F.get(city, (-20, 130))
    if not (low <= temp_f <= high):
        raise ValueError(
            f"UNIT BUG DETECTED: {city} from {source} = {temp_f}F "
            f"(expected {low}-{high}F). "
            f"Check temperature_unit='fahrenheit' in API call."
        )
    return temp_f


async def get_forecast(lat: float, lon: float) -> Optional[dict]:
    """Fetch the daily max temperature forecast for a single location.

    Args:
        lat: Latitude of the location.
        lon: Longitude of the location.

    Returns:
        A dict with keys: temp_max_c, temp_max_f, date, lat, lon.
        Returns None on any error (network, parsing, missing data).
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "timezone": "auto",
        "forecast_days": 1,
        "temperature_unit": "fahrenheit",  # Force Fahrenheit
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(OPEN_METEO_URL, params=params)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.error("HTTP error fetching forecast for (%s, %s): %s", lat, lon, exc)
        return None
    except Exception as exc:
        logger.error("Unexpected error fetching forecast for (%s, %s): %s", lat, lon, exc)
        return None

    try:
        daily = data["daily"]
        temp_max_f = float(daily["temperature_2m_max"][0])
        date = daily["time"][0]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.error("Malformed response for (%s, %s): %s", lat, lon, exc)
        return None

    temp_max_c = round((temp_max_f - 32) * 5 / 9, 1)  # Convert F→C for storage

    result = {
        "temp_max_c": temp_max_c,
        "temp_max_f": temp_max_f,
        "date": date,
        "lat": lat,
        "lon": lon,
    }

    logger.info(
        "Forecast for (%s, %s) on %s: %.1f°F (%.1f°C)",
        lat, lon, date, temp_max_f, temp_max_c,
    )

    return result


async def get_all_forecasts(cities: list[dict]) -> list[dict]:
    """Fetch forecasts for multiple cities concurrently.

    Args:
        cities: A list of dicts, each with keys:
            - name (str): city name
            - lat  (float): latitude
            - lon  (float): longitude

    Returns:
        A list of result dicts (same as get_forecast) each with an
        additional "city" key. Failed lookups are omitted.
    """
    tasks = []
    for city in cities:
        task = get_forecast(city["lat"], city["lon"])
        tasks.append(task)

    results = await asyncio.gather(*tasks, return_exceptions=False)

    forecasts = []
    for city, result in zip(cities, results):
        if result is not None:
            result["city"] = city["name"]
            forecasts.append(result)

    logger.info(
        "Fetched %d/%d forecasts successfully", len(forecasts), len(cities)
    )

    return forecasts
