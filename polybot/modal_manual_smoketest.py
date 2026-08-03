"""
Minimal single-city sanity check for manual Modal use.

Usage:
  modal run polybot/modal_manual_smoketest.py::run_city --city "New York"
  modal run polybot/modal_manual_smoketest.py::check_balance
"""

import os

import modal

IMAGE_PATH = "/workspaces/polymarket-weather-agent"
REMOTE_POLYBOT = "/data/polybot"

app = modal.App("polybot-test")

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


@app.function(image=image, secrets=secrets, timeout=600, cpu=2.0, memory=4096, region="eu-west")
def run_city(city: str = ""):
    import asyncio
    import sys
    sys.path.insert(0, "/data")
    from polybot.cities import ACTIVE_CITIES
    from polybot.polymarket import find_markets
    from polybot.orchestrator import CityAgent

    target = city.strip() or ACTIVE_CITIES[0]["name"]
    print(f"[SMOKE] city={target}")

    async def _inner():
        found = []
        for day_offset in range(3):
            date_str = (__import__("datetime").datetime.now(__import__("datetime").timezone.utc) + __import__("datetime").timedelta(days=day_offset)).strftime("%B %-d")
            try:
                markets = await find_markets(city_name=target, date_str=date_str)
            except Exception as exc:
                print(f"[SMOKE] find_markets error: {exc}")
                markets = []
            if markets:
                print(f"[SMOKE] found {len(markets)} market(s) on {date_str}")
                for m in markets[:10]:
                    q = m.get("question", "")[:90]
                    cid = m.get("conditionId", "")[:12]
                    ed = m.get("endDate", "")
                    print(f"  {q} conditionId={cid}... endDate={ed}")
                found = markets
                break

        agent = CityAgent({"name": target, "slug": target.lower(), "lat": 0.0, "lon": 0.0, "unit": "F", "buckets": []})
        analysis = await agent.analyze(bankroll=0.10, date_detector=None, risk_manager=None)
        return {
            "city": target,
            "markets_found": len(found),
            "analysis_type": type(analysis).__name__,
            "analysis_keys": sorted(analysis.keys()) if isinstance(analysis, dict) else [],
        }

    return asyncio.run(_inner())


@app.function(image=image, secrets=secrets, timeout=120, region="eu-west")
def check_balance():
    from polybot.clob import get_usdc_balance

    balance = get_usdc_balance()
    return {"balance_usdc": float(balance)}
