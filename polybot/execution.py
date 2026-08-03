"""
Polybot - Trade Execution, Settlement & Learning Module

Inspired by CashClaw's agent loop architecture.
Handles trade submission (via Polymarket CLOB API),
outcome settlement against actual temperatures,
and performance-driven learning for Kelly adjustment.
"""

import uuid
import json
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_BANKROLL: float = 100.0
DEFAULT_LOG_PATH: str = "polybot/trades.jsonl"

TRADE_STATUS_PENDING: str = "pending"
TRADE_STATUS_SETTLED: str = "settled"
TRADE_STATUS_FAILED: str = "failed"


class TradeExecutor:
    """Manages trade lifecycle: submission, settlement, and learning."""

    def __init__(
        self,
        bankroll: float = DEFAULT_BANKROLL,
        log_path: str = DEFAULT_LOG_PATH,
    ) -> None:
        self.bankroll: float = bankroll
        self.log_path: Path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        self._pending_trades: list[dict] = []
        self._settled_trades: list[dict] = []

        self._load_trades()
        logger.info(
            "TradeExecutor initialised | bankroll=%.2f | log=%s",
            self.bankroll,
            self.log_path,
        )

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load_trades(self) -> None:
        """Load trade history from JSONL log into memory."""
        if not self.log_path.exists():
            logger.info("No existing trade log at %s — starting fresh.", self.log_path)
            return

        pending_count = 0
        settled_count = 0
        with self.log_path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    trade = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Corrupt JSON at line %d — skipping.", line_no)
                    continue

                if trade.get("status") == TRADE_STATUS_PENDING:
                    self._pending_trades.append(trade)
                    pending_count += 1
                else:
                    self._settled_trades.append(trade)
                    settled_count += 1

        logger.info(
            "Loaded %d pending and %d settled trades from %s",
            pending_count,
            settled_count,
            self.log_path,
        )

    def _append_trade(self, trade: dict) -> None:
        """Append a single trade record to the JSONL log."""
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(trade) + "\n")

    def _rewrite_log(self) -> None:
        """Rewrite the entire JSONL log from in-memory state."""
        with self.log_path.open("w", encoding="utf-8") as fh:
            for trade in self._pending_trades:
                fh.write(json.dumps(trade) + "\n")
            for trade in self._settled_trades:
                fh.write(json.dumps(trade) + "\n")

    # ------------------------------------------------------------------
    # Trade submission
    # ------------------------------------------------------------------

    def submit_trade(
        self,
        city: str,
        direction: str,
        amount: float,
        price: float,
        market_id: str,
        reason: str,
    ) -> dict:
        """
        Record and (simulate) execute a trade via Polymarket CLOB API.

        Parameters
        ----------
        city : str
            City the trade relates to.
        direction : str
            'YES' or 'NO'.
        amount : float
            Dollar amount to wager.
        price : float
            Execution price (0-1 range, Polymarket style).
        market_id : str
            Polymarket condition/market identifier.
        reason : str
            Human-readable rationale.

        Returns
        -------
        dict
            Normalised trade record.
        """
        direction = direction.upper()
        if direction not in ("YES", "NO"):
            raise ValueError(f"direction must be 'YES' or 'NO', got {direction!r}")

        if amount <= 0:
            raise ValueError(f"amount must be positive, got {amount}")

        if not (0.0 < price < 1.0):
            raise ValueError(f"price must be between 0 and 1 exclusive, got {price}")

        if amount > self.bankroll:
            logger.warning(
                "Trade amount %.2f exceeds bankroll %.2f — rejecting.",
                amount,
                self.bankroll,
            )
            raise ValueError("Insufficient bankroll.")

        trade_id = uuid.uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat()

        trade: dict = {
            "id": trade_id,
            "city": city,
            "direction": direction,
            "amount": round(amount, 4),
            "price": round(price, 6),
            "market_id": market_id,
            "reason": reason,
            "timestamp": timestamp,
            "status": TRADE_STATUS_PENDING,
            "outcome": None,
        }

        # --- Simulate Polymarket CLOB execution --
        # TODO: integrate real CLOB API call here.
        # For now we accept the trade and log it.
        logger.info(
            "Executing %s %s $%.2f @ %.4f on market %s (%s)",
            city,
            direction,
            amount,
            price,
            market_id,
            reason,
        )

        # Deduct from bankroll
        self.bankroll = round(self.bankroll - amount, 4)
        logger.info("Bankroll after trade: %.4f", self.bankroll)

        # Persist
        self._pending_trades.append(trade)
        self._append_trade(trade)
        logger.info("Trade %s recorded — bankroll now %.4f", trade_id, self.bankroll)

        return trade

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    def settle_trades(self, temperatures: dict) -> list[dict]:
        """
        Settle all pending trades against observed temperatures.

        The settlement logic assumes a Polymarket contract that pays $1
        if the condition is met (YES) or not met (NO).

        Parameters
        ----------
        temperatures : dict
            ``{city: actual_max_temp_f}`` mapping.

        Returns
        -------
        list[dict]
            Settlement result records.
        """
        if not self._pending_trades:
            logger.info("No pending trades to settle.")
            return []

        results: list[dict] = []
        still_pending: list[dict] = []

        for trade in self._pending_trades:
            city = trade["city"]
            if city not in temperatures:
                logger.warning(
                    "No temperature for %s — trade %s remains pending.",
                    city,
                    trade["id"],
                )
                still_pending.append(trade)
                continue

            actual_temp = temperatures[city]
            won = self._determine_outcome(trade, actual_temp)

            payout = 0.0
            if won:
                payout = self._compute_payout(trade)
                self.bankroll = round(self.bankroll + payout, 4)

            trade["status"] = TRADE_STATUS_SETTLED
            trade["outcome"] = "win" if won else "loss"
            actual_key = "actual_temp_f"
            trade[actual_key] = actual_temp
            trade["payout"] = round(payout, 4)
            trade["settled_at"] = datetime.now(timezone.utc).isoformat()

            result = {
                "trade_id": trade["id"],
                "city": city,
                "direction": trade["direction"],
                "amount": trade["amount"],
                "price": trade["price"],
                "actual_temp_f": actual_temp,
                "outcome": trade["outcome"],
                "payout": round(payout, 4),
                "bankroll_after": self.bankroll,
            }
            results.append(result)
            self._settled_trades.append(trade)
            logger.info(
                "Settled %s %s — %s (payout %.4f, bankroll %.4f)",
                city,
                trade["id"],
                trade["outcome"],
                payout,
                self.bankroll,
            )

        self._pending_trades = still_pending
        self._rewrite_log()

        logger.info(
            "Settlement complete: %d settled, %d still pending.",
            len(results),
            len(still_pending),
        )
        return results

    @staticmethod
    def _determine_outcome(trade: dict, actual_temp: float) -> bool:
        """
        Decide if a trade won.

        Convention:
        - YES  → the temperature exceeded the threshold implied by the market.
        - NO   → it did not exceed it.

        For the MVP we use a simple rule:
        YES wins when actual_temp >= 90 °F, otherwise NO wins.
        When richer metadata is available on the trade record the
        threshold key is consulted instead.
        """
        threshold = trade.get("threshold_f", 90.0)
        exceeded = actual_temp >= threshold
        if trade["direction"] == "YES":
            return exceeded
        return not exceeded

    @staticmethod
    def _compute_payout(trade: dict) -> float:
        """
        Polymarket binary contract payout.

        YES: profit = (1 - price) * amount
        NO:  profit = price * amount
        The total returned to bankroll is amount + profit (= payout).
        """
        amount = trade["amount"]
        price = trade["price"]
        if trade["direction"] == "YES":
            profit = (1.0 - price) * amount
        else:
            profit = price * amount
        return amount + profit

    # ------------------------------------------------------------------
    # Performance summary
    # ------------------------------------------------------------------

    def get_performance_summary(self) -> dict:
        """Return aggregate performance statistics."""
        settled = self._settled_trades
        wins = [t for t in settled if t.get("outcome") == "win"]
        losses = [t for t in settled if t.get("outcome") == "loss"]
        total_trades = len(settled)
        win_rate = (len(wins) / total_trades) if total_trades > 0 else 0.0

        total_wagered = sum(t["amount"] for t in settled)
        total_payout = sum(t.get("payout", 0.0) for t in settled)
        profit_loss = round(total_payout - total_wagered, 4)

        summary = {
            "total_trades": total_trades,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 4),
            "current_bankroll": round(self.bankroll, 4),
            "profit_loss": profit_loss,
            "total_wagered": round(total_wagered, 4),
            "total_payout": round(total_payout, 4),
            "pending_trades": len(self._pending_trades),
        }
        logger.info("Performance summary: %s", json.dumps(summary))
        return summary

    # ------------------------------------------------------------------
    # Learning
    # ------------------------------------------------------------------

    async def learn_from_outcomes(self) -> dict:
        """
        Analyse historical trades and derive adjusted Kelly fractions.

        Computes per-city accuracy, identifies the best/worst performing
        cities and directions, and recommends Kelly multiplier adjustments.

        Returns
        -------
        dict
            Learning statistics and recommendations.
        """
        logger.info("Learning from %d settled trades …", len(self._settled_trades))

        # Allow other coroutines to run
        await asyncio.sleep(0)

        settled = self._settled_trades
        if not settled:
            logger.info("No settled trades — nothing to learn yet.")
            return {
                "baseline_kelly": 0.25,
                "city_adjustments": {},
                "recommendation": "No data — use conservative Kelly (0.25).",
            }

        # --- Per-city accuracy ------------------------------------------
        city_stats: dict[str, dict] = {}
        for trade in settled:
            city = trade["city"]
            city_stats.setdefault(city, {"total": 0, "wins": 0})
            city_stats[city]["total"] += 1
            if trade.get("outcome") == "win":
                city_stats[city]["wins"] += 1

        city_adjustments: dict[str, float] = {}
        for city, stats in city_stats.items():
            accuracy = stats["wins"] / stats["total"] if stats["total"] else 0.0
            # Simple adjustment: scale Kelly factor by (accuracy - 0.5) * 2
            # Range roughly -1 … +1 for 0 … 100 % accuracy.
            adjustment = round((accuracy - 0.5) * 2.0, 4)
            city_adjustments[city] = adjustment

        # --- Per-direction accuracy -------------------------------------
        dir_stats: dict[str, dict] = {}
        for trade in settled:
            d = trade["direction"]
            dir_stats.setdefault(d, {"total": 0, "wins": 0})
            dir_stats[d]["total"] += 1
            if trade.get("outcome") == "win":
                dir_stats[d]["wins"] += 1

        dir_accuracy: dict[str, float] = {}
        for d, stats in dir_stats.items():
            dir_accuracy[d] = round(
                stats["wins"] / stats["total"] if stats["total"] else 0.0, 4
            )

        # --- Recommendation ---------------------------------------------
        best_city = max(city_adjustments, key=lambda k: city_adjustments[k]) if city_adjustments else "N/A"
        worst_city = min(city_adjustments, key=lambda k: city_adjustments[k]) if city_adjustments else "N/A"
        baseline_kelly = 0.25

        worst_adj = city_adjustments.get(worst_city, 0)
        best_adj = city_adjustments.get(best_city, 0)
        recommendation = (
            f"Baseline Kelly {baseline_kelly}. "
            f"Best city: {best_city} (adj {best_adj:+.3f}). "
            f"Worst city: {worst_city} (adj {worst_adj:+.3f}). "
            f"YES acc: {dir_accuracy.get('YES', 'N/A')}, "
            f"NO acc: {dir_accuracy.get('NO', 'N/A')}."
        )

        result = {
            "baseline_kelly": baseline_kelly,
            "city_adjustments": city_adjustments,
            "direction_accuracy": dir_accuracy,
            "best_city": best_city,
            "worst_city": worst_city,
            "total_trades_analysed": len(settled),
            "recommendation": recommendation,
        }

        logger.info("Learning complete: %s", recommendation)
        return result


# ---------------------------------------------------------------------------
# Convenience: synchronous wrapper for learn_from_outcomes
# ---------------------------------------------------------------------------

def _run_learn(executor: TradeExecutor) -> dict:
    """Helper to run async learning synchronously."""
    return asyncio.run(executor.learn_from_outcomes())
