"""
Polybot trading configuration — city tiers, sanity gates, climatology.

This file contains all the trading logic upgrades:
- City tier system (TIER_1_ACTIVE, TIER_2_PENDING, TIER_3_SUSPENDED)
- Pre-trade sanity gate (5 checks)
- Climatological normals per city
- Adaptive cycle speed
- Multi-model ensemble weights
"""

from __future__ import annotations

import json
import logging
import os
import math
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# =============================================================================
# CITY TIER SYSTEM
# =============================================================================

TIER_1_ACTIVE = [
    "Atlanta",    # KATL — flat terrain, HRRR excellent
    "Dallas",     # KDFW — flat terrain, HRRR excellent
    "Houston",    # KIAH — flat terrain, Gulf moisture
    "Miami",      # KMIA — SST-stabilized, consistent
    "Cape Town",  # FACT — ECMWF excellent for southern Africa
    "Mumbai",     # VABB — monsoon predictable when calibrated
    "Hong Kong",  # VHHH — ECMWF good, maritime moderation
]

TIER_2_PENDING = [
    "Seoul",         # RKSI — good summer, spring volatile
    "Chongqing",     # ZUCK — add after spring ends (July+)
    "Buenos Aires",  # SAEZ — low bot saturation = high edge
    "Bangkok",       # VTBS — consistent tropical = predictable
    "Singapore",     # WSSS — extremely stable = high accuracy
    "Lagos",         # DNMM — very low bot saturation
]

TIER_3_SUSPENDED = [
    "Beijing",   # ZBAA — Siberian outbreaks cause 20°F swings
    "Shanghai",  # ZSPD — same continental volatility issue
]


def is_city_active(city: str) -> bool:
    """Check if city is in the active trading tier."""
    if city in TIER_3_SUSPENDED:
        logger.info(f"SKIP {city}: suspended due to high forecast error")
        return False
    if city not in TIER_1_ACTIVE:
        logger.info(f"SKIP {city}: pending calibration data (Tier 2)")
        return False
    return True


# =============================================================================
# CLIMATOLOGY — Monthly normal highs (°F) for sanity checking
# =============================================================================

CLIMATOLOGY = {
    "Atlanta":   {"jan": 52, "feb": 56, "mar": 64, "apr": 73,
                  "may": 80, "jun": 87, "jul": 90, "aug": 89,
                  "sep": 83, "oct": 73, "nov": 63, "dec": 54},
    "Dallas":    {"jan": 55, "feb": 60, "mar": 68, "apr": 76,
                  "may": 84, "jun": 92, "jul": 97, "aug": 97,
                  "sep": 89, "oct": 78, "nov": 66, "dec": 57},
    "Houston":   {"jan": 62, "feb": 66, "mar": 73, "apr": 80,
                  "may": 87, "jun": 93, "jul": 96, "aug": 96,
                  "sep": 90, "oct": 81, "nov": 71, "dec": 63},
    "Miami":     {"jan": 75, "feb": 77, "mar": 81, "apr": 85,
                  "may": 88, "jun": 90, "jul": 91, "aug": 91,
                  "sep": 90, "oct": 86, "nov": 81, "dec": 77},
    "Cape Town": {"jan": 78, "feb": 79, "mar": 75, "apr": 68,
                  "may": 63, "jun": 59, "jul": 58, "aug": 61,
                  "sep": 65, "oct": 70, "nov": 74, "dec": 77},
    "Mumbai":    {"jan": 88, "feb": 90, "mar": 94, "apr": 97,
                  "may": 95, "jun": 89, "jul": 86, "aug": 85,
                  "sep": 88, "oct": 92, "nov": 92, "dec": 90},
    "Hong Kong": {"jan": 65, "feb": 65, "mar": 70, "apr": 77,
                  "may": 84, "jun": 89, "jul": 91, "aug": 91,
                  "sep": 88, "oct": 82, "nov": 74, "dec": 68},
}

MONTH_NAMES = {
    1: "jan", 2: "feb", 3: "mar", 4: "apr", 5: "may", 6: "jun",
    7: "jul", 8: "aug", 9: "sep", 10: "oct", 11: "nov", 12: "dec"
}


# =============================================================================
# PRE-TRADE SANITY GATE — 5 checks before ANY trade
# =============================================================================

def pre_trade_gate(
    city: str,
    forecast_f: float,
    edge: float,
    month: int,
    ensemble_spread: float,
) -> tuple[bool, str]:
    """
    5-point sanity check before any trade is placed.
    Returns (should_trade: bool, reason: str)
    """

    # CHECK 1: Edge sufficient (raised from 8% which lost money)
    if edge < 0.20:
        return False, f"Edge {edge*100:.1f}% below 20% minimum"

    # CHECK 2: Forecast within 12°F of climatological normal
    month_name = MONTH_NAMES.get(month, "jan")
    climo = CLIMATOLOGY.get(city, {}).get(month_name)
    if climo:
        deviation = abs(forecast_f - climo)
        if deviation > 12.0:
            return False, (f"Forecast {forecast_f:.1f}F deviates {deviation:.1f}F "
                          f"from climatology {climo}F — possible unit bug")

    # CHECK 3: Ensemble not wildly disagreeing
    if ensemble_spread > 8.0:
        return False, f"Ensemble spread {ensemble_spread:.1f}F too wide — skip"

    # CHECK 4: City is active
    if not is_city_active(city):
        return False, f"{city} not in active trading tier"

    # CHECK 5: Temperature sanity (unit check)
    from polybot.forecast import sanity_check_temp
    try:
        sanity_check_temp(city, forecast_f, "pre_trade_gate")
    except ValueError as e:
        return False, str(e)

    return True, "ALL CHECKS PASSED"


# =============================================================================
# ADAPTIVE CYCLE SPEED
# =============================================================================

GFS_UTC_HOURS = [0, 6, 12, 18]


def minutes_to_next_gfs() -> int:
    """Returns minutes until next GFS model release."""
    now = datetime.now(timezone.utc)
    current_hour = now.hour
    current_min = now.minute

    next_hour = next((h for h in GFS_UTC_HOURS if h > current_hour), GFS_UTC_HOURS[0])
    if next_hour <= current_hour:
        hours_away = (24 - current_hour) + next_hour
    else:
        hours_away = next_hour - current_hour

    return (hours_away * 60 - current_min) + 5  # +5 for processing time


def get_adaptive_cycle_seconds(
    cities_data: dict,
    gfs_imminent_override: bool = False,
) -> tuple[int, str]:
    """
    Dynamically adjusts scan frequency based on conditions.
    Returns (cycle_seconds, reason).
    """
    max_slope = 0.0
    any_peak = False
    for city_data in cities_data.values():
        slope = abs(city_data.get("slope_f_per_5min", 0))
        if slope > max_slope:
            max_slope = slope
        if city_data.get("in_peak_window", False):
            any_peak = True

    gfs_imminent = gfs_imminent_override or minutes_to_next_gfs() <= 10

    if gfs_imminent:
        return 60, "GFS window imminent"
    elif max_slope > 0.5:
        return 90, f"Fast slope {max_slope:.2f}F/5min"
    elif any_peak:
        return 300, "Peak window active"
    else:
        return 900, "Normal cycle"


# =============================================================================
# MULTI-MODEL ENSEMBLE WEIGHTS
# =============================================================================

DEFAULT_MODEL_WEIGHTS = {
    "gfs":   1.0,
    "ecmwf": 1.4,   # generally best globally
    "icon":  1.1,
    "ukmo":  1.2,
}


def get_model_weights(city: str) -> dict[str, float]:
    """Load city-specific model weights or return defaults."""
    weights_file = f"/polybot-data/model_weights_{city.replace(' ', '_')}.json"
    if os.path.exists(weights_file):
        try:
            with open(weights_file) as f:
                return json.load(f)
        except Exception:
            pass
    return DEFAULT_MODEL_WEIGHTS.copy()


# =============================================================================
# ATMOSPHERIC CORRECTIONS
# =============================================================================

AFTERNOON_SPIKE = {
    "Seoul": 2.1, "Beijing": 2.5, "Dallas": 1.8,
    "Atlanta": 1.5, "Shanghai": 1.9, "Chongqing": 1.6,
    "Mumbai": 0.5, "Cape Town": 0.8, "Hong Kong": 1.0,
}


def apply_atmospheric_corrections(
    base_forecast_f: float,
    atmos: dict,
    city: str,
) -> tuple[float, float, float]:
    """
    Apply physical corrections to the base GFS forecast.
    Returns (corrected_forecast, confidence, total_adjustment).
    """
    adjustment = 0.0
    confidence_penalty = 0.0

    # CAPE correction (storm suppression)
    cape = atmos.get("cape", 0)
    if cape > 2000:
        adjustment -= 6.0
        confidence_penalty += 0.4
    elif cape > 1000:
        adjustment -= 3.0
        confidence_penalty += 0.2

    # Dew point correction (near-saturation = fog/rain)
    dew_spread = atmos.get("dew_spread_f", 20)
    if dew_spread < 5:
        adjustment -= 3.5
        confidence_penalty += 0.3

    # Cloud cover correction
    cloud = atmos.get("cloud_pct", 0)
    if cloud > 80:
        adjustment -= 3.5
    elif cloud > 60:
        adjustment -= 2.0

    # Solar radiation correction (sunny day boost)
    solar = atmos.get("solar_wm2", 0)
    if solar > 700:
        adjustment += 2.5
    elif solar < 200:
        adjustment -= 2.0

    # Pressure trend (falling pressure = incoming storm)
    pressure_trend = atmos.get("pressure_trend", 0)
    if pressure_trend < -3:
        adjustment -= 4.0
        confidence_penalty += 0.3
    elif pressure_trend > 2:
        adjustment += 1.5

    # Rain probability
    rain_prob = atmos.get("rain_prob_pct", 0)
    if rain_prob > 70:
        adjustment -= 3.0
        confidence_penalty += 0.2

    # Afternoon spike correction (known systematic underestimate)
    if cloud < 30 and solar > 500:  # only on sunny days
        adjustment += AFTERNOON_SPIKE.get(city, 0.5)

    corrected = base_forecast_f + adjustment
    confidence = max(0.1, 1.0 - confidence_penalty)

    return corrected, confidence, adjustment
