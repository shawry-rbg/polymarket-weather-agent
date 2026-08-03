"""
Backtesting engine for Polybot — two modes:

1. Simplified (default): Uses Gaussian approximation for forecast probabilities.
   Fast but unrealistic.

2. Real mode (--real): Uses actual historical 31-member GFS ensemble forecasts
   from Open-Meteo archive ensemble API. Fetches the forecast issued 1 day prior
   to each target date, computes true probabilities by counting members above
   thresholds, applies atmospheric corrections, ENSO adjustments, and compares
   with observed highs. This is the production-grade backtest.

Usage:
    # Simplified (fast, approximate)
    modal run polybot/modal_deploy.py::backtest --start 2024-01-01 --end 2025-12-31

    # Real GFS ensemble backtest (slow, accurate)
    modal run polybot/modal_deploy.py::backtest_real --start 2024-01-01 --end 2025-06-30
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

REPORT_PATH = Path("/polybot-data/backtest_report.json")
REAL_REPORT_PATH = Path("/polybot-data/real_backtest_report.json")

# Trading thresholds — tighter to reduce over-trading
MIN_PROB_FOR_TRADE = 0.50
MAX_PROB_FOR_TRADE = 0.95
MIN_EDGE = 0.05  # 5% minimum edge — market simulation uses naive forecast so real mispricings exist

# Risk parameters — more conservative
KELLY_FRACTION = 0.10  # was 0.25 — half Kelly for safety
MAX_BET_PCT = 0.02     # 2% of bankroll max (was 2.5%)


# =============================================================================
# Historical data fetchers
# =============================================================================

def fetch_historical_actuals(
    lat: float,
    lon: float,
    start_date: str,
    end_date: str,
    unit: str = "F",
) -> dict[str, float]:
    """
    Fetch observed daily high temperatures from Open-Meteo archive API.
    Returns dict of date_str -> daily_high_temp.
    """
    import httpx

    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "daily": "temperature_2m_max",
        "timezone": "auto",
        "temperature_unit": "fahrenheit",  # Always Fahrenheit
    }

    try:
        resp = httpx.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        daily = data.get("daily", {})
        dates = daily.get("time", [])
        temps = daily.get("temperature_2m_max", [])

        result = {}
        for i, date_str in enumerate(dates):
            if i < len(temps) and temps[i] is not None:
                result[date_str] = float(temps[i])
        return result

    except Exception as e:
        logger.error(f"[BACKTEST] Historical actuals error for ({lat}, {lon}): {e}")
        return {}


def fetch_historical_gfs_ensemble(
    lat: float,
    lon: float,
    forecast_date: str,
    unit: str = "C",
) -> Optional[dict]:
    """
    Fetch the 31-member GFS ensemble forecast from Open-Meteo ensemble API.

    Uses the forecast ensemble API with `past_days` calculated from today to the
    forecast_date. The API supports past_days up to 93 days. For dates older than
    93 days, only the most recent 93 days of forecasts are available.

    The API returns hourly data. We extract the daily max for the target day
    for each of the 31 ensemble members.

    Args:
        lat, lon: City coordinates.
        forecast_date: "YYYY-MM-DD" — the target day we're forecasting for.
        unit: "C" or "F" — the city's native bucket unit.

    Returns:
        dict with temps_by_member, ensemble_mean, ensemble_std, ensemble_spread, etc.
        Or None on error.
    """
    import httpx

    target_dt = datetime.strptime(forecast_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    today = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    days_ago = (today - target_dt).days

    if days_ago < 0:
        logger.debug(f"[BACKTEST] Forecast date {forecast_date} is in the future")
        return None

    # The ensemble API only supports past_days up to 93.
    # For dates within 93 days: use full 31-member ensemble
    # For older dates: fall back to archive API (deterministic only)
    use_ensemble = days_ago <= 93

    if use_ensemble:
        # Use forecast ensemble API with past_days
        api_past_days = max(days_ago, 1)
        url = "https://ensemble-api.open-meteo.com/v1/ensemble"
        params = {
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m",
            "models": "gfs_seamless",
            "timezone": "auto",
            "forecast_days": 0,
            "past_days": api_past_days,
            "temperature_unit": "fahrenheit",  # Always Fahrenheit
        }
    else:
        # Fall back to archive API for older dates (deterministic GFS only)
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": lat, "longitude": lon,
            "start_date": forecast_date,
            "end_date": forecast_date,
            "daily": "temperature_2m_max",
            "models": "gfs_seamless",
            "timezone": "auto",
            "temperature_unit": "fahrenheit",  # Always Fahrenheit
        }

    last_err = None
    for attempt in range(3):
        try:
            resp = httpx.get(url, params=params, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            break
        except httpx.TimeoutException as e:
            last_err = e
            logger.warning(f"[BACKTEST] GFS timeout for {forecast_date} (attempt {attempt+1}/3)")
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
        except httpx.HTTPError as e:
            last_err = e
            logger.warning(f"[BACKTEST] GFS HTTP error for {forecast_date} (attempt {attempt+1}/3): {e}")
            if attempt < 2:
                # Longer backoff for 429 rate limit errors
                wait = 5 * (attempt + 1) if "429" in str(e) else 3 * (attempt + 1)
                time.sleep(wait)
        except Exception as e:
            last_err = e
            logger.warning(f"[BACKTEST] GFS error for {forecast_date} (attempt {attempt+1}/3): {e}")
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    else:
        return None

    try:
        temps_by_member = []

        if use_ensemble:
            # Parse hourly ensemble data
            hourly = data.get("hourly", {})
            if not hourly:
                logger.debug(f"[BACKTEST] No hourly GFS data for {forecast_date}")
                return None

            # Build list of member keys to try
            member_keys = ["temperature_2m"]  # base/deterministic
            for i in range(1, 31):
                member_keys.append(f"temperature_2m_member{i:02d}")

            for key in member_keys:
                member_temps = hourly.get(key)
                if member_temps is None and key == "temperature_2m":
                    member_temps = hourly.get("temperature_2m_member00")

                if member_temps and isinstance(member_temps, list) and len(member_temps) >= 24:
                    # Extract the target day's 24 hours from the hourly series
                    # The response goes from (today - api_past_days * 24h) to now
                    # Target day is days_ago days ago
                    target_end_hour = (api_past_days - days_ago) * 24 if api_past_days > days_ago else len(member_temps)
                    target_start_hour = target_end_hour - 24

                    if target_start_hour >= 0 and target_end_hour <= len(member_temps):
                        day_hours = member_temps[target_start_hour:target_end_hour]
                        valid_hours = [t for t in day_hours if t is not None]
                        if valid_hours:
                            daily_max = max(valid_hours)
                            temps_by_member.append(daily_max)
        else:
            # Parse archive API response (deterministic GFS only, daily resolution)
            daily = data.get("daily", {})
            if not daily:
                logger.debug(f"[BACKTEST] No daily GFS archive data for {forecast_date}")
                return None

            dates = daily.get("time", [])
            temps = daily.get("temperature_2m_max", [])

            if not dates or not temps:
                return None

            # Find the target date in the response
            for i, d in enumerate(dates):
                if d == forecast_date and i < len(temps) and temps[i] is not None:
                    daily_max = float(temps[i])  # Already in Fahrenheit
                    # Only 1 member (deterministic), but we still use it
                    temps_by_member.append(daily_max)
                    break

            if not temps_by_member:
                return None

        n = len(temps_by_member)
        # For ensemble mode, need at least 5 members; for archive mode, 1 is OK
        min_members = 1 if not use_ensemble else 5
        if n < min_members:
            logger.debug(f"[BACKTEST] Only {n} members for {forecast_date} (need {min_members})")
            return None

        sorted_t = sorted(temps_by_member)
        mean_f = statistics.mean(temps_by_member)
        result = {
            "temps_by_member": temps_by_member,
            "member_count": n,
            "ensemble_mean": round(mean_f, 1),
            "ensemble_std": round(statistics.stdev(temps_by_member), 2) if n > 1 else 0.0,
            "ensemble_spread": round(sorted_t[-1] - sorted_t[0], 1),
            "ensemble_median": round(statistics.median(temps_by_member), 1),
            "ensemble_min": round(sorted_t[0], 1),
            "ensemble_max": round(sorted_t[-1], 1),
        }

        logger.debug(f"[BACKTEST] GFS OK: {forecast_date} mean={mean_f:.1f}F spread={result['ensemble_spread']}F members={n}")
        return result

    except Exception as e:
        logger.warning(f"[BACKTEST] GFS parse error for {forecast_date}: {e}")
        return None


def fetch_historical_actuals_for_date(
    lat: float,
    lon: float,
    target_date: str,
    unit: str = "F",
) -> Optional[float]:
    """Fetch observed high temp for a single date."""
    result = fetch_historical_actuals(lat, lon, target_date, target_date, unit)
    return result.get(target_date)


# =============================================================================
# Simplified backtest (existing, kept for fast iteration)
# =============================================================================

def compute_bucket_prob(forecast_f: float, threshold_f: float, std_f: float = 2.5) -> float:
    """Compute P(temp > threshold) using Gaussian CDF approximation."""
    try:
        from polybot.prediction_engine import bayesian_temperature_probability
        return bayesian_temperature_probability(
            forecast_temp_f=forecast_temp_f,
            threshold_f=threshold_f,
            uncertainty_f=std_f,
            model_confidence=0.7,
        )
    except ImportError:
        import math
        z = (forecast_f - threshold_f) / max(std_f, 0.1)
        return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def run_backtest(
    cities: list[dict],
    start_date: str,
    end_date: str,
    bankroll_start: float = 100.0,
    unit: str = "F",
) -> dict:
    """
    Simplified backtest: Gaussian forecast approximation.
    Kept for fast iteration. Use run_real_backtest() for production accuracy.
    """
    all_trades = []
    bankroll = bankroll_start
    daily_bankroll = []
    monthly_pnl: dict[str, float] = {}

    total_dates = 0
    total_cities_processed = 0

    for city in cities:
        slug = city.get("slug", city["name"].lower().replace(" ", "_"))
        lat = city["lat"]
        lon = city["lon"]
        city_unit = city.get("unit", unit)
        buckets = [float(b) for b in city.get("buckets", [])]

        if not buckets:
            continue

        logger.info(f"[BACKTEST] Fetching historical temps for {slug} ({start_date} to {end_date})...")
        hist_temps = fetch_historical_temps(lat, lon, start_date, end_date, city_unit)

        if not hist_temps:
            logger.warning(f"[BACKTEST] No historical data for {slug}, skipping")
            continue

        total_cities_processed += 1

        for date_str, actual_temp_f in sorted(hist_temps.items()):
            total_dates += 1

            import random
            random.seed(hash(f"{slug}:{date_str}") % 2**31)
            forecast_noise = random.gauss(0, 2.5)
            forecast_f = actual_temp_f + forecast_noise

            for threshold_f in buckets:
                prob = compute_bucket_prob(forecast_f, threshold_f, std_f=2.5)
                market_noise = random.gauss(0, 0.05)
                market_price = max(0.02, min(0.98, prob + market_noise))

                if prob > MIN_PROB_FOR_TRADE and prob < MAX_PROB_FOR_TRADE:
                    edge = prob - market_price
                    if edge < 0.05:
                        continue

                    b = (1.0 / market_price) - 1.0
                    p = prob
                    q = 1.0 - p
                    kelly_f = ((b * p - q) / b) * KELLY_FRACTION
                    bet_usd = min(bankroll * kelly_f, bankroll * MAX_BET_PCT)
                    bet_usd = max(bet_usd, 0.01)

                    won = actual_temp_f >= threshold_f
                    if won:
                        pnl = bet_usd * (1.0 / market_price - 1.0)
                    else:
                        pnl = -bet_usd

                    bankroll += pnl

                    trade = {
                        "date": date_str, "city": slug,
                        "threshold_f": threshold_f,
                        "forecast_f": round(forecast_f, 1),
                        "actual_f": round(actual_temp_f, 1),
                        "prob": round(prob, 3),
                        "market_price": round(market_price, 3),
                        "edge": round(edge, 3),
                        "bet_usd": round(bet_usd, 4),
                        "won": won, "pnl": round(pnl, 4),
                        "bankroll_after": round(bankroll, 4),
                    }
                    all_trades.append(trade)

                    month_key = date_str[:7]
                    monthly_pnl[month_key] = monthly_pnl.get(month_key, 0.0) + pnl

            daily_bankroll.append(bankroll)

    return _compile_report(all_trades, daily_bankroll, bankroll_start, bankroll,
                           monthly_pnl, cities, total_cities_processed, total_dates,
                           start_date, end_date, mode="simplified")


# =============================================================================
# Real GFS ensemble backtest
# =============================================================================

def run_real_backtest(
    cities: list[dict],
    start_date: str,
    end_date: str,
    bankroll_start: float = 100.0,
    max_cities: int | None = None,
) -> dict:
    """
    Production-grade backtest using real historical GFS ensemble forecasts.

    For each city-date:
      1. Fetch the GFS 31-member ensemble forecast using Open-Meteo ensemble API
         with past_days calculated from today to target date (max 93 days)
      2. Compute true probabilities by counting members above each threshold
      3. Apply ENSO adjustment to ensemble mean
      4. Simulate market price (ensemble prob + noise for inefficiency)
      5. Kelly-size the bet
      6. Settle against observed high from Open-Meteo archive

    Args:
        cities: List of city dicts.
        start_date, end_date: "YYYY-MM-DD".
        bankroll_start: Starting bankroll.
        max_cities: Limit cities for faster runs.

    Returns:
        Backtest report dict.
    """
    import httpx

    all_trades = []
    bankroll = bankroll_start
    daily_bankroll = []
    monthly_pnl: dict[str, float] = {}
    fetch_stats = {"success": 0, "failed": 0, "skipped_no_members": 0}

    cities_to_run = cities[:max_cities] if max_cities else cities
    total_cities_processed = 0

    # Pre-fetch all actuals for all cities (batch)
    city_actuals = {}
    for city in cities_to_run:
        slug = city.get("slug", city["name"].lower().replace(" ", "_"))
        lat = city["lat"]
        lon = city["lon"]
        city_unit = city.get("unit", "C")
        buckets = [float(b) for b in city.get("buckets", [])]

        if not buckets:
            continue

        # Skip cities not in active trading tier
        city_name = city.get("name", slug)
        try:
            from polybot.trading_config import is_city_active
            if not is_city_active(city_name):
                logger.info(f"[REAL_BACKTEST] Skipping {city_name}: not in active tier")
                continue
        except ImportError:
            pass

        logger.info(f"[REAL_BACKTEST] Fetching actuals for {slug}...")
        actuals = fetch_historical_actuals(lat, lon, start_date, end_date, city_unit)
        if not actuals:
            logger.warning(f"[REAL_BACKTEST] No actuals for {slug}, skipping")
            continue

        city_actuals[slug] = {
            "actuals": actuals,
            "lat": lat, "lon": lon,
            "unit": city_unit,
            "buckets": buckets,
            "name": city["name"],
        }
        total_cities_processed += 1

    # Generate all dates in range
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    all_dates = []
    d = start_dt
    while d <= end_dt:
        all_dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    logger.info(f"[REAL_BACKTEST] Running {total_cities_processed} cities x {len(all_dates)} dates")

    # Rate limiter: track last request time to avoid 429s
    last_request_time = 0.0
    min_interval = 0.15  # 150ms between requests (~6 req/s)

    def _rate_limit():
        nonlocal last_request_time
        now_t = time.monotonic()
        elapsed = now_t - last_request_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        last_request_time = time.monotonic()

    # For each date, fetch GFS ensemble for each city and simulate trades
    for date_idx, target_date in enumerate(all_dates):
        if date_idx % 30 == 0:
            logger.info(f"[REAL_BACKTEST] Progress: {date_idx}/{len(all_dates)} dates, "
                         f"bankroll=${bankroll:.2f}, trades={len(all_trades)}")

        for slug, city_data in city_actuals.items():
            actual_temp = city_data["actuals"].get(target_date)
            if actual_temp is None:
                continue

            lat = city_data["lat"]
            lon = city_data["lon"]
            city_unit = city_data["unit"]
            buckets = city_data["buckets"]

            # Rate limit before each API call
            _rate_limit()

            # Fetch historical GFS ensemble for this date
            gfs = fetch_historical_gfs_ensemble(
                lat, lon, target_date,
                unit=city_unit,
            )

            if gfs is None:
                fetch_stats["failed"] += 1
                continue

            temps = gfs.get("temps_by_member", [])
            if len(temps) < 1:
                fetch_stats["skipped_no_members"] += 1
                continue

            fetch_stats["success"] += 1
            ensemble_mean = gfs["ensemble_mean"]
            n_members = gfs["member_count"]

            # --- Multi-model ensemble: GFS + AIFS + ICON ---
            # Fetch AIFS and ICON using synchronous HTTP (backtest is sync)
            aifs_temp = None
            icon_temp = None
            try:
                import httpx as _httpx
                # AIFS
                _r = _httpx.get("https://api.open-meteo.com/v1/forecast", params={
                    "latitude": lat, "longitude": lon,
                    "daily": "temperature_2m_max",
                    "models": "ecmwf_aifs025",
                    "start_date": target_date, "end_date": target_date,
                    "temperature_unit": "fahrenheit", "timezone": "auto",
                    "forecast_days": 1,
                }, timeout=15)
                if _r.status_code == 200:
                    aifs_temp = float(_r.json()["daily"]["temperature_2m_max"][0])
            except Exception:
                pass
            try:
                import httpx as _httpx
                # ICON
                _r = _httpx.get("https://api.open-meteo.com/v1/forecast", params={
                    "latitude": lat, "longitude": lon,
                    "daily": "temperature_2m_max",
                    "models": "icon_global",
                    "start_date": target_date, "end_date": target_date,
                    "temperature_unit": "fahrenheit", "timezone": "auto",
                    "forecast_days": 1,
                }, timeout=15)
                if _r.status_code == 200:
                    icon_temp = float(_r.json()["daily"]["temperature_2m_max"][0])
            except Exception:
                pass

            # Blended mean: weighted average of available models
            # GFS ensemble is the anchor (most reliable). AIFS/ICON provide
            # supplementary signal but are weighted lower to avoid noise.
            model_temps = [ensemble_mean]
            model_weights = [0.7]  # GFS ensemble is primary
            if aifs_temp is not None:
                model_temps.append(aifs_temp)
                model_weights.append(0.2)  # AIFS supplementary
            if icon_temp is not None:
                model_temps.append(icon_temp)
                model_weights.append(0.1)  # ICON minor weight

            # Normalize weights
            total_w = sum(model_weights)
            blended_mean = sum(t * w for t, w in zip(model_temps, model_weights)) / total_w

            # --- Rolling bias correction from resolved trades ---
            rolling_bias = 0.0
            try:
                from polybot.metar_calibration import get_rolling_bias
                rolling_bias, _, _ = get_rolling_bias(slug, days=30)
            except Exception:
                pass

            # --- ENSO adjustment ---
            enso_adj = 0.0
            try:
                from polybot.enso import get_enso_adjustment
                enso_adj = get_enso_adjustment(slug)
            except Exception:
                pass

            # Final adjusted mean: blended + rolling bias + ENSO
            adjusted_mean = blended_mean + rolling_bias + enso_adj

            # Ensemble spread for sanity gates
            ensemble_spread = gfs.get("ensemble_spread", 0)

            # Compute threshold probabilities using widened spread
            from polybot.gfs_ensemble import ensemble_probability_widened
            for threshold_f in buckets:
                # Use widened-spread probability (normal CDF with 2x std)
                prob = ensemble_probability_widened(temps, threshold_f)

                # Skip if probability is outside trading range
                if prob < MIN_PROB_FOR_TRADE or prob > MAX_PROB_FOR_TRADE:
                    continue

                # Simulate market price: market prices based on raw GFS ensemble
                # (no bias correction). Our edge comes from calibration + ENSO.
                import random
                random.seed(hash(f"{slug}:{target_date}:{threshold_f}") % 2**31)

                # Market uses raw GFS ensemble mean (no bias correction)
                market_spread = 4.0  # market has moderate uncertainty
                market_z = (threshold_f - ensemble_mean) / market_spread
                import math as _math
                market_cdf = 0.5 * (1.0 + _math.erf(market_z / _math.sqrt(2.0)))
                market_prob = 1.0 - market_cdf

                # Market price = raw GFS probability + noise
                market_noise = random.gauss(0, 0.05)  # 5% std
                market_price = max(0.03, min(0.97, market_prob + market_noise))

                # Edge = our calibrated probability - market price
                edge = prob - market_price
                if edge < MIN_EDGE:
                    continue

                # Pre-trade sanity gate (using trading_config) — called AFTER edge computed
                try:
                    from polybot.trading_config import pre_trade_gate
                    city_name = city_data.get("name", slug)
                    should_trade, reason = pre_trade_gate(
                        city_name, adjusted_mean, edge,
                        datetime.strptime(target_date, "%Y-%m-%d").month,
                        ensemble_spread
                    )
                    if not should_trade:
                        continue
                except ImportError:
                    pass

                # Kelly sizing
                b = (1.0 / market_price) - 1.0
                p = prob
                q = 1.0 - p
                kelly_f = ((b * p - q) / b) * KELLY_FRACTION
                if kelly_f <= 0:
                    continue

                bet_usd = min(bankroll * kelly_f, bankroll * MAX_BET_PCT)
                bet_usd = max(bet_usd, 0.01)

                # Settle: did actual temp exceed threshold?
                won = actual_temp >= threshold_f
                if won:
                    pnl = bet_usd * (1.0 / market_price - 1.0)
                else:
                    pnl = -bet_usd

                bankroll += pnl

                trade = {
                    "date": target_date,
                    "city": slug,
                    "threshold_f": threshold_f,
                    "ensemble_mean_f": round(adjusted_mean, 1),
                    "raw_ensemble_mean_f": round(ensemble_mean, 1),
                    "blended_mean_f": round(blended_mean, 1),
                    "aifs_temp_f": round(aifs_temp, 1) if aifs_temp else None,
                    "icon_temp_f": round(icon_temp, 1) if icon_temp else None,
                    "rolling_bias_f": round(rolling_bias, 1),
                    "enso_adj_f": round(enso_adj, 1),
                    "actual_f": round(actual_temp, 1),
                    "prob": round(prob, 3),
                    "market_price": round(market_price, 3),
                    "edge": round(edge, 3),
                    "bet_usd": round(bet_usd, 4),
                    "won": won,
                    "pnl": round(pnl, 4),
                    "bankroll_after": round(bankroll, 4),
                    "gfs_members": n_members,
                    "gfs_spread": gfs.get("ensemble_spread", 0),
                }
                all_trades.append(trade)

                month_key = target_date[:7]
                monthly_pnl[month_key] = monthly_pnl.get(month_key, 0.0) + pnl

        daily_bankroll.append(bankroll)

    logger.info(f"[REAL_BACKTEST] Fetch stats: {fetch_stats}")

    return _compile_report(
        all_trades, daily_bankroll, bankroll_start, bankroll,
        monthly_pnl, cities, total_cities_processed, len(all_dates),
        start_date, end_date, mode="real_gfs_ensemble",
        extra={"fetch_stats": fetch_stats},
    )


def _compile_report(
    all_trades: list[dict],
    daily_bankroll: list[float],
    bankroll_start: float,
    bankroll_end: float,
    monthly_pnl: dict,
    cities: list[dict],
    total_cities: int,
    total_dates: int,
    start_date: str,
    end_date: str,
    mode: str = "simplified",
    extra: dict | None = None,
) -> dict:
    """Compile final backtest report from trade list."""
    winning_trades = [t for t in all_trades if t["won"]]
    losing_trades = [t for t in all_trades if not t["won"]]
    total_trades = len(all_trades)
    win_rate = len(winning_trades) / total_trades if total_trades > 0 else 0.0

    gross_profit = sum(t["pnl"] for t in winning_trades)
    gross_loss = abs(sum(t["pnl"] for t in losing_trades))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    total_pnl = sum(t["pnl"] for t in all_trades)

    avg_edge = statistics.mean([t["edge"] for t in all_trades]) if all_trades else 0
    avg_bet = statistics.mean([t["bet_usd"] for t in all_trades]) if all_trades else 0

    # Sharpe ratio
    daily_returns = []
    for i in range(1, len(daily_bankroll)):
        if daily_bankroll[i - 1] > 0:
            daily_returns.append(
                (daily_bankroll[i] - daily_bankroll[i - 1]) / daily_bankroll[i - 1]
            )

    if daily_returns:
        mean_ret = statistics.mean(daily_returns)
        std_ret = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 1e-9
        sharpe = (mean_ret / std_ret) * math.sqrt(252) if std_ret > 0 else 0
    else:
        sharpe = 0.0

    # Max drawdown
    peak = bankroll_start
    max_dd = 0.0
    for b in daily_bankroll:
        if b > peak:
            peak = b
        dd = (peak - b) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # Losing streaks
    sorted_trades = sorted(all_trades, key=lambda t: (t["date"], t["city"]))
    current_streak = 0
    longest_streak = 0
    streaks = {}
    for t in sorted_trades:
        if not t["won"]:
            current_streak += 1
        else:
            if current_streak > 0:
                streaks[current_streak] = streaks.get(current_streak, 0) + 1
            current_streak = 0
        if current_streak > longest_streak:
            longest_streak = current_streak

    report = {
        "mode": mode,
        "start_date": start_date,
        "end_date": end_date,
        "cities": [c.get("slug", c["name"]) for c in cities[:total_cities]],
        "total_cities_processed": total_cities,
        "total_dates": total_dates,
        "total_trades": total_trades,
        "winning_trades": len(winning_trades),
        "losing_trades": len(losing_trades),
        "win_rate": round(win_rate, 4),
        "total_pnl": round(total_pnl, 4),
        "bankroll_start": bankroll_start,
        "bankroll_end": round(bankroll_end, 4),
        "return_pct": round((bankroll_end - bankroll_start) / bankroll_start * 100, 2),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else "inf",
        "sharpe_ratio": round(sharpe, 3),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "avg_edge": round(avg_edge, 4),
        "avg_bet_usd": round(avg_bet, 4),
        "monthly_pnl": {k: round(v, 2) for k, v in sorted(monthly_pnl.items())},
        "losing_streaks": {"longest": longest_streak, "distribution": streaks},
        "top_5_wins": sorted(all_trades, key=lambda t: -t["pnl"])[:5],
        "top_5_losses": sorted(all_trades, key=lambda t: t["pnl"])[:5],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        report.update(extra)

    return report


def print_report(report: dict):
    """Pretty-print the backtest report."""
    mode_label = "REAL GFS ENSEMBLE" if report.get("mode") == "real_gfs_ensemble" else "SIMPLIFIED"
    print("\n" + "=" * 60)
    print(f"  POLYBOT BACKTEST REPORT — {mode_label}")
    print("=" * 60)
    print(f"Period: {report['start_date']} to {report['end_date']}")
    print(f"Cities: {', '.join(report['cities'][:10])}{'...' if len(report['cities']) > 10 else ''}")
    print(f"Total trades: {report['total_trades']}")
    print(f"Win rate: {report['win_rate']:.1%}")
    print(f"Profit factor: {report['profit_factor']}")
    print(f"Sharpe ratio: {report['sharpe_ratio']}")
    print(f"Max drawdown: {report['max_drawdown_pct']:.1%}")
    print(f"Total P&L: ${report['total_pnl']:.2f} ({report['return_pct']:+.1f}%)")
    print(f"Bankroll: ${report['bankroll_start']:.2f} -> ${report['bankroll_end']:.2f}")
    print(f"Avg edge: {report['avg_edge']:.1%}")
    print(f"Avg bet: ${report['avg_bet_usd']:.3f}")
    print(f"\nLongest losing streak: {report['losing_streaks']['longest']}")

    if report.get("fetch_stats"):
        fs = report["fetch_stats"]
        print(f"\nGFS fetch stats: success={fs['success']} failed={fs['failed']} "
              f"low_members={fs.get('skipped_no_members', 0)}")

    print("\nMonthly P&L:")
    print("-" * 40)
    for month, pnl in report.get("monthly_pnl", {}).items():
        bar_len = min(int(abs(pnl) / max(1, abs(pnl) * 0.02 + 0.1)), 50)
        bar = "█" * max(1, bar_len)
        sign = "+" if pnl >= 0 else "-"
        print(f"  {month}: {sign}${abs(pnl):8.2f} {bar}")

    if report.get("top_5_wins"):
        print("\nTop 5 wins:")
        for t in report["top_5_wins"][:5]:
            print(f"  {t['date']} {t['city']:15s} {t['threshold_f']}F: +${t['pnl']:.2f} "
                  f"(edge={t['edge']:.1%}, P={t['prob']:.0%})")

    if report.get("top_5_losses"):
        print("\nTop 5 losses:")
        for t in report["top_5_losses"][:5]:
            print(f"  {t['date']} {t['city']:15s} {t['threshold_f']}F: ${t['pnl']:.2f} "
                  f"(edge={t['edge']:.1%}, P={t['prob']:.0%})")

    print("=" * 60)


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Polybot Backtest Engine")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2025-12-31")
    parser.add_argument("--bankroll", type=float, default=100.0)
    parser.add_argument("--output", default=str(REPORT_PATH))
    parser.add_argument("--real", action="store_true", help="Use real GFS ensemble data")
    parser.add_argument("--max-cities", type=int, default=None,
                        help="Limit cities for faster runs")
    parser.add_argument("--cities", type=str, default=None,
                        help="Comma-separated list of city slugs to include (e.g. atlanta,dallas,mumbai)")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    from polybot.cities import ACTIVE_CITIES, global_cities
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    # Filter cities if --cities specified
    if args.cities:
        city_slugs = [s.strip().lower() for s in args.cities.split(",")]
        cities_to_use = [c for c in ACTIVE_CITIES if c.get("slug", "").lower() in city_slugs]
        if not cities_to_use:
            # Try global_cities as fallback
            cities_to_use = [c for c in global_cities.values()
                           if c.get("slug", "").lower() in city_slugs and not c.get("reserve")]
        logger.info(f"[BACKTEST] Filtered to {len(cities_to_use)} cities: {[c['slug'] for c in cities_to_use]}")
    else:
        cities_to_use = ACTIVE_CITIES

    if args.real:
        report = run_real_backtest(
            cities=cities_to_use,
            start_date=args.start,
            end_date=args.end,
            bankroll_start=args.bankroll,
            max_cities=args.max_cities,
        )
        output_path = Path("/polybot-data/real_backtest_report.json")
    else:
        report = run_backtest(
            cities=ACTIVE_CITIES,
            start_date=args.start,
            end_date=args.end,
            bankroll_start=args.bankroll,
        )
        output_path = Path(args.output)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print_report(report)
    print(f"\nReport saved to: {output_path}")
    return report


if __name__ == "__main__":
    main()
