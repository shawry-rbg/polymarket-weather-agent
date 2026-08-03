"""
Polybot - Autonomous Polymarket Weather Trading Bot
Main entry point. Run with: python -m polybot.bot
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from polybot.cities import CITIES
from polybot.orchestrator import SwarmCoordinator, CityAgent, AgentMemory
from polybot.execution import TradeExecutor
from polybot.ai_brain import AIBrain

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

BANKROLL = float(os.environ.get("POLYBOT_BANKROLL", "100.0"))
TRADE_LOG = Path(__file__).resolve().parent / "trades.jsonl"
MEMORY_PATH = Path(__file__).resolve().parent / "memory.json"


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    log_file = LOG_DIR / f"polybot_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file),
        ],
    )
    print(f"[BOT] Logging to {log_file}")


async def run_once(verbose: bool = False) -> dict:
    """Execute a single bot run: analyze all cities, decide trades, execute."""
    setup_logging(verbose)
    logger = logging.getLogger("polybot.bot")

    print("=" * 60)
    print("  POLYBOT - Polymarket Weather Trading Bot")
    print(f"  Time: {datetime.now(timezone.utc).isoformat()}")
    print(f"  Bankroll: ${BANKROLL:.2f}")
    print(f"  Cities: {len(CITIES)}")
    print("=" * 60)

    # Initialize components
    executor = TradeExecutor(bankroll=BANKROLL, log_path=str(TRADE_LOG))
    brain = AIBrain()
    memory = AgentMemory(MEMORY_PATH)

    # Build city agents
    city_dicts = [
        {"name": c.name, "slug": c.slug, "lat": c.lat, "lon": c.lon}
        for c in CITIES
    ]
    agents = [CityAgent(cd) for cd in city_dicts]

    # Run swarm analysis
    coordinator = SwarmCoordinator(agents, bankroll=BANKROLL)
    results = await coordinator.run_swarm()

    # Execute trades
    executed = []
    for result in results:
        best = result.get("best_trade")
        if not best or best.get("kelly_usd", 0) <= 0:
            continue

        city = result["city"]
        direction = best.get("direction", "NONE")
        amount = best.get("kelly_usd", 0)
        price = best.get("yes_price", 0.5)
        question = best.get("question", "unknown")

        if direction == "NONE" or amount < 1.0:
            continue

        # Use AI brain for final confirmation
        try:
            ai_result = await brain.analyze_opportunity(
                city=city,
                current_temp=result.get("current_temp_f") or 0,
                forecast_temp=(result.get("forecast") or {}).get("temp_max_f") or 0,
                market_price=price,
                direction=direction,
            )
            confidence = ai_result.get("confidence", 0.5)
            if confidence < 0.55:
                print(f"[BOT] AI brain rejected {city} {direction} (confidence={confidence:.2f})")
                continue
        except Exception as e:
            logger.warning(f"AI brain error for {city}: {e}")
            # Proceed with rule-based trade if AI fails

        # Submit trade
        trade = executor.submit_trade(
            city=city,
            direction=direction,
            amount=amount,
            price=price,
            market_id="manual_" + city.lower().replace(" ", "_"),
            reason=f"EV={best.get('edge', 0):.1%} prob={best.get('true_prob', 0):.2f} forecast={result.get('forecast', {}).get('temp_max_f', '?')}F",
        )
        executed.append(trade)
        print(f"[BOT] Executed: {city} {direction} ${amount:.2f} @ {price:.3f}")

    # Summary
    summary = executor.get_performance_summary()
    print()
    print("=" * 60)
    print("  RUN SUMMARY")
    print(f"  Cities analyzed: {len(results)}")
    print(f"  Profitable signals: {sum(1 for r in results if r.get('best_trade'))}")
    print(f"  Trades executed: {len(executed)}")
    print(f"  Bankroll: ${summary['current_bankroll']:.2f} (PnL: ${summary['profit_loss']:+.2f})")
    print(f"  Total trades: {summary['total_trades']} Win rate: {summary['win_rate']:.0%}")
    print("=" * 60)

    # Store run report
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bankroll": summary["current_bankroll"],
        "pnl": summary["profit_loss"],
        "trades_executed": len(executed),
        "results": results,
    }
    report_path = LOG_DIR / f"report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    return report


def main():
    """CLI entry point."""
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    asyncio.run(run_once(verbose=verbose))


if __name__ == "__main__":
    main()
