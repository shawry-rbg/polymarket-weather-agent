"""
Health Monitor — 24/7 Modal app that checks bot health every 10 minutes.

Checks:
  - Redis connectivity
  - City data freshness (updated within last 2 hours)
  - Cloudflare Worker health
  - Last full scan timestamp
  - Alerts via Discord on any failure
  - Daily OK ping at 09:00 UTC
"""

from __future__ import annotations

import os
import json
import datetime

import modal

from polybot.notify import send_discord, send_health_alert, send_daily_ok

# --- Modal app ----------------------------------------------------------------

image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "redis>=5.0.0",
    "httpx>=0.28.0",
)

app = modal.App("polybot-health", image=image)

CF_WORKER_URL = "https://polymarket-commander.elvischemoiywo.workers.dev"
HEALTH_CHECK_INTERVAL = 600  # 10 minutes
STALE_THRESHOLD = 7200  # 2 hours in seconds
DAILY_HOUR_UTC = 9


def _get_redis():
    url = os.environ.get("REDIS_URL")
    if url:
        import redis as _redis  # type: ignore
        return _redis.from_url(url)
    return None


def _check_redis_connection(r) -> tuple[bool, str]:
    """Check Redis is reachable."""
    try:
        r.ping()
        return True, "Redis OK"
    except Exception as e:
        return False, f"Redis ping failed: {e}"


def _check_cloudflare_worker() -> tuple[bool, str]:
    """Check Cloudflare Worker returns 200."""
    import httpx
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(f"{CF_WORKER_URL}/health")
            if resp.status_code == 200:
                return True, "CF Worker OK"
            return False, f"CF Worker returned {resp.status_code}"
    except Exception as e:
        return False, f"CF Worker unreachable: {e}"


def _check_city_freshness(r) -> tuple[bool, list[str]]:
    """Check all city keys have been updated within the last 2 hours."""
    stale = []
    try:
        keys = r.keys("city:*")
        if not keys:
            return False, ["No city keys found in Redis"]

        now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
        for key in keys:
            raw = r.hgetall(key)
            updated = None
            for k, v in raw.items():
                k2 = k.decode() if isinstance(k, bytes) else str(k)
                if k2 == "updated":
                    v2 = v.decode() if isinstance(v, bytes) else str(v)
                    updated = v2
                    break

            if not updated:
                stale.append("unknown (no updated field)")
                continue

            try:
                updated_dt = datetime.datetime.fromisoformat(updated.replace("Z", "+00:00"))
                age = now_ts - updated_dt.timestamp()
                if age > STALE_THRESHOLD:
                    name = key.decode() if isinstance(key, bytes) else str(key)
                    stale.append(f"{name} (last update {age / 60:.0f}m ago)")
            except Exception as e:
                stale.append(f"parse error: {e}")
    except Exception as e:
        return False, [f"Error reading keys: {e}"]

    return len(stale) == 0, stale


def _check_last_scan(r) -> tuple[bool, str]:
    """Check last_full_scan timestamp."""
    try:
        raw = r.get("last_full_scan")
        if not raw:
            return False, "last_full_scan key not set"
        val = raw.decode() if isinstance(raw, bytes) else str(raw)
        scan_dt = datetime.datetime.fromisoformat(val.replace("Z", "+00:00"))
        age = (datetime.datetime.now(datetime.timezone.utc) - scan_dt).total_seconds()
        if age > 7200:  # 2 hours
            return False, f"Last scan was {age / 60:.0f}m ago"
        return True, f"Last scan {age / 60:.0f}m ago"
    except Exception as e:
        return False, f"last_full_scan check error: {e}"


def _run_health_check():
    """Run health checks every 10 minutes."""
    now = datetime.datetime.now(datetime.timezone.utc)
    print(f"[HEALTH] Check at {now.isoformat()}")

    r = _get_redis()
    issues = []

    # 1. Redis connection
    ok, msg = _check_redis_connection(r)
    print(f"[HEALTH] Redis: {msg}")
    if not ok:
        issues.append(f"Redis: {msg}")

    if r:
        # 2. City freshness
        ok, stale = _check_city_freshness(r)
        if not ok:
            detail = ", ".join(stale) if stale else "no cities found"
            print(f"[HEALTH] Cities: {detail}")
            issues.append(f"Stale cities: {detail}")
        else:
            keys = r.keys("city:*")
            print(f"[HEALTH] Cities: {len(keys)} cities fresh")

        # 3. Last scan
        ok, msg = _check_last_scan(r)
        print(f"[HEALTH] Last scan: {msg}")
        if not ok:
            issues.append(f"Scan: {msg}")

    # 4. Cloudflare Worker
    ok, msg = _check_cloudflare_worker()
    print(f"[HEALTH] CF Worker: {msg}")
    if not ok:
        issues.append(f"CF Worker: {msg}")

    # 5. Send alerts or daily OK
    if issues:
        issue_text = "\n".join(f"• {i}" for i in issues)
        print(f"[HEALTH] ALERT: {issue_text}")
        send_health_alert("Health Check Failed", issue_text)
    elif now.hour == DAILY_HOUR_UTC and now.minute < 10:
        cities_count = 0
        pnl = 0
        if r:
            keys = r.keys("city:*")
            cities_count = len(keys)
            pnl_raw = r.get("session_pnl")
            if pnl_raw:
                val = pnl_raw.decode() if isinstance(pnl_raw, bytes) else str(pnl_raw)
                pnl = float(val)
        send_daily_ok(cities_count, 0, pnl)
        print("[HEALTH] Daily OK sent")

    print(f"[HEALTH] Check complete — {len(issues)} issues")


@app.function(
    schedule=modal.Period(seconds=HEALTH_CHECK_INTERVAL),
    secrets=[modal.Secret.from_name("discord-webhook"), modal.Secret.from_name("redis-url")],
    image=image,
)
def health_check():
    """Run health checks every 10 minutes."""
    _run_health_check()
