"""
Rebalancer - Dynamic position rebalancing based on live probabilities.

Compares target allocation (from live_prob.py) with current holdings
(from positions.json) and executes buy/sell orders when drift > 5%.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

POSITIONS_PATH = "/polybot-data/positions.json"
MAX_REBALANCE_PCT = 0.10  # Max 10% of bankroll per bucket per rebalance
DRIFT_THRESHOLD = 0.05  # 5% drift triggers rebalance
HIGH_CONFIDENCE_THRESHOLD = 0.90
HIGH_CONFIDENCE_MULTIPLIER = 1.5
SELL_CONFIDENCE_THRESHOLD = 0.90
TAKE_PROFIT_PRICE = 0.60  # Sell if price > 0.60 and confident
STOP_LOSS_PRICE = 0.20  # Cut loss if price < 0.20


class PositionManager:
    """Track and manage current positions."""

    def __init__(self, positions_path: str = POSITIONS_PATH) -> None:
        self._path = Path(positions_path)
        self._positions: dict[str, dict] = {}  # key = f"{city}_{bucket}"
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    self._positions = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._positions = {}

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(self._positions, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Failed to save positions: {e}")

    def get_position(self, city: str, bucket: str) -> Optional[dict]:
        key = f"{city}_{bucket}"
        return self._positions.get(key)

    def get_all_positions(self) -> dict[str, dict]:
        return dict(self._positions)

    def update_position(self, city: str, bucket: str, update: dict) -> None:
        key = f"{city}_{bucket}"
        if key not in self._positions:
            self._positions[key] = {
                "city": city,
                "bucket": bucket,
                "size": 0.0,
                "entry_price": 0.0,
                "current_price": 0.0,
                "side": "YES",
                "opened_at": None,
                "last_rebalanced": None,
                "confidence_at_entry": 0.0,
            }
        self._positions[key].update(update)
        self._positions[key]["last_updated"] = datetime.now(timezone.utc).isoformat()
        self.save()

    def remove_position(self, city: str, bucket: str) -> None:
        key = f"{city}_{bucket}"
        if key in self._positions:
            del self._positions[key]
            self.save()

    def get_position_ids(self) -> set[str]:
        return set(self._positions.keys())


def compute_target_allocation(
    live_probs: dict,
    markets: dict,
    bankroll: float,
) -> dict[str, float]:
    """
    Compute target allocation per bucket based on live probabilities.

    Args:
        live_probs: {bucket_threshold: probability}
        markets: {bucket_threshold: {yes_price, conditionId, ...}}
        bankroll: current bankroll

    Returns:
        {bucket_key: target_usd_amount}
    """
    targets = {}
    for bucket, prob in live_probs.items():
        bucket_str = str(bucket)
        market = markets.get(bucket_str, {})
        yes_price = market.get("yes_price", 0.5)

        # Skip extreme prices
        if yes_price < 0.02 or yes_price > 0.98:
            continue

        edge = prob - yes_price
        if edge <= 0:
            # Check if we should sell existing position
            if prob < 0.1 and yes_price > 0.5:
                targets[bucket_str] = 0.0  # Target: zero (sell)
            continue

        # Kelly-like sizing for rebalancing
        b = (1.0 / yes_price) - 1.0
        kelly_f = (b * prob - (1 - prob)) / b if b > 0 else 0
        kelly_f *= 0.25  # Quarter Kelly

        # Confidence multiplier for high-confidence buckets
        if prob > HIGH_CONFIDENCE_THRESHOLD and yes_price < 0.50:
            kelly_f *= HIGH_CONFIDENCE_MULTIPLIER

        size = kelly_f * bankroll
        size = min(size, bankroll * MAX_REBALANCE_PCT)
        size = max(0, size)

        targets[bucket_str] = round(size, 2)

    return targets


async def rebalance_city(
    city: str,
    live_probs: dict,
    markets: dict,
    bankroll: float,
    trade_func,
    position_manager: PositionManager,
) -> list[dict]:
    """
    Rebalance positions for a single city.

    1. Compute target allocation from live probabilities
    2. Compare with current holdings
    3. If target > holding + 5%: buy YES
    4. If target < holding - 5%: sell YES (take profit / cut loss)
    5. Use take-profit and stop-loss rules

    Args:
        city: city name
        live_probs: {bucket: prob} from live_prob.py
        markets: {bucket: market_data}
        bankroll: current bankroll
        trade_func: async function(market_id, side, price, size) -> result
        position_manager: PositionManager instance

    Returns:
        List of rebalance trades executed
    """
    actions = []
    targets = compute_target_allocation(live_probs, markets, bankroll)

    # Process each bucket with a target
    for bucket, target_usd in targets.items():
        current = position_manager.get_position(city, bucket)
        current_size = current.get("size", 0.0) if current else 0.0
        current_price = current.get("current_price", 0.0) if current else 0.0
        market = markets.get(bucket, {})
        yes_price = market.get("yes_price", 0.5)
        condition_id = market.get("conditionId", "")
        prob = live_probs.get(int(bucket), live_probs.get(bucket, 0.5))

        # Take profit: if price > $0.60 and we have position
        if current_size > 0 and current_price > 0 and yes_price >= TAKE_PROFIT_PRICE and prob > SELL_CONFIDENCE_THRESHOLD:
            actions.append({
                "action": "SELL",
                "city": city,
                "bucket": bucket,
                "reason": "take_profit",
                "size": current_size,
                "price": yes_price,
                "conditionId": condition_id,
                "confidence": prob,
            })
            continue

        # Stop loss: if price dropped below $0.20
        if current_size > 0 and yes_price <= STOP_LOSS_PRICE:
            actions.append({
                "action": "SELL",
                "city": city,
                "bucket": bucket,
                "reason": "stop_loss",
                "size": current_size,
                "price": yes_price,
                "conditionId": condition_id,
                "confidence": prob,
            })
            continue

        # Drift-based rebalance
        drift = target_usd - current_size
        if abs(drift) < bankroll * DRIFT_THRESHOLD:
            continue  # Within tolerance

        if drift > 0 and condition_id:
            # Buy more YES
            actions.append({
                "action": "BUY",
                "city": city,
                "bucket": bucket,
                "reason": "increase_position",
                "size": min(drift, bankroll * MAX_REBALANCE_PCT),
                "price": yes_price,
                "conditionId": condition_id,
                "confidence": prob,
            })
        elif drift < 0 and current_size > 0 and condition_id:
            # Sell YES (reduce position)
            sell_size = min(abs(drift), current_size)
            actions.append({
                "action": "SELL",
                "city": city,
                "bucket": bucket,
                "reason": "decrease_position",
                "size": sell_size,
                "price": yes_price,
                "conditionId": condition_id,
                "confidence": prob,
            })

    # Execute trades
    executed = []
    for action in actions:
        try:
            if action["size"] <= 0:
                continue

            logger.info(
                f"[REBALANCE] {action['city']} bucket={action['bucket']}: "
                f"{action['action']} ${action['size']:.2f} @ {action['price']:.3f} "
                f"(reason={action['reason']}, conf={action['confidence']:.1%})"
            )

            result = await trade_func(
                action["conditionId"],
                "YES",
                action["price"],
                action["size"],
            )

            trade_record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "city": action["city"],
                "bucket": action["bucket"],
                "action": action["action"],
                "reason": action["reason"],
                "size": action["size"],
                "price": action["price"],
                "confidence": action["confidence"],
                "result": result,
            }
            executed.append(trade_record)

            # Update position tracking
            if action["action"] == "BUY":
                existing = current if current else {"size": 0}
                position_manager.update_position(
                    action["city"],
                    action["bucket"],
                    {
                        "size": existing.get("size", 0) + action["size"],
                        "current_price": action["price"],
                        "last_rebalanced": datetime.now(timezone.utc).isoformat(),
                    },
                )
            elif action["action"] == "SELL":
                new_size = max(0, current_size - action["size"]) if current else 0
                if new_size <= 0:
                    position_manager.remove_position(action["city"], action["bucket"])
                else:
                    position_manager.update_position(
                        action["city"],
                        action["bucket"],
                        {"size": new_size},
                    )

            # Log trade
            _log_rebalance_trade(trade_record)

        except Exception as e:
            logger.error(f"[REBALANCE] Trade execution error: {e}")

    return executed


def _log_rebalance_trade(trade: dict, log_path: str = "/polybot-data/rebalance_log.jsonl") -> None:
    """Append rebalance trade to log."""
    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(json.dumps(trade, default=str) + "\n")
    except Exception as e:
        logger.error(f"Failed to log rebalance trade: {e}")


async def rebalance_all(
    live_results: dict,
    all_markets: dict,
    bankroll: float,
    trade_func,
    position_manager: PositionManager,
) -> list[dict]:
    """
    Rebalance all cities.

    Args:
        live_results: {city: live_prob_results}
        all_markets: {city: {bucket: market_data}}
        bankroll: current bankroll
        trade_func: async trade function
        position_manager: PositionManager

    Returns:
        All executed rebalance trades
    """
    all_trades = []
    for city, live_data in live_results.items():
        if "error" in live_data:
            continue
        bucket_probs = live_data.get("bucket_probs", {})
        markets = all_markets.get(city, {})
        trades = await rebalance_city(
            city, bucket_probs, markets, bankroll, trade_func, position_manager,
        )
        all_trades.extend(trades)

    if all_trades:
        logger.info(f"[REBALANCE] Executed {len(all_trades)} rebalance trades across all cities")

    return all_trades
