"""
One-time Polymarket setup script: USDC approvals + deposit.

1. Approves USDC for Polymarket CLOB contracts
2. Deposits a small amount of USDC into the Polymarket exchange

Usage: modal run polybot/modal_deploy.py::approve_allowances
"""
import sys
import os

from web3 import Web3
from eth_account import Account

# --- Configuration ---
RPC_URL = "https://polygon-bor-rpc.publicnode.com"

# USDC.e on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Polymarket CLOB exchange contracts
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_ADAPTER = "0xd91E7cF7bA55C6daB6c6B0240eB0991B8DeB2d8B"
NEG_RISK_EXCHANGE = "0x4D97dcd97ec945f40cF65F87097ACe5EA0476045"
CONDITIONAL_TOKEN = "0x4e9e2c0D3A42B6CbFFd9A5C83209af13d52D80B1"

# Polymarket deposit contract (the main USDC deposit contract on Polygon)
# This is the address that holds user funds for the CLOB
USDC_PROXY = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e

# Standard ERC-20 ABI
ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]

# Minimal CTF Exchange ABI for deposit
CTF_EXCHANGE_ABI = [
    {
        "inputs": [
            {"name": "amount", "type": "uint256"},
        ],
        "name": "deposit",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "user", "type": "address"},
        ],
        "name": "getBalance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

MAX_ALLOWANCE = 2**256 - 1
DEPOSIT_AMOUNT = 1_000_000  # 1 USDC (6 decimals)


def send_tx(w3, account, txn):
    """Sign, send, and wait for a transaction."""
    signed = account.sign_transaction(txn)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"    tx sent {tx_hash.hex()}")
    w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"    confirmed ✓")
    return tx_hash


def check_and_approve(usdc, account, w3, spender_name: str, spender_address: str):
    """Check current allowance and approve if zero."""
    spender = Web3.to_checksum_address(spender_address)
    current = usdc.functions.allowance(account.address, spender).call()
    if current > 0:
        print(f"  {spender_name}: already approved (allowance={current})")
        return

    print(f"  {spender_name}: approving USDC spend...")
    txn = usdc.functions.approve(spender, MAX_ALLOWANCE).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": 100_000,
        "gasPrice": w3.eth.gas_price,
    })
    send_tx(w3, account, txn)


def main():
    PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not PRIVATE_KEY:
        print("Error: POLYMARKET_PRIVATE_KEY environment variable not set.")
        sys.exit(1)

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        print("Error: Could not connect to Polygon RPC.")
        sys.exit(1)

    account = Account.from_key(PRIVATE_KEY)
    print(f"Wallet address: {account.address}")

    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS),
        abi=ERC20_ABI,
    )

    # Check USDC balance
    usdc_balance = usdc.functions.balanceOf(account.address).call()
    print(f"USDC balance: {usdc_balance / 1_000_000:.2f} USDC")
    if usdc_balance < DEPOSIT_AMOUNT:
        print(f"\n⚠ Insufficient USDC balance ({usdc_balance / 1_000_000:.2f} USDC).")
        print(f"Please send at least 1 USDC to {account.address} on Polygon.")
        print(f"USDC.e contract: {USDC_ADDRESS}")
        print(f"\nOnce USDC is received, run this script again to complete the deposit.")
        # Don't exit — approvals above are still useful
        print("\nUSDC allowances are set. Deposit step skipped (no USDC balance).")
        return

    print("\n--- Step 1: Setting USDC allowances ---\n")
    check_and_approve(usdc, account, w3, "CTF_EXCHANGE", CTF_EXCHANGE)
    check_and_approve(usdc, account, w3, "NEG_RISK_ADAPTER", NEG_RISK_ADAPTER)
    check_and_approve(usdc, account, w3, "NEG_RISK_EXCHANGE", NEG_RISK_EXCHANGE)
    check_and_approve(usdc, account, w3, "CONDITIONAL_TOKEN", CONDITIONAL_TOKEN)

    print("\n--- Step 2: Depositing USDC into Polymarket CLOB ---\n")
    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_EXCHANGE),
        abi=CTF_EXCHANGE_ABI,
    )
    
    # Check current CLOB balance
    try:
        clob_balance = ctf.functions.getBalance(account.address).call()
        print(f"Current CLOB balance: {clob_balance / 1_000_000:.2f} USDC")
    except Exception:
        clob_balance = 0
        print("Current CLOB balance: 0 USDC")

    if clob_balance < DEPOSIT_AMOUNT:
        print(f"Depositing 1 USDC into Polymarket CLOB...")
        txn = ctf.functions.deposit(DEPOSIT_AMOUNT).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 150_000,
            "gasPrice": w3.eth.gas_price,
        })
        send_tx(w3, account, txn)
    else:
        print("CLOB balance sufficient, skipping deposit.")

    print("\nAll done. Wallet is ready for live trading.")


if __name__ == "__main__":
    main()
