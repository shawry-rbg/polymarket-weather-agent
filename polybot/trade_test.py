import modal
import os
import json

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "httpx>=0.28.0",
        "py-clob-client>=0.1.0",
    )
    .add_local_dir(os.path.join(WORKSPACE, "polybot"), remote_path="/data/polybot", copy=True)
)

app = modal.App("polybot-trade", image=image)

@app.function(
    cpu=1.0,
    memory=512,
    timeout=60,
    secrets=[modal.Secret.from_name("polymarket-secrets")],
)
def place_test_trade():
    """Place a real $0.20 test trade on London 28°C market."""
    import sys, os, json, logging
    sys.path.insert(0, "/data")
    logging.basicConfig(level=logging.INFO)

    from polybot.clob import execute_trade
    from polybot.polymarket import find_markets

    async def run():
        markets = await find_markets(city_name="London", date_str="May 30")
        target = None
        for m in markets:
            q = m.get("question", "").lower()
            if "28°c" in q or "28 c" in q or "28°c" in q:
                target = m
                break

        if not target:
            return {"error": "target_market_not_found", "available": [m.get("question","")[:60] for m in markets[:10]]}

        prices_str = target.get("outcomePrices", "[]")
        prices = json.loads(prices_str)
        yes_price = float(prices[0]) if len(prices) >= 2 else 0.5
        condition_id = target.get("conditionId", "")
        volume = float(target.get("volume24hr", 0) or 0)

        print(f"TARGET: {target.get('question','')[:80]}")
        print(f"YES_PRICE: {yes_price:.3f}  VOLUME: {volume:.0f}  COND_ID: {condition_id}")

        if yes_price < 0.30 or yes_price > 0.50:
            return {"skipped": "price_out_of_range", "price": yes_price, "target_range": "0.30-0.50", "note": "Market moved since scan"}

        result = execute_trade(
            market_id=condition_id,
            side="YES",
            price=yes_price,
            size=0.20,
        )
        return result

    import asyncio
    return asyncio.run(run())

@app.local_entrypoint()
def main():
    result = place_test_trade.remote()
    print(json.dumps(result, indent=2, default=str))
