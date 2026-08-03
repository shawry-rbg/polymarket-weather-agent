"""
METAR-based calibration with rolling 30-day bias correction.

Fetches actual daily highs from Open-Meteo archive and compares with GFS
hindcast to compute per-city bias corrections. Uses a rolling 30-day window
that updates as new trades resolve.

Usage:
    from polybot.metar_calibration import get_rolling_bias, update_bias_after_resolution
    bias, std, n = get_rolling_bias("dallas")
    update_bias_after_resolution("KDFW", forecast_f, actual_f)
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

CALIBRATION_PATH = Path("/polybot-data/calibration_table.json")
METAR_CACHE_PATH = Path("/polybot-data/metar_history.json")

# ICAO station code -> city slug mapping
STATION_TO_CITY: dict[str, str] = {
    "KATL": "atlanta",
    "KDFW": "dallas",
    "KIAH": "houston",
    "KMIA": "miami",
    "KJFK": "nyc",
    "FACT": "cape_town",
    "VABB": "mumbai",
    "VHHH": "hong_kong",
    "RKSI": "seoul",
    "ZSPD": "shanghai",
    "ZBAA": "beijing",
    "ZUCK": "chongqing",
    "MMMX": "mexico_city",
    "SAEZ": "buenos_aires",
    "EGLL": "london",
    "LTFM": "istanbul",
}

# City slug -> ICAO station code
CITY_TO_STATION: dict[str, str] = {v: k for k, v in STATION_TO_CITY.items()}


def get_rolling_bias(city: str, days: int = 30) -> tuple[float, float, int]:
    """
    Get rolling bias correction for a city from the last N days of resolved trades.
    Returns (bias_correction, std_dev, sample_count).
    bias_correction: amount to ADD to forecast (negative = forecast too warm).
    """
    cal = _load_calibration()
    city_cal = cal.get(city, {})

    # Collect errors from the last N days
    errors = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    for date_str, entry in city_cal.items():
        if date_str >= cutoff and entry.get("resolved") and entry.get("error") is not None:
            errors.append(entry["error"])

    if len(errors) < 3:
        # Not enough data — return safe defaults
        return 0.0, 3.0, 0

    bias = statistics.mean(errors)
    std = statistics.stdev(errors) if len(errors) > 1 else 3.0
    return round(bias, 2), round(std, 2), len(errors)


def update_bias_after_resolution(
    station_code: str,
    forecast_f: float,
    actual_f: float,
    date_str: str | None = None,
) -> None:
    """
    Called after every market resolves. Updates the calibration table
    with the forecast error for this city-date.
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    city = STATION_TO_CITY.get(station_code, station_code.lower())
    cal = _load_calibration()

    if city not in cal:
        cal[city] = {}

    error = actual_f - forecast_f  # positive = forecast too cold

    cal[city][date_str] = {
        "forecast": round(forecast_f, 1),
        "actual": round(actual_f, 1),
        "error": round(error, 2),
        "resolved": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    _save_calibration(cal)

    # Log the update
    bias, std, n = get_rolling_bias(city)
    logger.info(
        f"[CALIB] {city} ({station_code}) resolved {date_str}: "
        f"forecast={forecast_f:.1f}F actual={actual_f:.1f}F error={error:+.1f}F | "
        f"rolling bias={bias:+.1f}F std={std:.1f}F n={n}"
    )


def build_calibration_from_history(
    station: str,
    lat: float,
    lon: float,
    days_back: int = 90,
) -> dict:
    """
    Build initial calibration table from historical data.
    Fetches actual daily highs and GFS hindcast for the same dates.
    """
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days_back)

    actuals = _fetch_actuals(lat, lon, start_date.isoformat(), end_date.isoformat())
    if not actuals:
        logger.warning(f"[CALIB] No actuals for {station}")
        return {}

    city = STATION_TO_CITY.get(station, station.lower())
    cal = _load_calibration()
    if city not in cal:
        cal[city] = {}

    errors_by_month: dict[str, list[float]] = {}

    for date_str, actual_temp in actuals.items():
        hindcast = _fetch_gfs_hindcast(lat, lon, date_str)
        if hindcast is None:
            continue

        error = actual_temp - hindcast  # positive = forecast too cold
        month = datetime.strptime(date_str, "%Y-%m-%d").strftime("%B")

        cal[city][date_str] = {
            "forecast": round(hindcast, 1),
            "actual": round(actual_temp, 1),
            "error": round(error, 2),
            "resolved": True,
        }

        errors_by_month.setdefault(month, []).append(error)

    _save_calibration(cal)

    # Log summary
    for month, errors in sorted(errors_by_month.items()):
        if len(errors) >= 3:
            logger.info(
                f"[CALIB] {station} {month}: bias={statistics.mean(errors):+.1f}F "
                f"std={statistics.stdev(errors):.1f}F n={len(errors)}"
            )

    return cal[city]


def _load_calibration() -> dict:
    if CALIBRATION_PATH.exists():
        try:
            with open(CALIBRATION_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_calibration(data: dict) -> None:
    CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CALIBRATION_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _fetch_actuals(lat: float, lon: float, start: str, end: str) -> dict[str, float]:
    """Fetch actual daily highs from Open-Meteo archive."""
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start,
        "end_date": end,
        "daily": "temperature_2m_max",
        "timezone": "auto",
        "temperature_unit": "fahrenheit",
    }
    try:
        resp = httpx.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        temps = daily.get("temperature_2m_max", [])
        return {d: float(t) for d, t in zip(dates, temps) if t is not None}
    except Exception as e:
        logger.warning(f"[CALIB] Actuals fetch error: {e}")
        return {}


def _fetch_gfs_hindcast(lat: float, lon: float, date_str: str) -> Optional[float]:
    """Fetch GFS hindcast for a specific date."""
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date_str,
        "end_date": date_str,
        "daily": "temperature_2m_max",
        "models": "gfs_seamless",
        "timezone": "auto",
        "temperature_unit": "fahrenheit",
    }
    try:
        resp = httpx.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        daily = data.get("daily", {})
        temps = daily.get("temperature_2m_max", [])
        if temps and temps[0] is not None:
            return float(temps[0])
    except Exception as e:
        logger.debug(f"[CALIB] GFS hindcast error for {date_str}: {e}")
    return None
