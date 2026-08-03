"""
Settlement corrections per weather station.

Each Polymarket weather market resolves against a specific weather station.
Systematic biases exist between forecast models and actual station readings.
This module provides per-station correction factors (in Fahrenheit) that are
applied to the ensemble forecast before probability calculation.

Corrections are derived from historical analysis of forecast vs. actual
settlement data. Positive values mean the model over-predicts (subtract),
negative values mean the model under-predicts (add).

Format: STATION_CORRECTIONS[city_slug] = correction_f

Usage:
    correction = get_settlement_correction("london")  # -> -2.1
    adjusted_forecast = raw_forecast + correction
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Per-city settlement corrections (Fahrenheit)
# Derived from historical forecast vs. actual settlement analysis
# Positive = model over-predicts → subtract from forecast
# Negative = model under-predicts → add to forecast
STATION_CORRECTIONS: dict[str, float] = {
    # Europe
    "london": -2.1,        # EGLL (Heathrow) - models run warm
    "istanbul": -1.2,      # LTFM (Istanbul) - slight warm bias
    "cape_town": -0.8,     # FACT (Cape Town) - mild warm bias

    # Asia
    "chongqing": -0.8,     # ZUCK (Chongqing) - valley cold bias
    "seoul": -1.5,         # RKSS (Incheon) - coastal warm bias
    "shanghai": -1.0,     # ZSPD (Pudong) - coastal warm bias
    "beijing": -1.3,      # ZBAA (Capital) - urban heat island
    "hong_kong": -0.5,    # VHHH (HK Intl) - mild warm bias
    "mumbai": -0.7,       # VABB (Mumbai) - coastal warm bias
    "bangkok": -0.9,      # VTBS (Bangkok) - urban heat island
    "manila": -0.6,       # RPLL (Manila) - mild warm bias
    "kuala_lumpur": -0.5, # WMKK (KL) - mild warm bias
    "ho_chi_minh_city": -0.7, # VVTS (Saigon) - mild warm bias
    "taipei": -0.8,       # RCTP (Taoyuan) - mild warm bias
    "shenzhen": -0.6,     # ZGSZ (Shenzhen) - mild warm bias
    "guangzhou": -0.7,    # ZGGG (Guangzhou) - mild warm bias
    "jakarta": -0.4,      # WIII (Jakarta) - mild warm bias

    # Americas
    "nyc": -1.8,          # KJFK (JFK) - coastal warm bias
    "mexico_city": -1.0,  # MMMX (Mexico City) - altitude cold bias
    "buenos_aires": -0.9, # SAZE (Ezeiza) - mild warm bias
    "atlanta": -1.5,      # KATL (Hartsfield) - urban heat island
    "dallas": -1.2,       # KDFW (DFW) - urban heat island
    "miami": -0.6,        # KMIA (Miami) - coastal warm bias
}

# Default correction for cities not in the table
DEFAULT_CORRECTION = 0.0


def get_settlement_correction(city_slug: str) -> float:
    """
    Get the settlement correction for a city.

    Returns the correction in Fahrenheit to apply to the ensemble forecast.
    """
    correction = STATION_CORRECTIONS.get(city_slug, DEFAULT_CORRECTION)
    if city_slug not in STATION_CORRECTIONS:
        logger.debug("[CORRECTION] No correction for %s, using default %.1f", city_slug, DEFAULT_CORRECTION)
    return correction


def apply_correction(forecast_temp_f: float, city_slug: str) -> float:
    """
    Apply the settlement correction to a forecast temperature.

    Args:
        forecast_temp_f: Raw ensemble forecast temperature (Fahrenheit)
        city_slug: City identifier

    Returns:
        Corrected temperature (Fahrenheit)
    """
    correction = get_settlement_correction(city_slug)
    adjusted = forecast_temp_f + correction
    logger.debug(
        "[CORRECTION] %s: %.1fF + %.1fF = %.1fF",
        city_slug, forecast_temp_f, correction, adjusted,
    )
    return adjusted


def store_corrections_in_redis():
    """
    Store all corrections in Redis for debugging.
    Called once at startup.
    """
    try:
        import redis as _redis_mod
        r = _redis_mod.from_url(os.environ.get("REDIS_URL", ""))
        for city, correction in STATION_CORRECTIONS.items():
            r.hset("settlement_corrections", city, str(correction))
        logger.info("[CORRECTION] Stored %d corrections in Redis", len(STATION_CORRECTIONS))
    except Exception as e:
        logger.warning("[CORRECTION] Failed to store corrections in Redis: %s", e)


def get_all_corrections() -> dict[str, float]:
    """Return a copy of all corrections."""
    return dict(STATION_CORRECTIONS)
