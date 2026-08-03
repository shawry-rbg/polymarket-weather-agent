import modal
import os
import json

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("httpx>=0.28.0", "eth-account>=0.13.0")
    .add_local_dir(os.path.join(WORKSPACE, "polybot"), remote_path="/data/polybot", copy=True)
)

app = modal.App("polybot-wallet", image=image)


@app.function(
    cpu=1.0, memory=512, timeout=60,
    secrets=[modal.Secret.from_name("polymarket-secrets")],
)
def check_wallet():
    import sys
    sys.path.insert(0, "/data")
    from polybot.clob import get_wallet_address, get_usdc_balance
    import httpx, json

    addr = get_wallet_address()
    balance = get_usdc_balance()

    # Also check via Polymarket API
    polymarket_balance = None
    try:
        r = httpx.get(f"https://poly-proxy.elvischemoiywo.workers.dev/gamma/balances?owner={addr}", timeout=10)
        if r.status_code == 200:
            polymarket_balance = r.json()
    except Exception:
        pass

    # Check CLOB API for balance
    clob_balance = None
    try:
        from polybot.clob import _get_clob_client
        client = _get_clob_client()
        clob_bal = client.get_balance()
        clob_balance = clob_bal
    except Exception as e:
        clob_balance = f"error: {e}"

    return {
        "address": addr,
        "polygon_usdc_balance": balance,
        "polymarket_balance": polymarket_balance,
        "clob_balance": clob_balance,
    }


@app.local_entrypoint()
def main():
    result = check_wallet.remote()
    print(json.dumps(result, indent=2, default=str))
