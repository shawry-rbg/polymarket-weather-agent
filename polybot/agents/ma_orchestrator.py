"""
Multi-Agent Orchestrator for intraday cron jobs — v3 with market-aware logic.

For each city:
1. Get next market info from Gamma API (cached in Redis)
2. If market is TODAY and OPEN → use live temp + today's forecast for rebalancing
3. If market is TOMTER or later → skip rebalancing (pre-market only)
4. If market is RESOLVED → move to next date
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import redis

logger = logging.getLogger(__name__)

_r: redis.Redis | None = None


def _get_redis() -> redis.Redis | None:
    global _r
    if _r is not None:
        return _r
    url = os.environ.get("REDIS_URL")
    if url:
        try:
            _r = redis.from_url(url)
            return _r
        except Exception:
            pass
    return None


# ----------------------------------------------------------------
# Agent-cycle city list: 12 most liquid cities only
# These are the cities that agent_cycle processes every 15 min.
# All other cities are processed by hourly_polybot.
# ----------------------------------------------------------------
AGENT_CYCLE_CITIES: list[dict] = [
    {"name": "Atlanta",        "slug": "atlanta",        "lat": 33.6407,  "lon": -84.4277},   # KATL
    {"name": "Dallas",         "slug": "dallas",         "lat": 32.8998,  "lon": -97.0403},   # KDFW
    {"name": "Cape Town",      "slug": "cape_town",      "lat": -33.9715, "lon": 18.6021},    # FACT
    {"name": "Mumbai",         "slug": "mumbai",         "lat": 19.0896,  "lon": 72.8656},    # VABB
    {"name": "Hong Kong",      "slug": "hong_kong",      "lat": 22.3080,  "lon": 113.9185},   # VHHH
    {"name": "Bangkok",        "slug": "bangkok",        "lat": 13.7367,  "lon": 100.5231},
    {"name": "Mexico City",    "slug": "mexico_city",    "lat": 19.4326,  "lon": -99.1332},
    {"name": "London",         "slug": "london",         "lat": 51.4700,  "lon": -0.4543},
    {"name": "New York",       "slug": "nyc",            "lat": 40.7772,  "lon": -73.8726},
    {"name": "Istanbul",       "slug": "istanbul",       "lat": 41.0082,  "lon": 28.9784},
    {"name": "Buenos Aires",   "slug": "buenos_aires",   "lat": -34.8222, "lon": -58.5358},
    {"name": "Lagos",          "slug": "lagos",          "lat": 6.5244,   "lon": 3.3792},
]


async def run_city_agent_cycle(city: dict, bankroll: float = 2.30) -> dict:
    """
    Run one agent cycle for a city with market-aware date logic.
    """
    name = city["name"]
    slug = city.get("slug", name.lower().replace(" ", "_"))
    lat = city["lat"]
    lon = city["lon"]

    print(f"[MA_ORCHESTRATOR] {name}: starting cycle")

    # Step 1: Get next market info (from Redis cache or Gamma API)
    market_info = None
    market_date = None
    market_is_open = False
    should_rebalance = False

    try:
        from polybot.polymarket import get_next_market
        market_info = await get_next_market(name)
        if market_info:
            market_date = market_info.get("date", "")
            market_is_open = market_info.get("is_open", False)

            # FIX: Double-check market is truly open by verifying end_date > now
            end_date_str = market_info.get("endDate", "")
            if end_date_str:
                try:
                    end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    now_utc = datetime.now(timezone.utc)
                    if end_dt <= now_utc:
                        market_is_open = False
                        print(f"[MA_ORCHESTRATOR] {name}: market end_date {end_date_str} <= now → CLOSED, skip")
                except (ValueError, TypeError):
                    pass

            # Determine if we should rebalance
            from polybot.cities import get_local_date
            today_local = get_local_date(slug, offset_days=0)

            if not market_is_open:
                should_rebalance = False
                print(f"[MA_ORCHESTRATOR] {name}: market CLOSED (is_open={market_is_open}) → skip rebalancing")
            elif market_date == today_local:
                should_rebalance = True
                print(f"[MA_ORCHESTRATOR] {name}: market TODAY ({market_date}) and OPEN → rebalancing")
            elif market_date > today_local:
                should_rebalance = False
                print(f"[MA_ORCHESTRATOR] {name}: market TOMORROW ({market_date}) → skip rebalancing")
            else:
                # Market date is in the past (resolved), try next
                should_rebalance = False
                print(f"[MA_ORCHESTRATOR] {name}: market {market_date} resolved/past → skip")
        else:
            print(f"[MA_ORCHESTRATOR] {name}: no market found → skip rebalancing")
    except Exception as e:
        logger.warning(f"[MA_ORCHESTRATOR] {name}: market info error: {e}")

    # Step 2: Get ensemble forecast (always, for logging)
    forecast_temp_f = None
    try:
        from polybot.ensemble import get_ensemble_forecast
        ensemble = await get_ensemble_forecast(lat, lon, city_name=name)
        forecast_temp_f = ensemble.get("ensemble_temp_f")
        print(f"[MA_ORCHESTRATOR] {name}: ensemble forecast = {forecast_temp_f}F")
    except Exception as e:
        logger.warning(f"[MA_ORCHESTRATOR] {name}: ensemble error: {e}")

    # Step 2b: Fetch GFS 31-member ensemble for dashboard and rebalancer
    gfs_data = None
    try:
        from polybot.gfs_ensemble import fetch_gfs_ensemble
        thresholds = [float(b) for b in city.get("buckets", [])]
        unit = city.get("unit", "C")
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cache_key = f"gfs_ensemble_cache:{slug}:{today_str}"

        # Check Redis cache first
        r_cache = _get_redis()
        cached_gfs = None
        if r_cache:
            try:
                cached_raw = r_cache.get(cache_key)
                if cached_raw:
                    import json as _json
                    cached_gfs = _json.loads(cached_raw.decode() if isinstance(cached_raw, bytes) else cached_raw)
                    print(f"[MA_ORCHESTRATOR] {name}: GFS cache hit ({cached_gfs.get('member_count', '?')} members)")
            except Exception:
                pass

        if cached_gfs:
            gfs_data = cached_gfs
        else:
            print(f"[MA_ORCHESTRATOR] {name}: fetching GFS 31-member ensemble...")
            gfs_data = await fetch_gfs_ensemble(lat, lon, thresholds=thresholds if thresholds else None, unit=unit)
            if gfs_data:
                # Cache in Redis
                if r_cache:
                    try:
                        import json as _json
                        r_cache.set(cache_key, _json.dumps(gfs_data, default=str), ex=3600)
                    except Exception:
                        pass
                # Also store temps in Redis list for rebalancer
                try:
                    from polybot.gfs_ensemble import ensemble_count_probabilities
                    temps = gfs_data.get("temps_by_member", [])
                    if temps:
                        r_gfs = redis.from_url(os.environ.get("REDIS_URL", ""))
                        gfs_key = f"gfs_ensemble:{slug}"
                        r_gfs.delete(gfs_key)
                        for t in temps:
                            r_gfs.rpush(gfs_key, str(t))
                        r_gfs.expire(gfs_key, 10800)
                        # Store summary metrics
                        r_gfs.hset(f"city_metrics:{slug}", mapping={
                            "gfs_ensemble_mean": str(gfs_data.get("ensemble_mean", "")),
                            "gfs_ensemble_std": str(gfs_data.get("ensemble_std", "")),
                            "gfs_ensemble_spread": str(gfs_data.get("ensemble_spread", "")),
                            "gfs_ensemble_min": str(gfs_data.get("ensemble_min", "")),
                            "gfs_ensemble_max": str(gfs_data.get("ensemble_max", "")),
                            "gfs_member_count": str(gfs_data.get("member_count", len(temps))),
                            "gfs_date": gfs_data.get("date", today_str),
                        })
                        r_gfs.expire(f"city_metrics:{slug}", 10800)
                        print(f"[MA_ORCHESTRATOR] {name}: GFS fetched OK — mean={gfs_data.get('ensemble_mean')}F "
                              f"spread={gfs_data.get('ensemble_spread')}F members={gfs_data.get('member_count')}")
                    else:
                        print(f"[MA_ORCHESTRATOR] {name}: GFS returned no member temps")
                except Exception as e2:
                    logger.warning(f"[MA_ORCHESTRATOR] {name}: GFS Redis write error: {e2}")
            else:
                print(f"[MA_ORCHESTRATOR] {name}: GFS fetch returned None")
    except Exception as e:
        logger.warning(f"[MA_ORCHESTRATOR] {name}: GFS ensemble error: {e}")

    # Step 3: Run rebalancer only if market is open today
    orders = []
    live_temp = None
    slope_f_per_5min = None
    local_hour_val = None
    in_peak = False

    if should_rebalance:
        try:
            from .rebalancer_agent import run_agent_cycle
            orders = run_agent_cycle(
                city_slug=slug,
                lat=lat,
                lon=lon,
                forecast_temp_f=forecast_temp_f if forecast_temp_f is not None else 70.0,
                bankroll=bankroll,
            )
            from .live_temp_agent import fetch_live_temp_cached
            live_temp, live_source = fetch_live_temp_cached(slug, lat, lon)
            # Debug log: source and value of live_temp
            now_ts = datetime.now(timezone.utc).isoformat()
            if live_temp is not None:
                print(f"[MA_ORCHESTRATOR][LIVE_TEMP] {name}: source={live_source} value={live_temp}F fetched_at={now_ts}")
            else:
                print(f"[MA_ORCHESTRATOR][LIVE_TEMP] {name}: source={live_source} value=None (no data) at={now_ts}")
        except Exception as e:
            logger.error(f"[MA_ORCHESTRATOR] {name}: agent cycle error: {e}")
    else:
        # Even when not rebalancing, fetch and cache live temp for dashboard
        try:
            from .live_temp_agent import fetch_live_temp_cached
            live_temp, live_source = fetch_live_temp_cached(slug, lat, lon)
            now_ts = datetime.now(timezone.utc).isoformat()
            if live_temp is not None:
                print(f"[MA_ORCHESTRATOR][LIVE_TEMP] {name}: source={live_source} value={live_temp}F fetched_at={now_ts}")
            else:
                print(f"[MA_ORCHESTRATOR][LIVE_TEMP] {name}: source={live_source} value=None (no data) at={now_ts}")
        except Exception:
            live_temp = None

    # Step 4: Calculate slope and time metrics
    try:
        from .time_agent import local_hour, is_peak_window, estimate_temperature_in_one_hour
        redis_url = os.environ.get("REDIS_URL", "")
        local_hour_val = local_hour(slug)
        in_peak = is_peak_window(slug)
        pred = estimate_temperature_in_one_hour(slug, redis_url)
        if pred is not None and live_temp is not None:
            slope_f_per_5min = round((pred - live_temp) / 12, 4)
        else:
            slope_f_per_5min = 0.0
    except Exception as e:
        logger.warning(f"[MA_ORCHESTRATOR] {name}: slope/time calc error: {e}")
        slope_f_per_5min = 0.0
        local_hour_val = local_hour_val or 0
        in_peak = False

    # Step 4b: Atmospheric corrections (wind/dew/cloud) with per-city exception isolation
    # Each correction is wrapped individually so one failure doesn't skip the rest.
    atmos_corrections = {}
    try:
        from polybot.atmospheric import (
            get_wind_correction, get_dew_point_suppression,
            get_cloud_correction, get_time_adjustment,
            log_corrections_to_redis,
        )

        wind_corr = 0.0
        dew_suppress = 0.0
        dew_kelly = 1.0
        cloud_corr = 0.0
        time_adj = {"edge_mult": 1.0, "kelly_mult": 1.0, "hours_to_close": 999, "zone": "unknown"}

        # Wind correction
        try:
            wind_corr = await get_wind_correction(slug, lat, lon)
            print(f"[ATMOS] Wind correction for {slug}: {wind_corr:+.2f}F")
        except Exception as e:
            print(f"[ATMOS] Wind correction failed for {slug}: {e}")
            wind_corr = 0.0

        # Dew point suppression
        try:
            dew_suppress, dew_kelly = await get_dew_point_suppression(lat, lon, forecast_temp_f or 70.0)
            print(f"[ATMOS] Dew point suppression for {slug}: {dew_suppress:+.1f}F (kelly={dew_kelly})")
        except Exception as e:
            print(f"[ATMOS] Dew point suppression failed for {slug}: {e}")
            dew_suppress = 0.0
            dew_kelly = 1.0

        # Cloud cover correction
        try:
            cloud_corr = await get_cloud_correction(lat, lon)
            print(f"[ATMOS] Cloud correction for {slug}: {cloud_corr:+.2f}F")
        except Exception as e:
            print(f"[ATMOS] Cloud correction failed for {slug}: {e}")
            cloud_corr = 0.0

        # Time-to-resolution adjustment
        try:
            market_end = market_info.get("endDate", "") if market_info else ""
            time_adj = get_time_adjustment(market_end if market_end else None)
            print(f"[ATMOS] Time adjustment for {slug}: zone={time_adj.get('zone','?')} hours={time_adj.get('hours_to_close','?')}")
        except Exception as e:
            print(f"[ATMOS] Time adjustment failed for {slug}: {e}")
            time_adj = {"edge_mult": 1.0, "kelly_mult": 1.0, "hours_to_close": 999, "zone": "error"}

        total_atmos = wind_corr + dew_suppress + cloud_corr
        atmos_corrections = {
            "wind_correction_f": wind_corr,
            "dew_point_suppression_f": dew_suppress,
            "dew_point_kelly_mult": dew_kelly,
            "cloud_correction_f": cloud_corr,
            "total_atmos_correction_f": round(total_atmos, 2),
            "time_adjustment": time_adj,
        }

        # Apply total correction to forecast
        if total_atmos != 0 and forecast_temp_f:
            forecast_temp_f = round(forecast_temp_f + total_atmos, 1)
            print(f"[MA_ORCHESTRATOR] {name}: atmos correction {total_atmos:+.2f}F -> forecast={forecast_temp_f}F")

        # Log to Redis
        try:
            log_corrections_to_redis(slug, atmos_corrections)
        except Exception as e:
            print(f"[ATMOS] Redis logging failed for {slug}: {e}")

    except Exception as e:
        logger.warning(f"[MA_ORCHESTRATOR] {name}: atmospheric import/outer error: {e}")
        atmos_corrections = {}

    # Step 5: Collect metrics for batch Redis write (done in run_all_cities_cycle)
    # Return metrics dict alongside result for batch processing
    metrics = {
        "slope_f_per_15min": str(slope_f_per_5min if slope_f_per_5min is not None else 0.0),
        "local_hour": str(local_hour_val if local_hour_val is not None else 0),
        "peak_window": "1" if in_peak else "0",
        "live_temp_f": str(live_temp if live_temp is not None else ""),
        "forecast_temp_f": str(forecast_temp_f if forecast_temp_f is not None else ""),
        "n_orders": str(len(orders)),
        "market_date": market_date or "",
        "market_open": "1" if market_is_open else "0",
        "resolve_time": market_info.get("resolve_time_local", "") if market_info else "",
        "updated": datetime.now(timezone.utc).isoformat(),
    }

    # Add atmospheric correction fields to metrics (safe, won't overwrite GFS fields)
    if atmos_corrections:
        try:
            metrics["atmos_wind"] = str(atmos_corrections.get("wind_correction_f", 0))
            metrics["atmos_dew"] = str(atmos_corrections.get("dew_point_suppression_f", 0))
            metrics["atmos_cloud"] = str(atmos_corrections.get("cloud_correction_f", 0))
            metrics["atmos_total"] = str(atmos_corrections.get("total_atmos_correction_f", 0))
            time_adj = atmos_corrections.get("time_adjustment", {})
            if time_adj:
                metrics["time_zone"] = str(time_adj.get("zone", ""))
                metrics["time_hours"] = str(time_adj.get("hours_to_close", ""))
        except Exception as e:
            print(f"[MA_ORCHESTRATOR] {name}: atmos metrics extraction error: {e}")

    result = {
        "city": name,
        "slug": slug,
        "live_temp_f": live_temp,
        "forecast_temp_f": forecast_temp_f,
        "orders": orders,
        "n_orders": len(orders),
        "market_date": market_date,
        "market_is_open": market_is_open,
        "should_rebalance": should_rebalance,
        "slope_f_per_5min": slope_f_per_5min,
        "local_hour": local_hour_val,
        "in_peak": in_peak,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # Include metrics for batch Redis write
        "_metrics": metrics,
    }
    print(f"[MA_ORCHESTRATOR] {name}: cycle complete, {len(orders)} orders, market={market_date}, open={market_is_open}")
    return result


async def run_all_cities_cycle(bankroll: float = 2.30, max_cities: int | None = None,
                                agent_cycle_only: bool = False) -> dict:
    """Run agent cycle for all active cities concurrently."""
    from polybot.orchestrator import LIVE_MODE
    if agent_cycle_only:
        cities = AGENT_CYCLE_CITIES[:max_cities] if max_cities else AGENT_CYCLE_CITIES
    else:
        from polybot.cities import ACTIVE_CITIES
        cities = ACTIVE_CITIES[:max_cities] if max_cities else ACTIVE_CITIES
    print(f"[MA_ORCHESTRATOR] Starting multi-agent cycle for {len(cities)} cities")

    tasks = [run_city_agent_cycle(city, bankroll) for city in cities]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    city_results = []
    errors = []
    total_orders = 0
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            errors.append({"city": cities[i]["name"], "error": str(r)})
        else:
            city_results.append(r)
            total_orders += r.get("n_orders", 0)

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "n_cities": len(cities),
        "n_errors": len(errors),
        "total_orders": total_orders,
        "errors": errors,
        "city_results": city_results,
    }

    # Batch write all city metrics to Redis in a single pipeline
    # Also execute live trades for generated orders
    r = _get_redis()
    if r:
        try:
            pipe = r.pipeline()
            for cr in city_results:
                slug = cr.get("slug", "")
                metrics = cr.get("_metrics", {})
                if slug and metrics:
                    key = f"city_metrics:{slug}"
                    pipe.hset(key, mapping={k: str(v) for k, v in metrics.items()})
                    pipe.expire(key, 10800)

                # Also write city:* key for dashboard SKY/TREND/MARKET/STATUS/TIME display
                city_key = f"city:{slug}"
                live_temp = cr.get("live_temp_f")
                forecast = cr.get("forecast_temp_f")
                slope = cr.get("slope_f_per_5min")
                in_peak = cr.get("in_peak", False)
                market_date = cr.get("market_date", "")
                market_open = cr.get("market_is_open", False)
                local_h = cr.get("local_hour", 0)

                # Build sky description from atmospheric corrections
                sky_desc = "N/A"
                atmos = cr.get("_metrics", {})
                if atmos:
                    cloud_val = atmos.get("atmos_cloud", "0")
                    try:
                        cloud_f = float(cloud_val)
                        if cloud_f < -1.0:
                            sky_desc = "Overcast"
                        elif cloud_f < -0.1:
                            sky_desc = "Cloudy"
                        else:
                            sky_desc = "Clear"
                    except (ValueError, TypeError):
                        sky_desc = "N/A"

                # Build trend arrow from slope
                trend_desc = "N/A"
                if slope is not None:
                    try:
                        s = float(slope)
                        if s > 0.05:
                            trend_desc = "Warming"
                        elif s < -0.05:
                            trend_desc = "Cooling"
                        else:
                            trend_desc = "Steady"
                    except (ValueError, TypeError):
                        trend_desc = "N/A"

                # Market status
                if market_open:
                    mkt_status = "OPEN"
                elif market_date:
                    mkt_status = "CLOSED"
                else:
                    mkt_status = "N/A"

                # Resolve time
                resolve_t = cr.get("resolve_time", "") or "N/A"

                pipe.hset(city_key, mapping={
                    "live_temp": str(live_temp) if live_temp is not None else "N/A",
                    "forecast_temp": str(forecast) if forecast is not None else "N/A",
                    "sky": sky_desc,
                    "trend": trend_desc,
                    "slope": str(slope) if slope is not None else "N/A",
                    "peak": "1" if in_peak else "0",
                    "market_date": str(market_date) if market_date else "N/A",
                    "market_status": mkt_status,
                    "resolve_time": resolve_t,
                    "local_hour": str(local_h) if local_h else "N/A",
                    "updated": datetime.now(timezone.utc).isoformat(),
                })
                pipe.expire(city_key, 10800)

                # Execute live trades for this city's orders
                if LIVE_MODE:
                    orders = cr.get("orders", [])
                    if orders:
                        from polybot.clob import execute_trade, get_usdc_balance
                        from polybot.polymarket import find_markets
                        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                        usdc_bal = get_usdc_balance()
                        try:
                            tomorrow = (_dt.now(_tz.utc) + _td(days=1)).strftime("%B %-d")
                            city_name = cr.get("city", slug)
                            markets_data = await find_markets(city_name=city_name, date_str=tomorrow)
                        except Exception:
                            markets_data = []
                        bucket_to_cid = {}
                        for m in markets_data:
                            threshold = str(m.get("threshold_f", ""))
                            cid = m.get("conditionId", "")
                            if threshold and cid:
                                bucket_to_cid[threshold] = cid
                        for o in orders:
                            price_val = float(o.get("price", 0))
                            if price_val < 0.02 or price_val > 0.98:
                                continue
                            size_val = min(bankroll * 0.02, 0.10)
                            if size_val <= 0 or usdc_bal < size_val + 0.50:
                                break
                            action = o.get("action", "BUY")
                            side = "YES" if action in ("BUY", "YES") else "NO"
                            condition_id = o.get("conditionId", "")
                            if not condition_id:
                                condition_id = bucket_to_cid.get(o.get("bucket", ""), "")
                            if not condition_id:
                                continue
                            try:
                                trade_result = await execute_trade(
                                    market_id=condition_id,
                                    side=side,
                                    price=price_val,
                                    size=round(size_val, 4),
                                    trade_log_path="/polybot-data/live_trades.jsonl",
                                    city=slug,
                                    bucket=o.get("bucket", ""),
                                )
                                if trade_result and trade_result.get("order_id"):
                                    print(f"[MA_ORDERS] LIVE: {action} {o.get('bucket')} @ {price_val:.4f} id={trade_result['order_id']}")
                                    usdc_bal -= size_val
                            except Exception as te:
                                print(f"[MA_ORDERS] Error: {te}")

            pipe.set("ma_orchestrator:latest", json.dumps(summary, default=str), ex=3600)
            pipe.execute()
        except Exception as e:
            logger.warning(f"[MA_ORCHESTRATOR] Batch Redis write error: {e}")

    print(f"[MA_ORCHESTRATOR] Complete: {total_orders} orders, {len(errors)} errors")
    return summary


def run_all_cities_cycle_sync(bankroll: float = 2.30, max_cities: int | None = None,
                              agent_cycle_only: bool = False) -> dict:
    """Synchronous wrapper for Modal cron entry points."""
    return asyncio.run(run_all_cities_cycle(bankroll=bankroll, max_cities=max_cities,
                                             agent_cycle_only=agent_cycle_only))
