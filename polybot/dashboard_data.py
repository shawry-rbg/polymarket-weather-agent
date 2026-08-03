"""Dashboard data recorder — writes scan/trade data to Redis for the real-time dashboard."""

import os
import json
from datetime import datetime, timezone

import redis as _redis

_client = None


def _get_client():
    global _client
    if _client is None:
        url = os.environ.get("REDIS_URL")
        print(f"DEBUG dashboard_data: REDIS_URL set = {bool(url)}")
        if url:
            _client = _redis.from_url(url)
            print(f"DEBUG dashboard_data: Redis client created successfully")
        else:
            print("DEBUG dashboard_data: REDIS_URL not set — no Redis client")
    return _client


def _ts():
    return datetime.now(timezone.utc).isoformat()


def record_city_scan(city, live_temp, ecmwf, icon, ukmo, sky, trend):
    print(f"DEBUG: record_city_scan called for city={city}")
    r = _get_client()
    if not r:
        print(f"DEBUG: record_city_scan SKIP — no Redis client for {city}")
        return
    try:
        mapping = {
            "live_temp": str(live_temp) if live_temp is not None else "N/A",
            "ecmwf": str(ecmwf) if ecmwf is not None else "N/A",
            "icon": str(icon) if icon is not None else "N/A",
            "ukmo": str(ukmo) if ukmo is not None else "N/A",
            "sky": str(sky) if sky is not None else "N/A",
            "trend": str(trend) if trend is not None else "N/A",
            "updated": _ts(),
        }
        r.hset(f"city:{city}", mapping=mapping)
        print(f"DEBUG: wrote city {city} with fields {mapping}")
    except Exception as e:
        print(f"DEBUG: record_city_scan ERROR for {city}: {e}")


def record_ensemble_bucket(bucket, agreement, spread, prob, market_price, edge):
    print(f"DEBUG: record_ensemble_bucket called bucket={bucket}")
    r = _get_client()
    if not r:
        print("DEBUG: record_ensemble_bucket SKIP — no Redis client")
        return
    try:
        r.hset("ensemble", mapping={
            "bucket": str(bucket) if bucket is not None else "",
            "agreement": str(agreement) if agreement is not None else "",
            "spread": str(spread) if spread is not None else "",
            "prob": str(prob) if prob is not None else "",
            "market_price": str(market_price) if market_price is not None else "",
            "edge": str(edge) if edge is not None else "",
        })
        print("DEBUG: record_ensemble_bucket OK")
    except Exception as e:
        print(f"DEBUG: record_ensemble_bucket ERROR: {e}")


def record_bucket_scan(bucket, p, q, edge, hit, price):
    print(f"DEBUG: record_bucket_scan called bucket={bucket}")
    r = _get_client()
    if not r:
        print("DEBUG: record_bucket_scan SKIP — no Redis client")
        return
    try:
        entry = {
            "bucket": str(bucket) if bucket is not None else "",
            "p": str(p) if p is not None else "",
            "q": str(q) if q is not None else "",
            "edge": str(edge) if edge is not None else "",
            "hit": str(hit) if hit is not None else "",
            "price": str(price) if price is not None else "",
        }
        r.lpush("bucket_scan", json.dumps(entry))
        r.ltrim("bucket_scan", 0, 19)
        print("DEBUG: record_bucket_scan OK")
    except Exception as e:
        print(f"DEBUG: record_bucket_scan ERROR: {e}")


def record_resolved_trade(city, bucket, entry_price, final_value, profit_pct):
    print(f"DEBUG: record_resolved_trade called city={city}")
    r = _get_client()
    if not r:
        print("DEBUG: record_resolved_trade SKIP — no Redis client")
        return
    try:
        entry = {
            "city": str(city),
            "bucket": str(bucket),
            "entry_price": str(entry_price),
            "final_value": str(final_value),
            "profit_pct": str(profit_pct),
        }
        r.lpush("resolved_trades", json.dumps(entry))
        r.ltrim("resolved_trades", 0, 9)
        print("DEBUG: record_resolved_trade OK")
    except Exception as e:
        print(f"DEBUG: record_resolved_trade ERROR: {e}")


def record_session_pnl(pnl):
    print(f"DEBUG: record_session_pnl called pnl={pnl}")
    r = _get_client()
    if not r:
        print("DEBUG: record_session_pnl SKIP — no Redis client")
        return
    try:
        r.set("session_pnl", str(pnl))
        print("DEBUG: record_session_pnl OK")
    except Exception as e:
        print(f"DEBUG: record_session_pnl ERROR: {e}")


def record_cycle(next_seconds, progress, safeguards, max_position):
    print(f"DEBUG: record_cycle called next={next_seconds}s progress={progress}%")
    r = _get_client()
    if not r:
        print("DEBUG: record_cycle SKIP — no Redis client")
        return
    try:
        r.hset("cycle", mapping={
            "next_seconds": str(next_seconds) if next_seconds is not None else "",
            "progress": str(progress) if progress is not None else "",
            "safeguards": str(safeguards) if safeguards is not None else "",
            "max_position": str(max_position) if max_position is not None else "",
        })
        print("DEBUG: record_cycle OK")
    except Exception as e:
        print(f"DEBUG: record_cycle ERROR: {e}")


# ---------------------------------------------------------------------------
# Historical accuracy recording
# ---------------------------------------------------------------------------

def record_accuracy_result(date_str: str, avg_error: str, bucket_accuracy: str,
                            bucket_pct: str, cities_count: int):
    """
    Store historical accuracy results in Redis for the dashboard.

    Args:
        date_str: Date analyzed (YYYY-MM-DD)
        avg_error: Average absolute error in °F
        bucket_accuracy: e.g. "8/15"
        bucket_pct: e.g. "53.3"
        cities_count: Number of cities analyzed
    """
    r = _get_client()
    if not r:
        print("DEBUG: record_accuracy_result SKIP — no Redis client")
        return
    try:
        r.hset("accuracy", mapping={
            "date": date_str,
            "avg_error": str(avg_error),
            "bucket_accuracy": str(bucket_accuracy),
            "bucket_pct": str(bucket_pct),
            "cities_count": str(cities_count),
            "updated": datetime.now(timezone.utc).isoformat(),
        })
        print(f"DEBUG: record_accuracy_result OK for {date_str}")
    except Exception as e:
        print(f"DEBUG: record_accuracy_result ERROR: {e}")


def get_dashboard_data():
    """Read current dashboard data from Redis."""
    r = _get_client()
    if not r:
        return {
            "redis_connected": False,
            "cities": {},
            "ensemble": {},
            "bucket_scan": [],
            "resolved_trades": [],
            "session_pnl": "0",
            "cycle": {},
            "accuracy": {},
        }

    cities = {}
    try:
        for key in r.scan_iter("city:*"):
            data = r.hgetall(key)
            if data:
                cities[key.replace("city:", "")] = data
    except Exception as e:
        print(f"DEBUG: get_dashboard_data ERROR reading cities: {e}")

    try:
        ensemble = r.hgetall("ensemble") or {}
    except Exception:
        ensemble = {}

    try:
        bucket_scan = [json.loads(x) for x in r.lrange("bucket_scan", 0, 19)]
    except Exception:
        bucket_scan = []

    try:
        resolved_trades = [json.loads(x) for x in r.lrange("resolved_trades", 0, 9)]
    except Exception:
        resolved_trades = []

    try:
        session_pnl = r.get("session_pnl") or "0"
    except Exception:
        session_pnl = "0"

    try:
        cycle = r.hgetall("cycle") or {}
    except Exception:
        cycle = {}

    try:
        accuracy = r.hgetall("accuracy") or {}
    except Exception:
        accuracy = {}

    return {
        "redis_connected": True,
        "cities": cities,
        "ensemble": ensemble,
        "bucket_scan": bucket_scan,
        "resolved_trades": resolved_trades,
        "session_pnl": session_pnl,
        "cycle": cycle,
        "accuracy": accuracy,
    }
