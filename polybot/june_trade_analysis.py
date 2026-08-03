"""
June 2026 live-signal trade analysis from Redis paper trades.

Requirements:
  - env: REDIS_URL
  - network: Open-Meteo and Polymarket proxy access from runtime

Usage:
  python polybot/june_trade_analysis.py
  # or from Modal with polymarket-secrets + redis-url injected
"""
from __future__ import annotations

import json
import math
import os
import statistics
from datetime import datetime, timezone
from typing import Optional, Tuple

try:
    import redis as _redis_mod
except Exception:
    _redis_mod = None

try:
    import httpx
except Exception:
    httpx = None

REDIS_LIST = "paper_trades"
JUNE_TRADE_REASON_PREFIXES = ()
JUNE_YEAR = 2026
JUNE_MONTH = 6
BANKROLL_PER_TRADE = 0.10
POLY_PROXY = "https://poly-proxy.elvischemoiywo.workers.dev/gamma"
CF_WORKER_URL = os.environ.get("CF_WORKER_URL", "")


def _get_redis():
    if _redis_mod is None:
        raise RuntimeError("redis package not installed")
    url = os.environ.get("REDIS_URL")
    if not url:
        raise RuntimeError("REDIS_URL not set")
    return _redis_mod.from_url(url)


def _redis_from_url(url: str):
    if _redis_mod is None:
        raise RuntimeError("redis package not installed")
    return _redis_mod.from_url(url)


def _markets_for(city: str, date_str: str) -> list[dict]:
    if httpx is None:
        return []
    params = {
        "city": city,
        "date": date_str,
        "active": "true",
        "closed": "true",
        "limit": 100,
        "order": "volume24hr",
        "ascending": "false",
    }
    url = f"{POLY_PROXY}/events"
    out = []
    try:
        with httpx.Client(timeout=20) as client:
            r = client.get(url, params=params)
            if r.status_code != 200:
                return []
            body = r.json()
            if isinstance(body, dict):
                body = body.get("markets") or body.get("events") or []
            events = body if isinstance(body, list) else []
            for ev in events:
                title = (ev.get("title") or ev.get("question") or "").lower()
                for mkt in ev.get("markets", []):
                    q = (mkt.get("question") or "").lower()
                    if city.lower() in q or city.lower() in title:
                        out.append(mkt)
    except Exception:
        pass
    # dedupe by conditionId/slug
    seen = set()
    unique = []
    for m in out:
        key = m.get("conditionId") or m.get("slug") or m.get("id")
        if key and key not in seen:
            seen.add(key)
            unique.append(m)
    return unique


def _closed_markets_for(city: str, date_str: str) -> list[dict]:
    if httpx is None:
        return []
    params = {
        "city": city,
        "date": date_str,
        "active": "false",
        "closed": "true",
        "limit": 100,
        "order": "volume24hr",
        "ascending": "false",
    }
    url = f"{POLY_PROXY}/events"
    out = []
    try:
        with httpx.Client(timeout=20) as client:
            r = client.get(url, params=params)
            if r.status_code != 200:
                return []
            body = r.json()
            if isinstance(body, dict):
                body = body.get("markets") or body.get("events") or []
            events = body if isinstance(body, list) else []
            for ev in events:
                title = (ev.get("title") or ev.get("question") or "").lower()
                for mkt in ev.get("markets", []):
                    q = (mkt.get("question") or "").lower()
                    if city.lower() in q or city.lower() in title:
                        out.append(mkt)
    except Exception:
        pass
    seen = set()
    unique = []
    for m in out:
        key = m.get("conditionId") or m.get("slug") or m.get("id")
        if key and key not in seen:
            seen.add(key)
            unique.append(m)
    return unique


def _extract_threshold(question: str) -> Optional[float]:
    import re
    q = (question or "").lower()
    pats = [
        r"([0-9]+(?:\\.[0-9]+)?)\\s*[°]?\\s*f(?:ahrenheit)?\\b",
        r"([0-9]+(?:\\.[0-9]+)?)\\s*degrees?\\s*f\\b",
        r"exceed\\s+(?:or\\s+equal\\s+(?:to)?\\s+)?([0-9]+(?:\\.[0-9]+)?)",
        r"above\\s+(?:or\\s+equal\\s+(?:to)?\\s+)?([0-9]+(?:\\.[0-9]+)?)",
        r"over\\s+(?:or\\s+equal\\s+(?:to)?\\s+)?([0-9]+(?:\\.[0-9]+)?)",
        r"higher\\s+than\\s+(?:or\\s+equal\\s+(?:to)?\\s+)?([0-9]+(?:\\.[0-9]+)?)",
        r"at\\s+least\\s+([0-9]+(?:\\.[0-9]+)?)",
    ]
    for pat in pats:
        m = re.search(pat, q)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
    return None


def _price_from_market(mkt: dict, threshold: Optional[float]) -> dict:
    raw = mkt.get("outcomePrices")
    prices = []
    if isinstance(raw, str):
        try:
            prices = json.loads(raw)
        except Exception:
            prices = []
    elif isinstance(raw, list):
        prices = [p for p in raw if isinstance(p, (int, float, str))]
    yes_price = None
    no_price = None
    try:
        if len(prices) >= 1:
            yes_price = float(prices[0])
        if len(prices) >= 2:
            no_price = float(prices[1])
    except Exception:
        pass
    return {
        "threshold": threshold if threshold is not None else _extract_threshold(mkt.get("question", "")),
        "yes_price": yes_price,
        "no_price": no_price,
        "question": mkt.get("question", ""),
        "slug": mkt.get("slug", ""),
        "conditionId": mkt.get("conditionId", ""),
        "endDate": mkt.get("endDate", ""),
    }


def _open_meteo_temperature(lat: float, lon: float, date_str: str) -> Optional[float]:
    if httpx is None:
        return None
    url = "https://archive-api.open-meteo.com/v1/archive"
    # Use reanalysis for closed historical dates
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date_str,
        "end_date": date_str,
        "daily": "temperature_2m_max",
        "timezone": "UTC",
    }
    try:
        with httpx.Client(timeout=20) as client:
            r = client.get(url, params=params)
            if r.status_code != 200:
                return None
            data = r.json()
            vals = (((data.get("daily") or {}).get("temperature_2m_max")) or [])
            if vals:
                return float(vals[0])
    except Exception:
        pass
    return None


def _city_latlon(city: str) -> Tuple[float, float]:
    # Fallback simple fuzzy lookup from common set
    mapping = {
        "new york": (40.7128, -74.0060),
        "london": (51.5074, -0.1278),
        "tokyo": (35.6762, 139.6503),
        "chicago": (41.8781, -87.6298),
        "houston": (29.7604, -95.3698),
        "paris": (48.8566, 2.3522),
        "berlin": (52.5200, 13.4050),
        "sydney": (-33.8688, 151.2093),
        "miami": (25.7617, -80.1918),
        "seattle": (47.6062, -122.3321),
        "austin": (30.2672, -97.7431),
        "toronto": (43.65107, -79.347015),
        "mexico city": (19.4326, -99.1332),
        "san francisco": (37.7749, -122.4194),
        "dallas": (32.7767, -96.7970),
        "hong kong": (22.3193, 114.1694),
        "singapore": (1.3521, 103.8198),
    }
    return mapping.get(city.lower(), (None, None))


def _load_paper_trades() -> list[dict]:
    r = _get_redis()
    raw = r.lrange(REDIS_LIST, 0, 499)
    trades = []
    for item in raw:
        if isinstance(item, bytes):
            item = item.decode("utf-8", errors="ignore")
        try:
            trades.append(json.loads(item))
        except Exception:
            pass
    return trades


def _is_june_ts(ts: str) -> bool:
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.year == JUNE_YEAR and dt.month == JUNE_MONTH
    except Exception:
        return False


def _settlement_payout(actual_temp_f: float, threshold_f: float, side: str) -> float:
    if threshold_f is None:
        return 0.0
    if side.upper() == "BUY":
        return 1.0 if actual_temp_f >= threshold_f else 0.0
    if side.upper() == "SELL":
        return 1.0 if actual_temp_f < threshold_f else 0.0
    return 0.0


def _normalize_side(x: str) -> Optional[str]:
    if not x:
        return None
    s = str(x).upper()
    if s == "BUY":
        return "BUY"
    if s == "SELL":
        return "SELL"
    if s in ("YES", "LONG"):
        return "BUY"
    if s in ("NO", "SHORT"):
        return "SELL"
    return None


def analyze() -> str:
    lines = []
    sep = "=" * 100
    lines += [
        "",
        sep,
        "  POLYBOT JUNE 2026 PAPER TRADE ANALYSIS",
        f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        sep,
        f"  Bankroll per trade: ${BANKROLL_PER_TRADE:.2f}",
        "",
    ]

    trades = []
    try:
        trades = _load_paper_trades()
    except Exception as e:
        lines.append(f"ERROR loading trades: {e}")
        return "\n".join(lines)

    if not trades:
        lines.append("No paper trades found in Redis.")
        return "\n".join(lines)

    june = [
        t
        for t in trades
        if _is_june_ts(t.get("timestamp", ""))
    ]
    live = [
        t for t in june
        if _is_live_signal(t.get("reason", ""), t.get("source", ""), t.get("status", ""), t.get("action", ""), t.get("side", ""))
    ]
    lines.append(f"All June trades:         {len(june)}")
    lines.append(f"Live signal trades:      {len(live)}")
    lines.append("")

    if not live:
        lines.append("No live-signal June trades to analyze.")
        return "\n".join(lines)

    rows = []
    for t in live:
        city = t.get("city", "")
        bucket = t.get("bucket", "")
        mkt_date = _trade_market_date(t)
        threshold = t.get("threshold_f") or _extract_threshold(t.get("question", "")) or _threshold_from_bucket(bucket)
        side = _trade_side(t)
        entry_price = _to_float(t.get("entry_price") or t.get("price"))
        size = BANKROLL_PER_TRADE if entry_price and entry_price > 0 else 0.0

        # start unresolved; try resolution from Gamma
        winning_bucket, final_yes, final_no = _resolve_market(city=city, date_str=mkt_date)

        # If unresolved, use observed weather as backend payout
        exit_price = None
        payout = None
        resolution = "unresolved/open"
        if winning_bucket is None:
            # unresolved path
            lat, lon = _city_latlon(city)
            actual = None
            if lat is not None and lon is not None:
                actual = _open_meteo_temperature(lat=lat, lon=lon, date_str=mkt_date)
            if actual is not None and threshold is not None:
                won = actual >= threshold if side == "BUY" else actual < threshold
                exit_price = 1.0 if won else 0.0
                payout = exit_price * size
                resolution = f"open-meteo {'WIN' if won else 'LOSS'} {actual:.1f}F/{threshold:.1f}F"
            else:
                resolution = "unknown/no-temp"
        else:
            # resolved market path from Gamma
            if side == "BUY":
                wp = final_yes if final_yes is not None else 1.0
            else:
                wp = final_no if final_no is not None else 0.0
            exit_price = wp / 1.0 if wp is not None else None
            if exit_price is not None:
                payout = exit_price * size
            won = exit_price == 1.0 if side == "BUY" else exit_price == 0.0
            resolution = f"resolved {'WIN' if won else 'LOSS'}"

        pnl = 0.0 if payout is None else round(payout - size, 6)
        rows.append({
            "city": city,
            "date": mkt_date,
            "bucket": bucket,
            "side": side,
            "threshold_f": _maybe_float(threshold),
            "entry_price": _maybe_float(entry_price),
            "size": size,
            "exit_price": _maybe_float(exit_price),
            "pnl": pnl,
            "resolution": resolution,
        })

    # ----- Output -----
    header = f"  {'City':<16} {'Date':<10} {'Bucket':<12} {'Side':<5} {'Thresh':>6} {'Entry':>6} {'Size':>6} {'Exit':>6} {'PnL':>8} {'Resolution'}"
    lines += [header, "  " + "-" * (len(header) - 2)]
    for row in rows:
        lines.append(
            f"  {row['city']:<16} {row['date']:<10} {row['bucket']:<12} {row['side']:<5} "
            f"{fmtf(row['threshold_f']):>6} {fmtf(row['entry_price']):>6} {fmtf(row['size']):>6} "
            f"{fmtf(row['exit_price']):>6} {fmtf(row['pnl']):>8} {row['resolution']}"
        )

    resolved_rows = [r for r in rows if r["resolution"] not in ("unresolved/open", "unknown/no-temp")]
    all_pnls = [r["pnl"] for r in resolved_rows if math.isfinite(r["pnl"])]
    total_pnl = sum(all_pnls)
    wins = sum(1 for r in resolved_rows if isinstance(r["exit_price"], float) and _maybe_float(r["exit_price"]) == (1.0 if _maybe_float(r["side"]) == "BUY" else 0.0))
    losses = len(resolved_rows) - wins
    win_rate = (wins / len(resolved_rows) * 100.0) if resolved_rows else 0.0
    best = max(resolved_rows, key=lambda r: r["pnl"]) if resolved_rows else None
    worst = min(resolved_rows, key=lambda r: r["pnl"]) if resolved_rows else None

    lines += ["", sep, "  OVERALL", sep, f"  Live trades:              {len(rows)}"]
    if resolved_rows:
        lines += [
            f"  Resolved:                 {len(resolved_rows)}",
            f"  Wins:                     {wins}",
            f"  Losses:                   {losses}",
            f"  Win rate:                 {win_rate:.1f}%",
            f"  Total P&L:                ${total_pnl:+.4f}",
        ]
        if best:
            lines.append(f"  Best trade:               {best['city']}/{best['bucket']} {best['date']} ${best['pnl']:+.4f}")
        if worst:
            lines.append(f"  Worst trade:              {worst['city']}/{worst['bucket']} {worst['date']} ${worst['pnl']:+.4f}")
    else:
        lines.append("  Resolved: 0")

    # unresolved summary
    open_count = sum(1 for r in rows if not r["resolution"].startswith("resolved"))
    lines.append(f"  Open/unresolved:         {open_count}")
    lines += [sep, ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _is_live_signal(reason: str, source: str, status: str, action: str, side: str) -> bool:
    text = " ".join(map(str, (reason, source, status, action, side))).lower()
    green_flags = ["green", "live signal", "live", "confirmed", "taken"]
    return any(flag in text for flag in green_flags)


def _trade_market_date(t: dict) -> str:
    for k in ("market_date", "date", "target_date", "expiry", "event_date"):
        v = t.get(k)
        if v:
            return str(v)
    ts = t.get("timestamp")
    if ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except Exception:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _trade_side(t: dict) -> str:
    for k in ("side", "action", "direction"):
        v = t.get(k)
        if v:
            s = _normalize_side(v)
            if s:
                return s
    bucket = t.get("bucket", "")
    if ">=" in str(bucket) or ">" in str(bucket) or "hot" in str(bucket).lower() or "exceed" in str(bucket).lower() or "above" in str(bucket).lower():
        return "BUY"
    if "<=" in str(bucket) or "<" in str(bucket) or "cold" in str(bucket).lower() or "below" in str(bucket).lower():
        return "SELL"
    return "BUY"


def _threshold_from_bucket(bucket: str) -> Optional[float]:
    import re
    b = str(bucket)
    for pat in (r">=?\s*([0-9]+(?:\\.[0-9]+)?)", r"<=\s*([0-9]+(?:\\.[0-9]+)?)", r"([0-9]+(?:\\.[0-9]+)?)\s*F"):
        m = re.search(pat, b)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
    return None


def _to_float(v) -> Optional[float]:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except Exception:
        return None


def _maybe_float(v) -> str:
    f = _to_float(v)
    if f is None:
        return "-"
    return f"{f:.4f}"


def fmtf(v) -> str:
    if v is None:
        return "-"
    try:
        f = float(v)
        if not math.isfinite(f):
            return "-"
        return f"{f:.4f}"
    except Exception:
        return "-"


def _resolve_market(city: str, date_str: str) -> Tuple[Optional[str], Optional[float], Optional[float]]:
    mkt_list = _closed_markets_for(city=city, date_str=date_str)
    if not mkt_list:
        return None, None, None
    best_price = -1.0
    wb = None
    wp = None
    np = None
    for mkt in mkt_list:
        th = _extract_threshold(mkt.get("question", ""))
        parsed = _price_from_market(mkt, th)
        yes_p = parsed.get("yes_price")
        no_p = parsed.get("no_price")
        if yes_p is None or no_p is None:
            continue
        if yes_p >= 0.95:
            return f">{th}F" if th is not None else "WINNER", yes_p, no_p
        if yes_p > best_price:
            best_price = yes_p
            wb = f">{th}F" if th is not None else "BUCKET"
            wp, np = yes_p, no_p
    return wb, wp, np


if __name__ == "__main__":
    print(analyze())
