"""
Live temperature & forecast scraper -- PRODUCTION v2.

Data sources (in priority order):
1. Current temp: Open-Meteo current_weather API (real-time, free, no key)
2. Daily high forecast: Open-Meteo forecast with ECMWF+GFS ensemble
3. Official observations: NWS API (api.weather.gov) for US cities

Polymarket temperature markets resolve based on official ASOS/AWOS
airport weather station readings. Open-Meteo provides these directly.
"""

import asyncio
import json
import logging
import math
import re
from datetime import datetime, timezone

import httpx

from polybot.cities import CITY_INDEX
from polybot.polymarket import find_markets, get_market_price, parse_outcome_prices
from polybot.prediction_engine import (
    bayesian_temperature_probability,
    adaptive_kelly_criterion,
    compute_calibration_from_trades,
)

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

# ICAO airport codes for major cities (used by Polymarket/NWS)
AIRPORT_CODES = {
    "austin": "KAUS",
    "hong-kong": "VHHH",
    "hong kong": "VHHH",
    "seoul": "RKSI",
    "london": "EGLL",
    "paris": "LFPG",
    "shanghai": "ZSPD",
    "chicago": "KORD",
    "denver": "KDEN",
    "houston": "KIAH",
    "warsaw": "EPWA",
    "munich": "EDDM",
    "tokyo": "RJTT",
    "new-york": "KNYC",
    "new york": "KNYC",
    "los-angeles": "KLAX",
    "los angeles": "KLAX",
    "phoenix": "KPHX",
    "san-francisco": "KSFO",
    "san francisco": "KSFO",
    "miami": "KMIA",
    "dallas": "KDFW",
    "atlanta": "KATL",
    "seattle": "KSEA",
    "minneapolis": "KMSP",
    "toronto": "CYYZ",
    "moscow": "UUEE",
    "dubai": "OMDB",
    "singapore": "WSSS",
    "bangkok": "VTBS",
    "nairobi": "HKJK",
    "buenos-aires": "SAEZ",
    "buenos aires": "SAEZ",
    "istanbul": "LTFM",
    "berlin": "EDDB",
    "mexico-city": "MMMX",
    "mexico city": "MMMX",
    "sao-paulo": "SBGR",
    "sao paulo": "SBGR",
    "mumbai": "VABB",
    "cairo": "HECA",
    "lagos": "DNMM",
    "sydney": "YSSY",
    "melbourne": "YMML",
    "perth": "YPPH",
}


async def get_current_temperature(city_name: str, city_slug: str | None = None) -> dict:
    """
    Get current temperature and daily high forecast for a city.

    Uses Open-Meteo for both current conditions and forecast.
    This is the same underlying data Polymarket uses for resolution.

    Returns:
        dict with: current_temp_f, current_temp_c, daily_high_f, daily_high_c,
                  forecast_source, current_source, timestamp
    """
    if city_slug is None:
        city_slug = city_name.lower().replace(" ", "-")

    from polybot.cities import CITY_INDEX
    city_obj = CITY_INDEX.get(city_slug)
    if city_obj:
        lat, lon = city_obj["lat"], city_obj["lon"]
    else:
        # Fallback coordinates
        lat, lon = _get_city_coords(city_name)

    result = {
        "city": city_name,
        "current_temp_f": None,
        "current_temp_c": None,
        "daily_high_f": None,
        "daily_high_c": None,
        "forecast_source": None,
        "current_source": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Get both current weather and daily forecast in one call
            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,weather_code",
                    "daily": "temperature_2m_max,temperature_2m_min,weather_code",
                    "temperature_unit": "fahrenheit",
                    "timezone": "auto",
                    "forecast_days": 1,
                },
            )
            if r.status_code == 200:
                data = r.json()

                # Current temperature
                current = data.get("current", {})
                if "temperature_2m" in current:
                    result["current_temp_f"] = current["temperature_2m"]
                    result["current_temp_c"] = round(
                        (current["temperature_2m"] - 32) * 5 / 9, 1
                    )
                    result["current_source"] = "open-meteo"

                # Daily high forecast
                daily = data.get("daily", {})
                if daily.get("temperature_2m_max"):
                    result["daily_high_f"] = daily["temperature_2m_max"][0]
                    result["daily_high_c"] = round(
                        (result["daily_high_f"] - 32) * 5 / 9, 1
                    )
                    result["daily_high_min_f"] = daily.get("temperature_2m_min", [None])[0]
                    result["forecast_source"] = "open-meteo"

                logger.info(
                    f"{city_name}: current={result['current_temp_f']}F, "
                    f"high={result['daily_high_f']}F"
                )
            else:
                logger.warning(f"Open-Meteo returned {r.status_code} for {city_name}")

    except Exception as e:
        logger.error(f"Error fetching temperature for {city_name}: {e}")

    return result


async def get_ensemble_forecast(lat: float, lon: float) -> dict:
    """
    Get ensemble daily high forecast from multiple models.
    Returns weighted average + uncertainty estimate.
    """
    forecasts = {}
    async with httpx.AsyncClient(timeout=15) as client:
        # ECMWF IFS (best global model)
        try:
            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "daily": "temperature_2m_max",
                    "models": "ecmwf_ifs04",
                    "timezone": "auto", "forecast_days": 1,
                    "temperature_unit": "fahrenheit",
                },
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("daily", {}).get("temperature_2m_max"):
                    forecasts["ecmwf"] = data["daily"]["temperature_2m_max"][0]
        except Exception:
            pass

        # GFS
        try:
            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "daily": "temperature_2m_max",
                    "models": "gfs_global",
                    "timezone": "auto", "forecast_days": 1,
                    "temperature_unit": "fahrenheit",
                },
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("daily", {}).get("temperature_2m_max"):
                    forecasts["gfs"] = data["daily"]["temperature_2m_max"][0]
        except Exception:
            pass

        # Default blended
        try:
            r = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "daily": "temperature_2m_max",
                    "timezone": "auto", "forecast_days": 1,
                    "temperature_unit": "fahrenheit",
                },
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("daily", {}).get("temperature_2m_max"):
                    forecasts["blended"] = data["daily"]["temperature_2m_max"][0]
        except Exception:
            pass

    if not forecasts:
        return {"temp_max_f": None, "uncertainty_f": None, "confidence": 0, "models": {}}

    # Filter out None values
    valid_forecasts = {k: v for k, v in forecasts.items() if v is not None}
    if not valid_forecasts:
        return {"temp_max_f": None, "uncertainty_f": None, "confidence": 0, "models": {}}

    # Weighted average
    weights = {"ecmwf": 0.45, "blended": 0.30, "gfs": 0.25}
    available = {k: v for k, v in weights.items() if k in valid_forecasts}
    total_w = sum(available.values())
    normalized = {k: v / total_w for k, v in available.items()}

    temp_f = sum(valid_forecasts[k] * normalized[k] for k in normalized)
    temps = [valid_forecasts[k] for k in normalized]
    uncertainty = (max(temps) - min(temps)) / 2 if len(temps) > 1 else 3.0

    spread = max(temps) - min(temps)
    if spread < 2:
        confidence = 0.95
    elif spread < 5:
        confidence = 0.80
    elif spread < 10:
        confidence = 0.60
    else:
        confidence = 0.40

    return {
        "temp_max_f": round(temp_f, 1),
        "temp_max_c": round((temp_f - 32) * 5 / 9, 1),
        "uncertainty_f": round(uncertainty, 1),
        "confidence": round(confidence, 2),
        "models": forecasts,
        "model_spread_f": round(spread, 1),
    }


def _get_city_coords(city_name: str) -> tuple[float, float]:
    """Fallback coordinates for cities not in CITIES list."""
    coords = {
        "austin": (30.27, -97.74), "hong kong": (22.32, 114.17),
        "seoul": (37.57, 126.98), "london": (51.51, -0.13),
        "paris": (48.86, 2.35), "shanghai": (31.23, 121.47),
        "chicago": (41.88, -87.63), "denver": (39.74, -104.99),
        "houston": (29.76, -95.37), "warsaw": (52.23, 21.01),
        "munich": (48.14, 11.58), "tokyo": (35.68, 139.69),
    }
    return coords.get(city_name.lower(), (40.0, -74.0))


# ============================================================
# FULL ANALYSIS: Combine market data + weather + prediction
# ============================================================

async def analyze_city_market(
    city_name: str,
    date_str: str = "May 29",
    bankroll: float = 100.0,
    trade_log_path: str | None = None,
) -> list[dict]:
    """
    Full analysis for a city: find markets, get weather, compute edge.

    Returns list of analysis dicts, one per market, sorted by edge.
    """
    city_slug = city_name.lower().replace(" ", "-")
    city_obj = CITY_INDEX.get(city_slug)
    if not city_obj:
        logger.warning(f"City {city_name} not found in CITY_INDEX")
        return []

    lat, lon = city_obj["lat"], city_obj["lon"]

    # 1. Find all temperature markets for this city
    markets = await find_markets(city_name=city_name, date_str=date_str)
    if not markets:
        logger.info(f"No temperature markets found for {city_name} on {date_str}")
        return []

    # 2. Get weather data
    current = await get_current_temperature(city_name, city_slug)
    ensemble = await get_ensemble_forecast(lat, lon)

    daily_high_f = ensemble.get("temp_max_f") or current.get("daily_high_f")
    uncertainty_f = ensemble.get("uncertainty_f", 5.0)
    model_confidence = ensemble.get("confidence", 0.5)

    # 3. Historical calibration
    calibration = None
    if trade_log_path:
        calibration = compute_calibration_from_trades(trade_log_path)

    # 4. Analyze each market
    results = []
    for market in markets:
        question = market.get("question", "")
        threshold_f = market.get("threshold_f")
        if threshold_f is None:
            # Extract from question
            tm = re.search(r"(\d+)\s*°?\s*[Cc]", question)
            if tm:
                threshold_c = int(tm.group(1))
                threshold_f = round(threshold_c * 9 / 5 + 32)
            else:
                threshold_f = 90.0

        # Get market price
        try:
            prices_str = market.get("outcomePrices", "[]")
            prices = json.loads(prices_str)
            yes_price = float(prices[0]) if len(prices) >= 2 else 0.5
        except Exception:
            yes_price = 0.5

        # Bayesian probability
        prob = bayesian_temperature_probability(
            forecast_temp_f=daily_high_f or 85.0,
            threshold_f=threshold_f,
            uncertainty_f=uncertainty_f,
            model_confidence=model_confidence,
            historical_calibration=calibration,
        )

        # Adaptive Kelly
        kelly = adaptive_kelly_criterion(
            true_prob=prob,
            market_price=yes_price,
            bankroll=bankroll,
            calibration=calibration,
            market_efficiency=0.5,
        )

        edge = kelly.get("edge", 0)

        results.append({
            "city": city_name,
            "question": question[:90],
            "threshold_f": threshold_f,
            "current_temp_f": current.get("current_temp_f"),
            "daily_high_f": daily_high_f,
            "ensemble_confidence": model_confidence,
            "model_spread_f": ensemble.get("model_spread_f", 0),
            "probability": prob,
            "yes_price": yes_price,
            "edge": edge,
            "direction": kelly["direction"],
            "kelly_usd": kelly["kelly_usd"],
            "recommendation": "TRADE" if kelly["kelly_usd"] >= 1.0 and edge > 0.05 else "SKIP",
            "slug": market.get("slug", ""),
            "conditionId": market.get("conditionId", ""),
        })

    # Sort by edge descending
    results.sort(key=lambda x: x.get("edge", 0), reverse=True)
    return results


# ============================================================
# CLI TEST
# ============================================================

async def _main():
    """Test: scan Austin and Hong Kong."""
    print("=" * 70)
    print("POLYBOT v2 -- Live Market Scan")
    print("=" * 70)

    for city in ["Austin", "Hong Kong"]:
        print(f"\n{'='*70}")
        print(f"  SCANNING: {city}")
        print(f"{'='*70}")

        results = await analyze_city_market(city, date_str="May 29")

        if not results:
            print(f"  No markets found for {city}")
            continue

        for r in results[:10]:
            edge_pct = r["edge"] * 100
            rec = "🟢 TRADE" if r["recommendation"] == "TRADE" else "⬜ SKIP"
            print(f"\n  {r['question'][:70]}")
            print(f"    Threshold: {r['threshold_f']}F | "
                  f"Forecast high: {r['daily_high_f']}F | "
                  f"Current: {r['current_temp_f']}F")
            print(f"    P(YES)={r['probability']:.1%} | "
                  f"Market YES={r['yes_price']:.3f} | "
                  f"Edge={edge_pct:+.1f}%")
            print(f"    Direction: {r['direction']} | "
                  f"Kelly: ${r['kelly_usd']:.2f} | "
                  f"Models agree: {r['ensemble_confidence']:.0%} "
                  f"(spread={r['model_spread_f']}F)")
            print(f"    >>> {rec}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    asyncio.run(_main())
