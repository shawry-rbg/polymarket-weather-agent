"""Multi-model ensemble weather forecast aggregator.

Combines temperature predictions from three independent sources:
  - ECMWF (via Open-Meteo ecmwf_ifs04 model) -- weight 40%
  - Weatherstack.com API -- weight 35%
  - Open-Meteo default blended forecast -- weight 25%

Each source carries a base confidence score.  The ensemble computes a
weighted-average temperature, cross-model standard deviation, discrete
agreement tiers, and a consensus flag used downstream by the prediction
engine to decide whether to abstain from trading.

All three fetches run concurrently (asyncio.gather).  Missing or failed
sources are skipped and the remaining weights are renormalised.

Sources
-------
* ECMWF: https://open-meteo.com/en/docs/ecmwf-api
* Weatherstack: https://weatherstack.com/documentation
* Open-Meteo blended: https://open-meteo.com/en/docs

Usage
-----
    forecast = await get_ensemble_forecast(
        lat=30.2672, lon=-97.7431, city_name="Austin",
        weatherstack_key="YOUR_KEY",
    )
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
WEATHERSTACK_URL = "http://api.weatherstack.com/current"
REQUEST_TIMEOUT = 10  # seconds

# Ensemble weighting scheme (must sum to 1.0 before renormalisation)
WEIGHTS: Dict[str, float] = {
    "ecmwf": 0.40,
    "weatherstack": 0.35,
    "openmeteo": 0.25,
}


# ---------------------------------------------------------------------------
# Individual source fetchers
# ---------------------------------------------------------------------------


async def fetch_ecmwf(lat: float, lon: float) -> Optional[dict]:
    """Fetch the ECMWF-IFS04 max-temperature forecast from Open-Meteo.

    Parameters
    ----------
    lat, lon : float
        Decimal coordinates.

    Returns
    -------
    dict | None
        ``{temp_f, temp_c, date, model, confidence}`` or *None* on error.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "models": "ecmwf_ifs04",
        "timezone": "auto",
        "forecast_days": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("ECMWF fetch error for (%.4f, %.4f): %s", lat, lon, exc)
        return None
    except Exception as exc:
        logger.warning("ECMWF unexpected error for (%.4f, %.4f): %s", lat, lon, exc)
        return None

    try:
        daily = data["daily"]
        raw_val = daily["temperature_2m_max"][0]
        if raw_val is None:
            logger.debug("ECMWF null value for (%.4f, %.4f) — skipping", lat, lon)
            return None
        temp_c = float(raw_val)
        date = daily["time"][0]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("ECMWF malformed response for (%.4f, %.4f): %s", lat, lon, exc)
        return None

    return {
        "temp_f": round(temp_c * 9 / 5 + 32, 1),
        "temp_c": round(temp_c, 1),
        "date": date,
        "model": "ecmwf",
        "confidence": 0.90,
    }


async def _sleep_zero() -> None:
    """Yield control back to the event-loop (kept for symmetry / tests)."""
    await asyncio.sleep(0)


async def fetch_weatherstack(
    lat: float, lon: float, api_key: str = ""
) -> Optional[dict]:
    """Fetch the current-day high from Weatherstack.

    Requires a valid API key.  When *api_key* is empty or the service
    returns an error, *None* is returned gracefully.

    Parameters
    ----------
    lat, lon : float
        Decimal coordinates.
    api_key : str
        Weatherstack access key.

    Returns
    -------
    dict | None
        ``{temp_f, temp_c, date, model, confidence}`` or *None*.
    """
    if not api_key:
        logger.debug("Weatherstack API key not provided -- skipping")
        return None

    query = f"{lat},{lon}"
    params = {
        "access_key": api_key,
        "query": query,
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(WEATHERSTACK_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("Weatherstack HTTP error for (%s): %s", query, exc)
        return None
    except Exception as exc:
        logger.warning("Weatherstack unexpected error for (%s): %s", query, exc)
        return None

    # Weatherstack returns {"success": false, "error": {...}} on failures.
    if not data.get("success", True):
        code = data.get("error", {}).get("code", "unknown")
        info = data.get("error", {}).get("info", "")
        logger.warning("Weatherstack API error %s: %s", code, info)
        return None

    try:
        current = data["current"]
        temp_c = float(current["temperature"])
        # Weatherstack provides local-time as ISO-8601 date part.
        local_time = current.get("observation_time", "")
        # observation_time looks like "03:15 PM" -- fall back to today.
        from datetime import date as _date

        today = _date.today().isoformat()
        # Some /paid plans include request.localtime ("2025-01-15 15:15")
        date_str = data.get("request", {}).get("localtime", "")[:10] or today
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Weatherstack malformed response for (%s): %s", query, exc)
        return None

    return {
        "temp_f": round(temp_c * 9 / 5 + 32, 1),
        "temp_c": round(temp_c, 1),
        "date": date_str,
        "model": "weatherstack",
        "confidence": 0.85,
    }


async def fetch_openmeteo_default(lat: float, lon: float) -> Optional[dict]:
    """Fetch the default Open-Meteo blended forecast.

    Uses Open-Meteo's own model-blending (no explicit model parameter).

    Parameters
    ----------
    lat, lon : float
        Decimal coordinates.

    Returns
    -------
    dict | None
        ``{temp_f, temp_c, date, model, confidence}`` or *None*.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "timezone": "auto",
        "forecast_days": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("Open-Meteo fetch error for (%.4f, %.4f): %s", lat, lon, exc)
        return None
    except Exception as exc:
        logger.warning(
            "Open-Meteo unexpected error for (%.4f, %.4f): %s", lat, lon, exc
        )
        return None

    try:
        daily = data["daily"]
        temp_c = float(daily["temperature_2m_max"][0])
        date = daily["time"][0]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning(
            "Open-Meteo malformed response for (%.4f, %.4f): %s", lat, lon, exc
        )
        return None

    return {
        "temp_f": round(temp_c * 9 / 5 + 32, 1),
        "temp_c": round(temp_c, 1),
        "date": date,
        "model": "openmeteo",
        "confidence": 0.80,
    }


# ---------------------------------------------------------------------------
# Ensemble combiner
# ---------------------------------------------------------------------------


def _renormalize_weights(
    available: List[str],
) -> Dict[str, float]:
    """Return a new weight dict restricted to *available* model names.

    Weights are scaled proportionally so they sum to 1.0.
    """
    raw = {m: WEIGHTS[m] for m in available}
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {m: w / total for m, w in raw.items()}


def _agreement_tier(model_temps: List[float]) -> tuple:
    """Classify multi-model agreement.

    Returns
    -------
    (confidence: float, abort: bool)

    agreement tier
    -------------
    * All within 2 F  -> high confidence  (0.95), no abort
    * All within 5 F  -> medium confidence (0.75), no abort
    * Otherwise        -> low confidence    (0.50), abort recommendation
    """
    if len(model_temps) < 2:
        return (1.0, False)  # single source -- no spread to evaluate

    spread = max(model_temps) - min(model_temps)
    if spread <= 2.0:
        return (0.95, False)
    if spread <= 5.0:
        return (0.75, False)
    return (0.50, True)


async def get_ensemble_forecast(
    lat: float,
    lon: float,
    city_name: str = "",
    weatherstack_key: str = "",
) -> dict:
    """Produce a multi-model ensemble temperature forecast.

    Fetches all three sources concurrently, combines them using the
    configured weighting scheme, and computes cross-model statistics.

    Parameters
    ----------
    lat, lon : float
        Decimal coordinates.
    city_name : str
        Human-readable label (included in the returned dict).
    weatherstack_key : str
        Optional Weatherstack API key.

    Returns
    -------
    dict
        {
            "city": str,
            "lat": float,
            "lon": float,
            "date": str,             # iso date from first successful source
            "models": list[dict],    # individual model results
            "models_used": int,
            "ensemble_temp_f": float,
            "ensemble_temp_c": float,
            "weights": dict,          # {model_name: normalised_weight}
            "uncertainty_f": float,  # std-dev across model temps
            "model_spread_f": float,  # max - min
            "agreement": str,         # "high" | "medium" | "low"
            "agreement_confidence": float,
            "consensus": bool,        # False when spread > 5 F
            "abort_probability": bool,
            "ensemble_confidence": float,  # weighted avg of source confidences
        }
    """
    label = city_name or f"({lat}, {lon})"

    # ---- concurrent fetch ---------------------------------------------------
    results = await asyncio.gather(
        fetch_ecmwf(lat, lon),
        fetch_weatherstack(lat, lon, api_key=weatherstack_key),
        fetch_openmeteo_default(lat, lon),
        return_exceptions=True,
    )

    models: List[dict] = []
    for item in results:
        if isinstance(item, BaseException):
            logger.warning("Ensemble source raised: %s", item)
            continue
        if item is None:
            continue
        models.append(item)

    if not models:
        logger.error("All ensemble sources failed for %s", label)
        return {
            "city": label,
            "lat": lat,
            "lon": lon,
            "date": "",
            "models": [],
            "models_used": 0,
            "ensemble_temp_f": None,
            "ensemble_temp_c": None,
            "weights": {},
            "uncertainty_f": None,
            "model_spread_f": None,
            "agreement": "none",
            "agreement_confidence": 0.0,
            "consensus": False,
            "abort_probability": True,
            "ensemble_confidence": 0.0,
        }

    # ---- weight renormalisation ---------------------------------------------
    available_names = [m["model"] for m in models]

    # Try DEB weights first, fall back to static weights
    deb_weights = {}
    try:
        from polybot.deb_weights import get_weighted_ensemble_temp, get_deb_weights
        # Get local hour for the city (use 12 as default if unknown)
        city_label = label.lower().replace(" ", "_")
        try:
            from polybot.agents.time_agent import local_hour
            from polybot.cities import CITY_INDEX
            city_config = CITY_INDEX.get(city_label, {})
            tz_name = city_config.get("timezone", "UTC")
            import pytz, datetime
            tz = pytz.timezone(tz_name)
            lh = datetime.datetime.now(tz).hour
        except Exception:
            lh = 12

        deb_weights = get_deb_weights(city_label, lh, available_names)
        if deb_weights:
            weights = deb_weights
            logger.debug("[ENSEMBLE] Using DEB weights for %s: %s", label, weights)
    except Exception as e:
        logger.debug("[ENSEMBLE] DEB weights unavailable: %s", e)
        deb_weights = {}

    if not deb_weights:
        weights = _renormalize_weights(available_names)

    # ---- weighted average (DEB or static) -----------------------------------
    weighted_f = sum(m["temp_f"] * weights[m["model"]] for m in models)
    weighted_c = round((weighted_f - 32) * 5 / 9, 1)
    weighted_f = round(weighted_f, 1)

    # ---- uncertainty (population std-dev across model predictions) ----------
    temps_f = [m["temp_f"] for m in models]
    mean_f = sum(temps_f) / len(temps_f)
    variance = sum((t - mean_f) ** 2 for t in temps_f) / len(temps_f)
    uncertainty_f = round(math.sqrt(variance), 2)

    model_spread_f = round(max(temps_f) - min(temps_f), 1)

    # ---- agreement tier ------------------------------------------------------
    agreement_conf, abort = _agreement_tier(temps_f)
    if model_spread_f <= 2.0:
        agreement = "high"
    elif model_spread_f <= 5.0:
        agreement = "medium"
    else:
        agreement = "low"

    consensus = not abort  # spread > 5 F triggers no-consensus

    # ---- weighted source-confidence ------------------------------------------
    ensemble_confidence = round(
        sum(m["confidence"] * weights[m["model"]] for m in models), 3
    )

    # ---- date (first available) ----------------------------------------------
    date = models[0]["date"]

    logger.info(
        "Ensemble [%s]: %.1f F  (%d models, spread %.1f F, %s)",
        label,
        weighted_f,
        len(models),
        model_spread_f,
        agreement,
    )

    return {
        "city": label,
        "lat": lat,
        "lon": lon,
        "date": date,
        "models": models,
        "models_used": len(models),
        "ensemble_temp_f": weighted_f,
        "ensemble_temp_c": weighted_c,
        "weights": weights,
        "uncertainty_f": uncertainty_f,
        "model_spread_f": model_spread_f,
        "agreement": agreement,
        "agreement_confidence": agreement_conf,
        "consensus": consensus,
        "abort_probability": abort,
        "ensemble_confidence": ensemble_confidence,
    }
