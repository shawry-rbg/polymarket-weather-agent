"""
Discord webhook notifier for Polybot.

Sends alerts to a Discord channel via webhook.
Rate-limited to 1 message per 2 seconds to avoid spam.

Slash command handlers for /status, /halt, /resume, /card.
These are called by the Cloudflare Worker or Modal webhook endpoint.
"""

from __future__ import annotations

import os
import time
import json
from datetime import datetime, timezone

import httpx

# Rate limiting
_last_sent: float = 0.0
_MIN_INTERVAL: float = 2.0  # seconds between messages


def _get_webhook_url() -> str | None:
    """Get webhook URL from environment or Modal secret."""
    return os.environ.get("DISCORD_WEBHOOK_URL")


def _rate_limit() -> bool:
    """Check if we can send (respects rate limit). Returns True if OK to send."""
    global _last_sent
    now = time.time()
    if now - _last_sent < _MIN_INTERVAL:
        return False
    _last_sent = now
    return True


def send_discord(message: str) -> bool:
    """Send a plain text message to Discord."""
    url = _get_webhook_url()
    if not url:
        print("[NOTIFY] No DISCORD_WEBHOOK_URL set - skipping message")
        return False
    if not _rate_limit():
        print("[NOTIFY] Rate limited - skipping message")
        return False
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json={"content": message})
            if resp.status_code in (200, 204):
                print(f"[NOTIFY] Discord message sent: {message[:60]}...")
                return True
            else:
                print(f"[NOTIFY] Discord error {resp.status_code}: {resp.text[:100]}")
                return False
    except Exception as e:
        print(f"[NOTIFY] Discord send failed: {e}")
        return False


def send_embed(title: str, description: str, fields: list[dict] | None = None,
               color: int = 0x5865F2) -> bool:
    """Send a rich embed message to Discord."""
    url = _get_webhook_url()
    if not url:
        print("[NOTIFY] No DISCORD_WEBHOOK_URL set - skipping embed")
        return False
    if not _rate_limit():
        print("[NOTIFY] Rate limited - skipping embed")
        return False
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Polybot v5"},
    }
    if fields:
        embed["fields"] = fields
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(url, json={"embeds": [embed]})
            if resp.status_code in (200, 204):
                print(f"[NOTIFY] Discord embed sent: {title}")
                return True
            else:
                print(f"[NOTIFY] Discord error {resp.status_code}")
                return False
    except Exception as e:
        print(f"[NOTIFY] Discord embed failed: {e}")
        return False


def send_trade_alert(city: str, bucket: str, price: float, edge: float,
                     bet_size: float, status: str = "PAPER") -> bool:
    """Send a formatted trade alert embed to Discord."""
    emoji = "green" if status == "LIVE" else "paper"
    color = 0x3FB950 if status == "LIVE" else 0x5865F2
    title = f"{emoji} Trade {status}: {city} {bucket}"
    description = (
        f"**Price:** {price:.3f} ({price * 100:.1f}cent)\n"
        f"**Edge:** {edge:.1%}\n"
        f"**Bet:** ${bet_size:.2f}\n"
        f"**Expected value:** ${(edge * bet_size):.4f}"
    )
    fields = [
        {"name": "City", "value": city, "inline": True},
        {"name": "Bucket", "value": bucket, "inline": True},
        {"name": "Price", "value": f"{price:.3f}", "inline": True},
        {"name": "Edge", "value": f"{edge:.1%}", "inline": True},
        {"name": "Bet Size", "value": f"${bet_size:.2f}", "inline": True},
        {"name": "EV", "value": f"${edge * bet_size:.4f}", "inline": True},
    ]
    return send_embed(title, description, fields, color=color)


def send_health_alert(issue: str, details: str = "") -> bool:
    """Send a health monitoring alert."""
    title = "Health Alert: " + issue
    return send_embed(title, details or issue, color=0xF85149)


def send_daily_ok(cities_count: int, trade_count: int, pnl: float) -> bool:
    """Send a daily OK ping."""
    title = "Daily Health Check"
    desc = (
        f"Cities tracked: {cities_count}\n"
        f"Trades today: {trade_count}\n"
        f"Session P&L: {'+' if pnl >= 0 else ''}{pnl:.1f}%\n"
        f"All checks passing."
    )
    return send_embed(title, desc, color=0x3FB950)


# ---------------------------------------------------------------------------
# Slash command handlers
# ---------------------------------------------------------------------------

def _decode_redis_val(val):
    """Decode a Redis value from bytes to string/float."""
    if val is None:
        return None
    if isinstance(val, bytes):
        val = val.decode()
    try:
        return float(val)
    except (ValueError, TypeError):
        return str(val)


def _get_city_metrics_from_redis(city_slug: str) -> dict:
    """Fetch all metrics for a city from Redis."""
    import redis as _redis_mod
    try:
        r = _redis_mod.from_url(os.environ.get("REDIS_URL", ""))
        raw = r.hgetall(f"city_metrics:{city_slug}")
        metrics = {}
        for k, v in raw.items():
            kk = k.decode() if isinstance(k, bytes) else str(k)
            metrics[kk] = _decode_redis_val(v)
        return metrics
    except Exception:
        return {}


def handle_status_command() -> dict:
    """
    Build and send a /status embed to Discord.
    Includes decision cards for top 3 cities.
    Returns the response dict for the Cloudflare Worker to forward.
    """
    import redis as _redis_mod

    warnings = []
    balance = 0.0
    open_trades = 0
    win_rate_str = "N/A"
    best_signal_str = "None"
    next_gfs_str = "N/A"
    trading_halted = False
    city_cards_data = []

    try:
        r = _redis_mod.from_url(os.environ.get("REDIS_URL", ""))

        # Balance
        bal_raw = r.get("wallet_balance_usdc") or r.get("usdc_balance")
        if bal_raw:
            try:
                balance = float(bal_raw.decode() if isinstance(bal_raw, bytes) else bal_raw)
            except (ValueError, TypeError):
                balance = 0.0

        # Open trades count — use LIVE trades
        raw_trades = r.lrange("live_trades", 0, 499)
        if not raw_trades:
            # Fallback to paper if no live trades yet (backward compat)
            raw_trades = r.lrange("paper_trades", 0, 499)
        for item in raw_trades:
            try:
                t = json.loads(item)
                if t.get("status") == "open":
                    open_trades += 1
            except Exception:
                pass

        # Win rate — use LIVE counters
        wins = int(r.get("live_win_count") or 0)
        total = int(r.get("live_trade_count") or 0)
        if total == 0:
            # Fallback to paper if no live trades yet
            wins = int(r.get("paper_win_count") or 0)
            total = int(r.get("paper_trade_count") or 0)
        if total > 0:
            win_rate_str = f"{wins / total:.0%} ({wins}W/{total - wins}L)"

        # Collect city data for decision cards
        city_edges = []
        for city_key in r.scan_iter("city_metrics:*"):
            try:
                metrics = r.hgetall(city_key)
                if not metrics:
                    continue
                city_name = city_key.decode() if isinstance(city_key, bytes) else str(city_key)
                city_name = city_name.replace("city_metrics:", "")

                edge_val = metrics.get(b"edge") or metrics.get("edge")
                ev = float(edge_val.decode() if isinstance(edge_val, bytes) else edge_val) if edge_val else 0.0

                forecast_f = _decode_redis_val(metrics.get(b"forecast_temp_f") or metrics.get("forecast_temp_f")) or 0
                live_temp_f = _decode_redis_val(metrics.get(b"live_temp_f") or metrics.get("live_temp_f")) or 0
                slope = _decode_redis_val(metrics.get(b"slope_f_per_5min") or metrics.get("slope_f_per_5min")) or 0
                peak = _decode_redis_val(metrics.get(b"peak_window") or metrics.get("peak_window")) or 0
                bucket = _decode_redis_val(metrics.get(b"best_bucket") or metrics.get("bucket")) or ""
                market_price = _decode_redis_val(metrics.get(b"market_price") or metrics.get("yes_price")) or 0
                model_prob = _decode_redis_val(metrics.get(b"model_prob") or metrics.get("gfs_prob")) or 0
                n_orders = int(_decode_redis_val(metrics.get(b"n_orders") or metrics.get("n_orders")) or 0)

                # Get settlement correction
                correction = 0.0
                try:
                    corr_raw = r.hget("settlement_corrections", city_name)
                    if corr_raw:
                        correction = float(corr_raw.decode() if isinstance(corr_raw, bytes) else corr_raw)
                except Exception:
                    pass

                adj_forecast = forecast_f + correction

                if ev > best_signal_str.__class__ == str and ev > 0:
                    pass  # will track below
                city_edges.append({
                    "city": city_name,
                    "edge": ev,
                    "forecast_f": forecast_f,
                    "adj_forecast_f": adj_forecast,
                    "live_temp_f": live_temp_f,
                    "slope": slope,
                    "peak": peak,
                    "bucket": bucket,
                    "market_price": market_price,
                    "model_prob": model_prob,
                    "n_orders": n_orders,
                    "correction": correction,
                })

            except Exception:
                pass

        # Sort by edge descending, take top 3
        city_edges.sort(key=lambda x: x["edge"], reverse=True)
        city_cards_data = city_edges[:3]

        # Best signal string
        if city_cards_data:
            best = city_cards_data[0]
            best_signal_str = f"{best['city']} - {best['bucket']} (edge={best['edge']:.1%})"

        # Trading halt
        halt_val = r.get("TRADING_HALT")
        if halt_val:
            halt_str = halt_val.decode() if isinstance(halt_val, bytes) else halt_val
            if halt_str and halt_str.lower() in ("1", "true", "yes"):
                trading_halted = True
                warnings.append("TRADING IS HALTED")

        # Next GFS time
        now_utc = datetime.now(timezone.utc)
        gfs_hours = [0, 6, 12, 18]
        next_gfs = None
        for h in gfs_hours:
            candidate = now_utc.replace(hour=h, minute=0, second=0, microsecond=0)
            if candidate > now_utc:
                next_gfs = candidate
                break
        if next_gfs is None:
            from datetime import timedelta as _td
            next_gfs = now_utc.replace(hour=0, minute=0, second=0, microsecond=0) + _td(days=1)
        next_gfs_str = next_gfs.strftime("%H:00 UTC")

    except Exception as e:
        warnings.append(f"Redis error: {e}")

    # Low balance warning
    if balance < 0.50:
        warnings.append(f"Low balance: ${balance:.2f}")
    if open_trades > 20:
        warnings.append(f"High open position count: {open_trades}")

    color = 0xF85149 if trading_halted else (0xF59E0B if warnings else 0x3FB950)

    # Build main status fields
    # P&L — prefer live, fall back to paper
    pnl_val = 0.0
    try:
        pnl_val = float(r.get("live_pnl_total") or 0)
        if pnl_val == 0:
            pnl_val = float(r.get("paper_pnl_total") or 0)
    except Exception:
        pass
    pnl_str = f"{'+' if pnl_val >= 0 else ''}{pnl_val:.4f}"
    fields = [
        {"name": "Balance", "value": f"${balance:.2f} USDC", "inline": True},
        {"name": "Open Trades", "value": str(open_trades), "inline": True},
        {"name": "Win Rate", "value": win_rate_str, "inline": True},
        {"name": "Live P&L", "value": f"${pnl_str}", "inline": True},
        {"name": "Best Signal", "value": best_signal_str, "inline": True},
        {"name": "Next GFS", "value": next_gfs_str, "inline": True},
        {"name": "Trading Halt", "value": "YES" if trading_halted else "No", "inline": True},
    ]

    if warnings:
        fields.append({"name": "Warnings", "value": "\n".join(warnings), "inline": False})

    # Add decision cards for top 3 cities
    for cd in city_cards_data:
        signal = "🔥 BUY" if cd["edge"] > 0.12 else ("📊 WATCH" if cd["edge"] > 0.05 else "❌ SKIP")
        card_value = (
            f"Station: {cd['city']}\n"
            f"Forecast: {cd['forecast_f']:.1f}F (adj: {cd['adj_forecast_f']:.1f}F, corr: {cd['correction']:+.1f}F)\n"
            f"Live: {cd['live_temp_f']:.1f}F | Slope: {cd['slope']:+.2f}F/5min\n"
            f"Best Bucket: {cd['bucket']}\n"
            f"Model Prob: {cd['model_prob']:.0%} | Market: {cd['market_price']:.3f}\n"
            f"Edge: {cd['edge']:.1%} | Signal: **{signal}**"
        )
        card_color = 0x3FB950 if cd["edge"] > 0.12 else (0xF59E0B if cd["edge"] > 0.05 else 0x666666)
        fields.append({
            "name": f"🏙️ {cd['city'].upper()} DECISION CARD",
            "value": card_value,
            "inline": False,
        })

    send_embed(
        title="Polybot Status",
        description=f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        fields=fields,
        color=color,
    )

    return {
        "embeds": [{
            "title": "Polybot Status",
            "description": f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "color": color,
            "fields": fields,
        }]
    }


def handle_card_command(city: str = "") -> dict:
    """
    /card <city> - Get a detailed decision card for a specific city.
    """
    import redis as _redis_mod

    if not city:
        return {"content": "Usage: /card <city_name> - e.g., /card chongqing"}

    city_slug = city.lower().replace(" ", "_")

    try:
        r = _redis_mod.from_url(os.environ.get("REDIS_URL", ""))
        metrics_raw = r.hgetall(f"city_metrics:{city_slug}")

        if not metrics_raw:
            return {"content": f"No data for '{city}'. City may not be active or no scan has run yet."}

        metrics = {}
        for k, v in metrics_raw.items():
            kk = k.decode() if isinstance(k, bytes) else str(k)
            metrics[kk] = _decode_redis_val(v)

        # Get settlement correction
        correction = 0.0
        try:
            corr_raw = r.hget("settlement_corrections", city_slug)
            if corr_raw:
                correction = float(corr_raw.decode() if isinstance(corr_raw, bytes) else corr_raw)
        except Exception:
            pass

        forecast_f = metrics.get("forecast_temp_f", 0)
        adj_forecast = forecast_f + correction
        live_temp = metrics.get("live_temp_f", 0)
        slope = metrics.get("slope_f_per_5min", 0)
        peak = metrics.get("peak_window", 0)
        bucket = metrics.get("bucket", metrics.get("best_bucket", "N/A"))
        market_price = metrics.get("yes_price", metrics.get("market_price", 0))
        model_prob = metrics.get("gfs_prob", metrics.get("model_prob", 0))
        edge = metrics.get("edge", 0)
        n_orders = metrics.get("n_orders", 0)
        local_hour = metrics.get("local_hour", 0)
        market_date = metrics.get("market_date", "N/A")
        market_open = metrics.get("market_open", 0)

        signal = "🔥 BUY" if edge > 0.12 else ("📊 WATCH" if edge > 0.05 else "❌ SKIP")
        signal_color = 0x3FB950 if edge > 0.12 else (0xF59E0B if edge > 0.05 else 0xF85149)

        card_fields = [
            {"name": "Forecast (raw)", "value": f"{forecast_f:.1f}F", "inline": True},
            {"name": "Settlement Correction", "value": f"{correction:+.1f}F", "inline": True},
            {"name": "Forecast (adjusted)", "value": f"{adj_forecast:.1f}F", "inline": True},
            {"name": "Live Temp", "value": f"{live_temp:.1f}F", "inline": True},
            {"name": "Slope (5min)", "value": f"{slope:+.2f}F", "inline": True},
            {"name": "Peak Window", "value": "YES" if peak else "No", "inline": True},
            {"name": "Best Bucket", "value": str(bucket), "inline": True},
            {"name": "Market Price", "value": f"{market_price:.3f}", "inline": True},
            {"name": "Model Probability", "value": f"{model_prob:.0%}", "inline": True},
            {"name": "Edge", "value": f"{edge:.1%}", "inline": True},
            {"name": "Signal", "value": signal, "inline": True},
            {"name": "Orders Placed", "value": str(n_orders), "inline": True},
            {"name": "Market Date", "value": str(market_date), "inline": True},
            {"name": "Market Status", "value": "OPEN" if market_open else "CLOSED", "inline": True},
            {"name": "Local Hour", "value": str(int(local_hour)), "inline": True},
        ]

        # Check for GFS ensemble data
        gfs_json = metrics.get("gfs_ensemble")
        if gfs_json:
            try:
                gfs_data = json.loads(gfs_json) if isinstance(gfs_json, str) else gfs_json
                card_fields.append({
                    "name": "GFS Ensemble",
                    "value": f"Mean: {gfs_data.get('mean', '?')}F | Std: {gfs_data.get('std', '?')}F | Spread: {gfs_data.get('spread', '?')}F",
                    "inline": False,
                })
            except Exception:
                pass

        send_embed(
            title=f"{city.upper()} DECISION CARD",
            description=f"Station analysis and trade signal for {city}",
            fields=card_fields,
            color=signal_color,
        )

        return {
            "embeds": [{
                "title": f"{city.upper()} DECISION CARD",
                "description": f"Station analysis and trade signal for {city}",
                "color": signal_color,
                "fields": card_fields,
            }]
        }

    except Exception as e:
        return {"content": f"Error building card for '{city}': {e}"}


def handle_halt_command() -> dict:
    """Set the TRADING_HALT flag in Redis. /halt - stop all trading."""
    try:
        import redis as _redis_mod
        r = _redis_mod.from_url(os.environ.get("REDIS_URL", ""))
        r.set("TRADING_HALT", "true")
        r.set("TRADING_HALT_SET_AT", datetime.now(timezone.utc).isoformat())
        send_embed(title="TRADING HALTED",
                   description="All trading stopped. Use /resume to restart.",
                   color=0xF85149)
        return {"content": "Trading HALTED. All trades paused."}
    except Exception as e:
        return {"content": f"Error setting halt: {e}"}


def handle_resume_command() -> dict:
    """Clear the TRADING_HALT flag in Redis. /resume - restart trading."""
    try:
        import redis as _redis_mod
        r = _redis_mod.from_url(os.environ.get("REDIS_URL", ""))
        r.delete("TRADING_HALT")
        r.set("TRADING_RESUMED_AT", datetime.now(timezone.utc).isoformat())
        send_embed(title="Trading Resumed",
                   description="Trading re-enabled.",
                   color=0x3FB950)
        return {"content": "Trading RESUMED."}
    except Exception as e:
        return {"content": f"Error resuming: {e}"}
