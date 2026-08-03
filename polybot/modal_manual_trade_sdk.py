"""Safest possible manual smoke test using the SDK POLY_1271 path.

This bypasses the broken manual safe-signature path by using
`py-clob-client-v2`'s built-in POLY_1271 order signing and posting a limit
buy through the Cloudflare proxy.

Usage:
  modal run polybot/modal_manual_trade_sdk.py::test_trade_sdk --city "New York"
"""

import os

import modal

app = modal.App("polybot-test-sdk")

image = (
    modal.Image.from_registry("python:3.11-slim")
    .apt_install("git")
    .pip_install(
        [
            "pydantic==2.10.4",
            "py-clob-client-v2>=1.0.1",
            "requests",
            "httpx",
            "web3",
            "eth-abi",
            "eth-account",
            "eth-utils",
            "poly-eip712-structs",
        ]
    )
    .add_local_python_source(
        "/workspaces/polymarket-weather-agent/polybot"
    # Force-include local polybot source tree so Modal can import `polybot.*`.
    )
)


def _market_payload(city: str) -> dict:
    """Build discovery+screening kwargs for the given city."""
    return {"city": city}


@app.function(image=image)
async def test_trade_sdk(city: str = "New York") -> dict:
    """Smoke-test a real $0.05 buy on a screened NYC temperature market."""

    # Required env
    if not os.environ.get("RELAYER_API_KEY") or not os.environ.get("RELAYER_API_KEY_ADDRESS"):
        return {"error": "RELAYER_API_KEY / RELAYER_API_KEY_ADDRESS missing"}

    from py_clob_client_v2 import ApiCreds, ClobClient, OrderArgs, OrderType, PartialCreateOrderOptions, Side
    from py_clob_client_v2.constants import POLYGON
    from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2

    pk = os.environ.get("PK", "")
    funder = os.environ.get("FUNDER", "") or os.environ.get("PROXY_WALLET", "")
    proxy_host = os.environ.get("CF_WORKER_URL", "https://poly-proxy.elvischemoiywo.workers.dev/clob").rstrip("/")
    api_key = os.environ.get("CLOB_API_KEY", "")
    api_secret = os.environ.get("CLOB_SECRET", "")
    api_passphrase = os.environ.get("CLOB_PASS_PHRASE", "")

    if not pk:
        return {"error": "PK missing"}
    if not funder:
        return {"error": "FUNDER / PROXY_WALLET missing"}
    if not (api_key and api_secret and api_passphrase):
        return {"error": "CLOB_API_KEY/CLOB_SECRET/CLOB_PASS_PHRASE missing"}

    client = ClobClient(
        host=proxy_host,
        chain_id=POLYGON,
        key=pk,
        funder=funder,
        signature_type=SignatureTypeV2.POLY_1271,
        creds=ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase),
    )
    print(f"[SDK_TEST] client={client.host} signer={client.address} funder={getattr(client,'funder','?')}")

    # Discover a screened market by directly inspecting recent temperature events.
    # Avoids importing the full orchestrator stack in the smoke script.
    import httpx
    events = []
    try:
        proxy_markets_path = f"{proxy_host}/markets"
        r = httpx.get(proxy_markets_path, params={"q": city, "limit": 20}, timeout=20)
        if r.status_code == 200:
            events = r.json()
    except Exception:
        events = []

    token_id = None
    market_question = ""
    resolution = ""
    collateral = os.environ.get("COLLATERAL", "COLLATERAL")
    if isinstance(events, list):
        for m in events:
            q = str(m.get("question") or m.get("name") or "")
            if city.lower() not in q.lower():
                continue
            if "°F" not in q or "July 26" not in q and "2026-07-26" not in q:
                continue
            tids = (m.get("tokens") or [])
            if not tids:
                continue
            token_id = tids[0].get("token_id") or tids[0].get("id")
            market_question = q
            resolution = str(m.get("resolution") or m.get("resolution_source") or "")
            break

    if not token_id:
        return {"error": "no_market", "city": city, "events_checked": len(events)}

    print(f"[SDK_TEST] market={market_question}")
    print(f"[SDK_TEST] token_id={token_id}")

    order_args = OrderArgs(token_id=token_id, price=0.01, size=5.0, side=Side.BUY)
    try:
        resp = client.create_and_post_order(
            order_args=order_args,
            options=PartialCreateOrderOptions(tick_size="0.0001", neg_risk=False),
            order_type=OrderType.GTC,
        )
    except Exception as e:
        return {"error": f"create_and_post_order failed: {e}","market": market_question,"token_id":token_id}

    print(f"[SDK_TEST] create_and_post_order resp={resp}")
    data = {"raw_response": resp}
    try:
        if hasattr(resp, "model_dump"):
            data = resp.model_dump()
        elif isinstance(resp, dict):
            data = resp
    except Exception:
        pass
    return {
        "city": city,
        "market": market_question,
        "token_id": token_id,
        "side": "BUY",
        "price": 0.01,
        "size": 5.0,
        "resolution_fingerprint": resolution,
        "status": "ok",
        "result": data,
    }
