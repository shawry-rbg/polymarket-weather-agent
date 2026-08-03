"""
Correlation matrix for weather-based city pairs.

High-correlation city pairs reduce effective bet size when both cities
have open positions. Before opening a new trade, check if any existing
open trade has correlation > 0.8 with the new city and reduce bet size
by (1 - correlation).

Usage:
    from polybot.correlation import CITY_CORRELATIONS, check_correlation_penalty
    penalty = check_correlation_penalty("seoul", open_trades)
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Correlation matrix between city pairs (symmetric).
# Values clamped to [0, 1]. Only pairs with correlation > 0.5 are listed.
CITY_CORRELATIONS: dict[tuple[str, str], float] = {
    # East Asian cluster
    ("seoul", "beijing"): 0.72,
    ("shanghai", "shenzhen"): 0.85,
    ("hong_kong", "guangzhou"): 0.91,
    ("shanghai", "hong_kong"): 0.78,
    ("beijing", "shanghai"): 0.68,
    ("seoul", "shanghai"): 0.61,
    ("taipei", "hong_kong"): 0.73,
    ("taipei", "shanghai"): 0.67,
    ("shenzhen", "guangzhou"): 0.82,

    # Southeast Asian cluster
    ("bangkok", "ho_chi_minh_city"): 0.78,
    ("bangkok", "kuala_lumpur"): 0.71,
    ("bangkok", "manila"): 0.63,
    ("ho_chi_minh_city", "kuala_lumpur"): 0.69,
    ("manila", "ho_chi_minh_city"): 0.58,
    ("jakarta", "kuala_lumpur"): 0.65,
    ("bangkok", "jakarta"): 0.55,

    # South Asian cluster
    ("mumbai", "karachi"): 0.52,

    # US cluster
    ("dallas", "mexico_city"): 0.54,
    ("miami", "dallas"): 0.48,

    # European cluster
    ("london", "paris"): 0.67,
    ("london", "istanbul"): 0.35,
    ("istanbul", "cape_town"): 0.28,

    # China internal
    ("chongqing", "shanghai"): 0.72,
    ("chongqing", "wuhan"): 0.80,
    ("chongqing", "chengdu"): 0.88,
}

_CORR_THRESHOLD = 0.80  # Only penalize above this


def _city_key(city_a: str, city_b: str) -> tuple[str, str]:
    """Normalize city pair key for lookup."""
    return (city_a.lower().replace(" ", "_"), city_b.lower().replace(" ", "_"))


def get_correlation(city_a: str, city_b: str) -> float:
    """
    Get correlation coefficient between two cities.
    Returns 0 if pair not found.
    """
    key = _city_key(city_a, city_b)
    if key in CITY_CORRELATIONS:
        return CITY_CORRELATIONS[key]
    # Try reversed
    rev = (key[1], key[0])
    if rev in CITY_CORRELATIONS:
        return CITY_CORRELATIONS[rev]
    return 0.0


def check_correlation_penalty(city: str, open_trades: list[dict]) -> float:
    """
    Check if opening a trade in `city` would correlate with any existing open trade.
    Returns a multiplier (0.0 - 1.0) to apply to the bet size.

    If correlation > 0.8, bet is reduced by (1 - correlation).
    The worst (highest) correlation determines the penalty.

    Args:
        city: The new city slug to check.
        open_trades: List of open trade dicts (each must have a "city" key).

    Returns:
        Multiplier: 1.0 = no penalty, lower = reduce bet size.
    """
    max_corr = 0.0
    max_corr_city = ""

    for trade in open_trades:
        existing_city = trade.get("city", "")
        if not existing_city or existing_city == city:
            continue
        corr = get_correlation(city, existing_city)
        if corr > max_corr:
            max_corr = corr
            max_corr_city = existing_city

    if max_corr > _CORR_THRESHOLD:
        penalty = 1.0 - max_corr  # e.g., corr=0.91 -> penalty=0.09
        multiplier = max(penalty, 0.05)  # Floor at 5% to still keep some exposure
        logger.info(
            f"[CORR] {city} vs {max_corr_city}: corr={max_corr:.2f} "
            f"-> bet multiplier={multiplier:.2f} (penalty={penalty:.2f})"
        )
        return multiplier

    return 1.0


def get_all_correlated_cities(city: str, min_corr: float = 0.5) -> list[dict]:
    """
    Get all cities correlated with `city` above a threshold.
    Returns list of {"city": str, "correlation": float}.
    """
    results = []
    seen = set()
    for (a, b), corr in CITY_CORRELATIONS.items():
        if corr < min_corr:
            continue
        if a == city and b not in seen:
            results.append({"city": b, "correlation": corr})
            seen.add(b)
        elif b == city and a not in seen:
            results.append({"city": a, "correlation": corr})
            seen.add(a)
    return sorted(results, key=lambda x: -x["correlation"])
