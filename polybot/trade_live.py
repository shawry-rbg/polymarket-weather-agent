import modal
import os
import json

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "httpx>=0.28.0",
        "py-clob-client>=0.1.0",
        "eth-account>=0.13.0",
    )
    .add_local_dir(os.path.join(WORKSPACE, "polybot"), remote_path="/data/polybot", copy=True)
)

VOLUME = modal.Volume.from_name("polybot-data", create_if_missing=True)
VOLUME_PATH = "/polybot-data"

app = modal.App("polybot-trade", image=image)


@app.function(
    volumes={VOLUME_PATH: VOLUME},
    cpu=1.0,
    memory=512,
    timeout=120,
    secrets=[modal.Secret.from_name("polymarket-secrets")],
)
def place_real_trade():
    """Place a real $0.20 trade on London 28°C (best liquid opportunity)."""
    import sys, os, json, logging, asyncio
    sys.path.insert(0, "/data")
    logging.basicConfig(level=logging.INFO)

    from polybot.clob import execute_trade, get_usdc_balance, log_trade
    from polybot.polymarket import find_markets

    async def run():
        # Check balance first
        balance = get_usdc_balance()
        print(f"Wallet USDC balance: ${balance:.2f}")

        if balance < 0.30:
            return {"error": "insufficient_balance", "balance": balance, "needed": 0.30}

        # Find London 28°C market
        markets = await find_markets(city_name="London", date_str="May 30")
        target = None
        for m in markets:
            q = m.get("question", "")
            if "28°c" in q.lower() or "28 c" in q.lower():
                target = m
                break

        if not target:
            return {"error": "market_not_found", "available": [m.get("question","")[:60] for m in markets[:10]]}

        prices_str = target.get("outcomePrices", "[]")
        prices = json.loads(prices_str)
        yes_price = float(prices[0]) if len(prices) >= 2 else 0.5
        condition_id = target.get("conditionId", "")
        volume = float(target.get("volume24hr", 0) or 0)

        print(f"TARGET: {target.get('question','')}")
        print(f"YES_PRICE: ${yes_price:.3f}  VOLUME: ${volume:.0f}  COND_ID: {condition_id}")

        # Place the real order
        result = execute_trade(
            market_id=condition_id,
            side="YES",
            price=yes_price,
            size=0.20,
            trade_log_path=f"{VOLUME_PATH}/cashclaw_memory.jsonl",
        )

        # Also log to trade log with extra metadata
        if result and result.get("order_id"):
            log_trade({
                "timestamp": result.get("timestamp"),
                "city": "London",
                "bucket": "28°C",
                "side": "YES",
                "size": 0.20,
                "price": yes_price,
                "order_id": result.get("order_id"),
                "status": "open",
                "pnl": 0.0,
                "volume": volume,
                "edge": 0.565,
            }, f"{VOLUME_PATH}/cashclaw_memory.jsonl")

        return {
            "result": result,
            "balance_before": balance,
            "market": target.get("question", ""),
            "yes_price": yes_price,
            "volume": volume,
        }

    return asyncio.run(run())


@app.local_entrypoint()
def main():
    result = place_real_trade.remote()
    print(json.dumps(result, indent=2, default=str))
