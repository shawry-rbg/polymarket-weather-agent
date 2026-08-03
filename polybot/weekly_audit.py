"""
Weekly self-improvement audit.

Runs every Sunday at 00:00 UTC via Modal cron.
Loads resolved paper trades from Redis (last 7 days), analyzes performance,
and suggests parameter adjustments for MIN_EDGE, DEVIATION_TRIGGER, and city weights.

Outputs suggestions to Discord for manual approval (first 2 weeks).
"""

import json
import os
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


def get_resolved_trades_last_7_days(r) -> list[dict]:
    """Fetch resolved paper trades from the last 7 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    all_raw = r.lrange("paper_trades", 0, 499)
    resolved = []
    for item in all_raw:
        try:
            t = json.loads(item)
            if t.get("status") != "resolved":
                continue
            resolved_at_str = t.get("resolved_at", "")
            if resolved_at_str:
                try:
                    resolved_at = datetime.fromisoformat(resolved_at_str.replace("Z", "+00:00"))
                    if resolved_at >= cutoff:
                        resolved.append(t)
                except ValueError:
                    pass
        except Exception:
            pass
    return resolved


def compute_per_city_stats(trades: list[dict]) -> dict[str, dict]:
    """Compute per-city win rate, avg edge, total PnL."""
    city_stats: dict[str, dict] = {}
    for t in trades:
        city = t.get("city", "unknown")
        if city not in city_stats:
            city_stats[city] = {"wins": 0, "losses": 0, "total_pnl": 0.0, "trades": 0}
        city_stats[city]["trades"] += 1
        won = t.get("won", "0") == "1" or t.get("won") is True
        if won:
            city_stats[city]["wins"] += 1
        else:
            city_stats[city]["losses"] += 1
        try:
            pnl = float(t.get("profit_usd", 0))
        except (ValueError, TypeError):
            pnl = 0.0
        city_stats[city]["total_pnl"] += pnl

    for city, stats in city_stats.items():
        total = stats["wins"] + stats["losses"]
        stats["win_rate"] = stats["wins"] / total if total > 0 else 0.0
        stats["avg_edge"] = stats["total_pnl"] / total if total > 0 else 0.0

    return city_stats


def generate_suggestions(city_stats: dict[str, dict], current_min_edge: float = 0.12) -> list[dict]:
    """
    Generate parameter adjustment suggestions based on 7-day performance.
    Rule-based approach (no LLM dependency for now).
    """
    suggestions = []

    for city, stats in city_stats.items():
        if stats["trades"] < 3:
            continue  # Not enough data

        win_rate = stats["win_rate"]
        avg_pnl = stats["avg_edge"]

        if win_rate < 0.40 and avg_pnl < -0.01:
            # Poor performance — increase edge threshold for this city
            suggestions.append({
                "type": "INCREASE_MIN_EDGE",
                "city": city,
                "current": current_min_edge,
                "suggested": round(min(current_min_edge + 0.03, 0.25), 2),
                "reason": f"Win rate {win_rate:.0%} | Avg PnL ${avg_pnl:.4f} over {stats['trades']} trades",
            })
        elif win_rate > 0.65 and avg_pnl > 0.01:
            # Strong performance — could lower threshold slightly for more opportunities
            suggestions.append({
                "type": "DECREASE_MIN_EDGE",
                "city": city,
                "current": current_min_edge,
                "suggested": round(max(current_min_edge - 0.02, 0.08), 2),
                "reason": f"Win rate {win_rate:.0%} | Avg PnL ${avg_pnl:.4f} over {stats['trades']} trades",
            })

        if stats["total_pnl"] < -0.05:
            # Significant losses — suggest reducing city weight
            suggestions.append({
                "type": "REDUCE_CITY_WEIGHT",
                "city": city,
                "reason": f"Total PnL ${stats['total_pnl']:.4f} over {stats['trades']} trades — consider reducing allocation",
            })

    return suggestions


def format_discord_report(
    trades: list[dict],
    city_stats: dict[str, dict],
    suggestions: list[dict],
) -> str:
    """Format a Discord-friendly report."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total_pnl = sum(float(t.get("profit_usd", 0) or 0) for t in trades)
    total_trades = len(trades)
    wins = sum(1 for t in trades if t.get("won") in ("1", True))

    lines = [
        f"**Weekly Audit Report** — {now}",
        f"Period: last 7 days",
        f"",
        f"**Summary:** {total_trades} trades | {wins}W/{total_trades - wins}L | PnL: {'+'if total_pnl >= 0 else ''}${total_pnl:.4f}",
        f"",
    ]

    if city_stats:
        lines.append("**Per-City Performance:**")
        for city, stats in sorted(city_stats.items(), key=lambda x: x[1]["total_pnl"], reverse=True):
            lines.append(
                f"  {city}: {stats['win_rate']:.0%} WR | ${stats['total_pnl']:+.4f} | {stats['trades']} trades"
            )
        lines.append("")

    if suggestions:
        lines.append("**Suggested Adjustments (manual approval required):**")
        for s in suggestions:
            lines.append(f"  [{s['type']}] {s['city']}: {s.get('reason', '')}")
            if "suggested" in s:
                lines.append(f"    Current: {s['current']} → Suggested: {s['suggested']}")
        lines.append("")
        lines.append("To apply: `hermes config set min_edge <value>` or edit the constants directly.")
    else:
        lines.append("**No adjustments suggested.** Performance is within normal parameters.")

    return "\n".join(lines)


def run_weekly_audit():
    """
    Main entry point for the weekly audit cron job.
    Called by Modal: modal_run polybot/weekly_audit.py
    """
    import redis as _redis_mod

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    redis_url = os.environ.get("REDIS_URL", "")
    if not redis_url:
        print("[AUDIT] No REDIS_URL — aborting")
        return {"error": "no redis"}

    r = _redis_mod.from_url(redis_url)

    # Fetch resolved trades
    trades = get_resolved_trades_last_7_days(r)
    print(f"[AUDIT] Found {len(trades)} resolved trades in last 7 days")

    if not trades:
        print("[AUDIT] No resolved trades — nothing to audit")
        try:
            from polybot.notify import send_embed
            send_embed(
                title="📊 Weekly Audit — No Data",
                description="No resolved trades in the last 7 days. Nothing to audit.",
                color=0x5865F2,
            )
        except Exception:
            pass
        return {"status": "no_data", "trades": 0}

    # Compute stats
    city_stats = compute_per_city_stats(trades)
    print(f"[AUDIT] City stats: {json.dumps(city_stats, indent=2, default=str)}")

    # Compute and store per-city bias correction
    try:
        from polybot.bias_correction import compute_bias_from_resolved_trades, store_bias_in_redis
        bias_dict = compute_bias_from_resolved_trades(trades)
        if bias_dict:
            store_bias_in_redis(bias_dict)
            print(f"[AUDIT] Bias corrections computed: {bias_dict}")
        else:
            print("[AUDIT] No bias corrections (insufficient resolved trade data)")
    except Exception as e:
        print(f"[AUDIT] Bias correction error: {e}")

    # Generate suggestions
    suggestions = generate_suggestions(city_stats)
    print(f"[AUDIT] {len(suggestions)} suggestions generated")

    # Format and send report
    report = format_discord_report(trades, city_stats, suggestions)
    print(f"[AUDIT] Report:\n{report}")

    try:
        from polybot.notify import send_embed
        fields = []
        for city, stats in sorted(city_stats.items(), key=lambda x: x[1]["total_pnl"], reverse=True)[:8]:
            fields.append({
                "name": city,
                "value": f"WR: {stats['win_rate']:.0%} | PnL: ${stats['total_pnl']:+.4f} | {stats['trades']} trades",
                "inline": True,
            })

        suggestion_text = "\n".join(
            f"[{s['type']}] {s['city']}: {s.get('reason', '')}"
            for s in suggestions[:5]
        ) or "No adjustments suggested."

        fields.append({"name": "Suggestions", "value": suggestion_text, "inline": False})

        total_pnl = sum(float(t.get("profit_usd", 0) or 0) for t in trades)
        color = 0x3FB950 if total_pnl >= 0 else 0xF85149

        send_embed(
            title="📊 Weekly Audit Report",
            description=f"{len(trades)} trades | PnL: {'+'if total_pnl >= 0 else ''}${total_pnl:.4f}",
            fields=fields,
            color=color,
        )
    except Exception as e:
        print(f"[AUDIT] Discord send error: {e}")

    return {
        "status": "complete",
        "trades_analyzed": len(trades),
        "cities": len(city_stats),
        "suggestions": len(suggestions),
        "total_pnl": sum(float(t.get("profit_usd", 0) or 0) for t in trades),
    }


if __name__ == "__main__":
    result = run_weekly_audit()
    print(f"[AUDIT] Result: {result}")
