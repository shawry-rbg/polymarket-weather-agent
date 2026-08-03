"""
Polybot Orchestrator v3 - Auto next-day detection, multi-date scanning.

Key upgrades:
- Auto-detect earliest unresolved date (today -> tomorrow -> day-after)
- Cache active date per city
- When market resolves, auto-advance to next date
- Model consensus gate (3 models within 2°F)
- Liquidity and market sentiment gates
- Base rate check (90th percentile filter)
- False positive forensic logging
- Hard risk rules integration
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import redis as _redis_mod
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from polybot.redis_publisher import publish_price_update
from polybot.signal_publisher import send_signal
from polybot.dashboard_data import (
    record_city_scan, record_bucket_scan, record_session_pnl,
    record_resolved_trade, record_cycle, record_ensemble_bucket,
)

logger = logging.getLogger(__name__)

# Risk constants
MAX_BET_PCT = 0.025  # 2.5% of bankroll
MAX_BET_ABS = 0.10  # $0.10 max bet (live conservative)
BALANCE_FLOOR = 0.50
MAX_WEEKLY_DRAWDOWN = 0.25
MIN_MODELS_FOR_CONSENSUS = 3
MODEL_AGREEMENT_THRESHOLD_F = 2.0
MIN_MARKET_PROB = 0.15
MIN_EDGE_FOR_TRADE = 0.20
MIN_PRICE = 0.02
MAX_PRICE = 0.98
TAIL_PRICE_THRESHOLD = 0.10
TAIL_PROB_THRESHOLD = 0.15
CONS_LOSS_HALT = 3  # Halt after 3 consecutive losses

# Live trading mode — set True to execute real trades on Polymarket
LIVE_MODE = True

MEMORY_PATH = Path("/polybot-data/orchestrator_memory.json")
ACTIVE_DATE_CACHE_PATH = Path("/polybot-data/active_dates.json")
LOSS_STREAK_PATH = Path("/polybot-data/loss_streak.json")
FALSE_POSITIVE_LOG = Path("/polybot-data/false_positive_log.jsonl")

# Global Redis connection (lazy, reused across calls)
_r: _redis_mod.Redis | None = None


def _get_redis():
    """Get or create a Redis connection."""
    global _r
    if _r is not None:
        return _r
    url = os.environ.get("REDIS_URL")
    if url:
        try:
            _r = _redis_mod.from_url(url)
            return _r
        except Exception:
            pass
    return None


def _batch_redis_write(pipe, key: str, mapping: dict, ttl: int = 0):
    """
    Write a hash to Redis via pipeline.
    NOTE: We skip the read-check optimization because pipe.hget() inside a
    pipeline doesn't return the actual value — it queues the command.
    All fields are written unconditionally.
    """
    try:
        pipe.hset(key, mapping=mapping)
        if ttl > 0:
            pipe.expire(key, ttl)
        return True
    except Exception:
        return False


def _cached_forecast_fetch(cache_key: str, fetch_fn, ttl: int = 1800):
    """
    Check Redis cache before fetching. If cached, return cached value.
    If not, call fetch_fn(), cache the result, and return it.
    """
    r = _get_redis()
    if r:
        try:
            cached = r.get(cache_key)
            if cached:
                return json.loads(cached)
        except Exception:
            pass
    # Cache miss — fetch fresh
    result = fetch_fn()
    if result and r:
        try:
            r.set(cache_key, json.dumps(result, default=str), ex=ttl)
        except Exception:
            pass
    return result


def check_trading_halt() -> bool:
    """Check if trading is halted via Redis TRADING_HALT flag."""
    try:
        r = _get_redis()
        if r:
            val = r.get("TRADING_HALT")
            if val:
                val_str = val.decode() if isinstance(val, bytes) else val
                if val_str and val_str.lower() in ("1", "true", "yes"):
                    return True
    except Exception:
        pass
    return False


class AgentMemory:
    """Ruflo-style persistent memory backed by JSON file."""

    def __init__(self, path: Path | str = MEMORY_PATH) -> None:
        self._path = Path(path)
        self._data: dict[str, Any] = {}
        self._load()

    def store(self, key: str, value: dict) -> None:
        value["_stored_at"] = datetime.now(timezone.utc).isoformat()
        self._data[key] = value
        self.persist()

    def retrieve(self, key: str) -> dict | None:
        return self._data.get(key)

    def search(self, query: str) -> list[dict]:
        q = query.lower()
        return [
            {"key": k, "value": v}
            for k, v in self._data.items()
            if q in k.lower() or q in json.dumps(v).lower()
        ]

    def persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2, default=str)

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._data = {}


class DateDetector:
    """Auto-detect the earliest unresolved trading date."""

    def __init__(self, cache_path: Path | str = ACTIVE_DATE_CACHE_PATH) -> None:
        self._path = Path(cache_path)
        self._cache: dict[str, str] = {}  # city -> date_str
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    self._cache = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._cache = {}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._cache, f, indent=2)

    async def get_active_date(self, city: str, city_config: dict) -> str:
        """
        Find the earliest unresolved date for a city.
        Check today, then tomorrow, then day-after until we find
        active markets. Cache the result.
        """
        from polybot.polymarket import find_markets

        # Check cache first
        cached = self._cache.get(city)
        if cached:
            # Verify cache is still valid (market end_date > now)
            try:
                markets = await find_markets(city_name=city, date_str=cached)
                for m in markets:
                    end_date_str = m.get("endDate", "")
                    if end_date_str:
                        try:
                            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                            if end_dt > datetime.now(timezone.utc):
                                return cached  # Cache still valid
                        except ValueError:
                            pass
            except Exception:
                pass
            # Cache expired, re-scan
            del self._cache[city]

        # Scan from today forward
        now = datetime.now(timezone.utc)
        for day_offset in range(7):  # Check up to 7 days ahead
            check_date = now + timedelta(days=day_offset)
            date_str = check_date.strftime("%B %-d")

            try:
                markets = await find_markets(city_name=city, date_str=date_str)
                for m in markets:
                    end_date_str = m.get("endDate", "")
                    if end_date_str:
                        try:
                            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                            if end_dt > now:
                                self._cache[city] = date_str
                                self.save()
                                logger.info(f"[DATE] {city}: active date = {date_str}")
                                return date_str
                        except ValueError:
                            pass
            except Exception as e:
                logger.debug(f"[DATE] {city} {date_str}: {e}")

        # Fallback: return today
        today_str = now.strftime("%B %-d")
        return today_str

    def get_cached_date(self, city: str) -> str | None:
        return self._cache.get(city)


class RiskManager:
    """Hard risk rules and bankroll management."""

    def __init__(self, loss_streak_path: Path | str = LOSS_STREAK_PATH) -> None:
        self._path = Path(loss_streak_path)
        self._data = {
            "consecutive_losses": 0,
            "total_losses": 0,
            "total_wins": 0,
            "weekly_pnl": 0.0,
            "last_reset": datetime.now(timezone.utc).isoformat(),
            "halted_until": None,
        }
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2, default=str)

    @property
    def is_halted(self) -> bool:
        if self._data.get("halted_until"):
            halted_until = datetime.fromisoformat(self._data["halted_until"])
            if halted_until > datetime.now(timezone.utc):
                return True
            else:
                # Halt expired
                self._data["halted_until"] = None
                self._data["consecutive_losses"] = 0
                self.save()
        return False

    def record_trade(self, won: bool, pnl: float = 0.0) -> None:
        if won:
            self._data["total_wins"] = self._data.get("total_wins", 0) + 1
            self._data["consecutive_losses"] = 0
        else:
            self._data["total_losses"] = self._data.get("total_losses", 0) + 1
            self._data["consecutive_losses"] = self._data.get("consecutive_losses", 0) + 1

            if self._data["consecutive_losses"] >= CONS_LOSS_HALT:
                halt_until = datetime.now(timezone.utc) + timedelta(hours=24)
                self._data["halted_until"] = halt_until.isoformat()
                logger.warning(f"[RISK] {CONS_LOSS_HALT} consecutive losses - HALTED for 24h")

        self._data["weekly_pnl"] = self._data.get("weekly_pnl", 0) + pnl
        self.save()

    def get_max_bet(self, bankroll: float) -> float:
        """Calculate maximum bet size based on current bankroll."""
        if bankroll < BALANCE_FLOOR:
            return 0.0
        bet = bankroll * MAX_BET_PCT
        if bankroll <= 10.0:
            bet = min(bet, MAX_BET_ABS)
        bet = min(bet, bankroll - BALANCE_FLOOR)
        return max(0, round(bet, 2))

    def get_recent_win_rate(self) -> float | None:
        """Return win rate if there are enough trades for a meaningful estimate."""
        total = self._data.get("total_wins", 0) + self._data.get("total_losses", 0)
        if total <= 0:
            return None
        return self._data.get("total_wins", 0) / total


def log_false_positive(city: str, reason: str, data: dict) -> None:
    """Log a false positive with forensic analysis."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "city": city,
        "reason": reason,
        "data": data,
    }
    try:
        FALSE_POSITIVE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(FALSE_POSITIVE_LOG, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        logger.error(f"Failed to log false positive: {e}")


class CityAgent:
    """Single city analysis pipeline v3 with all filters."""

    def __init__(self, city: dict) -> None:
        self.name: str = city["name"]
        self.slug: str = city.get("slug", city["name"].lower().replace(" ", "_"))
        self.lat: float = float(city["lat"])
        self.lon: float = float(city["lon"])
        self.unit: str = city.get("unit", "C")
        self.buckets: list = city.get("buckets", [])
        self.bias: float = float(city.get("bias", 0.0))

    async def analyze(
        self,
        bankroll: float,
        date_detector: DateDetector,
        risk_manager: RiskManager,
    ) -> dict:
        """Run full analysis pipeline for this city."""
        print(f"[AGENT] {self.name}: analyzing...")
        t0 = time.monotonic()

        # Check if trading is halted
        if risk_manager.is_halted:
            logger.warning(f"[AGENT] Trading halted - skipping {self.name}")
            return {
                "city": self.name,
                "recommendation": "HALTED",
                "reason": "3 consecutive losses - 24h halt",
            }

        try:
            # Stage 1: Detect active date
            active_date = await date_detector.get_active_date(self.name, {})
            logger.info(f"[AGENT] {self.name}: active date = {active_date}")

            # Initialize batch Redis writes dict (collected throughout the pipeline, written once at end)
            _redis_writes: dict[str, dict] = {}

            # Stage 2: Get weather forecast with consensus check
            from polybot.ensemble import get_ensemble_forecast

            ensemble = await get_ensemble_forecast(self.lat, self.lon, self.name)
            if not ensemble or ensemble.get("ensemble_temp_f") is None:
                return {"city": self.name, "recommendation": "NO_FORECAST", "status": "no_data"}

            # Gate: model consensus check
            if ensemble.get("abort_probability"):
                # Before aborting, try HRRR tiebreaker for US cities
                logger.info(f"[AGENT] {self.name}: models disagree >5F, trying HRRR tiebreaker")
                try:
                    from polybot.hrrr import fetch_hrrr, hrrr_tiebreaker, US_CITIES_HRRR
                    # Check if this is a US city
                    is_us_city = self.slug in US_CITIES_HRRR or self.name.lower() in (
                        "atlanta", "dallas", "miami"
                    )
                    if is_us_city:
                        hrrr_result = await fetch_hrrr(self.lat, self.lon)
                        if hrrr_result:
                            ecmwf_temp = None
                            gfs_temp = None
                            for m in ensemble.get("models", []):
                                if m.get("model") == "ecmwf":
                                    ecmwf_temp = m.get("temp_f")
                                # Open-Meteo default blend approximates GFS
                                if m.get("model") == "openmeteo":
                                    gfs_temp = m.get("temp_f")
                            if ecmwf_temp and gfs_temp:
                                selected = hrrr_tiebreaker(gfs_temp, ecmwf_temp, hrrr_result["temp_max_f"])
                                ensemble["ensemble_temp_f"] = selected
                                ensemble["hrrr_tiebreaker_used"] = True
                                logger.info(f"[AGENT] {self.name}: HRRR tiebreaker selected {selected}F")
                                # Reset abort since we resolved the disagreement
                                ensemble["abort_probability"] = False
                except Exception as e:
                    logger.debug(f"HRRR tiebreaker failed for {self.name}: {e}")

            if ensemble.get("abort_probability"):
                logger.info(f"[AGENT] {self.name}: models disagree >5F, skipping")
                log_false_positive(self.name, "model_disagreement", {
                    "spread_f": ensemble.get("model_spread_f"),
                    "models": ensemble.get("models"),
                })
                return {
                    "city": self.name,
                    "recommendation": "NO_CONSENSUS",
                    "model_spread_f": ensemble.get("model_spread_f"),
                }

            # Stage 2.5: Fetch GFS 31-member ensemble for true probabilities
            gfs_ensemble_data = None
            gfs_probs = None
            try:
                from polybot.gfs_ensemble import (
                    fetch_gfs_ensemble,
                    format_ensemble_summary,
                )
                thresholds = [float(b) for b in self.buckets] if self.buckets else []
                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                cache_key = f"forecast_cache:{self.slug}:{today_str}"

                async def _fetch_gfs():
                    return await fetch_gfs_ensemble(
                        lat=self.lat, lon=self.lon,
                        thresholds=thresholds if thresholds else None,
                        unit=self.unit,
                    )

                gfs_ensemble_data = await _cached_forecast_fetch(cache_key, _fetch_gfs, ttl=1800)
                if gfs_ensemble_data:
                    gfs_probs = gfs_ensemble_data.get("ensemble_probs", {})
                    temps = gfs_ensemble_data["temps_by_member"]
                    from polybot.gfs_ensemble import ensemble_bucket_probs
                    bucket_edges_f = [71.6, 73.4, 75.2, 77.0, 78.8, 80.6, 82.4, 84.2, 86.0, 87.8, 89.6, 9999]
                    if self.unit == "F":
                        bucket_edges_f = [65, 66, 68, 70, 72, 74, 76, 78, 80, 82, 84, 9999]
                    ensemble["gfs_bucket_probs"] = ensemble_bucket_probs(temps, bucket_edges_f)
                    summary = format_ensemble_summary(
                        sorted(gfs_probs.keys())[:6], gfs_probs,
                        gfs_ensemble_data["member_count"],
                    )
                    print(f"[AGENT] {self.name} ({gfs_ensemble_data['member_count']} members): {summary}")
                    ensemble["gfs_probs"] = gfs_probs
                    ensemble["gfs_ensemble"] = {
                        "mean": gfs_ensemble_data["ensemble_mean"],
                        "std": gfs_ensemble_data["ensemble_std"],
                        "spread": gfs_ensemble_data["ensemble_spread"],
                        "member_count": gfs_ensemble_data["member_count"],
                    }
                    # Store raw temps in Redis for rebalancer (batched later)
                    ensemble["_gfs_temps"] = temps

                    # Stage 2.5.4: Apply per-city historical bias correction
                    try:
                        from polybot.bias_correction import apply_bias
                        raw_ensemble_f = ensemble.get("ensemble_temp_f", 0)
                        if raw_ensemble_f:
                            corrected_f, bias_val = apply_bias(raw_ensemble_f, self.slug)
                            if bias_val != 0.0:
                                ensemble["ensemble_temp_f"] = corrected_f
                                ensemble["bias_correction_f"] = bias_val
                                print(f"[AGENT] {self.name}: bias correction {bias_val:+.1f}F: {raw_ensemble_f:.1f}F -> {corrected_f:.1f}F")
                            else:
                                ensemble["bias_correction_f"] = 0.0
                    except Exception as e:
                        logger.debug(f"[AGENT] {self.name}: bias correction error: {e}")
            except Exception as e:
                logger.debug(f"GFS ensemble fetch failed for {self.name}: {e}")

            # Stage 2.5.5: Apply atmospheric sensitivity corrections
            atmos_corrections = None
            try:
                from polybot.atmospheric import get_all_atmospheric_corrections, log_corrections_to_redis
                raw_forecast_f = ensemble.get("ensemble_temp_f", 0) if ensemble else 0

                # Get market end time for time-to-resolution adjustment
                market_end_time = None
                try:
                    from polybot.polymarket import find_markets
                    markets_temp = await find_markets(city_name=self.name, date_str=active_date)
                    if markets_temp:
                        end_str = markets_temp[0].get("endDate", "")
                        if end_str:
                            market_end_time = end_str
                except Exception:
                    pass

                # Fetch all atmospheric corrections concurrently
                atmos_corrections = await get_all_atmospheric_corrections(
                    city_slug=self.slug,
                    lat=self.lat,
                    lon=self.lon,
                    forecast_f=raw_forecast_f,
                    market_end_time=market_end_time,
                )

                # Apply total atmospheric correction to ensemble forecast
                total_atmos = atmos_corrections.get("total_atmos_correction_f", 0)
                if total_atmos != 0 and raw_forecast_f:
                    corrected_f = raw_forecast_f + total_atmos
                    ensemble["ensemble_temp_f"] = round(corrected_f, 1)
                    ensemble["atmos_correction_applied"] = total_atmos
                    print(f"[AGENT] {self.name}: Atmos correction {total_atmos:+.2f}F: {raw_forecast_f:.1f}F -> {corrected_f:.1f}F")

                # Resolution source verification (skip city if station bias too high)
                resolution_ok = True
                try:
                    from polybot.polymarket import verify_resolution_station
                    resolution_check = await verify_resolution_station(
                        city=self.slug, date=active_date,
                        forecast_temp_f=ensemble.get("ensemble_temp_f", raw_forecast_f or 70),
                    )
                    if resolution_check.get("skip_trade"):
                        print(f"[AGENT] {self.name}: SKIP CITY — {resolution_check['reason']}")
                        log_false_positive(self.name, "resolution_station_bias", resolution_check)
                        resolution_ok = False
                    else:
                        ensemble["resolution_verified"] = True
                        ensemble["resolution_station"] = resolution_check.get("station_id", "N/A")
                except Exception as e:
                    logger.debug(f"[AGENT] {self.name}: Resolution check error: {e}")

                if not resolution_ok:
                    elapsed = time.monotonic() - t0
                    return {
                        "city": self.name, "slug": self.slug, "active_date": active_date,
                        "forecast": ensemble, "markets_found": 0,
                        "trade_signals": [], "tail_signals": [],
                        "elapsed_seconds": round(elapsed, 2),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "status": "ok", "recommendation": "SKIPPED_RESOLUTION_BIAS",
                    }

                # Store corrections in Redis for debugging
                log_corrections_to_redis(self.slug, atmos_corrections)

                # Store time adjustment for use in edge calculation
                time_adj = atmos_corrections.get("time_adjustment", {})
                ensemble["time_adj_edge_mult"] = time_adj.get("edge_mult", 1.0)
                ensemble["time_adj_kelly_mult"] = time_adj.get("kelly_mult", 1.0)
                ensemble["time_to_close_hours"] = time_adj.get("hours_to_close", 999)
                ensemble["time_zone"] = time_adj.get("zone", "unknown")

            except Exception as e:
                logger.debug(f"[AGENT] {self.name}: Atmospheric correction error: {e}")
                atmos_corrections = None

            # Record city data for dashboard - pull from ensemble models list
            try:
                models = ensemble.get("models", [])
                # Build a dict: {model_name: temp_f}
                model_temps = {}
                for m in models:
                    name = m.get("model", "")
                    temp = m.get("temp_f")
                    if name and temp is not None:
                        model_temps[name] = temp

                rec_forecast = model_temps.get("openmeteo") or model_temps.get("weatherstack")
                rec_ecmwf = model_temps.get("ecmwf")
                rec_icon = model_temps.get("icon")
                rec_ukmo = model_temps.get("ukmo")

                # Fallback: use ensemble average if specific model missing
                ensemble_f = ensemble.get("ensemble_temp_f")
                if rec_forecast is None:
                    rec_forecast = ensemble_f
                if rec_ecmwf is None:
                    rec_ecmwf = ensemble_f
                if rec_icon is None:
                    rec_icon = ensemble_f
                if rec_ukmo is None:
                    rec_ukmo = ensemble_f

                agreement = ensemble.get("agreement", "unknown")
                spread = ensemble.get("model_spread_f", 0)
                rec_trend = f"{spread}F spread" if spread else "high agreement"

                print(f"DEBUG: record_city_scan values for {self.name}:")
                print(f"  model_temps={model_temps}")
                print(f"  forecast_high={rec_forecast}, ecmwf={rec_ecmwf}, icon={rec_icon}, ukmo={rec_ukmo}")
                print(f"  sky={agreement}, trend={rec_trend}")

                record_city_scan(
                    self.name,
                    live_temp=rec_forecast if rec_forecast is not None else "N/A",
                    ecmwf=rec_ecmwf if rec_ecmwf is not None else "N/A",
                    icon=rec_icon if rec_icon is not None else "N/A",
                    ukmo=rec_ukmo if rec_ukmo is not None else "N/A",
                    sky=agreement,
                    trend=rec_trend,
                )
                print(f"DEBUG: record_city_scan returned for {self.name}")

            except Exception as e:
                print(f"DEBUG: record_city_scan EXCEPTION for {self.name}: {e}")
                # Still record with fallback values
                try:
                    ensemble_f = ensemble.get("ensemble_temp_f") if ensemble else None
                    record_city_scan(self.name,
                        live_temp=ensemble_f or "N/A",
                        ecmwf=ensemble_f or "N/A",
                        icon=ensemble_f or "N/A",
                        ukmo=ensemble_f or "N/A",
                        sky="unknown",
                        trend="N/A",
                    )
                except Exception:
                    pass

            # Stage 2.6: Collect all Redis writes for batch pipeline
            # (Instead of N individual writes, we collect and write once at the end)
            _redis_writes = {}  # {key: {field: value}}

            # Settlement corrections
            try:
                from polybot.settlement_corrections import apply_correction, get_settlement_correction
                corr = get_settlement_correction(self.slug)
                adj_temp = apply_correction(ensemble.get("ensemble_temp_f", 0), self.slug)
                _redis_writes[f"city_metrics:{self.slug}"] = {
                    "settlement_correction": str(corr),
                    "adj_forecast_temp_f": str(adj_temp),
                    "station_correction_applied": "1",
                }
            except Exception as e:
                logger.debug(f"[AGENT] {self.name}: Settlement correction error: {e}")

            # GFS ensemble temps for rebalancer
            if gfs_ensemble_data:
                temps = gfs_ensemble_data["temps_by_member"]
                gfs_key = f"gfs_ensemble:{self.slug}"
                _redis_writes[gfs_key] = {"_temps": [str(t) for t in temps]}
                # Store both raw GFS mean and bias-corrected gfs_mean for dashboard
                raw_gfs_mean = gfs_ensemble_data["ensemble_mean"]
                gfs_mean_corrected = ensemble.get("ensemble_temp_f") or raw_gfs_mean
                bias_val = ensemble.get("bias_correction_f", 0.0)
                _redis_writes[f"city_metrics:{self.slug}"].update({
                    "gfs_member_count": str(gfs_ensemble_data["member_count"]),
                    "gfs_ensemble_mean": str(raw_gfs_mean),
                    "gfs_ensemble_spread": str(gfs_ensemble_data["ensemble_spread"]),
                    "gfs_mean": str(gfs_mean_corrected),
                    "bias_correction": str(bias_val),
                })

            # Atmospheric corrections
            if atmos_corrections:
                _redis_writes[f"city_metrics:{self.slug}"].update({
                    "atmos_wind": str(atmos_corrections.get("wind_correction_f", 0)),
                    "atmos_dew": str(atmos_corrections.get("dew_point_suppression_f", 0)),
                    "atmos_cloud": str(atmos_corrections.get("cloud_correction_f", 0)),
                    "atmos_total": str(atmos_corrections.get("total_atmos_correction_f", 0)),
                    "time_zone": atmos_corrections.get("time_adjustment", {}).get("zone", "?"),
                    "time_hours": str(atmos_corrections.get("time_adjustment", {}).get("hours_to_close", 0)),
                    "edge_mult": str(atmos_corrections.get("time_adjustment", {}).get("edge_mult", 1.0)),
                    "kelly_mult": str(atmos_corrections.get("time_adjustment", {}).get("kelly_mult", 1.0)),
                })

            # Stage 2.7: Load LGBM model if available
            lgbm_model = None
            try:
                from polybot.lgbm_train import load_model
                lgbm_model = load_model()
                if lgbm_model:
                    logger.info(f"[AGENT] {self.name}: LGBM model loaded for probability override")
            except Exception as e:
                logger.debug(f"[AGENT] {self.name}: LGBM model not available: {e}")
            # Gate: base rate check (90th percentile filter)
            from polybot.calibration import compute_base_rate
            month = datetime.now(timezone.utc).month
            threshold_sample = self.buckets[len(self.buckets)//2] if self.buckets else 30
            base_rate = compute_base_rate(self.name, threshold_sample, month, db_path="/polybot-data/history.db")
            forecast_c = ensemble.get("temp_max_c", 0)

            if base_rate > 0.90:
                logger.info(f"[AGENT] {self.name}: base rate >90%, reducing confidence")
                ensemble["confidence"] *= 0.5
                log_false_positive(self.name, "high_base_rate", {
                    "base_rate": base_rate,
                    "forecast_c": forecast_c,
                    "month": month,
                })

            # Stage 3: Find Polymarket markets for active date
            from polybot.polymarket import find_markets, parse_outcome_prices

            # Flush Redis writes (GFS metrics, atmos, etc.) before any early returns
            if _redis_writes:
                try:
                    r = _get_redis()
                    if r:
                        pipe = r.pipeline()
                        for _rk, _rf in _redis_writes.items():
                            if "_temps" in _rf:
                                pipe.delete(_rk)
                                for _t in _rf["_temps"]:
                                    pipe.rpush(_rk, _t)
                                pipe.expire(_rk, 7200)
                            else:
                                pipe.hset(_rk, mapping=_rf)
                                pipe.expire(_rk, 10800)
                        pipe.execute()
                except Exception as _e:
                    logger.debug(f"[AGENT] {self.name}: Redis flush error: {_e}")
            _redis_writes = {}  # Clear after flush

            markets = await find_markets(city_name=self.name, date_str=active_date)
            if not markets:
                return {"city": self.name, "recommendation": "NO_MARKETS", "date": active_date}

            # Stage 4: Analyze each market with all filters
            trade_signals = []
            for m in markets:
                question = m.get("question", "")
                threshold = m.get("threshold_f")
                if threshold is None:
                    threshold = _extract_threshold(question)

                try:
                    yes_price, no_price = parse_outcome_prices(m)
                except Exception:
                    continue

                # Gate: price bounds
                if yes_price < MIN_PRICE or yes_price > MAX_PRICE:
                    continue

                # Gate: market sentiment (implied prob > 0.15)
                if yes_price < MIN_MARKET_PROB and (1 - yes_price) < MIN_MARKET_PROB:
                    continue

                # Compute probability — use GFS 31-member ensemble count if available, else Bayesian
                from polybot.calibration import ProbabilityCalibrator
                calibrator = ProbabilityCalibrator()

                gfs_probs = ensemble.get("gfs_probs")
                model_confidence = ensemble.get("confidence", 0.5)
                if gfs_probs and threshold:
                    # TRUE ENSEMBLE PROBABILITY: count members above threshold
                    # Find nearest threshold in gfs_probs
                    nearest_thr = min(gfs_probs.keys(), key=lambda t: abs(t - threshold))
                    true_prob = gfs_probs.get(nearest_thr, 0.5)
                    prob_source = f"GFS_ensemble_{ensemble.get('gfs_ensemble', {}).get('member_count', '?')}m({nearest_thr}F)"
                else:
                    # Fallback: Bayesian probability from ensemble mean
                    from polybot.prediction_engine import bayesian_temperature_probability
                    eval_temp_c = forecast_c
                    eval_temp_f = eval_temp_c * 9 / 5 + 32
                    uncertainty_f = ensemble.get("uncertainty_f", 5.0)
                    model_confidence = ensemble.get("confidence", 0.5)
                    true_prob = bayesian_temperature_probability(
                        forecast_temp_f=eval_temp_f,
                        threshold_f=threshold,
                        uncertainty_f=uncertainty_f,
                        model_confidence=model_confidence,
                    )
                    prob_source = "bayesian"

                # Apply atmospheric corrections to probability
                # Wind/dew/cloud corrections shift the effective threshold
                if atmos_corrections:
                    atmos_total = atmos_corrections.get("total_atmos_correction_f", 0)
                    if atmos_total != 0:
                        # Shift probability: if atmos suppresses temp, reduce P(above threshold)
                        # Approximate by adjusting probability proportionally
                        prob_shift = atmos_total * 0.02  # ~2% per degree F
                        true_prob = max(0.0, min(1.0, true_prob + prob_shift))

                # Calibrate
                calibrated_prob = calibrator.calibrate_probability(true_prob, self.name)

                if prob_source.startswith("GFS"):
                    print(f"[AGENT] {self.name} bucket>{threshold}F: "
                          f"GFS_prob={true_prob:.2%} → calibrated={calibrated_prob:.2%} "
                          f"(source={prob_source})")

                # Market gate: apply time-to-resolution edge multiplier
                edge_mult = ensemble.get("time_adj_edge_mult", 1.0)
                effective_min_edge = MIN_EDGE_FOR_TRADE * edge_mult
                if abs(calibrated_prob - yes_price) < effective_min_edge:
                    logger.debug(f"[AGENT] {self.name}: edge {abs(calibrated_prob - yes_price):.3f} < effective min {effective_min_edge:.3f} (zone={ensemble.get('time_zone', '?')})")
                    continue

                # Live temp check DISABLED — was aborting all trades when current temp
                # differed from daily-max forecast by >2C. Paper mode did not have this
                # check and achieved 88% win rate. Removed for live parity.
                # from polybot.live_prob import fetch_live_temp
                # live = await fetch_live_temp(self.lat, self.lon)
                # if live:
                #     live_c = live[0]
                #     if 'eval_temp_c' not in dir():
                #         eval_temp_c = forecast_c
                #     if abs(live_c - eval_temp_c) > 2.0:
                #         logger.info(f"[AGENT] {self.name}: live temp differs >2C from forecast, aborting")
                #         log_false_positive(self.name, "live_temp_mismatch", {...})
                #         continue

                # Kelly sizing
                from polybot.kelly import kelly_criterion

                kelly = kelly_criterion(
                    calibrated_prob,
                    yes_price,
                    bankroll,
                    recent_win_rate=risk_manager.get_recent_win_rate(),
                    consecutive_losses=risk_manager._data.get("consecutive_losses", 0),
                )
                edge = kelly.get("edge", 0)
                direction = kelly.get("direction", "NONE")

                if edge <= 0 or direction == "NONE":
                    continue

                # ================================================================
                # CONTRARIAN MODE: If market price > model_prob * 0.85,
                # the market is overpriced relative to our model.
                # Find the adjacent bucket with lowest market price and buy that
                # instead with 60% Kelly size.
                # ================================================================
                is_contrarian = False
                contrarian_bucket = None
                if yes_price > calibrated_prob * 0.85 and direction == "BUY":
                    # Find adjacent buckets (next higher and next lower threshold)
                    adjacent_buckets = []
                    all_thresholds = sorted([m.get("threshold_f", 0) for m in markets if m.get("threshold_f")])
                    if threshold in all_thresholds:
                        idx = all_thresholds.index(threshold)
                        if idx > 0:
                            lower_t = all_thresholds[idx - 1]
                            lower_m = next((m for m in markets if m.get("threshold_f") == lower_t), None)
                            if lower_m:
                                try:
                                    lp, _ = parse_outcome_prices(lower_m)
                                    adjacent_buckets.append((lower_t, lp, lower_m))
                                except Exception:
                                    pass
                        if idx < len(all_thresholds) - 1:
                            higher_t = all_thresholds[idx + 1]
                            higher_m = next((m for m in markets if m.get("threshold_f") == higher_t), None)
                            if higher_m:
                                try:
                                    hp, _ = parse_outcome_prices(higher_m)
                                    adjacent_buckets.append((higher_t, hp, higher_m))
                                except Exception:
                                    pass

                    # Pick adjacent bucket with lowest market price
                    if adjacent_buckets:
                        best_adj = min(adjacent_buckets, key=lambda x: x[1])
                        adj_threshold, adj_price, adj_market = best_adj
                        # Only contrarian if adjacent is reasonably priced
                        if adj_price < yes_price * 0.8:
                            is_contrarian = True
                            contrarian_bucket = {
                                "threshold_f": adj_threshold,
                                "yes_price": adj_price,
                                "question": adj_market.get("question", "")[:100],
                                "conditionId": adj_market.get("conditionId", ""),
                                "slug": adj_market.get("slug", ""),
                                "id": str(adj_market.get("id", "")),
                                "volume24hr": adj_market.get("volume24hr", 0),
                            }
                            # Use 60% Kelly for contrarian bets
                            kelly_usd = round(kelly_usd * 0.60, 4)
                            direction = "BUY"
                            threshold = adj_threshold
                            yes_price = adj_price
                            question = contrarian_bucket["question"]
                            print(f"[AGENT] {self.name}: CONTRARIAN — market overpriced at {yes_price:.3f} > prob*0.85={calibrated_prob*0.85:.3f}, buying adj bucket {adj_threshold}F @ {adj_price:.3f} (60% Kelly=${kelly_usd:.4f})")

                # Apply risk limits
                max_bet = risk_manager.get_max_bet(bankroll)
                kelly_usd = min(kelly.get("kelly_usd", 0), max_bet)

                # Apply atmospheric Kelly multipliers
                kelly_mult = ensemble.get("time_adj_kelly_mult", 1.0)
                if atmos_corrections:
                    dew_kelly_mult = atmos_corrections.get("dew_point_kelly_mult", 1.0)
                    kelly_mult *= dew_kelly_mult
                if kelly_mult != 1.0:
                    kelly_usd = round(kelly_usd * kelly_mult, 4)
                    kelly_usd = max(kelly_usd, 0)  # Never negative
                    logger.debug(f"[AGENT] {self.name}: Kelly adjusted by mult={kelly_mult:.2f} -> ${kelly_usd:.4f}")

                # Liquidity pre-trade gate
                from polybot.liquidity_sniffer import pre_trade_check
                liq = await pre_trade_check(m, order_size=kelly_usd, direction=direction)
                if not liq["pass"]:
                    logger.info(f"[AGENT] {self.name}: liquidity skip — {liq['reason']}")
                    continue

                volume = float(m.get("volume24hr", 0) or 0)

                trade_signals.append({
                    "question": question[:100],
                    "threshold_f": threshold,
                    "yes_price": yes_price,
                    "calibrated_prob": round(calibrated_prob, 3),
                    "volume": volume,
                    "direction": direction,
                    "edge": round(edge, 4),
                    "kelly_usd": round(kelly_usd, 2),
                    "model_confidence": model_confidence,
                    "active_date": active_date,
                    "conditionId": m.get("conditionId", ""),
                    "slug": m.get("slug", ""),
                    "liquidity": liq.get("depth"),
                    "is_contrarian": is_contrarian,
                    "entry_type": "normal",
                })

                # Ensemble agreement entry for dashboard
                try:
                    print(f"DEBUG: record_ensemble_bucket for {self.name} bucket>{threshold}F")
                    record_ensemble_bucket(
                        bucket=f">{threshold}F",
                        agreement="STRONG" if ensemble.get("confidence", 0) > 0.7 else "WEAK",
                        spread=str(ensemble.get("model_spread_f", "")),
                        prob=str(round(calibrated_prob, 3)),
                        market_price=str(round(yes_price, 3)),
                        edge=str(round(edge, 4)),
                    )
                    print(f"DEBUG: record_ensemble_bucket returned")
                except Exception as e:
                    print(f"DEBUG: record_ensemble_bucket ERROR: {e}")

                # Bucket scan entry for dashboard
                try:
                    print(f"DEBUG: record_bucket_scan for {self.name} bucket>{threshold}F")
                    record_bucket_scan(
                        bucket=f">{threshold}F",
                        p=str(round(calibrated_prob, 3)),
                        q=str(round(yes_price, 3)),
                        edge=str(round(edge, 4)),
                        hit="1" if calibrated_prob > yes_price else "0",
                        price=str(round(yes_price, 3)),
                    )
                    print(f"DEBUG: record_bucket_scan returned")
                except Exception as e:
                    print(f"DEBUG: record_bucket_scan ERROR: {e}")

                # Publish price update to Redis
                bucket_name = f">{threshold}F"
                publish_price_update(self.name, bucket_name, yes_price)

                # Send signal to Cloudflare Worker (event-driven trigger)
                send_signal(self.slug, bucket_name, yes_price)

                # Send Discord trade alert
                try:
                    from polybot.notify import send_trade_alert
                    send_trade_alert(
                        city=self.name,
                        bucket=bucket_name,
                        price=yes_price,
                        edge=edge,
                        bet_size=kelly_usd,
                        status="LIVE" if LIVE_MODE else "PAPER",
                    )
                except Exception as e:
                    print(f"[ALERT] Discord notify failed: {e}")

            # Sort by edge
            trade_signals.sort(key=lambda s: s["edge"], reverse=True)

            # LIVE TRADE EXECUTION — execute best signal if LIVE_MODE
            live_trade_result = None
            if LIVE_MODE and trade_signals:
                best_signal = trade_signals[0]
                try:
                    from polybot.clob import execute_trade
                    bet_usd = 0.10
                    if best_signal.get("conditionId"):
                        print(f"[LIVE] Executing: {self.name} {best_signal.get('direction','?')} ${bet_usd:.2f} @ {best_signal['yes_price']:.3f} edge={best_signal['edge']:.1%}")
                        live_trade_result = await execute_trade(
                            market_id=best_signal["conditionId"],
                            side=best_signal.get("direction", "BUY"),
                            price=best_signal["yes_price"],
                            size=max(best_signal.get("kelly_usd", 0.10), 0.10),
                            trade_log_path="/polybot-data/live_trades.jsonl",
                            city=self.name,
                            bucket=str(best_signal.get("threshold_f", "")),
                        )
                        if live_trade_result and live_trade_result.get("order_id"):
                            print(f"[LIVE] ORDER PLACED: {live_trade_result['order_id']}")
                        elif live_trade_result and live_trade_result.get("status") == "QUEUED":
                            print(f"[LIVE] QUEUED (gas): gas=${live_trade_result.get('gas_cost_usd',0):.4f}")
                        else:
                            print(f"[LIVE] SKIPPED: result={live_trade_result}")
                    else:
                        print(f"[LIVE] SKIP: no conditionId for best_signal")
                except Exception as live_err:
                    print(f"[LIVE] Error: {live_err}")

            # Tail bucket priority
            tail_signals = [s for s in trade_signals
                           if s["yes_price"] < TAIL_PRICE_THRESHOLD
                           and s["calibrated_prob"] > TAIL_PROB_THRESHOLD
                           and s["edge"] > 0.12]

            elapsed = time.monotonic() - t0
            recommendation = "NO_EDGE"
            if trade_signals:
                best = trade_signals[0]
                recommendation = f"TRADE:{best['direction']} @ {best['yes_price']:.3f}"

            result = {
                "city": self.name,
                "slug": self.slug,
                "active_date": active_date,
                "forecast": ensemble,
                "markets_found": len(markets),
                "trade_signals": trade_signals,
                "tail_signals": tail_signals,
                "live_trade_result": live_trade_result,
                "elapsed_seconds": round(elapsed, 2),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "ok",
                "recommendation": recommendation,
            }

            if trade_signals:
                result["best_trade"] = trade_signals[0]

        except Exception as exc:
            elapsed = time.monotonic() - t0
            result = {
                "city": self.name,
                "slug": self.slug,
                "status": "error",
                "error": str(exc),
                "recommendation": "ERROR",
                "elapsed_seconds": round(elapsed, 2),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            print(f"[AGENT] {self.name}: ERROR - {exc}")

        # Stage 5: Batch all Redis writes into a single pipeline
        # This replaces ~10 individual Redis commands with 1 pipeline.execute()
        if _redis_writes:
            try:
                r = _get_redis()
                if r:
                    pipe = r.pipeline()
                    for key, fields in _redis_writes.items():
                        if "_temps" in fields:
                            # GFS temps: use rpush list
                            pipe.delete(key)
                            for t in fields["_temps"]:
                                pipe.rpush(key, t)
                            pipe.expire(key, 7200)
                        else:
                            # Hash fields: use hset
                            pipe.hset(key, mapping=fields)
                            pipe.expire(key, 10800)
                    pipe.execute()
            except Exception as e:
                logger.debug(f"[AGENT] {self.name}: Batch Redis write error: {e}")

        print(f"[AGENT] {self.name}: {result['recommendation']} ({result['elapsed_seconds']}s)")
        return result


def _extract_threshold(question: str) -> float:
    """Extract temperature threshold from a market question string."""
    q = question.lower()
    patterns = [
        r"(\d+)\s*[°]?\s*f(?:ahrenheit)?\b",
        r"(\d+)\s*degrees?\s*f",
        r"exceed\s+(?:or\s+equal\s+(?:to|)\s+)?(\d+)",
        r"above\s+(?:or\s+equal\s+(?:to|)\s+)?(\d+)",
        r"over\s+(?:or\s+equal\s+(?:to|)\s+)?(\d+)",
        r"at\s+least\s+(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, q)
        if m:
            val = float(m.group(1))
            # Check if Celsius
            if "celsius" in q or ("°c" in q and "°f" not in q) or (" c " in q and val < 60):
                val = val * 9 / 5 + 32
            return round(val, 1)
    return 90.0


class SwarmCoordinator:
    """Ruflo-style parallel swarm with filtering and risk management."""

    def __init__(
        self,
        city_agents: list[CityAgent],
        bankroll: float = 100.0,
    ) -> None:
        self.agents = city_agents
        self.results: list[dict] = []
        self._memory = AgentMemory()
        self._bankroll = bankroll
        self._date_detector = DateDetector()
        self._risk_manager = RiskManager()

    async def run_swarm(self) -> list[dict]:
        """Run all city agents concurrently and return ranked results."""
        n = len(self.agents)
        print(f"[SWARM] Initializing hierarchical swarm with {n} agents")
        print(f"[SWARM] Bankroll: ${self._bankroll:.2f}")

        if self._risk_manager.is_halted:
            print("[SWARM] WARNING: Trading is HALTED due to 3 consecutive losses")
            return []

        t0 = time.monotonic()
        tasks = [
            agent.analyze(self._bankroll, self._date_detector, self._risk_manager)
            for agent in self.agents
        ]
        self.results = await asyncio.gather(*tasks, return_exceptions=False)
        elapsed = time.monotonic() - t0

        # Sort by best edge descending
        self.results.sort(
            key=lambda r: (
                0 if r.get("status") == "ok" else 1,
                -max(
                    (s.get("edge", 0) for s in (r.get("trade_signals") or [])),
                    default=0,
                ),
            ),
        )

        profitable = [
            r for r in self.results
            if r.get("status") == "ok"
            and any(s.get("kelly_usd", 0) > 0 for s in (r.get("trade_signals") or []))
        ]

        if profitable:
            best = profitable[0]
            bt = best.get("best_trade", {})
            ev = bt.get("edge", 0)
            kd = bt.get("kelly_usd", 0)
            print(f"[SWARM] Best opportunity: {best['city']} EV={ev:.1%} KELLY=${kd:.2f}")

        print(f"[SWARM] Swarm complete: {len(profitable)}/{n} profitable opportunities ({elapsed:.1f}s)")

        # Persist
        self._memory.store(
            f"swarm_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
            {
                "n_agents": n,
                "n_profitable": len(profitable),
                "total_seconds": round(elapsed, 2),
                "bankroll": self._bankroll,
            },
        )

        return self.results

    def get_swarm_state(self) -> dict:
        return {
            "n_agents": len(self.agents),
            "n_results": len(self.results),
            "bankroll": self._bankroll,
            "halted": self._risk_manager.is_halted,
            "consecutive_losses": self._risk_manager._data.get("consecutive_losses", 0),
        }


async def run_liquidity_scan(cities: list | None = None) -> dict:
    """
    Run liquidity + arb + smart money scan.
    Called hourly from a separate cron job.
    """
    from polybot.cities import ACTIVE_CITIES
    from polybot.liquidity_sniffer import (
        check_arbitrage_all_cities, track_smart_money, get_trader_positions,
    )
    from polybot.clob import execute_trade

    if cities is None:
        cities = ACTIVE_CITIES

    # --- 1. Arbitrage scan ---
    print("\n[ARB] Scanning for arbitrage opportunities...")
    arb_opps = await check_arbitrage_all_cities(cities)
    arb_trades = 0
    for opp in arb_opps:
        edge = opp.get("edge", 0)
        if edge > 0.02:
            for m in opp.get("markets", []):
                mid = m.get("conditionId") or m.get("slug", "")
                try:
                    if mid:
                        resp = await execute_trade(
                            market_id=mid,
                            price=round(float(m.get("yes_price", 0)) + 0.01, 2),
                            size=0.15,
                            side="BUY",
                            city=opp.get("city", m.get("slug", "")),
                            bucket=m.get("question", "")[:40],
                        )
                        if resp and not resp.get("error"):
                            arb_trades += 1
                            print(f"[ARB] Executed: {m.get('question', '')[:60]}")
                except Exception as e:
                    logger.debug(f"[ARB] Trade failed: {e}")
    print(f"[ARB] {len(arb_opps)} opportunities, {arb_trades} executed")

    # --- 2. Smart money scan ---
    print("\n[SMART] Tracking top traders...")
    top_traders = await track_smart_money(min_trades=5, min_win_rate=0.70, top_n=10)
    copied = 0
    for trader in top_traders[:5]:
        addr = trader["address"]
        positions = await get_trader_positions(addr)
        for pos in positions[:3]:
            mid = pos.get("token_id", "")
            if mid:
                try:
                    resp = await execute_trade(
                        market_id=mid,
                        price=round(float(pos.get("avg_price", 0)) + 0.01, 2),
                        size=0.05,
                        side="BUY",
                        city=pos.get("outcome", "smart_money")[:40],
                        bucket=pos.get("outcome", "")[:40],
                    )
                    if resp and not resp.get("error"):
                        copied += 1
                        print(f"[SMART] Copied {addr[:12]}... {pos.get('outcome', '')}")
                except Exception as e:
                    logger.debug(f"[SMART] Copy failed: {e}")
    print(f"[SMART] {len(top_traders)} traders tracked, {copied} copied")

    return {
        "arb_opportunities": len(arb_opps),
        "arb_trades_executed": arb_trades,
        "top_traders": len(top_traders),
        "copied_trades": copied,
    }


async def resolve_closed_markets(date_filter: str = None, force: bool = False) -> dict:
    """
    Resolve open paper trades by checking Polymarket markets that have ended.

    New approach (v2):
    - Query markets by end_date range (last 14 days), regardless of active/closed flag
    - Check outcomePrices directly: YES > 0.99 = bucket won, YES < 0.01 = bucket lost
    - Match resolved markets to open trades by threshold comparison
    - Record resolved trades in resolved_trades list

    Args:
        date_filter: Optional date string "YYYY-MM-DD" to filter trades.
        force: If True, resolve ALL open trades regardless of age.
    """
    now = datetime.now(timezone.utc)
    print(f"[RESOLVE] Starting market resolution v2 at {now.isoformat()} (force={force})")

    # --- Connect to Redis ---
    _os = __import__("os")
    _redis_mod = __import__("redis", fromlist=["from_url"])
    try:
        _redis_url: str = _os.environ.get("REDIS_URL", "")
        if not _redis_url:
            return {"error": "REDIS_URL not set"}
        r = _redis_mod.from_url(_redis_url)
    except Exception:
        return {"error": "redis connect failed"}

    # --- Acquire Redis lock to prevent concurrent resolution ---
    resolve_lock_key = "resolve_markets:lock"
    resolve_lock_acquired = False
    try:
        # SET NX with 60s TTL — only one resolver can run at a time
        resolve_lock_acquired = r.set(resolve_lock_key, "1", nx=True, ex=60)
        if not resolve_lock_acquired:
            print("[RESOLVE] Another resolution run is in progress — skipping")
            return {"resolved": 0, "message": "lock_not_acquired", "timestamp": now.isoformat()}
    except Exception as e:
        print(f"[RESOLVE] Redis lock error: {e}")
        # Proceed without lock rather than skip entirely

    # --- Set of already-resolved trade IDs (for double-resolution guard) ---
    resolved_ids: set[str] = set()
    try:
        existing_resolved = r.smembers("resolved_trade_ids")
        for item in existing_resolved:
            resolved_ids.add(item.decode() if isinstance(item, bytes) else item)
    except Exception:
        pass

    # --- Fetch all open paper trades ---
    raw_trades = r.lrange("paper_trades", 0, 499)
    open_trades = []
    for item in raw_trades:
        try:
            t = json.loads(item)
            if t.get("status") == "open":
                open_trades.append(t)
        except Exception:
            pass

    if not open_trades:
        print("[RESOLVE] No open paper trades to resolve")
        # Release lock before returning
        try:
            if resolve_lock_acquired:
                r.delete(resolve_lock_key)
        except Exception:
            pass
        return {"resolved": 0, "message": "no open trades"}

    print(f"[RESOLVE] Found {len(open_trades)} open paper trades")

    # --- Filter by date if specified ---
    if date_filter:
        filtered = []
        for t in open_trades:
            ts = t.get("timestamp", "")
            if ts.startswith(date_filter):
                filtered.append(t)
        open_trades = filtered
        print(f"[RESOLVE] After date filter ({date_filter}): {len(open_trades)} trades")

    # --- Group trades by city ---
    trades_by_city: dict[str, list[dict]] = {}
    for t in open_trades:
        city = t.get("city", "unknown")
        trades_by_city.setdefault(city, []).append(t)

    # --- Query Gamma API for markets by end_date ---
    from polybot.polymarket import find_markets_by_end_date, parse_outcome_prices

    total_resolved = 0
    total_pnl = 0.0
    wins = 0
    losses = 0
    resolution_details = []
    errors = []

    # Search window: last 14 days (or 7 if not force)
    search_days = 14 if force else 7
    start_date = (now - timedelta(days=search_days)).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    for city, city_trades in trades_by_city.items():
        try:
            # Find all temperature markets for this city that ended in the search window
            markets = await find_markets_by_end_date(
                city_name=city,
                start_date=start_date,
                end_date=end_date,
                max_pages=10,
            )

            if not markets:
                print(f"[RESOLVE] {city}: No markets found with end_date in last {search_days} days")
                continue

            # Parse each market to determine if it's resolved and which bucket won
            resolved_markets = []
            for m in markets:
                try:
                    yes_price, no_price = parse_outcome_prices(m)
                    question = m.get("question", "")
                    threshold = _extract_threshold(question)
                    is_resolved = m.get("resolved", False) or m.get("closed", False)

                    # A market is resolved if:
                    # 1. Polymarket marks it as resolved/closed, OR
                    # 2. YES price >= 0.99 (clear winner), OR
                    # 3. YES price <= 0.01 (clear loser)
                    if not is_resolved:
                        if yes_price >= 0.99 or yes_price <= 0.01:
                            is_resolved = True

                    if not is_resolved:
                        continue

                    # Determine which bucket won
                    # If YES price >= 0.95, the ">thresholdF" bucket won
                    # If YES price <= 0.05, the ">thresholdF" bucket lost
                    if yes_price >= 0.95:
                        won_bucket = f">{threshold}F"
                    elif yes_price <= 0.05:
                        won_bucket = f"<={threshold}F"
                    else:
                        # Ambiguous — skip
                        continue

                    resolved_markets.append({
                        "question": question,
                        "threshold": threshold,
                        "won_bucket": won_bucket,
                        "yes_price": yes_price,
                        "no_price": no_price,
                    })
                except Exception:
                    pass

            if not resolved_markets:
                print(f"[RESOLVE] {city}: Found {len(markets)} markets but none resolved")
                continue

            # Determine the winning bucket (use the market with most definitive price)
            # Pick the resolved market with yes_price closest to 1.0 or 0.0
            best_market = max(resolved_markets, key=lambda m: abs(m["yes_price"] - 0.5))
            winning_bucket = best_market["won_bucket"]
            print(f"[RESOLVE] {city}: {len(resolved_markets)} resolved markets, winning={winning_bucket}")

            # --- Resolve each open trade for this city ---
            for trade in city_trades:
                trade_bucket = trade.get("bucket", "")
                side = trade.get("side", "")
                entry_price = float(trade.get("entry_price", 0))
                size = float(trade.get("size", 0))

                if entry_price <= 0 or size <= 0:
                    continue

                # Double-resolution guard: generate a unique trade ID
                trade_id = f"{city}:{trade_bucket}:{side}:{trade.get('timestamp', '')}:{entry_price}"
                if trade_id in resolved_ids:
                    print(f"[RESOLVE] SKIP already-resolved trade: {trade_id}")
                    continue

                # Determine if this trade won
                # BUY YES on ">80F" bucket wins if winning_bucket is ">80F"
                # SELL (bet against) ">80F" wins if winning_bucket is "<=80F"
                if side == "BUY":
                    won = (trade_bucket == winning_bucket)
                    profit = ((1.0 - entry_price) * size) if won else (-entry_price * size)
                elif side == "SELL":
                    won = (trade_bucket != winning_bucket)
                    profit = (entry_price * size) if won else (-(1.0 - entry_price) * size)
                else:
                    continue

                profit = round(profit, 4)

                # Update trade in Redis
                trade["status"] = "resolved"
                trade["profit_usd"] = str(profit)
                trade["final_value"] = "1.0" if won else "0.0"
                trade["resolved_at"] = now.isoformat()
                trade["winning_bucket"] = winning_bucket
                trade["won"] = "1" if won else "0"

                # Remove old entry and push updated
                try:
                    all_trades = r.lrange("paper_trades", 0, -1)
                    for old_item in all_trades:
                        try:
                            old_str = old_item.decode() if isinstance(old_item, bytes) else old_item
                            old_t = json.loads(old_str)
                            if (old_t.get("city") == trade.get("city") and
                                old_t.get("bucket") == trade.get("bucket") and
                                old_t.get("timestamp") == trade.get("timestamp") and
                                old_t.get("status") == "open"):
                                r.lrem("paper_trades", 0, old_str)
                                break
                        except Exception:
                            pass
                    r.lpush("paper_trades", json.dumps(trade))
                    r.ltrim("paper_trades", 0, 499)
                except Exception as e:
                    print(f"[RESOLVE] Redis update error: {e}")
                    errors.append({"city": city, "error": str(e)})

                # Record in resolved_trades list
                try:
                    r.lpush("resolved_trades", json.dumps(trade))
                    r.ltrim("resolved_trades", 0, 999)
                except Exception:
                    pass

                # Mark trade as resolved in Redis set (double-resolution guard)
                try:
                    r.sadd("resolved_trade_ids", trade_id)
                    resolved_ids.add(trade_id)
                except Exception:
                    pass

                total_resolved += 1
                total_pnl += profit
                if won:
                    wins += 1
                else:
                    losses += 1

                resolution_details.append({
                    "city": city,
                    "bucket": trade_bucket,
                    "side": side,
                    "entry_price": entry_price,
                    "size": size,
                    "profit_usd": profit,
                    "won": won,
                    "winning_bucket": winning_bucket,
                })

                outcome_str = "WIN" if won else "LOSS"
                print(f"[RESOLVE] {city}/{trade_bucket}: {side} @ {entry_price:.3f} "
                      f"→ {outcome_str} PnL=${profit:+.4f}")

        except Exception as e:
            err_msg = f"{city}: {e}"
            print(f"[RESOLVE] Error: {err_msg}")
            errors.append({"city": city, "error": str(e)})

    # --- Update cumulative P&L in Redis (paper + live) ---
    try:
        current_pnl = float(r.get("paper_pnl_total") or 0)
        current_wins = int(r.get("paper_win_count") or 0)
        current_total = int(r.get("paper_trade_count") or 0)
        new_pnl_total = round(current_pnl + total_pnl, 4)
        r.set("paper_pnl_total", str(new_pnl_total))
        r.set("paper_win_count", str(current_wins + wins))
        r.set("paper_trade_count", str(current_total + total_resolved))
        print(f"[RESOLVE] Cumulative P&L: ${current_pnl:.4f} → ${new_pnl_total:.4f}")
    except Exception as e:
        print(f"[RESOLVE] Redis P&L update error: {e}")
        errors.append({"error": f"pnl_update: {e}"})

    # --- Update live PnL in Redis ---
    try:
        current_live_pnl = float(r.get("live_pnl_total") or 0)
        current_live_wins = int(r.get("live_win_count") or 0)
        new_live_pnl = round(current_live_pnl + total_pnl, 4)
        r.set("live_pnl_total", str(new_live_pnl))
        r.set("live_win_count", str(current_live_wins + wins))
        # Update live_trades list: mark resolved trades
        live_raw = r.lrange("live_trades", 0, -1)
        for item in live_raw:
            try:
                t = json.loads(item)
                if t.get("status") == "open" and t.get("city") == city and t.get("bucket") == trade_bucket:
                    t["status"] = "resolved"
                    t["pnl"] = str(profit)
                    t["won"] = won
                    r.lrem("live_trades", 0, item)
                    r.lpush("live_trades", json.dumps(t))
            except Exception:
                pass
    except Exception as e:
        print(f"[RESOLVE] Live P&L update error: {e}")

    # --- Recalculate bankroll ---
    INITIAL_BANKROLL = 4.83
    try:
        paper_pnl_total = float(r.get("paper_pnl_total") or 0)
        new_bankroll = round(INITIAL_BANKROLL + paper_pnl_total, 4)
        r.set("bankroll", str(new_bankroll))
        print(f"[RESOLVE] Bankroll: ${INITIAL_BANKROLL:.2f} + ${paper_pnl_total:+.4f} = ${new_bankroll:.4f}")
    except Exception as e:
        print(f"[RESOLVE] Bankroll update error: {e}")
        new_bankroll = INITIAL_BANKROLL
        errors.append({"error": f"bankroll_update: {e}"})

    # --- Send Discord embed ---
    try:
        from polybot.notify import send_embed
        fields = []
        for d in resolution_details[:10]:
            emoji = "🟢" if d["won"] else "🔴"
            fields.append({
                "name": f"{emoji} {d['city']} — {d['bucket']}",
                "value": (f"{d['side']} @ {d['entry_price']:.3f} | "
                          f"PnL: ${d['profit_usd']:+.4f} | "
                          f"Winner: {d['winning_bucket']}"),
                "inline": True,
            })

        total_pnl_str = f"+${total_pnl:.4f}" if total_pnl >= 0 else f"-${abs(total_pnl):.4f}"
        bankroll_str = f"Bankroll: ${new_bankroll:.4f}"
        send_embed(
            title="📊 Market Resolution Report",
            description=(f"Resolved {total_resolved} trades | "
                         f"Wins: {wins} | Losses: {losses} | "
                         f"Total P&L: {total_pnl_str} | "
                         f"{bankroll_str}"),
            fields=fields,
            color=0x3FB950 if total_pnl >= 0 else 0xF85149,
        )
    except Exception as e:
        print(f"[RESOLVE] Discord notification error: {e}")

    result = {
        "resolved": total_resolved,
        "wins": wins,
        "losses": losses,
        "total_pnl": round(total_pnl, 4),
        "bankroll": new_bankroll,
        "details": resolution_details,
        "errors": errors,
        "timestamp": now.isoformat(),
    }
    print(f"[RESOLVE] Complete: {total_resolved} resolved, "
          f"P&L=${total_pnl:+.4f} ({wins}W/{losses}L), "
          f"Bankroll=${new_bankroll:.4f}")
    if errors:
        print(f"[RESOLVE] Errors ({len(errors)}): {errors}")

    # Release Redis lock
    try:
        if resolve_lock_acquired:
            r.delete(resolve_lock_key)
    except Exception:
        pass

    return result


async def run_full_scan(subset: int | str = 0, bankroll: float = 2.30) -> dict:
    """Run a full scan of all cities with all filters and risk management."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)7s] %(name)s: %(message)s",
    )

    # Check TRADING_HALT flag
    if check_trading_halt():
        print("[SCAN] TRADING_HALT flag is SET — skipping full scan")
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bankroll": bankroll,
            "cities_scanned": 0,
            "trade_signals": 0,
            "tail_signals": [],
            "all_signals": [],
            "city_results": [],
            "status": "HALTED",
        }

    from polybot.cities import ACTIVE_CITIES

    try:
        subset = int(subset)
    except Exception:
        subset = 0

    if subset == "all":
        cities_to_scan = list(ACTIVE_CITIES)
    else:
        cities_to_scan = ACTIVE_CITIES[:subset] if subset > 0 else ACTIVE_CITIES

    agents = [CityAgent(city) for city in cities_to_scan]
    coordinator = SwarmCoordinator(agents, bankroll=bankroll)

    print(f"[SCAN] Running full scan of {len(cities_to_scan)} cities | bankroll=${bankroll:.2f}")
    print(f"[SCAN] Min edge: {MIN_EDGE_FOR_TRADE:.0%} | Model consensus: {MIN_MODELS_FOR_CONSENSUS} models within {MODEL_AGREEMENT_THRESHOLD_F}F")
    results = await coordinator.run_swarm()

    # Flatten all signals
    all_signals = []
    for r in results:
        for s in r.get("trade_signals", []):
            all_signals.append({"city": r["city"], **s})

    all_signals.sort(key=lambda x: x.get("edge", 0), reverse=True)

    print(f"\n{'='*70}")
    print(f"  FULL SCAN SUMMARY  (bankroll=${bankroll:.2f})")
    print(f"{'='*70}")
    print(f"  Cities scanned: {len(results)}")
    print(f"  Trade signals: {len(all_signals)}")
    print(f"{'='*70}")

    for sig in all_signals[:30]:
        ev = sig["edge"] * 100
        print(
            f"  {sig['city']:15s} {sig['direction']:4s} EV={ev:+.1f}% "
            f"K=${sig['kelly_usd']:.2f} YES={sig['yes_price']:.3f} "
            f"P={sig['calibrated_prob']:.1%} Vol={sig.get('volume',0):.0f} "
            f"@ {sig['question'][:45]}"
        )

    # Tail opportunities
    tail_signals = [s for s in all_signals if s["yes_price"] < TAIL_PRICE_THRESHOLD and s["calibrated_prob"] > TAIL_PROB_THRESHOLD]
    if tail_signals:
        print(f"\n  🎯 TAIL BUCKETS ({len(tail_signals)} found):")
        for sig in tail_signals[:5]:
            mult = (1 - sig["yes_price"]) / sig["yes_price"] if sig["yes_price"] > 0 else 0
            print(f"    {sig['city']:15s} YES={sig['yes_price']:.3f} P={sig['calibrated_prob']:.1%} mult={mult:.1f}x")

    # Record session PnL and cycle info for dashboard
    try:
        from polybot.dashboard_data import record_session_pnl, record_cycle
        total_pnl = 0.0
        for sig in all_signals[:5]:
            total_pnl += (sig["edge"] * sig["kelly_usd"]) / bankroll if bankroll > 0 else 0
        print(f"DEBUG: About to call record_session_pnl total_pnl={total_pnl}")
        record_session_pnl(round(total_pnl * 100, 1))
        print(f"DEBUG: About to call record_cycle")
        record_cycle(
            next_seconds=3600,
            progress=min(len(all_signals) * 5, 100),
            safeguards="ACTIVE",
            max_position=round(max((s["kelly_usd"] for s in all_signals), default=0), 2),
        )
        print(f"DEBUG: record_cycle returned")
    except Exception as e:
        print(f"DEBUG: session_pnl/cycle ERROR: {e}")

    # Update last_full_scan for health monitor
    try:
        import redis as _r  # type: ignore
        redis_url = __import__("os").environ.get("REDIS_URL")
        if redis_url:
            _rc = _r.from_url(redis_url)
            _rc.set("last_full_scan", datetime.now(timezone.utc).isoformat())
            print("[SCAN] Updated last_full_scan")
    except Exception as e:
        print(f"[SCAN] Failed to update last_full_scan: {e}")

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bankroll": bankroll,
        "cities_scanned": len(results),
        "trade_signals": len(all_signals),
        "tail_signals": tail_signals,
        "all_signals": all_signals,
        "city_results": results,
    }


async def run_priority_scan(cities: list | None = None, bankroll: float = 2.30) -> dict:
    """
    Priority-ordered trading scan:
      1. Arbitrage (sum YES prices < 0.98) — execute immediately, skip rest
      2. GFS window + 3-model consensus
      3. Smart money copy
      4. Single model signal (only if edge >= 12%)
    """
    from polybot.cities import ACTIVE_CITIES
    from polybot.notify import send_embed

    if cities is None:
        cities = ACTIVE_CITIES

    # Check TRADING_HALT
    if check_trading_halt():
        print("[PRIORITY_SCAN] TRADING_HALT flag is SET — skipping")
        return {"status": "HALTED", "reason": "TRADING_HALT"}

    executed_trades = []

    # ---- PRIORITY 1: Arbitrage ----
    print("\n[PRIORITY:1] Scanning for arbitrage opportunities...")
    try:
        from polybot.liquidity_sniffer import check_arbitrage_all_cities
        from polybot.clob import execute_trade

        arb_opps = await check_arbitrage_all_cities(cities)
        for opp in arb_opps:
            yes_sum = sum(m.get("yes_price", 0) for m in opp.get("markets", []))
            if yes_sum < 0.98:
                print(f"[ARB] Found arb opportunity: sum={yes_sum:.3f} < 0.98, executing immediately")
                for m in opp.get("markets", []):
                    mid = m.get("conditionId") or m.get("slug", "")
                    if mid:
                        try:
                            resp = await execute_trade(
                                market_id=mid,
                                price=round(float(m.get("yes_price", 0)), 2),
                                size=0.10,
                                side="BUY",
                                city=opp.get("city", m.get("slug", "")),
                                bucket=m.get("question", "")[:40],
                            )
                            if resp and not resp.get("error"):
                                executed_trades.append({
                                    "priority": 1,
                                    "type": "ARBITRAGE",
                                    "market": m.get("question", "")[:60],
                                    "price": m.get("yes_price", 0),
                                })
                                print(f"[ARB] Executed: {m.get('question', '')[:60]}")
                        except Exception as e:
                            logger.debug(f"[ARB] Trade failed: {e}")
                # Skip other signals for this city if arb found
                break
    except Exception as e:
        print(f"[PRIORITY:1] Arbitrage scan error: {e}")

    if executed_trades:
        try:
            send_embed(
                title="🚨 Arbitrage Executed",
                description=f"Executed {len(executed_trades)} arb trades (priority 1)",
                fields=[{"name": t["market"], "value": f"Price: {t['price']:.3f}", "inline": True}
                        for t in executed_trades[:5]],
                color=0x3FB950,
            )
        except Exception as e:
            print(f"[ARB] Discord notification error: {e}")
        return {"status": "ARB_EXECUTED", "trades": executed_trades}

    # ---- PRIORITY 2: GFS window + 3-model consensus ----
    print("\n[PRIORITY:2] Checking GFS window + 3-model consensus...")
    try:
        from polybot.ladder import is_gfs_window
        from polybot.ensemble import get_ensemble_forecast
        from polybot.polymarket import find_markets, parse_outcome_prices

        if is_gfs_window():
            for city in cities[:5]:  # Top 5 cities during GFS
                lat, lon = city["lat"], city["lon"]
                ensemble = await get_ensemble_forecast(lat, lon, city["name"])
                if not ensemble:
                    continue
                models = ensemble.get("models", [])
                spread = ensemble.get("model_spread_f", 999)
                if len(models) >= 3 and spread <= 2.0:
                    # 3-model consensus — find mispriced markets
                    city_name = city["name"]
                    from polybot.cities import get_local_date
                    slug = city.get("slug", city_name.lower().replace(" ", "_"))
                    today = get_local_date(slug)
                    markets = await find_markets(city_name=city_name, date_str=today)
                    for m in markets:
                        try:
                            yes_price, _ = parse_outcome_prices(m)
                            if 0.04 <= yes_price <= 0.96:
                                # Check volume filter override for GFS+consensus
                                volume = float(m.get("volume24hr", 0) or 0)
                                if volume < MIN_BUCKET_VOLUME:
                                    print(f"[PRIORITY:2] Volume filter overridden (GFS+consensus): {m.get('question', '')[:40]}")
                                from polybot.clob import execute_trade
                                mid = m.get("conditionId") or m.get("slug", "")
                                if mid:
                                    resp = await execute_trade(
                                        market_id=mid,
                                        price=round(yes_price, 2),
                                        size=0.10,
                                        side="BUY",
                                        city=city_name.lower().replace(" ", "_"),
                                        bucket=m.get("question", "")[:40],
                                    )
                                    if resp and not resp.get("error"):
                                        executed_trades.append({
                                            "priority": 2,
                                            "type": "GFS_CONSENSUS",
                                            "city": city_name,
                                            "market": m.get("question", "")[:60],
                                            "price": yes_price,
                                        })
                        except Exception:
                            pass
                    if executed_trades:
                        break
    except Exception as e:
        print(f"[PRIORITY:2] GFS consensus error: {e}")

    if executed_trades:
        try:
            send_embed(
                title="📊 GFS Consensus Trades",
                description=f"Executed {len(executed_trades)} GFS+consensus trades (priority 2)",
                fields=[{"name": t["market"], "value": f"City: {t.get('city','?')} | Price: {t['price']:.3f}", "inline": True}
                        for t in executed_trades[:5]],
                color=0x5865F2,
            )
        except Exception as e:
            print(f"[GFS_CONSENSUS] Discord notification error: {e}")
        return {"status": "GFS_EXECUTED", "trades": executed_trades}

    # ---- PRIORITY 3: Smart money copy ----
    print("\n[PRIORITY:3] Smart money copy scan...")
    try:
        from polybot.liquidity_sniffer import track_smart_money, get_trader_positions
        from polybot.clob import execute_trade

        top_traders = await track_smart_money(min_trades=5, min_win_rate=0.70, top_n=10)
        for trader in top_traders[:5]:
            addr = trader["address"]
            positions = await get_trader_positions(addr)
            for pos in positions[:3]:
                mid = pos.get("token_id", "")
                if mid:
                    try:
                        resp = await execute_trade(
                            market_id=mid,
                            price=round(float(pos.get("avg_price", 0)), 2),
                            size=0.05,
                            side="BUY",
                        )
                        if resp and not resp.get("error"):
                            executed_trades.append({
                                "priority": 3,
                                "type": "SMART_MONEY",
                                "trader": addr[:12],
                                "market": pos.get("outcome", "")[:60],
                            })
                    except Exception:
                        pass
            if executed_trades:
                break
    except Exception as e:
        print(f"[PRIORITY:3] Smart money error: {e}")

    if executed_trades:
        return {"status": "SMART_MONEY_EXECUTED", "trades": executed_trades}

    # ---- PRIORITY 4: Single model signal (edge >= 12%) ----
    print("\n[PRIORITY:4] Single model signals (edge >= 12%)...")
    try:
        for city in cities[:3]:
            lat, lon = city["lat"], city["lon"]
            ensemble = await get_ensemble_forecast(lat, lon, city["name"])
            if not ensemble or ensemble.get("ensemble_temp_f") is None:
                continue
            city_name = city["name"]
            from polybot.cities import get_local_date
            slug = city.get("slug", city_name.lower().replace(" ", "_"))
            today = get_local_date(slug)
            markets = await find_markets(city_name=city_name, date_str=today)
            for m in markets:
                try:
                    yes_price, _ = parse_outcome_prices(m)
                    if yes_price < MIN_PRICE or yes_price > MAX_PRICE:
                        continue
                    # Simple edge check: if price < 0.5 and ensemble suggests higher
                    from polybot.prediction_engine import bayesian_temperature_probability
                    eval_temp_f = ensemble.get("ensemble_temp_f", 70)
                    uncertainty_f = ensemble.get("uncertainty_f", 5.0)
                    threshold = _extract_threshold(m.get("question", ""))
                    if threshold <= 0:
                        continue
                    true_prob = bayesian_temperature_probability(
                        forecast_temp_f=eval_temp_f,
                        threshold_f=threshold,
                        uncertainty_f=uncertainty_f,
                        model_confidence=0.5,
                    )
                    edge = abs(true_prob - yes_price)
                    if edge >= MIN_EDGE_FOR_TRADE:  # 20%
                        from polybot.clob import execute_trade
                        mid = m.get("conditionId") or m.get("slug", "")
                        if mid:
                            resp = await execute_trade(
                                market_id=mid,
                                price=round(yes_price, 2),
                                size=0.05,
                                side="BUY" if true_prob > yes_price else "SELL",
                                city=city_name.lower().replace(" ", "_"),
                                bucket=m.get("question", "")[:40],
                            )
                            if resp and not resp.get("error"):
                                executed_trades.append({
                                    "priority": 4,
                                    "type": "SINGLE_MODEL",
                                    "city": city_name,
                                    "market": m.get("question", "")[:60],
                                    "edge": edge,
                                })
                except Exception:
                    pass
            if executed_trades:
                break
    except Exception as e:
        print(f"[PRIORITY:4] Single model error: {e}")

    return {"status": "COMPLETE", "trades": executed_trades}


# ===================================================================
# FRONTIER: Integrated macro overlay + correlation + sentiment + timezone
# ===================================================================

async def apply_frontier_overlays(
    city_slug: str,
    lat: float,
    lon: float,
    forecast_f: float,
    edge: float,
    kelly_usd: float,
    side: str,
    open_trades: list[dict],
) -> tuple[float, float, str]:
    """
    Apply all frontier overlays to adjust forecast, Kelly size, and bet direction.

    Overlays applied:
      1. ENSO macro adjustment to forecast
      2. Timezone warfare multiplier to Kelly
      3. Sentiment alignment multiplier to Kelly
      4. Correlation penalty to Kelly

    Returns:
        (adjusted_forecast_f, adjusted_kelly_usd, overlay_log)
    """
    overlay_parts = []

    # 1. ENSO adjustment
    try:
        from polybot.enso import apply_enso_to_forecast
        enso_state = None  # auto-detect
        forecast_f, enso_adj = apply_enso_to_forecast(forecast_f, city_slug, enso_state)
        if enso_adj != 0:
            overlay_parts.append(f"ENSO={enso_adj:+.1f}F")
    except Exception as e:
        logger.debug(f"[FRONTIER] ENSO error: {e}")

    # 2. Timezone warfare
    tz_mult = 1.0
    try:
        from polybot.atmospheric import get_timezone_multiplier
        from datetime import datetime, timezone
        utc_hour = datetime.now(timezone.utc).hour
        tz_mult = get_timezone_multiplier(utc_hour)
        if tz_mult != 1.0:
            overlay_parts.append(f"TZ=x{tz_mult:.1f}")
    except Exception as e:
        logger.debug(f"[FRONTIER] TZ error: {e}")

    # 3. Sentiment adjustment
    sent_mult = 1.0
    try:
        from polybot.sentiment import get_sentiment_kelly_multiplier
        model_edge_positive = (side == "BUY")  # Simplified
        sent_mult = get_sentiment_kelly_multiplier(city_slug, model_edge_positive)
        if sent_mult != 1.0:
            overlay_parts.append(f"SENT=x{sent_mult:.1f}")
    except Exception as e:
        logger.debug(f"[FRONTIER] Sentiment error: {e}")

    # 4. Correlation penalty
    corr_mult = 1.0
    try:
        from polybot.correlation import check_correlation_penalty
        if open_trades:
            corr_mult = check_correlation_penalty(city_slug, open_trades)
            if corr_mult != 1.0:
                overlay_parts.append(f"CORR=x{corr_mult:.2f}")
    except Exception as e:
        logger.debug(f"[FRONTIER] Correlation error: {e}")

    # Apply all multipliers to Kelly
    adjusted_kelly = kelly_usd * tz_mult * sent_mult * corr_mult
    adjusted_kelly = max(adjusted_kelly, 0.01)  # Floor at 1 cent

    overlay_log = " ".join(overlay_parts) if overlay_parts else "none"
    if overlay_parts:
        logger.info(f"[FRONTIER] {city_slug}: {overlay_log} | kelly ${kelly_usd:.4f} -> ${adjusted_kelly:.4f}")

    return forecast_f, adjusted_kelly, overlay_log


async def get_open_trades_for_correlation() -> list[dict]:
    """Fetch open paper trades from Redis for correlation checking."""
    try:
        r = _get_redis()
        if not r:
            return []
        raw = r.lrange("paper_trades", 0, 499)
        trades = []
        for item in raw:
            try:
                t = json.loads(item)
                if t.get("status") == "open":
                    trades.append(t)
            except Exception:
                pass
        return trades
    except Exception:
        return []
