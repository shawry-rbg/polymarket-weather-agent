"""
Withdraw 0.01 USDC from Polymarket CLOB exchange to raw wallet.

Usage: modal run polybot/withdraw_only.py
"""
import sys
import os

import modal

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "web3>=7.0.0",
        "eth-account>=0.13.0",
    )
)

app = modal.App("polybot-withdraw")

RPC_URL = "https://polygon-bor-rpc.publicnode.com"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CLOB_ADDRESS = "0x4D97dcd97ec945f40cF65F87097ACe5EA0476045"

CLOB_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "withdraw",
        "outputs": [],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "user", "type": "address"},
            {"name": "asset", "type": "address"},
        ],
        "name": "getBalance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
]


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("polymarket-secrets")],
    timeout=120,
)
def main():
    from web3 import Web3
    from eth_account import Account

    PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not PRIVATE_KEY:
        print("Error: POLYMARKET_PRIVATE_KEY not set")
        sys.exit(1)

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print("Error: Cannot connect to Polygon RPC")
        sys.exit(1)

    account = Account.from_key(PRIVATE_KEY)
    checksum_wallet = Web3.to_checksum_address(account.address)
    checksum_usdc = Web3.to_checksum_address(USDC_ADDRESS)
    checksum_clob = Web3.to_checksum_address(CLOB_ADDRESS)
    print(f"Wallet: {checksum_wallet}")

    usdc = w3.eth.contract(address=checksum_usdc, abi=ERC20_ABI)
    clob = w3.eth.contract(address=checksum_clob, abi=CLOB_ABI)

    # Check raw wallet USDC balance
    raw_balance = usdc.functions.balanceOf(checksum_wallet).call()
    print(f"Raw wallet USDC: {raw_balance / 1_000_000:.4f}")

    # Check user's CLOB exchange balance
    try:
        clob_balance = clob.functions.getBalance(checksum_wallet, checksum_usdc).call()
        print(f"CLOB exchange balance: {clob_balance / 1_000_000:.4f} USDC")
    except Exception as e:
        print(f"Warning: could not query CLOB balance: {e}")
        clob_balance = None

    amount_raw = 10000  # 0.01 USDC

    if clob_balance is not None and clob_balance < amount_raw:
        print("Error: CLOB balance < 0.01 USDC. Cannot withdraw.")
        sys.exit(1)

    print("Withdrawing 0.01 USDC from CLOB to wallet...")
    nonce = w3.eth.get_transaction_count(checksum_wallet)
    tx = clob.functions.withdraw(checksum_usdc, amount_raw).build_transaction(
        {
            "from": checksum_wallet,
            "nonce": nonce,
            "gas": 150000,
            "gasPrice": w3.eth.gas_price,
        }
    )
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"Tx: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"Confirmed. Status: {receipt['status']}")

    new_raw = usdc.functions.balanceOf(checksum_wallet).call()
    print(f"New raw wallet USDC: {new_raw / 1_000_000:.4f}")


@app.local_entrypoint()
def run():
    main.remote()
