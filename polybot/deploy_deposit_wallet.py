"""
Deploy a Polymarket deposit wallet for the bot's address and deposit USDC.

Usage:
    modal run polybot/deploy_deposit_wallet.py
"""
import sys
import os

import modal

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "web3>=7.0.0",
        "eth-account>=0.13.0",
        "httpx>=0.28.0",
        "polymarket>=0.1.2",
    )
)

app = modal.App("polybot-deploy-deposit")


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("polymarket-secrets")],
    timeout=180,
)
def deploy_deposit_wallet():
    from web3 import Web3
    from eth_account import Account
    import httpx as hx
    import time

    PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not PRIVATE_KEY:
        print("Error: POLYMARKET_PRIVATE_KEY not set")
        return

    w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
    account = Account.from_key(PRIVATE_KEY)
    bot_address = account.address
    print(f"Bot wallet: {bot_address}")

    USDC_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"

    # Check USDC balance
    erc20_abi = [
        {"constant": True, "inputs": [{"name": "account", "type": "address"}],
         "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    ]
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=erc20_abi)
    usdc_balance = usdc.functions.balanceOf(Web3.to_checksum_address(bot_address)).call()
    print(f"Bot USDC balance: {usdc_balance / 1e6:.4f}")

    if usdc_balance < 100_000:
        print("Error: Need at least 0.1 USDC")
        return

    # Step 1: Get relayer params
    print("\n--- Step 1: Get relayer params ---")
    relayer_url = "https://relayer-v2.polymarket.com"
    resp = hx.get(
        f"{relayer_url}/v1/account/transactions/params",
        params={"address": bot_address, "type": "WALLET"},
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    params = resp.json()
    deposit_wallet_address = params["address"]
    nonce = params["nonce"]
    print(f"Deposit wallet: {deposit_wallet_address}")
    print(f"Nonce: {nonce}")

    # Check if already deployed
    code = w3.eth.get_code(Web3.to_checksum_address(deposit_wallet_address))
    if len(code) > 0:
        print("Deposit wallet already deployed!")
    else:
        # Step 2: Deploy deposit wallet
        print("\n--- Step 2: Deploy deposit wallet ---")
        # Hard-coded values from Polymarket SDK
        factory = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
        chain_id = 137
        deadline = str(int(time.time()) + 600)

        domain = {
            "chainId": chain_id,
            "name": "DepositWallet",
            "verifyingContract": deposit_wallet_address,
            "version": "1",
        }
        types = {
            "Batch": [
                {"name": "wallet", "type": "address"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
                {"name": "calls", "type": "Call[]"},
            ],
            "Call": [
                {"name": "target", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "data", "type": "bytes"},
            ],
        }

        message = {
            "wallet": deposit_wallet_address,
            "nonce": int(nonce),
            "deadline": int(deadline),
            "calls": [],
        }

        from eth_account.messages import encode_typed_data
        full_message = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Batch": [
                    {"name": "wallet", "type": "address"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                    {"name": "calls", "type": "Call[]"},
                ],
                "Call": [
                    {"name": "target", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "data", "type": "bytes"},
                ],
            },
            "primaryType": "Batch",
            "domain": domain,
            "message": message,
        }
        signable = encode_typed_data(full_message=full_message)
        signed_message = account.sign_message(signable)
        signature = signed_message.signature.hex()
        print(f"Signed. Signature: {signature[:40]}...")

        payload = {
            "type": "WALLET-CREATE",
            "from": bot_address,
            "to": factory,
            "nonce": str(nonce),
            "signature": "0x" + signature,
            "metadata": "Deploy Deposit Wallet for polybot",
            "depositWalletParams": {
                "depositWallet": deposit_wallet_address,
                "deadline": deadline,
                "calls": [],
            },
        }

        resp = hx.post(f"{relayer_url}/submit", json=payload, timeout=30)
        print(f"Submit response: {resp.status_code} {resp.text[:500]}")

        if resp.status_code not in (200, 201):
            print(f"Error: {resp.text}")
            return

        print("Waiting for deployment...")
        for i in range(30):
            time.sleep(2)
            code = w3.eth.get_code(Web3.to_checksum_address(deposit_wallet_address))
            if len(code) > 0:
                print(f"Deployed after {(i+1)*2}s!")
                break
            print(f"  Polling {i+1}...")
        else:
            print("Deployment timed out")
            return

    # Step 3: Deposit USDC
    print("\n--- Step 3: Deposit USDC ---")
    ctf_exchange = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
    deposit_amount = 100_000  # 0.1 USDC

    ctf_abi = [
        {
            "inputs": [
                {"name": "token", "type": "address"},
                {"name": "amount", "type": "uint256"},
            ],
            "name": "deposit",
            "outputs": [],
            "stateMutability": "nonpayable",
            "type": "function",
        }
    ]
    ctf = w3.eth.contract(address=ctf_exchange, abi=ctf_abi)
    deposit_call_data = ctf.encode_abi("deposit", [USDC_ADDRESS, deposit_amount])

    # Hard-coded values from Polymarket SDK
    factory = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
    deadline = str(int(time.time()) + 600)

    domain = {
        "chainId": 137,
        "name": "DepositWallet",
        "verifyingContract": deposit_wallet_address,
        "version": "1",
    }
    types = {
        "Batch": [
            {"name": "wallet", "type": "address"},
            {"name": "nonce", "type": "uint256"},
            {"name": "deadline", "type": "uint256"},
            {"name": "calls", "type": "Call[]"},
        ],
        "Call": [
            {"name": "target", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
        ],
    }

    message = {
        "wallet": deposit_wallet_address,
        "nonce": 0,
        "deadline": int(deadline),
        "calls": [
            {
                "target": ctf_exchange,
                "value": 0,
                "data": deposit_call_data,
            }
        ],
    }

    from eth_account.messages import encode_typed_data
    full_message = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Batch": [
                {"name": "wallet", "type": "address"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
                {"name": "calls", "type": "Call[]"},
            ],
            "Call": [
                {"name": "target", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "data", "type": "bytes"},
            ],
        },
        "primaryType": "Batch",
        "domain": domain,
        "message": message,
    }
    signable = encode_typed_data(full_message=full_message)
    signed_message = account.sign_message(signable)
    signature = signed_message.signature.hex()

    payload = {
        "type": "WALLET",
        "from": bot_address,
        "to": factory,
        "nonce": "0",
        "signature": "0x" + signature,
        "metadata": "Deposit USDC for polybot",
        "depositWalletParams": {
            "depositWallet": deposit_wallet_address,
            "deadline": deadline,
            "calls": [
                {
                    "target": ctf_exchange,
                    "value": "0",
                    "data": deposit_call_data,
                }
            ],
        },
    }

    resp = hx.post(f"{relayer_url}/submit", json=payload, timeout=30)
    print(f"Deposit response: {resp.status_code} {resp.text[:500]}")

    if resp.status_code in (200, 201):
        print("Deposit submitted!")
    else:
        print(f"Deposit failed: {resp.text}")

    print(f"\n=== DONE ===")
    print(f"Deposit wallet: {deposit_wallet_address}")
    print(f"Amount: {deposit_amount / 1e6} USDC")


@app.local_entrypoint()
def run():
    deploy_deposit_wallet.remote()
