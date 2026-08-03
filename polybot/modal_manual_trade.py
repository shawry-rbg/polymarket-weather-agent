"""Minimal single-market manual trade smoke test for Modal.

Usage:
  modal run polybot/modal_manual_trade.py::test_trade --city "New York"
"""

import os

import modal

IMAGE_PATH = "/workspaces/polymarket-weather-agent"
REMOTE_POLYBOT = "/data/polybot"

app = modal.App("polybot-test-trade")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git", "curl")
    .pip_install(
        "web3",
        "redis",
        "httpx",
        "python-dotenv",
        "py-clob-client-v2>=1.1.0",
        "modal",
        "nest-asyncio",
        "websockets",
    )
    .add_local_dir(
        os.path.join(IMAGE_PATH, "polybot"),
        remote_path=REMOTE_POLYBOT,
        copy=True,
    )
)

secrets = [
    modal.Secret.from_name("polymarket-secrets-live"),
]


@app.function(image=image, secrets=secrets, timeout=120, region="eu-west")
def test_trade(city: str = "New York", size: float = 0.10):
    import asyncio
    import sys
    sys.path.insert(0, "/data")
    from polybot.polymarket import find_markets
    from polybot.clob import execute_trade

    async def _inner():
        markets = await find_markets(city_name=city.strip(), date_str=None)
        print(f"[TEST] city={city} markets_found={len(markets)}")
        if markets:
            m = markets[0]
            print(f"[TEST] using: {m.get('question','')[:100]} -> {m.get('conditionId','')}")
        else:
            return {"ok": False, "error": "no_markets", "city": city}

        mid = m.get("conditionId") or m.get("slug") or ""
        if not mid:
            return {"ok": False, "error": "missing_market_id", "market": m}

        # Limit to a tight bid to avoid accidental fills.
        price = 0.01
        try:
            orderbook = m.get("order_book") or {}
            best_bid = orderbook.get("bestBid") or m.get("bestBid")
            if best_bid:
                price = float(best_bid)
        except Exception:
            pass

        print(f"[TEST] execute_trade market={mid} side=BUY price={price} size={size}")
        result = await execute_trade(
            market_id=mid,
            side="BUY",
            price=price,
            size=float(size),
            city=city,
            bucket=m.get("question", "")[:40],
        )
        print(f"[TEST] result={result}")
        return {
            "ok": True,
            "city": city,
            "market_id": mid,
            "price": price,
            "size": size,
            "result": result,
        }

    return asyncio.run(_inner())
