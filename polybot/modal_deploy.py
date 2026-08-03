import os
import random
import requests

# Bright Data MCP configuration
BRIGHTDATA_API_KEY = os.environ.get("BRIGHTDATA_API_KEY", "95e07e0a-67ff-4301-ad13-461be37952d8")
BRIGHTDATA_MCP_URL = os.environ.get("BRIGHTDATA_MCP_URL", "https://mcp.brightdata.com")


def mcp_fetch(url: str):
    """Fetch a URL through Bright Data MCP."""
    response = requests.post(
        f"{BRIGHTDATA_MCP_URL}/fetch",
        json={"url": url},
        headers={"Authorization": f"Bearer {BRIGHTDATA_API_KEY}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


os.environ["BRIGHTDATA_API_KEY"] = BRIGHTDATA_API_KEY

# Proxy rotation list
PROXY_LIST = [
"http://ikumpigi:j4hh624a1jdm@84.247.60.125:6095",
"http://ikumpigi:j4hh624a1jdm@142.111.67.146:5611",
"http://xmkdrvmz:6msj3ti5yzb1@84.247.60.125:6095",
"http://xmkdrvmz:6msj3ti5yzb1@142.111.67.146:5611",
"http://xbemoufo:119ciesi47gv@84.247.60.125:6095",
"http://xbemoufo:119ciesi47gv@142.111.67.146:5611",
]
# Select a random proxy
proxy = random.choice(PROXY_LIST)
os.environ["HTTP_PROXY"] = proxy
os.environ["HTTPS_PROXY"] = proxy
print(f"🔀 Using proxy: {proxy[:30]}...")
import os
import sys
import asyncio
import modal
from pathlib import Path

WORKSPACE = "/workspaces/polymarket-weather-agent"

app = modal.App("polybot")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git", "curl")
    .pip_install(
        "web3",
        "redis",
        "httpx",
        "python-dotenv",
        "py-clob-client-v2",
        "polymarket",
        "modal",
        "nest-asyncio",
        "websockets",
    )
    .add_local_dir(
        os.path.join(WORKSPACE, "polybot"),
        remote_path="/data/polybot",
        copy=True,
    )
)

secrets = [
    modal.Secret.from_name("polymarket-secrets"),
    modal.Secret.from_name("redis-url"),
]


@app.function(
    image=image,
    secrets=secrets,
    schedule=modal.Cron("*/15 * * * *"),
    cpu=2.0,
    memory=4096,
    timeout=1800,
    region="eu-west",
)
def scheduled_bot():
    import sys, asyncio
    sys.path.insert(0, '/data')
    from polybot.orchestrator import run_full_scan
    bankroll = float(os.environ.get("POLYBOT_BANKROLL", "0.10"))
    asyncio.run(run_full_scan(subset='all', bankroll=bankroll))


@app.function(
    image=image,
    secrets=secrets,
    volumes={"/polybot-data": modal.Volume.from_name("polybot-data", create_if_missing=True)},
    cpu=2.0,
    memory=4096,
    timeout=1800,
    schedule=modal.Cron("*/15 * * * *"),
    region="eu-west",
)
def run_polybot_manual(subset=None, bankroll=0.10):
    """Run the full bot manually."""
    import sys, asyncio, logging
    sys.path.insert(0, '/data')
    from polybot.orchestrator import run_full_scan

    # Modal CLI may pass args as strings; normalize to expected types.
    try:
        subset = int(subset) if subset is not None and not isinstance(subset, int) else subset
    except Exception:
        subset = None
    try:
        bankroll = float(bankroll)
    except Exception:
        bankroll = 0.10

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    return asyncio.run(run_full_scan(subset=subset, bankroll=bankroll))


@app.function(
    image=image,
    secrets=secrets,
    timeout=120,
    region="ap-south",
)
def run_polybot_sdk_manual(city: str = "New York", price: float = 0.05, size: float = 0.05):
    """Smoke-test the SDK POLY_1271 order path without touching orchestrator.

    Uses the existing Modal app image exactly: py-clob-client-v2, redis,
    httpx, web3, modal, eth-* libs, and the copied /data/polybot source.
    """
    import os, sys, json
    sys.path.insert(0, '/data')

    order_host = os.environ.get("CLOB_HOST", "https://clob.polymarket.com").rstrip("/")
    proxy_host = os.environ.get("CF_WORKER_URL", "https://poly-proxy.elvischemoiywo.workers.dev/clob").rstrip("/")
    pk = os.environ.get("PK", "")
    funder = os.environ.get("FUNDER", "") or os.environ.get("PROXY_WALLET", "")
    relayer_key = os.environ.get("RELAYER_API_KEY", "")
    relayer_address = os.environ.get("RELAYER_API_KEY_ADDRESS", "")
    api_key = os.environ.get("CLOB_API_KEY", "")
    api_secret = os.environ.get("CLOB_SECRET", "")
    api_passphrase = os.environ.get("CLOB_PASS_PHRASE", "")

    missing = [name for name, val in [
        ("PK", pk),
        ("FUNDER/PROXY_WALLET", funder),
        ("RELAYER_API_KEY", relayer_key),
        ("RELAYER_API_KEY_ADDRESS", relayer_address),
        ("CLOB_API_KEY", api_key),
        ("CLOB_SECRET", api_secret),
        ("CLOB_PASS_PHRASE", api_passphrase),
    ] if not val]
    if missing:
        return {"error": f"missing env: {', '.join(missing)}", "city": city}

    from py_clob_client_v2 import ApiCreds, ClobClient, OrderArgs, OrderType, PartialCreateOrderOptions, Side
    from py_clob_client_v2.constants import POLYGON
    from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2

    client = ClobClient(
        host=order_host,
        chain_id=POLYGON,
        key=pk,
        funder=funder,
        signature_type=int(os.environ.get("SIGNATURE_TYPE", "2")),
        creds=ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase),
    )
    print(f"[SDK_SMOKE] signer={client.get_address()} funder={funder} relayer={relayer_address} order_host={order_host}")

    discovery_events_count = 0
    try:
        import httpx
        r = httpx.get(f"{proxy_host}/events", params={"active": "true", "closed": "false", "limit": 100}, timeout=30)
        body = r.json() if r.status_code == 200 else {}
        events = body.get("events") or body.get("data") or []
        discovery_events_count = len(events) if isinstance(events, list) else 0
        print(f"[SDK_SMOKE] discovery events_status event_count={discovery_events_count} keys={sorted(list(body.keys()))[:8] if isinstance(body, dict) else type(body).__name__}")
        if len(events) == 0:
            rr = httpx.get(f"{proxy_host}/markets", params={"active": "true", "closed": "false", "limit": 100}, timeout=30)
            body = rr.json() if rr.status_code == 200 else {}
            events = body.get("markets") or body.get("data") or (body if isinstance(body, list) else ([body] if isinstance(body, dict) else []))
            discovery_events_count = len(events) if isinstance(events, list) else 0
            print(f"[SDK_SMOKE] discovery markets_status markets_count={discovery_events_count} keys={sorted(list(body.keys()))[:8] if isinstance(body, dict) else type(body).__name__}")
    except Exception as e:
        print(f"[SDK_SMOKE] discovery exception={e!r}")
    print(f"[SDK_SMOKE] discovery events_count={discovery_events_count}")

    token_id = None
    market_question = ""
    resolution_fp = ""
    if isinstance(events, list):
        for m in events:
            q = str(m.get("question") or m.get("name") or m.get("title") or m.get("slug") or m.get("condition_id") or "")
            if city.lower() not in q.lower():
                continue
            print(f"[SDK_SMOKE] candidate_question={q}")
            if "July 26" not in q and "2026-07-26" not in q and "°F" not in q and "temperature" not in q.lower():
                continue
            tids = (m.get("tokens") or [])
            if not tids:
                continue
            token_id = (tids[0].get("token_id") or tids[0].get("id") or "").strip()
            market_question = q
            resolution_fp = str(m.get("resolution") or m.get("resolution_source") or "")
            break

    if not token_id:
        return {"error": "no_market", "city": city, "events_checked": len(events) if isinstance(events, list) else 0}

    print(f"[SDK_SMOKE] market={market_question}")
    print(f"[SDK_SMOKE] token_id={token_id} price={price} size={size}")

    try:
        resp = client.create_and_post_order(
            order_args=OrderArgs(token_id=token_id, price=price, size=size, side=Side.BUY),
            options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
            order_type=OrderType.GTC,
        )
    except Exception as e:
        print(f"[SDK_SMOKE] order_exception={e!r}")
        return {"error": f"create_and_post_order failed: {e}", "market": market_question, "token_id": token_id}

    print(f"[SDK_SMOKE] resp={resp!r}")
    data = {"raw_response": resp}
    try:
        if hasattr(resp, "model_dump"):
            data = resp.model_dump()
        elif isinstance(resp, dict):
            data = resp
    except Exception as e2:
        print(f"[SDK_SMOKE] model_dump exception={e2!r}")
    print(f"[SDK_SMOKE] outcome=order_posted tokens={data}")
    return {
        "city": city,
        "market": market_question,
        "token_id": token_id,
        "side": "BUY",
        "price": price,
        "size": size,
        "resolution_fingerprint": resolution_fp,
        "relayer": relayer_address,
        "signer": client.get_address(),
        "status": "ok",
        "result": data,
    }


@app.function(
    image=image,
    secrets=secrets,
    timeout=60,
    region="eu-west",
)
def dashboard_data():
    import sys
    sys.path.insert(0, '/data')
    from polybot.dashboard_data import get_dashboard_data
    return get_dashboard_data()


@app.function(
    image=image,
    secrets=secrets,
    timeout=120,
    region="eu-west",
)
def get_balance_mcp():
    """Get Polymarket wallet balance via direct Relayer API call."""
    import os
    import requests

    relayer_key = os.environ.get("RELAYER_API_KEY")
    relayer_address = os.environ.get("RELAYER_API_KEY_ADDRESS")
    if not relayer_key or not relayer_address:
        print("❌ Relayer credentials missing")
        return 0.0

    url = "https://relayer-v2.polymarket.com/balance"
    headers = {
        "RELAYER_API_KEY": relayer_key,
        "RELAYER_API_KEY_ADDRESS": relayer_address,
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        print(f"Relayer response status: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            balance = data.get("balance", 0)
            print(f"✅ Polymarket wallet balance: {balance} USDC")
            return float(balance)
        else:
            print(f"❌ Relayer balance check failed: {response.text}")
            return 0.0
    except Exception as e:
        print(f"❌ Error: {e}")

    return 0.0


@app.function(
    image=image,
    secrets=secrets,
    timeout=120,
    region="eu-west",
)
def check_and_fund():
    """Check polymarket wallet balance and fund if needed using the MCP/tool path."""
    balance = get_balance_mcp.remote()
    print(f"📊 Polymarket wallet balance: {float(balance):.2f} USDC")

    if float(balance) < 0.50:
        print("⚠️ Balance below $0.50. Attempting auto-deposit...")
        import sys
        sys.path.insert(0, '/data')
        try:
            from polybot.auto_deposit_modal import deposit_to_polymarket_modal
            ok = bool(deposit_to_polymarket_modal(1.0))
            print("✅ Auto-deposit succeeded." if ok else "❌ Auto-deposit failed.")
            return {"balance": float(balance), "action": "deposit_attempted", "success": ok}
        except Exception as e:
            print(f"❌ Auto-deposit error: {e}")
            return {"balance": float(balance), "action": "deposit_error"}
    print("✅ Balance sufficient for trading.")
    return {"balance": float(balance), "action": "sufficient"}
