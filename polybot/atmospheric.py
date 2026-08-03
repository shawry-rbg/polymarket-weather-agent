"""
Atmospheric sensitivity layers for weather forecast correction.

Provides four correction functions that adjust the raw ensemble forecast
based on real-time atmospheric conditions:
  1. Wind direction correction
  2. Dew point suppression
  3. Cloud cover correction
  4. Time-to-resolution adjustment (Kelly/edge multipliers)

All corrections are logged to Redis for debugging via city_metrics hash.

Usage:
    correction = get_atmospheric_corrections(city_slug, lat, lon, forecast_f, market_end_time)
    # correction = {"wind": -0.5, "dew_point": -3.5, "cloud": 0.0, "time_adj": {"edge_mult": 0.8, "kelly_mult": 0.6}}
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 10

# Wind direction corrections per city (degrees -> correction_f)
# Positive = wind from that direction reduces forecast, negative = increases
WIND_CORRECTIONS: dict[str, dict[str, float]] = {
    # Atlanta: Northerly wind (0-45 deg) brings cooler air
    "atlanta": {
        "default": 0.0,
        "from_north": -1.5,    # 0-45 deg
        "from_west": -0.5,     # 225-270 deg (Gulf moisture)
    },
    # Chongqing: NE wind brings cooler mountain air
    "chongqing": {
        "default": 0.0,
        "from_north": -1.0,
        "from_south": +0.8,    # Warm valley wind
    },
    # Cape Town: Southeasterly (135 deg) brings hot berg wind
    "cape_town": {
        "default": 0.0,
        "from_southeast": +2.0,    # Berg wind
        "from_northwest": -1.5,   # Cold front
    },
    # Buenos Aires: Pampero (south) brings cold
    "buenos_aires": {
        "default": 0.0,
        "from_south": -2.0,    # Pampero cold front
        "from_northeast": +1.0,  # Warm humid
    },
    # Dallas: Southerly wind from Gulf = warmer
    "dallas": {
        "default": 0.0,
        "from_north": -2.0,    # Canadian cold front
        "from_south": +1.5,   # Gulf moisture
    },
    # London: Northerly/Southeasterly corrections
    "london": {
        "default": 0.0,
        "from_north": -1.8,   # Arctic air
        "from_east": -0.8,    # Continental (cold in winter)
        "from_southwest": +0.6,  # Atlantic warmth
    },
    # Istanbul: Lodos (south) very warm
    "istanbul": {
        "default": 0.0,
        "from_north": -1.2,    # Black Sea cold
        "from_south": +1.5,   # Lodos warm wind
    },
    # Mumbai: Southwest monsoon = cooler
    "mumbai": {
        "default": 0.0,
        "from_west": -1.5,    # Monsoon
        "from_east": +0.5,    # Hot continent
    },
    # Default for cities not listed
    "default": {
        "default": 0.0,
    },
}

# Dew point spread threshold (F) for suppression
DEW_POINT_SPREAD_THRESHOLD = 5.0
DEW_POINT_SUPPRESSION_F = 3.5
DEW_POINT_KELLY_REDUCTION = 0.40  # Reduce Kelly by 40%

# Cloud cover threshold for suppression
CLOUD_COVER_THRESHOLD = 80.0  # percent
CLOUD_SUPPRESSION_F = 3.5


async def _fetch_weather_data(lat: float, lon: float) -> Optional[dict]:
    """
    Fetch current weather data from Open-Meteo.
    Returns dict with wind, dew_point, cloud_cover or None.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "wind_direction_10m,wind_speed_10m",
        "hourly": "temperature_2m,dew_point_2m,cloud_cover",
        "timezone": "auto",
        "forecast_days": 1,
        "temperature_unit": "fahrenheit",  # Force Fahrenheit
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning("[ATMOS] Open-Meteo fetch error for (%.4f, %.4f): %s", lat, lon, e)
        return None


def _get_wind_direction_bucket(wind_dir: float) -> str:
    """Classify wind direction into named buckets."""
    if wind_dir is None:
        return "unknown"
    wind_dir = wind_dir % 360
    if wind_dir <= 45 or wind_dir > 315:
        return "from_north"
    elif wind_dir <= 90:
        return "from_east"
    elif wind_dir <= 135:
        return "from_southeast"
    elif wind_dir <= 180:
        return "from_south"
    elif wind_dir <= 225:
        return "from_southwest"
    elif wind_dir <= 270:
        return "from_west"
    else:
        return "from_northwest"


async def get_wind_correction(city_slug: str, lat: float, lon: float) -> float:
    """
    Fetch current wind direction and apply per-city wind correction.

    Returns correction in Fahrenheit to add to forecast.
    """
    data = await _fetch_weather_data(lat, lon)
    if not data:
        return 0.0

    try:
        current = data.get("current", {})
        wind_dir = current.get("wind_direction_10m")
        wind_speed = current.get("wind_speed_10m", 0)

        if wind_dir is None:
            return 0.0

        wind_dir = float(wind_dir)
        wind_speed = float(wind_speed) if wind_speed else 0

        # Only apply correction if wind is significant (>5 km/h)
        if wind_speed < 5:
            return 0.0

        bucket = _get_wind_direction_bucket(wind_dir)
        city_corrections = WIND_CORRECTIONS.get(city_slug, WIND_CORRECTIONS["default"])
        correction = city_corrections.get(bucket, city_corrections.get("default", 0.0))

        # Scale correction by wind speed (stronger wind = more effect)
        speed_factor = min(wind_speed / 20.0, 1.5)  # Cap at 1.5x
        correction *= speed_factor

        logger.debug("[ATMOS] %s wind correction: dir=%.0f bucket=%s speed=%.1f -> %.2fF",
                     city_slug, wind_dir, bucket, wind_speed, correction)
        return round(correction, 2)

    except Exception as e:
        logger.debug("[ATMOS] Wind correction error for %s: %s", city_slug, e)
        return 0.0


async def get_dew_point_suppression(lat: float, lon: float, forecast_f: float) -> tuple[float, float]:
    """
    Fetch dew point and check for suppression condition.

    If forecast temp is within 5°F of dew point (high humidity, fog risk),
    the actual high temp is often lower than forecast due to evaporative cooling.

    Returns:
        (temp_adjustment_f, kelly_multiplier)
    """
    data = await _fetch_weather_data(lat, lon)
    if not data:
        return 0.0, 1.0

    try:
        hourly = data.get("hourly", {})
        dew_points = hourly.get("dew_point_2m", [])
        temps = hourly.get("temperature_2m", [])

        if not dew_points or not temps:
            return 0.0, 1.0

        # Get max dew point for today (already in Fahrenheit)
        max_dew_point_f = max(dew_points[:24]) if dew_points else 0

        spread = forecast_f - max_dew_point_f

        if spread < DEW_POINT_SPREAD_THRESHOLD:
            logger.info("[ATMOS] Dew point suppression: forecast=%.1fF dew_point=%.1fF spread=%.1fF -> suppress %.1fF, Kelly * %.0f%%",
                        forecast_f, max_dew_point_f, spread, DEW_POINT_SUPPRESSION_F, DEW_POINT_KELLY_REDUCTION * 100)
            return -DEW_POINT_SUPPRESSION_F, DEW_POINT_KELLY_REDUCTION
        else:
            return 0.0, 1.0

    except Exception as e:
        logger.debug("[ATMOS] Dew point error: %s", e)
        return 0.0, 1.0


async def get_cloud_correction(lat: float, lon: float) -> float:
    """
    Fetch cloud cover and apply suppression if overcast.

    Heavy cloud cover (>80%) reduces daytime high temperatures.
    Returns correction in Fahrenheit (negative = suppress).
    """
    data = await _fetch_weather_data(lat, lon)
    if not data:
        return 0.0

    try:
        hourly = data.get("hourly", {})
        cloud_cover = hourly.get("cloud_cover", [])

        if not cloud_cover:
            return 0.0

        # Get max cloud cover for today
        max_cloud = max(cloud_cover[:24]) if cloud_cover else 0
        max_cloud = float(max_cloud)

        if max_cloud > CLOUD_COVER_THRESHOLD:
            # Scale suppression: 80% = 0%, 100% = full suppression
            overcast_pct = (max_cloud - CLOUD_COVER_THRESHOLD) / (100.0 - CLOUD_COVER_THRESHOLD)
            correction = -CLOUD_SUPPRESSION_F * overcast_pct
            logger.info("[ATMOS] Cloud suppression: cloud=%.0f%% -> suppress %.1fF",
                        max_cloud, abs(correction))
            return round(correction, 2)
        else:
            return 0.0

    except Exception as e:
        logger.debug("[ATMOS] Cloud correction error: %s", e)
        return 0.0


def get_time_adjustment(market_end_time: str | None) -> dict:
    """
    Compute MIN_EDGE and MAX_BET multipliers based on hours to market resolution.

    Time zones:
      > 48h:    Early entry  - edge_mult=1.3, kelly_mult=0.5  (wider edge needed, smaller bets)
      24-48h:   Standard     - edge_mult=1.0, kelly_mult=1.0
      12-24h:   High conf    - edge_mult=0.8, kelly_mult=1.2  (tighter edge OK, bigger bets)
      2-12h:    Danger zone  - edge_mult=0.6, kelly_mult=1.5  (very tight, max bets)
      < 2h:     Too late     - edge_mult=2.0, kelly_mult=0.0  (no new trades)

    Returns:
        {"edge_mult": float, "kelly_mult": float, "hours_to_close": float, "zone": str}
    """
    if not market_end_time:
        return {"edge_mult": 1.0, "kelly_mult": 1.0, "hours_to_close": 999, "zone": "unknown"}

    try:
        end_dt = datetime.fromisoformat(market_end_time.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        hours_to_close = (end_dt - now).total_seconds() / 3600.0

        if hours_to_close < 2:
            zone = "too_late"
            edge_mult = 2.0
            kelly_mult = 0.0
        elif hours_to_close < 12:
            zone = "danger"
            edge_mult = 0.6
            kelly_mult = 1.5
        elif hours_to_close < 24:
            zone = "high_confidence"
            edge_mult = 0.8
            kelly_mult = 1.2
        elif hours_to_close < 48:
            zone = "standard"
            edge_mult = 1.0
            kelly_mult = 1.0
        else:
            zone = "early_entry"
            edge_mult = 1.3
            kelly_mult = 0.5

        return {
            "edge_mult": edge_mult,
            "kelly_mult": kelly_mult,
            "hours_to_close": round(hours_to_close, 1),
            "zone": zone,
        }

    except Exception as e:
        logger.debug("[ATMOS] Time adjustment error: %s", e)
        return {"edge_mult": 1.0, "kelly_mult": 1.0, "hours_to_close": 999, "zone": "error"}


async def get_all_atmospheric_corrections(
    city_slug: str,
    lat: float,
    lon: float,
    forecast_f: float,
    market_end_time: str | None = None,
) -> dict:
    """
    Compute all atmospheric corrections for a city.

    Returns:
        {
            "wind_correction_f": float,
            "dew_point_suppression_f": float,
            "dew_point_kelly_mult": float,
            "cloud_correction_f": float,
            "total_atmos_correction_f": float,
            "time_adjustment": dict,
        }
    """
    wind = await get_wind_correction(city_slug, lat, lon)
    dew_suppress, dew_kelly = await get_dew_point_suppression(lat, lon, forecast_f)
    cloud = await get_cloud_correction(lat, lon)
    time_adj = get_time_adjustment(market_end_time)

    total = wind + dew_suppress + cloud

    result = {
        "wind_correction_f": wind,
        "dew_point_suppression_f": dew_suppress,
        "dew_point_kelly_mult": dew_kelly,
        "cloud_correction_f": cloud,
        "total_atmos_correction_f": round(total, 2),
        "time_adjustment": time_adj,
    }

    logger.info("[ATMOS] %s corrections: wind=%+.2f dew=%+.1f cloud=%+.2f total=%+.2f zone=%s",
                city_slug, wind, dew_suppress, cloud, total, time_adj.get("zone", "?"))

    return result


def log_corrections_to_redis(city_slug: str, corrections: dict):
    """Store atmospheric corrections in Redis for debugging."""
    try:
        import redis as _redis_mod
        r = _redis_mod.from_url(os.environ.get("REDIS_URL", ""))
        if r:
            r.hset(f"city_metrics:{city_slug}", mapping={
                "atmos_wind": str(corrections.get("wind_correction_f", 0)),
                "atmos_dew": str(corrections.get("dew_point_suppression_f", 0)),
                "atmos_cloud": str(corrections.get("cloud_correction_f", 0)),
                "atmos_total": str(corrections.get("total_atmos_correction_f", 0)),
                "time_zone": corrections.get("time_adjustment", {}).get("zone", "?"),
                "time_hours": str(corrections.get("time_adjustment", {}).get("hours_to_close", 0)),
                "edge_mult": str(corrections.get("time_adjustment", {}).get("edge_mult", 1.0)),
                "kelly_mult": str(corrections.get("time_adjustment", {}).get("kelly_mult", 1.0)),
            })
    except Exception as e:
        logger.debug("[ATMOS] Redis logging error: %s", e)


# ---------------------------------------------------------------------------
# Timezone warfare
# ---------------------------------------------------------------------------

def get_timezone_multiplier(utc_hour: int) -> float:
    """
    Return a Kelly multiplier based on UTC hour.

    During the Asian midnight window (03:00-06:00 UTC), liquidity is thin
    and markets underreact to fresh data. This is our edge window.

    03:00 <= utc_hour < 06:00 -> 1.5x (high-confidence entries)
    otherwise                  -> 1.0x (normal)

    Args:
        utc_hour: Current UTC hour (0-23).

    Returns:
        Multiplier for Kelly bet size.
    """
    if 3 <= utc_hour < 6:
        logger.info(f"[TZ_WARFARE] UTC hour {utc_hour} -> 1.5x Kelly multiplier (thin liquidity window)")
        return 1.5
    return 1.0


def get_sst_adjustment(city: str, lat: float, lon: float) -> float:
    """
    Fetch Sea Surface Temperature anomaly for coastal cities and return
    a temperature adjustment in Fahrenheit.

    Uses NOAA OISST (Optimum Interpolation SST) via ERDDAP or Open-Meteo marine API.
    Only applied to coastal cities:
        Cape Town, Mumbai, Hong Kong, Miami, Manila, Jakarta, Bangkok.

    The adjustment is: temp_change = sst_anomaly_c * influence_factor

    Args:
        city: City slug.
        lat: Latitude.
        lon: Longitude.

    Returns:
        Temperature adjustment in Fahrenheit (positive = warmer).
    """
    COASTAL_CITIES = {"cape_town", "mumbai", "hong_kong", "miami", "manila", "jakarta", "bangkok"}
    if city.lower() not in COASTAL_CITIES:
        return 0.0

    # Influence factor: how many F per 1C SST anomaly
    SST_INFLUENCE = {
        "cape_town": 0.8,
        "mumbai": 1.2,
        "hong_kong": 1.0,
        "miami": 1.3,
        "manila": 1.1,
        "jakarta": 0.7,
        "bangkok": 0.9,
    }

    influence = SST_INFLUENCE.get(city.lower(), 1.0)

    try:
        # Use Open-Meteo Marine API (free, no key needed)
        url = "https://marine-api.open-meteo.com/v1/marine"
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "sea_surface_temperature",
            "timezone": "auto",
            "forecast_days": 0,  # Today only = most recent obs
            "past_days": 1,       # Include yesterday for latest data
        }
        resp = httpx.get(url, params=params, timeout=10)
        data = resp.json()
        daily = data.get("daily", {})
        sst_values = daily.get("sea_surface_temperature", [])

        if not sst_values:
            logger.debug(f"[SST] No SST data for {city}")
            return 0.0

        # Most recent valid SST
        current_sst = None
        for v in reversed(sst_values):
            if v is not None:
                current_sst = float(v)
                break

        if current_sst is None:
            return 0.0

        # Approximate 30-year baseline per city (climatological monthly mean SST)
        CITY_SST_BASELINE = {
            "cape_town": 14.0,   # °C annual avg
            "mumbai": 28.0,
            "hong_kong": 24.0,
            "miami": 26.0,
            "manila": 28.5,
            "jakarta": 29.0,
            "bangkok": 27.0,     # Gulf of Thailand
        }

        baseline = CITY_SST_BASELINE.get(city.lower(), 25.0)
        sst_anomaly_c = current_sst - baseline
        adjustment_f = sst_anomaly_c * influence * 1.8  # Convert C to F and apply influence

        logger.info(f"[SST] {city}: SST={current_sst:.1f}C baseline={baseline:.1f}C "
                     f"anomaly={sst_anomaly_c:+.1f}C -> adj={adjustment_f:+.1f}F")
        return round(adjustment_f, 2)

    except Exception as e:
        logger.debug(f"[SST] Error for {city}: {e}")
        return 0.0


def detect_active_storm(lat: float, lon: float) -> dict:
    """
    Detect active storm conditions using Open-Meteo current weather data.

    Signals checked:
        1. High precipitation (>10mm in last hour)
        2. High cloud cover (>90%)
        3. Strong wind gusts (>50 km/h)
        4. Low atmospheric pressure trend (from Open-Meteo)
        5. High lightning potential (approximated by convective available potential energy)

    If >= 3 signals detected, storm is active -> suppress forecasts and reduce Kelly.

    Returns:
        {"suppress": bool, "adjustment": float, "signals": list[str], "signal_count": int}
    """
    signals = []

    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": "precipitation,weather_code,wind_gusts_10m,cloud_cover,surface_pressure",
            "hourly": "precipitation_probability",
            "timezone": "auto",
            "forecast_days": 1,
        }
        resp = httpx.get(url, params=params, timeout=10)
        data = resp.json()
        current = data.get("current", {})

        # Signal 1: Precipitation
        precip = float(current.get("precipitation", 0))
        if precip > 10:
            signals.append(f"precip_{precip:.1f}mm")

        # Signal 2: Cloud cover
        cloud = float(current.get("cloud_cover", 0))
        if cloud > 90:
            signals.append(f"cloud_{cloud:.0f}pct")

        # Signal 3: Wind gusts
        gusts = float(current.get("wind_gusts_10m", 0))
        if gusts > 50:
            signals.append(f"gust_{gusts:.0f}kmh")

        # Signal 4: Low pressure (below 1000 hPa is stormy)
        pressure = float(current.get("surface_pressure", 1013))
        if pressure < 1000:
            signals.append(f"low_pressure_{pressure:.0f}hPa")

        # Signal 5: Severe weather code
        wmo_code = int(current.get("weather_code", 0))
        if wmo_code >= 95:  # Thunderstorm codes: 95-99
            signals.append(f"thunderstorm_wmo{wmo_code}")

        signal_count = len(signals)
        suppress = signal_count >= 3
        adjustment = -6.0 if suppress else 0.0

        if suppress:
            logger.warning(f"[STORM] ACTIVE STORM detected ({signal_count} signals: {signals}) -> suppress=True adj={adjustment}F")
        elif signal_count > 0:
            logger.debug(f"[STORM] {signal_count} weak signals: {signals}")

        return {"suppress": suppress, "adjustment": adjustment, "signals": signals, "signal_count": signal_count}

    except Exception as e:
        logger.debug(f"[STORM] Detection error: {e}")
        return {"suppress": False, "adjustment": 0.0, "signals": [], "signal_count": 0}


async def get_all_atmospheric_corrections_v2(
    city_slug: str,
    lat: float,
    lon: float,
    forecast_f: float,
    market_end_time: str | None = None,
) -> dict:
    """
    Extended atmospheric corrections including SST and storm detection.
    Replaces get_all_atmospheric_corrections() with additional layers.

    Returns dict with all corrections plus sst_adjustment and storm info.
    """
    # Base corrections
    base = await get_all_atmospheric_corrections(
        city_slug, lat, lon, forecast_f, market_end_time
    )

    # SST adjustment (synchronous httpx call)
    sst_adj = get_sst_adjustment(city_slug, lat, lon)
    base["sst_adjustment_f"] = sst_adj
    if sst_adj != 0:
        base["total_atmos_correction_f"] = round(
            base["total_atmos_correction_f"] + sst_adj, 2
        )

    # Storm detection
    storm = detect_active_storm(lat, lon)
    base["storm_detected"] = storm["suppress"]
    base["storm_adjustment_f"] = storm["adjustment"]
    base["storm_signals"] = storm["signals"]
    if storm["suppress"]:
        base["total_atmos_correction_f"] = round(
            base["total_atmos_correction_f"] + storm["adjustment"], 2
        )
        base["storm_kelly_mult"] = 0.5  # Reduce Kelly by 50%
    else:
        base["storm_kelly_mult"] = 1.0

    return base
