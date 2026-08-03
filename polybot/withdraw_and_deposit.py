"""
Withdraw and re-deposit USDC to activate Polymarket CLOB wallet.

Usage: modal run polybot/withdraw_and_deposit.py
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

app = modal.App("polybot-activate")

RPC_URL = "https://polygon-bor-rpc.publicnode.com"

# USDC.e on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Polymarket CLOB NegRiskExchange
CLOB_ADDRESS = "0x4D97dcd97ec945f40cF65F87097ACe5EA0476045"

# Minimal ABI for deposit/withdraw
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
        "constant": False,
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "deposit",
        "outputs": [],
        "type": "function",
    },
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
    print(f"Wallet: {account.address}")

    # Check USDC balance first
    erc20_abi = [
        {
            "constant": True,
            "inputs": [{"name": "account", "type": "address"}],
            "name": "balanceOf",
            "outputs": [{"name": "", "type": "uint256"}],
            "type": "function",
        }
    ]
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS), abi=erc20_abi
    )
    usdc_balance = usdc.functions.balanceOf(account.address).call()
    print(f"USDC balance: {usdc_balance / 1_000_000:.4f} USDC")

    if usdc_balance < 10000:
        print("Error: Need at least 0.01 USDC for activation")
        sys.exit(1)

    contract = w3.eth.contract(
        address=Web3.to_checksum_address(CLOB_ADDRESS), abi=CLOB_ABI
    )

    amount_raw = 10000  # 0.01 USDC (6 decimals)

    # 1. Withdraw 0.01 USDC
    print("Withdrawing 0.01 USDC from CLOB...")
    nonce = w3.eth.get_transaction_count(account.address)
    tx_withdraw = contract.functions.withdraw(
        USDC_ADDRESS, amount_raw
    ).build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "gas": 150000,
            "gasPrice": w3.eth.gas_price,
        }
    )
    signed_withdraw = account.sign_transaction(tx_withdraw)
    tx_hash_withdraw = w3.eth.send_raw_transaction(signed_withdraw.raw_transaction)
    print(f"Withdraw tx: {tx_hash_withdraw.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash_withdraw)
    print(f"Withdraw confirmed. Status: {receipt['status']}")

    # 2. Deposit the same amount back (activates wallet)
    nonce += 1
    print("Depositing 0.01 USDC to CLOB...")
    tx_deposit = contract.functions.deposit(
        USDC_ADDRESS, amount_raw
    ).build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "gas": 150000,
            "gasPrice": w3.eth.gas_price,
        }
    )
    signed_deposit = account.sign_transaction(tx_deposit)
    tx_hash_deposit = w3.eth.send_raw_transaction(signed_deposit.raw_transaction)
    print(f"Deposit tx: {tx_hash_deposit.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash_deposit)
    print(f"Deposit confirmed. Status: {receipt['status']}")
    print("Wallet is now activated.")


@app.local_entrypoint()
def run():
    main.remote()
