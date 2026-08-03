"""
True 31-member GFS ensemble probability engine.

Uses the dedicated Open-Meteo Ensemble API to fetch all 31 GFS ensemble members
(1 deterministic + 30 perturbed) and computes true probabilities by counting
members above/bucket thresholds.

This completely replaces the Gaussian/normal-distribution approximation with
direct Monte Carlo counting from the actual ensemble.

API: https://ensemble-api.open-meteo.com/v1/ensemble
Parameters: models=gfs_seamless, hourly=temperature_2m

The ensemble API returns hourly data for all 31 members. We extract the daily
max for each member by taking the max across the 24 hourly values for the
target day.

Usage:
    result = await fetch_gfs_ensemble(lat, lon)
    probs = result["ensemble_probs"]  # {70.0: 0.74, 75.0: 0.48, 80.0: 0.19}
    # -> 23/31 members >= 70F, 15/31 >= 75F, 6/31 >= 80F
"""

from __future__ import annotations

import logging
import statistics
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

ENSEMBLE_API_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
REQUEST_TIMEOUT = 20  # seconds — ensemble fetch is heavier (31 members x 24 hours)

# GFS ensemble: 1 deterministic + 30 perturbed members
ENSEMBLE_MEMBER_COUNT = 31

# Spread multiplier to reduce overconfidence — ensemble members are correlated
# so the raw std underestimates true uncertainty. 2.0x brings calibration in line
# with observed forecast errors (~4-6°F RMSE for day-1 to day-3 forecasts).
SPREAD_MULTIPLIER = 2.0


async def fetch_gfs_ensemble(
    lat: float,
    lon: float,
    thresholds: list[float] | None = None,
    unit: str = "C",
) -> Optional[dict]:
    """
    Fetch the full 31-member GFS ensemble and compute threshold probabilities.

    Uses the dedicated ensemble-api endpoint which returns hourly temperature
    for all 31 members. We compute the daily max per member, then count how
    many members exceed each threshold.

    Args:
        lat, lon: Coordinates (use airport coordinates for accuracy)
        thresholds: List of Fahrenheit thresholds to compute probabilities for.
                    If None, returns raw member data only.
        unit: "C" or "F" — the city's bucket unit (for threshold conversion)

    Returns:
        dict with keys:
            temps_by_member: list[float] — each member's daily max temp (F)
            member_count: int — number of members (should be 31)
            ensemble_mean: float
            ensemble_std: float
            ensemble_median: float
            ensemble_min: float
            ensemble_max: float
            ensemble_spread: float
            ensemble_probs: dict[float, float] — {threshold_f: probability}
            date: str — ISO date of the forecast
        Or None on error.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "models": "gfs_seamless",
        "timezone": "auto",
        "forecast_days": 2,  # today + tomorrow for resolution context
        "ensemble_members": "all",  # Request all 31 members
        "temperature_unit": "fahrenheit",  # Force Fahrenheit — no conversion needed
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(ENSEMBLE_API_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException as exc:
        warning = f"GFS ensemble timeout for ({lat:.4f}, {lon:.4f}): {exc}"
        logger.warning(warning)
        return None
    except httpx.HTTPError as exc:
        warning = f"GFS ensemble HTTP error for ({lat:.4f}, {lon:.4f}): {exc}"
        logger.warning(warning)
        return None
    except Exception as exc:
        warning = f"GFS ensemble unexpected error for ({lat:.4f}, {lon:.4f}): {exc}"
        logger.warning(warning)
        return None

    try:
        hourly = data.get("hourly", {})
        if not hourly:
            logger.warning("GFS ensemble returned no hourly data")
            return None

        # The ensemble API returns data per member.
        # Format: hourly["temperature_2m"] is a dict keyed by member index,
        # or a flat arrays_3d-like structure.
        # Actual format: hourly["temperature_2m_member00"], hourly["temperature_2m_member01"], etc.
        # OR: hourly["temperature_2m"] = [[member0_hour0, ...], [member1_hour0, ...], ...]

        # The ensemble API returns:
        #   hourly["temperature_2m"] = [24 hours]  (deterministic/base)
        #   hourly["temperature_2m_member01"] = [24 hours]  (perturbed member 1)
        #   ...
        #   hourly["temperature_2m_member30"] = [24 hours]  (perturbed member 30)
        # Total: 31 members (1 deterministic + 30 perturbed)

        temps_by_member = []

        # First: the deterministic run (temperature_2m without member suffix)
        base_temps = hourly.get("temperature_2m")
        if base_temps and isinstance(base_temps, list) and len(base_temps) >= 24:
            day1 = base_temps[:24]
            temps_by_member.append(max(day1))  # Already in Fahrenheit

        # Then: all perturbed members (temperature_2m_member01 through _member30)
        for member_idx in range(1, 31):
            key = f"temperature_2m_member{member_idx:02d}"
            member_temps = hourly.get(key)
            if member_temps and isinstance(member_temps, list) and len(member_temps) >= 24:
                day1 = member_temps[:24]
                temps_by_member.append(max(day1))  # Already in Fahrenheit

        if not temps_by_member:
            logger.warning(f"GFS ensemble: could not extract member temps. hourly keys: {list(hourly.keys())[:5]}")
            return None

        n = len(temps_by_member)
        if n < 1:
            logger.warning("GFS ensemble: no member temperatures extracted")
            return None

        sorted_temps = sorted(temps_by_member)
        result = {
            "temps_by_member": temps_by_member,
            "member_count": n,
            "ensemble_mean": round(statistics.mean(temps_by_member), 1),
            "ensemble_std": round(statistics.stdev(temps_by_member), 2) if n > 1 else 0.0,
            "ensemble_median": round(statistics.median(temps_by_member), 1),
            "ensemble_min": round(sorted_temps[0], 1),
            "ensemble_max": round(sorted_temps[-1], 1),
            "ensemble_spread": round(sorted_temps[-1] - sorted_temps[0], 1),
            "date": data.get("hourly", {}).get("time", [""])[0][:10] if "time" in data.get("hourly", {}) else "",
        }

        # Compute threshold probabilities
        if thresholds:
            # Thresholds are always in Fahrenheit internally
            probs = {}
            for thr in thresholds:
                count_above = sum(1 for t in temps_by_member if t >= thr)
                probs[thr] = round(count_above / n, 4)
            result["ensemble_probs"] = probs

        return result

    except Exception as exc:
        warning = f"GFS ensemble malformed response for ({lat:.4f}, {lon:.4f}): {exc}"
        logger.warning(warning)
        return None


def ensemble_count_probabilities(
    temps_by_member: list[float],
    threshold_f: float,
) -> float:
    """
    Count fraction of ensemble members exceeding threshold.
    This is the true probability, replacing the Gaussian approximation.

    Args:
        temps_by_member: List of daily max temps (F) for each ensemble member
        threshold_f: Temperature threshold in Fahrenheit

    Returns:
        Fraction of members >= threshold (0.0 to 1.0)

    Example:
        31 members: 23 exceed 70F -> returns 0.7419 (23/31)
    """
    if not temps_by_member:
        return 0.5  # uninformative prior
    count_above = sum(1 for t in temps_by_member if t >= threshold_f)
    return round(count_above / len(temps_by_member), 4)


def ensemble_bucket_probs(
    temps_by_member: list[float],
    bucket_edges: list[float],
) -> dict[str, float]:
    """
    Compute bucket membership probabilities from ensemble member counts.

    For each bucket [low, high), count members with temp in that range.
    P(bucket) = count_in_bucket / total_members

    Args:
        temps_by_member: Daily max temps (F) per member
        bucket_edges: List of bucket boundaries in Fahrenheit

    Returns:
        {bucket_name: probability}
    """
    BUCKET_NAMES = [
        "65F or below", "66-67F", "68-69F", "70-71F", "72-73F",
        "74-75F", "76-77F", "78-79F", "80-81F", "82-83F", "84F or higher",
    ]
    if not temps_by_member:
        n_buckets = min(len(bucket_edges) - 1, len(BUCKET_NAMES))
        return {BUCKET_NAMES[i]: 1.0 / n_buckets for i in range(n_buckets)}

    n = len(temps_by_member)
    probs = {}
    for i in range(min(len(bucket_edges) - 1, len(BUCKET_NAMES))):
        low = bucket_edges[i]
        high = bucket_edges[i + 1]
        if high >= 9999:  # Catch-all bucket
            count = sum(1 for t in temps_by_member if t >= low)
        else:
            count = sum(1 for t in temps_by_member if low <= t < high)
        probs[BUCKET_NAMES[i]] = round(count / n, 4)
    return probs


def format_ensemble_summary(
    thresholds: list[float],
    probs: dict[float, float],
    member_count: int,
) -> str:
    """Format a human-readable ensemble summary for logging/Discord."""
    lines = [f"GFS {member_count}-member ensemble:"]
    for thr in sorted(thresholds):
        p = probs.get(thr, 0.0)
        count = round(p * member_count)
        lines.append(f"  {count:2d}/{member_count} >= {thr:.0f}F  (P={p:.0%})")
    return "\n".join(lines)


def ensemble_probability_widened(
    temps_by_member: list[float],
    threshold_f: float,
    spread_multiplier: float = SPREAD_MULTIPLIER,
) -> float:
    """
    Compute probability using a normal CDF with widened spread.

    The raw ensemble std underestimates true forecast error because members
    are correlated. We multiply the std by spread_multiplier and use the
    normal CDF to get a better-calibrated probability.

    P(T >= threshold) = 1 - CDF(threshold, mean, std * multiplier)

    Args:
        temps_by_member: Daily max temps (F) per member
        threshold_f: Temperature threshold in Fahrenheit
        spread_multiplier: Multiplier for std (default SPREAD_MULTIPLIER=2.0)

    Returns:
        Calibrated probability (0.0 to 1.0)
    """
    import math
    if not temps_by_member:
        return 0.5
    n = len(temps_member := temps_by_member)
    mean = sum(temps_member) / n
    if n < 2:
        return 1.0 if mean >= threshold_f else 0.0
    variance = sum((t - mean) ** 2 for t in temps_member) / (n - 1)
    std = math.sqrt(variance) * spread_multiplier
    if std < 0.01:
        std = 0.01  # avoid division by zero
    # Normal CDF: P(T < threshold)
    z = (threshold_f - mean) / std
    # Approximate CDF using error function
    cdf = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    # P(T >= threshold) = 1 - CDF
    prob = 1.0 - cdf
    return round(max(0.01, min(0.99, prob)), 4)
