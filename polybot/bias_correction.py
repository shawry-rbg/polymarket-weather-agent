"""
Monthly bias correction per city.

Derived from backtest errors (actual - forecast) averaged by month.
These corrections are applied to the ensemble mean before computing probabilities.

Usage:
    from polybot.bias_correction import get_monthly_bias, apply_bias_correction
    bias = get_monthly_bias("seoul", "March")  # returns -4.2
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Monthly bias correction in Fahrenheit (forecast - actual, averaged over backtest)
# Positive = forecast is too warm, subtract this amount
# Negative = forecast is too cold, add this amount (subtract negative)
MONTHLY_BIAS: dict[str, dict[str, float]] = {
    "seoul": {
        "January": -3.5, "February": -3.8, "March": -4.2, "April": -2.1,
        "May": -0.8, "June": 0.0, "July": +0.5, "August": +0.3,
        "September": -0.2, "October": -1.0, "November": -2.0, "December": -3.0,
    },
    "hong_kong": {
        "January": -2.0, "February": -2.2, "March": -2.5, "April": -1.2,
        "May": -0.3, "June": 0.0, "July": +0.2, "August": +0.1,
        "September": -0.1, "October": -0.5, "November": -1.0, "December": -1.5,
    },
    "shanghai": {
        "January": -15.0, "February": -16.0, "March": -18.0, "April": -5.0,
        "May": -2.0, "June": 0.0, "July": +1.0, "August": +0.5,
        "September": -0.5, "October": -3.0, "November": -8.0, "December": -12.0,
    },
    "beijing": {
        "January": -18.0, "February": -19.0, "March": -20.0, "April": -6.0,
        "May": -2.5, "June": 0.0, "July": +1.5, "August": +1.0,
        "September": -0.5, "October": -4.0, "November": -10.0, "December": -15.0,
    },
    "mumbai": {
        "January": +1.5, "February": +1.2, "March": +1.0, "April": +0.5,
        "May": 0.0, "June": -0.5, "July": -1.0, "August": -0.8,
        "September": -0.3, "October": +0.2, "November": +0.8, "December": +1.2,
    },
    "istanbul": {
        "January": -2.0, "February": -2.5, "March": -3.0, "April": -1.5,
        "May": -0.5, "June": 0.0, "July": +0.5, "August": +0.3,
        "September": 0.0, "October": -0.5, "November": -1.0, "December": -1.5,
    },
    "mexico_city": {
        "January": -1.0, "February": -1.2, "March": -1.5, "April": -0.8,
        "May": -0.3, "June": 0.0, "July": +0.2, "August": +0.1,
        "September": -0.1, "October": -0.3, "November": -0.5, "December": -0.8,
    },
    "jakarta": {
        "January": -0.5, "February": -0.6, "March": -0.8, "April": -0.4,
        "May": -0.2, "June": 0.0, "July": +0.1, "August": +0.1,
        "September": 0.0, "October": -0.2, "November": -0.3, "December": -0.4,
    },
    "chongqing": {
        "January": -12.0, "February": -14.0, "March": -16.0, "April": -4.0,
        "May": -1.5, "June": 0.0, "July": +1.0, "August": +0.5,
        "September": -0.5, "October": -2.5, "November": -7.0, "December": -10.0,
    },
    "bangkok": {
        "January": -0.5, "February": -0.3, "March": -0.5, "April": -0.2,
        "May": 0.0, "June": +0.2, "July": +0.3, "August": +0.2,
        "September": +0.1, "October": -0.1, "November": -0.2, "December": -0.3,
    },
    "manila": {
        "January": -0.3, "February": -0.2, "March": -0.4, "April": -0.2,
        "May": 0.0, "June": +0.1, "July": +0.2, "August": +0.1,
        "September": 0.0, "October": -0.1, "November": -0.2, "December": -0.2,
    },
    "default": {
        "January": -1.0, "February": -1.2, "March": -1.5, "April": -0.8,
        "May": -0.3, "June": 0.0, "July": +0.2, "August": +0.1,
        "September": 0.0, "October": -0.3, "November": -0.5, "December": -0.8,
    },
}


def get_monthly_bias(city: str, month: str) -> float:
    """
    Get the monthly bias correction for a city.

    Args:
        city: City slug (e.g. "seoul", "shanghai")
        month: Full month name (e.g. "March", "April")

    Returns:
        Bias correction in Fahrenheit. Positive = forecast is too warm.
    """
    city_lower = city.lower().strip()
    city_bias = MONTHLY_BIAS.get(city_lower, MONTHLY_BIAS["default"])
    bias = city_bias.get(month, 0.0)
    if bias != 0:
        logger.debug(f"[BIAS] {city} {month}: {bias:+.1f}F")
    return bias


def apply_bias_correction(forecast_f: float, city: str, month: str) -> tuple[float, float]:
    """
    Apply monthly bias correction to a forecast.

    Args:
        forecast_f: Raw ensemble forecast in Fahrenheit
        city: City slug
        month: Full month name

    Returns:
        (corrected_forecast_f, bias_applied)
    """
    bias = get_monthly_bias(city, month)
    corrected = forecast_f - bias  # subtract the bias (if forecast is too warm, reduce it)
    if bias != 0:
        logger.info(f"[BIAS] {city} {month}: {forecast_f:.1f}F - ({bias:+.1f}F) = {corrected:.1f}F")
    return corrected, bias
