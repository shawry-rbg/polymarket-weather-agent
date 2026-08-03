"""Rebalance / portfolio management."""
import logging
import os
from polybot.clob import submit_trade

logger = logging.getLogger("polybot.rebalance")


def rebalance_portfolio(summary) -> None:
    try:
        candidate_trades = [
            r for r in summary.results
            if getattr(r, "action", None) == "TRADE" and getattr(r, "token_id", None)
        ]
        logger.info("rebalance: evaluating %d candidate trades", len(candidate_trades))

        DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
        for result in candidate_trades:
            if not result.token_id or not result.side or not result.price:
                continue
            size = getattr(result, "size", 5.0)
            logger.info(
                f"Attempting trade: {result.city} | {result.side} {size} @ {result.price} | token {result.token_id[:8]}..."
            )
            if DRY_RUN:
                logger.info(f"🔴 DRY_RUN: would have placed trade on {result.city}")
                continue
            trade_result = submit_trade(
                token_id=result.token_id,
                side=result.side,
                price=float(result.price),
                size=float(size),
            )
            if trade_result.get("ok"):
                result.tx_hash = trade_result.get("tx_hash")
                logger.info(f"✅ Trade placed! tx: {trade_result.get('tx_hash')}")
            else:
                logger.error(f"❌ Trade failed: {trade_result.get('error')}")
    except Exception as e:
        logger.exception("rebalance_portfolio failed")
