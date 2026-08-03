"""
Intraday Rebalancing Agent — v2 with Speed & Slope intelligence.

Three trading triggers:
  1. DEVIATION: |live_temp - forecast| > threshold (standard rebalancing)
  2. SPEED (bucket-crossing): live_temp within 0.9°F of a bucket boundary
  3. SLOPE (time-aware): during peak windows, if predicted temp crosses a bucket

All orders include a "reason" field: "DEVIATION", "SPEED", or "SLOPE".

Usage:
    from polybot.agents.rebalancer_agent import rebalance_if_needed, run_agent_cycle
"""

import json
import math
import os
import asyncio
from datetime import datetime, timedelta

from .live_temp_agent import check_rebalancing_trigger, crossing_imminent, fetch_live_temp_cached, fetch_live_temp_openmeteo
from .time_agent import is_peak_window, estimate_temperature_in_one_hour


# Module-level Redis connection (lazy, reused)
_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    url = os.environ.get("REDIS_URL")
    if url:
        try:
            import redis as _r
            _redis_client = _r.from_url(url)
            return _redis_client
        except Exception:
            pass
    return None

# Standard bucket edges (Fahrenheit) for NYC-style markets
BUCKET_EDGES = [65, 66, 68, 70, 72, 74, 76, 78, 80, 82, 84, 200]
BUCKET_NAMES = [
    "65F or below", "66-67F", "68-69F", "70-71F", "72-73F",
    "74-75F", "76-77F", "78-79F", "80-81F", "82-83F", "84F or higher",
]
DEFAULT_STD_F = 2.5  # Fallback standard deviation for temperature distribution

# Liquidity filter constants
MIN_BUCKET_VOLUME = 500       # Skip if market volume < $500
MIN_BUCKET_PRICE = 0.04       # Skip if price < 4 cents
MAX_TAIL_ALLOCATION = 0.30    # Max 30% of portfolio on tail buckets

# Dynamic std lookup
def _get_dynamic_std(city_slug: str) -> float:
    """Get per-city, per-hour dynamic standard deviation."""
    try:
        from polybot.calibration_dynamic import get_city_hour_std
        from .time_agent import local_hour
        hour = local_hour(city_slug)
        return get_city_hour_std(city_slug, hour)
    except Exception:
        return DEFAULT_STD_F


def _get_gfs_temps_from_redis(city_slug: str) -> list[float] | None:
    """
    Retrieve GFS ensemble member temperatures from Redis.
    The orchestrator stores these after fetching from the ensemble API.
    
    Returns:
        List of daily max temps (F) per ensemble member, or None if not available.
    """
    try:
        import redis as _redis_mod
        r = _redis_mod.from_url(os.environ.get("REDIS_URL", ""))
        if not r:
            return None
        # Check for gfs_ensemble hash stored by orchestrator
        key = f"gfs_ensemble:{city_slug}"
        if r.exists(key):
            raw = r.lrange(key, 0, -1)
            temps = []
            for item in raw:
                val = item.decode() if isinstance(item, bytes) else str(item)
                try:
                    temps.append(float(val))
                except ValueError:
                    pass
            if temps:
                return temps
        # Fallback: check city_metrics for ensemble_member_count
        # If present, we had a successful ensemble fetch
        metrics_raw = r.hget(f"city_metrics:{city_slug}", "gfs_member_count")
        if metrics_raw:
            count = int(float(metrics_raw.decode() if isinstance(metrics_raw, bytes) else metrics_raw))
            if count >= 2:
                # Ensemble was fetched but temps not stored separately
                # Signal that ensemble is available
                return None  # Will fall back to Gaussian in rebalancer
    except Exception:
        pass
    return None


def _extract_threshold_from_question(question: str) -> float:
    """Extract temperature threshold (F) from a market question string."""
    import re
    q = question.lower()
    # First try Fahrenheit patterns
    f_patterns = [
        r"(\d+)\s*°\s*f",
        r"(\d+)\s*[°]?\s*f(?:ahrenheit)?\b",
        r"(\d+)\s*degrees?\s*f",
    ]
    for pat in f_patterns:
        m = re.search(pat, q)
        if m:
            return round(float(m.group(1)), 1)

    # Then try Celsius patterns and convert
    c_patterns = [
        r"(\d+)\s*°\s*c",
        r"(\d+)\s*c(?:elsius)?\b",
    ]
    for pat in c_patterns:
        m = re.search(pat, q)
        if m:
            c_val = float(m.group(1))
            return round(c_val * 9 / 5 + 32, 1)

    # Generic fallback: any number near "exceed/above/over"
    gen_patterns = [
        r"(?:exceed|above|over|higher than)\s+(\d+)",
        r"at\s+least\s+(\d+)",
        r"be\s+(\d+)",
    ]
    for pat in gen_patterns:
        m = re.search(pat, q)
        if m:
            val = float(m.group(1))
            if "celsius" in q or "°c" in q:
                if val < 60:
                    val = val * 9 / 5 + 32
            return round(val, 1)

    return 0.0


def _bucket_probabilities(live_temp_f: float, std_f: float = None, city_slug: str = "",
                         gfs_temps_by_member: list[float] | None = None) -> dict[str, float]:
    """
    Compute bucket probabilities.
    
    If GFS ensemble member temps are provided, use true 31-member counting
    instead of the Gaussian approximation.
    
    Args:
        live_temp_f: Live temperature (F) — used for fallback Gaussian
        std_f: Standard deviation (F) — used for fallback Gaussian
        city_slug: City identifier for dynamic std lookup
        gfs_temps_by_member: If provided, list of daily max temps per ensemble member (F)
    """
    # TRUE ENSEMBLE: Count members in each bucket
    if gfs_temps_by_member and len(gfs_temps_by_member) > 0:
        return _ensemble_bucket_count(gfs_temps_by_member)

    # FALLBACK: Gaussian approximation centered on live_temp_f
    if std_f is None:
        std_f = _get_dynamic_std(city_slug) if city_slug else DEFAULT_STD_F
    probs = {}
    for i in range(len(BUCKET_EDGES) - 1):
        low = BUCKET_EDGES[i]
        high = BUCKET_EDGES[i + 1]
        if high == 200:
            prob = 0.5 * (1 - math.erf((low - live_temp_f) / (std_f * math.sqrt(2))))
        else:
            prob = 0.5 * (
                math.erf((high - live_temp_f) / (std_f * math.sqrt(2)))
                - math.erf((low - live_temp_f) / (std_f * math.sqrt(2)))
            )
        probs[BUCKET_NAMES[i]] = max(0.0, float(prob))

    total = sum(probs.values())
    if total > 0:
        probs = {k: v / total for k, v in probs.items()}
    return probs


def _ensemble_bucket_count(temps_by_member: list[float]) -> dict[str, float]:
    """
    Compute bucket probabilities by counting ensemble members in each bucket.
    This replaces the Gaussian approximation with true Monte Carlo counting.
    
    Args:
        temps_by_member: Daily max temps (F) for each ensemble member
        
    Returns:
        {bucket_name: probability} where probability = members_in_bucket / total_members
    """
    if not temps_by_member:
        return {bn: 1.0 / len(BUCKET_NAMES) for bn in BUCKET_NAMES}

    n = len(temps_by_member)
    probs = {}
    for i in range(len(BUCKET_EDGES) - 1):
        low = BUCKET_EDGES[i]
        high = BUCKET_EDGES[i + 1]
        high_val = float(high)
        if high_val >= 9999:
            count = sum(1 for t in temps_by_member if t >= low)
        else:
            count = sum(1 for t in temps_by_member if low <= t < high)
        bn = BUCKET_NAMES[i] if i < len(BUCKET_NAMES) else f"{low}-{high}F"
        probs[bn] = round(count / n, 4)
    return probs


def _fetch_market_prices(city_slug: str, target_date_str: str) -> dict[str, float]:
    """
    Fetch current market prices for all of a city's bucket markets.

    Returns:
        Dict of {bucket_name: yes_price}
    """
    market_price_map = {}

    # Primary: find_markets via Polymarket API
    try:
        from polybot.polymarket import find_markets, parse_outcome_prices
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()
            markets = asyncio.get_event_loop().run_until_complete(
                find_markets(city_name=city_slug, date_str=target_date_str)
            )
        else:
            markets = asyncio.run(
                find_markets(city_name=city_slug, date_str=target_date_str)
            )

        if markets:
            for m in markets:
                question = m.get("question", "")
                threshold_f = _extract_threshold_from_question(question)
                try:
                    yes_price, _ = parse_outcome_prices(m)
                except Exception:
                    continue

                # Map to nearest bucket by threshold edge proximity
                best_bucket = None
                best_dist = float("inf")
                for i, bn in enumerate(BUCKET_NAMES):
                    edge = BUCKET_EDGES[i]
                    dist = abs(threshold_f - edge)
                    if dist < best_dist:
                        best_dist = dist
                        best_bucket = bn

                if best_bucket and best_bucket not in market_price_map:
                    market_price_map[best_bucket] = yes_price
            print(f"[REBALANCER] {city_slug}: Found {len(market_price_map)} market prices via find_markets")
            for bn, p in market_price_map.items():
                print(f"  {bn}: yes_price={p:.4f}")
        else:
            print(f"[REBALANCER] {city_slug}: find_markets returned no results")
    except Exception as e:
        print(f"[REBALANCER] {city_slug}: Error fetching markets: {e}")

    # Fallback: cached Redis prices
    if not market_price_map:
        try:
            import redis as _redis
            r = _redis.from_url(os.environ.get("REDIS_URL", ""))
            cached_prices = r.hgetall(f"market_prices:{city_slug}")
            if cached_prices:
                for k, v in cached_prices.items():
                    bk = k.decode() if isinstance(k, bytes) else str(k)
                    vv = v.decode() if isinstance(v, bytes) else str(v)
                    try:
                        market_price_map[bk] = float(vv)
                    except ValueError:
                        pass
                print(f"[REBALANCER] {city_slug}: Using {len(market_price_map)} cached Redis prices")
        except Exception:
            pass

    return market_price_map


def rebalance_if_needed(
    city_slug: str,
    lat: float,
    lon: float,
    forecast_temp_f: float,
    bankroll: float = 2.30,
    live_temp: float | None = None,
    threshold: float = 1.5,
    edge_threshold: float = 0.05,
) -> list[dict]:
    """
    Check if rebalancing is needed and generate orders using three strategies:
    1. DEVIATION: live vs forecast divergence
    2. SPEED: bucket-crossing imminent (within 0.9°F of threshold)
    3. SLOPE: pre-emptive BUY during peak window if temperature rising

    Args:
        city_slug: City identifier
        lat, lon: City coordinates
        forecast_temp_f: Current forecast temperature in Fahrenheit
        bankroll: Available bankroll in USDC
        live_temp: Override live temp (if None, fetches from Open-Meteo)
        threshold: Deviation threshold (F) to trigger standard rebalance
        edge_threshold: Minimum edge to generate a DEVIATION trade order

    Returns:
        List of order dicts with "reason" field: "SPEED", "SLOPE", or "DEVIATION"
    """
    # ----------------------------------------------------------------
    # Step 1: Get live temperature (with freshness check)
    # ----------------------------------------------------------------
    live_source = "none"
    if live_temp is None:
        live_temp, live_source = fetch_live_temp_cached(city_slug, lat, lon)
    else:
        live_source = "override"
    if live_temp is None:
        print(f"[REBALANCER] {city_slug}: No live temp available, skipping")
        return []
    print(f"[REBALANCER] {city_slug}: live_temp={live_temp}F (source={live_source})")

    redis_url = os.environ.get("REDIS_URL", "")
    orders = []

    # ----------------------------------------------------------------
    # TRIGGER 1: SPEED — bucket crossing detection
    # ----------------------------------------------------------------
    try:
        from polybot.cities import BUCKET_THRESHOLDS_F
        city_buckets_f = BUCKET_THRESHOLDS_F.get(city_slug, [])
    except ImportError:
        city_buckets_f = []

    if city_buckets_f:
        bucket_out, bucket_in = crossing_imminent(city_slug, live_temp, city_buckets_f)
        if bucket_out and bucket_in:
            # Fetch market prices for both buckets to validate the trade
            target_date_str = datetime.now().strftime("%Y-%m-%d")
            price_map = _fetch_market_prices(city_slug, target_date_str)
            out_price = price_map.get(bucket_out, 0.0)
            in_price = price_map.get(bucket_in, 0.0)

            # Skip SELL if the bucket being exited has price > 0.85 (not worth selling)
            if out_price > 0.85:
                print(f"[REBALANCER] {city_slug}: SPEED trigger skipped — {bucket_out} price {out_price:.3f} > 0.85")
                # Still generate BUY for the entering bucket
                orders.append({
                    "action": "BUY",
                    "city": city_slug,
                    "bucket": bucket_in,
                    "edge": 0.0,
                    "prob": 0.0,
                    "price": in_price,
                    "live_temp_f": live_temp,
                    "forecast_temp_f": forecast_temp_f,
                    "timestamp": datetime.now().isoformat(),
                    "reason": "SPEED",
                })
                print(f"[REBALANCER] {city_slug}: SPEED trigger (BUY only) — BUY {bucket_in} @ {in_price:.3f}")
                return orders

            orders.append({
                "action": "SELL",
                "city": city_slug,
                "bucket": bucket_out,
                "edge": 0.0,
                "prob": 0.0,
                "price": out_price,
                "live_temp_f": live_temp,
                "forecast_temp_f": forecast_temp_f,
                "timestamp": datetime.now().isoformat(),
                "reason": "SPEED",
            })
            orders.append({
                "action": "BUY",
                "city": city_slug,
                "bucket": bucket_in,
                "edge": 0.0,
                "prob": 0.0,
                "price": 0.0,
                "live_temp_f": live_temp,
                "forecast_temp_f": forecast_temp_f,
                "timestamp": datetime.now().isoformat(),
                "reason": "SPEED",
            })
            print(f"[REBALANCER] {city_slug}: SPEED trigger — SELL {bucket_out}, BUY {bucket_in}")
            return orders

    # ----------------------------------------------------------------
    # Step 2: Standard deviation check
    # ----------------------------------------------------------------
    deviation = abs(live_temp - forecast_temp_f)
    if deviation <= threshold:
        print(f"[REBALANCER] {city_slug}: deviation={deviation:.1f}F <= threshold={threshold}F, no rebalance")
        return []

    print(f"[REBALANCER] {city_slug}: deviation={deviation:.1f}F > threshold={threshold}F, computing orders...")

    # ----------------------------------------------------------------
    # TRIGGER 2: SLOPE — pre-emptive BUY during peak window
    # ----------------------------------------------------------------
    if is_peak_window(city_slug):
        pred_temp = estimate_temperature_in_one_hour(city_slug, redis_url)
        if pred_temp and pred_temp > live_temp + 0.9:
            # Predicted temp is rising significantly — find the bucket it would hit
            target_bucket = f"{round(pred_temp)}F"
            print(f"[REBALANCER] {city_slug}: SLOPE trigger — pred_1h={pred_temp:.1f}F > live={live_temp:.1f}F, target={target_bucket}")

            # Fetch market price for the target bucket
            target_date_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            price_map = _fetch_market_prices(city_slug, target_date_str)
            price = price_map.get(target_bucket)

            if price is not None and price < 0.20:
                orders.append({
                    "action": "BUY",
                    "city": city_slug,
                    "bucket": target_bucket,
                    "edge": 0.50,
                    "prob": 0.0,
                    "price": price,
                    "live_temp_f": live_temp,
                    "forecast_temp_f": forecast_temp_f,
                    "timestamp": datetime.now().isoformat(),
                    "reason": "SLOPE",
                })
                print(f"[REBALANCER] {city_slug}: Adding SLOPE order — BUY {target_bucket} @ {price:.4f}")
            else:
                print(f"[REBALANCER] {city_slug}: SLOPE — price={price} (not < 0.20), skipping pre-emptive order")

    # ----------------------------------------------------------------
    # Step 3: Compute bucket probabilities (ensemble count > Gaussian fallback)
    # ----------------------------------------------------------------
    # Try to get GFS ensemble temps from Redis for true counting
    gfs_temps = _get_gfs_temps_from_redis(city_slug)
    probs = _bucket_probabilities(live_temp, city_slug=city_slug, gfs_temps_by_member=gfs_temps)
    sorted_probs = sorted(probs.items(), key=lambda x: x[1], reverse=True)

    print(f"[REBALANCER] {city_slug}: Top 3 bucket probabilities (live_temp={live_temp}F):")
    for bucket_name, prob in sorted_probs[:3]:
        print(f"  {bucket_name}: P={prob:.4f}")

    # ----------------------------------------------------------------
    # Step 4: Fetch market prices (DEVIATION trigger)
    # ----------------------------------------------------------------
    target_date_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"[REBALANCER] {city_slug}: target_date_str={target_date_str}")
    market_price_map = _fetch_market_prices(city_slug, target_date_str)

    # ----------------------------------------------------------------
    # DEBUG: print price + edge for top 3 buckets
    # ----------------------------------------------------------------
    print(f"[REBALANCER] {city_slug}: Price / Edge analysis for top 3 buckets:")
    for bucket_name, prob in sorted_probs[:3]:
        price = market_price_map.get(bucket_name)
        if price is None:
            print(f"  {bucket_name}: price=No market price, prob={prob:.4f}, edge=N/A")
        else:
            edge = prob - price
            flag = "Edge sufficient, adding order" if edge > edge_threshold else ""
            print(f"  {bucket_name}: price={price:.4f}, prob={prob:.4f}, edge={edge:.4f} {flag}")

    # ----------------------------------------------------------------
    # TRIGGER 3: DEVIATION — generate BUY/SELL orders for mispriced buckets
    # with liquidity filter
    # ----------------------------------------------------------------
    # Compute current tail allocation from Redis
    tail_alloc_pct = 0.0
    try:
        r_tmp = _get_redis()
        if r_tmp:
            tail_usd = float(r_tmp.get("tail_allocation_usd") or 0)
            total_usd = float(r_tmp.get("total_allocation_usd") or bankroll)
            if total_usd > 0:
                tail_alloc_pct = tail_usd / total_usd
    except Exception:
        pass

    for bucket, prob in probs.items():
        price = market_price_map.get(bucket)
        if price is None or price <= 0.0:
            print(f"[REBALANCER] Skipping {bucket}: invalid price {price}")
            continue
        if price < 0.001:
            print(f"[REBALANCER] Skipping {bucket}: price {price} too low (illiquid)")
            continue

        # --- Liquidity filter ---
        # Skip if price < MIN_BUCKET_PRICE
        if price < MIN_BUCKET_PRICE:
            print(f"[REBALANCER] Skipping {bucket}: price {price:.4f} < MIN_BUCKET_PRICE {MIN_BUCKET_PRICE}")
            continue

        # Skip tail buckets if allocation exceeded
        is_tail_bucket = price < 0.10
        if is_tail_bucket and tail_alloc_pct >= MAX_TAIL_ALLOCATION:
            print(f"[REBALANCER] Skipping tail {bucket}: tail allocation {tail_alloc_pct:.0%} >= max {MAX_TAIL_ALLOCATION:.0%}")
            continue

        edge = prob - price
        if edge > edge_threshold:
            orders.append({
                "action": "BUY",
                "city": city_slug,
                "bucket": bucket,
                "edge": round(edge, 4),
                "prob": round(prob, 4),
                "price": round(price, 4),
                "live_temp_f": live_temp,
                "forecast_temp_f": forecast_temp_f,
                "timestamp": datetime.now().isoformat(),
                "reason": "DEVIATION",
            })
        elif edge < -edge_threshold:
            # SELL: only when market_price is significantly ABOVE our probability
            # Sanity checks to avoid erroneous SELL orders:
            # 1. Skip if price > 0.85 for non-top buckets (market is pricing it too high to sell into)
            # 2. Skip if price > 0.95 for any bucket (already highly priced)
            # 3. Skip if abs(prob - price) < 0.05 (too close to call)
            # 4. Skip if prob > 0.9 (we think it's very likely, don't sell)
            # Determine if this is the top bucket
            is_top_bucket = bucket == BUCKET_NAMES[-1]  # "84F or higher"
            if not is_top_bucket and price > 0.85:
                print(f"[REBALANCER] Skipping SELL {bucket}: price {price:.3f} > 0.85 for non-top bucket")
                continue
            if price > 0.95:
                print(f"[REBALANCER] Skipping SELL {bucket}: price {price:.3f} > 0.95 (already highly priced)")
                continue
            if abs(prob - price) < 0.05:
                print(f"[REBALANCER] Skipping SELL {bucket}: |prob-price|={abs(prob-price):.3f} < 0.05 (too close)")
                continue
            if prob > 0.90:
                print(f"[REBALANCER] Skipping SELL {bucket}: prob={prob:.3f} > 0.90 (we think it's likely)")
                continue
            # Valid SELL: market is overpriced relative to our model
            orders.append({
                "action": "SELL",
                "city": city_slug,
                "bucket": bucket,
                "edge": round(abs(edge), 4),
                "prob": round(prob, 4),
                "price": round(price, 4),
                "live_temp_f": live_temp,
                "forecast_temp_f": forecast_temp_f,
                "timestamp": datetime.now().isoformat(),
                "reason": "DEVIATION",
            })

    if orders:
        print(f"[REBALANCER] {city_slug}: {len(orders)} total orders generated:")
        for o in orders:
            print(f"  [{o['reason']}] {o['action']} {o['bucket']}")
    else:
        print(f"[REBALANCER] {city_slug}: No orders generated")

    # ----------------------------------------------------------------
    # Step 5: Execute live trades via Polymarket CLOB
    # ----------------------------------------------------------------
    if orders:
        from polybot.orchestrator import LIVE_MODE
        if LIVE_MODE:
            from polybot.clob import execute_trade, get_usdc_balance
            from polybot.polymarket import find_markets
            from datetime import datetime, timezone, timedelta
            usdc_bal = get_usdc_balance()
            # Fetch markets once for conditionId lookup
            try:
                tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%B %-d")
                markets = asyncio.run(find_markets(city_name=city_slug, date_str=tomorrow))
            except Exception:
                markets = []
            # Build bucket->conditionId map from markets
            bucket_to_cid = {}
            for m in markets:
                threshold = str(m.get("threshold_f", ""))
                cid = m.get("conditionId", "")
                if threshold and cid:
                    bucket_to_cid[threshold] = cid
            for o in orders:
                price_val = float(o.get("price", 0))
                if price_val < 0.02 or price_val > 0.98:
                    continue
                size_val = min(bankroll * 0.02, 0.10)
                if size_val <= 0:
                    continue
                action = o.get("action", "BUY")
                side = "YES" if action in ("BUY", "YES") else "NO"
                condition_id = o.get("conditionId", "")
                if not condition_id:
                    # Try to resolve from bucket
                    bucket_key = o.get("bucket", "")
                    condition_id = bucket_to_cid.get(bucket_key, "")
                if not condition_id:
                    continue
                if usdc_bal < size_val + 0.50:
                    break
                try:
                    trade_result = asyncio.run(execute_trade(
                        market_id=condition_id,
                        side=side,
                        price=price_val,
                        size=round(size_val, 4),
                        trade_log_path="/polybot-data/live_trades.jsonl",
                        city=city_slug,
                        bucket=o.get("bucket", ""),
                    )
                    )
                    if trade_result and trade_result.get("order_id"):
                        usdc_bal -= size_val
                    elif trade_result and trade_result.get("status") == "QUEUED":
                        print(f"[REBALANCER] QUEUED (gas): {action} {o.get('bucket')} gas=${trade_result.get('gas_cost_usd',0):.4f}")
                except Exception as te:
                    print(f"[REBALANCER] Trade error: {te}")
        else:
            # Paper mode — store in Redis
            r = _get_redis()
            if r:
                for o in orders:
                    price_val = float(o.get("price", 0))
                    if price_val < 0.001:
                        continue
                    size_val = bankroll * 0.02
                    trade_record = {
                        "city": city_slug,
                        "bucket": o.get("bucket", ""),
                        "side": o.get("action", ""),
                        "price": str(round(price_val, 4)),
                        "size": str(round(size_val, 4)),
                        "reason": o.get("reason", ""),
                        "timestamp": datetime.now().isoformat(),
                        "status": "open",
                    }
                    try:
                        r.lpush("paper_trades", json.dumps(trade_record))
                        r.ltrim("paper_trades", 0, 199)
                    except Exception:
                        pass

    return orders


def run_agent_cycle(
    city_slug: str,
    lat: float,
    lon: float,
    forecast_temp_f: float,
    bankroll: float = 2.30,
) -> list[dict]:
    """
    Run one full agent cycle for a single city:
    1. Update live temperature in Redis (with history)
    2. Check rebalancing trigger (SPEED → SLOPE → DEVIATION)
    3. Send Discord alert if rebalancing

    Args:
        city_slug: City identifier
        lat, lon: City coordinates
        forecast_temp_f: Forecast temperature in Fahrenheit
        bankroll: Available bankroll

    Returns:
        List of order dicts (may be empty)
    """
    from .live_temp_agent import update_live_temp

    # Step 1: Update live temp (also writes history to Redis)
    live = update_live_temp(city_slug, lat, lon)

    # Step 1.5: Check exit rules for existing positions (before new entries)
    exit_orders = run_exits_for_city(city_slug, bankroll)

    # Step 2: Rebalance (SPEED → SLOPE → DEVIATION)
    orders = rebalance_if_needed(
        city_slug, lat, lon, forecast_temp_f, bankroll, live_temp=live
    )

    # Combine exit + new orders
    all_orders = exit_orders + orders

    # Step 3: Discord alert
    if all_orders:
        print(f"[REBALANCER] {city_slug}: {len(all_orders)} orders ({len(exit_orders)} exits, {len(orders)} entries), sending Discord alert")
        try:
            from ..notify import send_embed
            fields = [
                {
                    "name": f"[{o.get('reason','?')}] {o['action']} {o['bucket']}",
                    "value": f"Edge: {o.get('edge',0):.2%} | P={o.get('prob',0):.2%} @ {o.get('price',0):.3f}",
                    "inline": True,
                }
                for o in all_orders[:5]
            ]
            send_embed(
                title=f" REBALANCE: {city_slug}",
                description=f"Live: {live}F | Forecast: {forecast_temp_f}F",
                fields=fields,
                color=0xF59E0B,
            )
        except Exception as e:
            print(f"[REBALANCER] Discord alert error: {e}")

    return all_orders


# ---------------------------------------------------------------------------
# Take-profit and exit rules
# ---------------------------------------------------------------------------

def check_exits(city_slug: str, bucket: str, entry_price: float,
                entry_time: str, current_price: float,
                peak_price: float = None,
                minutes_to_resolution: int = 9999) -> dict:
    """
    Check if an open paper trade should be closed.

    Exit conditions:
      1. Profit target:     current_price >= entry_price * 3  (3x return)
      2. Edge convergence:  |current_price - entry_price| / entry_price < 0.02
      3. Trailing stop:     current_price < peak_price * 0.6  (40% drawdown from peak)
      4. Stop loss:         current_price < entry_price * 0.85  (15% loss)
      5. Time decay:        minutes_to_resolution < 120

    Args:
        city_slug: City identifier
        bucket: Bucket name
        entry_price: Trade entry price (0-1 float)
        entry_time: ISO timestamp of entry
        current_price: Current market price (0-1 float)
        peak_price: Highest price since entry (from Redis), defaults to current_price
        minutes_to_resolution: Estimated minutes until market resolves

    Returns:
        {
            "exit": bool,
            "reason": str | None,  # which rule triggered, or None
            "exit_price": float,   # the current_price if exiting
            "profit_pct": float,   # (exit_price - entry_price) / entry_price * 100
        }
    """
    if peak_price is None:
        peak_price = current_price

    profit_pct = round((current_price - entry_price) / entry_price * 100, 2) if entry_price > 0 else 0.0

    # 1. Profit target: 3x
    if entry_price > 0 and current_price >= entry_price * 3:
        return {"exit": True, "reason": "PROFIT_TARGET_3X", "exit_price": current_price, "profit_pct": profit_pct}

    # 2. Edge convergence: price has converged (spread < 2% of entry)
    if entry_price > 0 and abs(current_price - entry_price) / entry_price < 0.02:
        return {"exit": True, "reason": "EDGE_CONVERGENCE", "exit_price": current_price, "profit_pct": profit_pct}

    # 3. Trailing stop: 40% drawdown from peak
    if peak_price > 0 and current_price < peak_price * 0.6:
        return {"exit": True, "reason": "TRAILING_STOP_40PCT", "exit_price": current_price, "profit_pct": profit_pct}

    # 4. Stop loss: 15% below entry
    if entry_price > 0 and current_price < entry_price * 0.85:
        return {"exit": True, "reason": "STOP_LOSS_15PCT", "exit_price": current_price, "profit_pct": profit_pct}

    # 5. Time decay: less than 2 hours to resolution
    if minutes_to_resolution < 120:
        return {"exit": True, "reason": "TIME_DECAY_2H", "exit_price": current_price, "profit_pct": profit_pct}

    return {"exit": False, "reason": None, "exit_price": current_price, "profit_pct": profit_pct}


def run_exits_for_city(city_slug: str, bankroll: float = 2.30) -> list[dict]:
    """
    Check all open paper trades for a city and generate exit orders.
    Called every agent cycle before new entry signals.

    Returns:
        List of exit SELL orders (may be empty)
    """
    r = _get_redis()
    if not r:
        return []

    exit_orders = []
    raw_trades = r.lrange("paper_trades", 0, 499)

    for item in raw_trades:
        try:
            trade = json.loads(item)
        except Exception:
            continue

        if trade.get("status") != "open":
            continue
        if trade.get("city", "") != city_slug:
            continue
        if trade.get("side", "") != "BUY":  # Only exit BUY positions
            continue

        entry_price = float(trade.get("entry_price", 0))
        entry_time = trade.get("timestamp", "")
        bucket = trade.get("bucket", "")

        if entry_price <= 0:
            continue

        # Get current market price (from Redis cache or default to entry)
        current_price = entry_price
        try:
            cached = r.hget(f"market_prices:{city_slug}", bucket)
            if cached:
                current_price = float(cached.decode() if isinstance(cached, bytes) else cached)
        except Exception:
            pass

        # Get peak price from Redis
        peak_price = current_price
        try:
            pk_raw = r.hget(f"trade_peaks:{city_slug}", bucket)
            if pk_raw:
                peak_price = float(pk_raw.decode() if isinstance(pk_raw, bytes) else pk_raw)
        except Exception:
            pass

        # Update peak if current is higher
        if current_price > peak_price:
            try:
                r.hset(f"trade_peaks:{city_slug}", bucket, str(current_price))
            except Exception:
                pass
            peak_price = current_price

        # Check exit conditions
        exit_result = check_exits(city_slug, bucket, entry_price, entry_time,
                                  current_price, peak_price)

        if exit_result["exit"]:
            exit_order = {
                "action": "SELL",
                "city": city_slug,
                "bucket": bucket,
                "edge": 0.0,
                "prob": 0.0,
                "price": exit_result["exit_price"],
                "entry_price": entry_price,
                "profit_pct": exit_result["profit_pct"],
                "exit_reason": exit_result["reason"],
                "live_temp_f": 0.0,
                "forecast_temp_f": 0.0,
                "timestamp": datetime.now().isoformat(),
                "reason": "EXIT",
            }
            exit_orders.append(exit_order)
            print(f"[EXIT] {city_slug}/{bucket}: {exit_result['reason']} "
                  f"@ {exit_result['exit_price']:.4f} (entry={entry_price:.4f}, "
                  f"pnl={exit_result['profit_pct']:+.1f}%)")

            # Mark original trade as resolved
            trade["status"] = "closed"
            trade["exit_reason"] = exit_result["reason"]
            trade["exit_price"] = str(exit_result["exit_price"])
            trade["profit_pct"] = str(exit_result["profit_pct"])
            trade["closed_at"] = datetime.now().isoformat()
            try:
                r.lrem("paper_trades", 0, item)
                r.lpush("paper_trades", json.dumps(trade))
                r.ltrim("paper_trades", 0, 199)
            except Exception:
                pass

    return exit_orders
