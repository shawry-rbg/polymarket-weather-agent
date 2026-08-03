"""
ENSO (El Nino-Southern Oscillation) macro overlay.

Adjusts forecasts based on the current ENSO state (El Nino, La Nina, or Neutral).
The ENSO state shifts regional temperature patterns globally.

Usage:
    from polybot.enso import get_enso_adjustment, get_enso_state
    adj = get_enso_adjustment("dallas")
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ENSO state sources:
# - CPC (Climate Prediction Center): https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/enso_advisory/ensodisc.shtml
# - NOAA ONI index: https://origin.cpc.ncep.noaa.gov/products/analysis_monitoring/ensostuff/ONI_v5.php

# Current ENSO state - can be updated via API or manual override
# Valid values: "EL_NINO", "EL_NINO_EMERGING", "LA_NINA", "LA_NINA_EMERGING", "NEUTRAL", "ENSODISC"
DEFAULT_ENSO_STATE = "EL_NINO_EMERGING"
ENSO_CACHE_PATH = "/polybot-data/enso_state.json"
ENSO_CACHE_TTL = 86400  # Refresh daily

# ---------------------------------------------------------------------------
# Regional ENSO temperature adjustments (in Fahrenheit)
# Positive = El Nino makes this region warmer; Negative = cooler
# ---------------------------------------------------------------------------

ENSO_CITY_ADJUSTMENTS: dict[str, dict[str, float]] = {
    # El Nino adjustments
    "EL_NINO": {
        # US South / Gulf: Warmer and wetter
        "dallas": +2.5,
        "miami": +2.0,
        "atlanta": +1.5,
        "houston": +2.0,
        "new_orleans": +1.8,

        # Pacific / Southeast Asia: Drier, variable
        "jakarta": -1.5,
        "manila": -0.5,
        "bangkok": -1.0,
        "ho_chi_minh_city": -0.8,
        "kuala_lumpur": -1.2,

        # South Asia: Weaker monsoon = warmer
        "mumbai": +1.5,

        # East Asia: Mixed
        "shanghai": +0.5,
        "hong_kong": +0.3,
        "seoul": +0.8,
        "beijing": +0.5,
        "taipei": +0.2,
        "shenzhen": +0.3,
        "guangzhou": +0.3,
        "chongqing": +0.0,

        # Southern Africa: Drier, cooler
        "cape_town": -1.5,

        # South America
        "buenos_aires": +1.0,
        "mexico_city": +0.5,

        # Europe: Mild effect
        "london": +0.3,
        "istanbul": +0.5,
    },

    # Emerging El Nino (partial effect: 60% of full)
    "EL_NINO_EMERGING": {
        "dallas": +1.5,       # 60% of +2.5
        "miami": +1.2,        # 60% of +2.0
        "atlanta": +0.9,
        "jakarta": -0.9,
        "manila": -0.3,
        "bangkok": -0.6,
        "ho_chi_minh_city": -0.5,
        "kuala_lumpur": -0.7,
        "mumbai": +0.9,
        "shanghai": +0.3,
        "hong_kong": +0.2,
        "seoul": +0.5,
        "beijing": +0.3,
        "cape_town": -0.9,
        "buenos_aires": +0.6,
        "mexico_city": +0.3,
        "london": +0.2,
        "istanbul": +0.3,
        "chongqing": +0.0,
        "taipei": +0.1,
        "shenzhen": +0.2,
        "guangzhou": +0.2,
    },

    # La Nina adjustments (reverse of El Nino)
    "LA_NINA": {
        "dallas": -1.5,
        "miami": -1.0,
        "atlanta": -0.8,
        "jakarta": +1.5,
        "manila": +0.8,
        "bangkok": +1.2,
        "ho_chi_minh_city": +1.0,
        "kuala_lumpur": +1.2,
        "mumbai": -1.0,
        "cape_town": +1.0,
    },

    # Neutral: no adjustments
    "NEUTRAL": {},
}

# Cities without explicit adjustment get 0.0


def get_enso_state() -> str:
    """
    Get current ENSO state. Checks cache first, then falls back to default.
    In production, this could fetch from CPC API.

    Returns:
        ENSO state string: "EL_NINO", "EL_NINO_EMERGING", "LA_NINA", etc.
    """
    # Try cached state
    try:
        if os.path.exists(ENSO_CACHE_PATH):
            with open(ENSO_CACHE_PATH) as f:
                cached = json.load(f)
            cached_ts = cached.get("updated_at", 0)
            if time.time() - cached_ts < ENSO_CACHE_TTL:
                state = cached.get("state", DEFAULT_ENSO_STATE)
                logger.debug(f"[ENSO] Using cached state: {state}")
                return state
    except Exception:
        pass

    # In production, fetch from CPC or NOAA ONI API
    # For now, return the default (manually updated)
    return DEFAULT_ENSO_STATE


def set_enso_state(state: str) -> None:
    """Manually set and cache the ENSO state."""
    os.makedirs(os.path.dirname(ENSO_CACHE_PATH), exist_ok=True)
    with open(ENSO_CACHE_PATH, "w") as f:
        json.dump({"state": state, "updated_at": time.time()}, f)
    logger.info(f"[ENSO] State set to: {state}")


def get_enso_adjustment(city: str, state: Optional[str] = None) -> float:
    """
    Get ENSO temperature adjustment for a city.

    Uses per-city adjustments from ENSO_CITY_ADJUSTMENTS dict.
    The default ENSO state is EL_NINO_EMERGING.

    Args:
        city: City slug.
        state: ENSO state (auto-detected if None).

    Returns:
        Temperature adjustment in Fahrenheit (positive = add to forecast).
    """
    if state is None:
        state = get_enso_state()

    adjustments = ENSO_CITY_ADJUSTMENTS.get(state, {})
    adj = adjustments.get(city.lower(), 0.0)

    if adj != 0:
        logger.info(f"[ENSO] {city}: state={state} -> adj={adj:+.1f}F")

    return adj


def apply_enso_to_forecast(forecast_f: float, city: str, state: Optional[str] = None) -> tuple[float, float]:
    """
    Apply ENSO correction to a forecast.

    Args:
        forecast_f: Raw ensemble forecast in Fahrenheit.
        city: City slug.
        state: ENSO state (auto-detected if None).

    Returns:
        (corrected_forecast_f, adjustment_f)
    """
    adj = get_enso_adjustment(city, state)
    corrected = forecast_f + adj
    logger.info(f"[ENSO] Forecast {forecast_f:.1f}F + {adj:+.1f}F (ENSO) = {corrected:.1f}F")
    return corrected, adj
