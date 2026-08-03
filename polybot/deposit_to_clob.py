"""
Deposit USDC into Polymarket CLOB.

Tries multiple approaches:
1. Gasless deposit via CLOB API (py-clob-client-v2 update_balance_allowance)
2. Direct on-chain deposit via CTF Exchange V2

Usage:
    modal run polybot/modal_deploy.py::deposit_to_clob
"""
import sys
import os


def main():
    try:
        from py_clob_client_v2 import ClobClient
        from py_clob_client_v2.clob_types import BalanceAllowanceParams
        from py_clob_client_v2.constants import POLYGON
    except Exception as e:
        print(f"py-clob-client-v2 import failed: {e}")
        return
    from eth_account import Account
    from web3 import Web3

    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not private_key:
        print("Error: POLYMARKET_PRIVATE_KEY not set")
        sys.exit(1)

    account = Account.from_key(private_key)
    wallet = account.address
    print(f"Wallet: {wallet}")

    w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
    usdc_addr = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
    wallet_cs = Web3.to_checksum_address(wallet)

    # Full ERC-20 ABI
    erc20_abi = [
        {
            "constant": True,
            "inputs": [{"name": "account", "type": "address"}],
            "name": "balanceOf",
            "outputs": [{"name": "", "type": "uint256"}],
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
            "constant": False,
            "inputs": [
                {"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint256"},
            ],
            "name": "approve",
            "outputs": [{"name": "", "type": "bool"}],
            "type": "function",
        },
    ]
    token = w3.eth.contract(address=usdc_addr, abi=erc20_abi)

    # Check raw USDC balance
    raw_balance = token.functions.balanceOf(wallet_cs).call()
    print(f"Raw USDC balance: {raw_balance / 1e6:.4f}")

    if raw_balance < 500_000:
        print("Error: Need at least 0.5 USDC")
        sys.exit(1)

    # ── Approach 1: Gasless via CLOB API ──────────────────────────
    print("\n--- Approach 1: Gasless deposit via CLOB API ---")
    cf_proxy = os.environ.get(
        "CF_WORKER_URL", "https://poly-proxy.elvischemoiywo.workers.dev"
    )
    host = f"{cf_proxy.rstrip('/')}/clob"

    try:
        from py_clob_client_v2.clob_types import AssetType
    except Exception:
        from py_clob_client_v2.clob_types import BalanceAllowanceParams as _BAP

        class _AssetType:
            COLLATERAL = "COLLATERAL"

        class _Module:
            AssetType = _AssetType
            BalanceAllowanceParams = _BAP

        AssetType = _AssetType

    client = ClobClient(
        host=host,
        chain_id=POLYGON,
        key=private_key,
    )
    try:
        creds = client.create_or_derive_api_key()
        client.set_api_creds(creds)
        print(f"API key: {creds.api_key[:16]}...")
    except Exception as e:
        print(f"API creds: {e}")
        try:
            creds = client.derive_api_key()
            client.set_api_creds(creds)
        except Exception as e2:
            print(f"Derive key also failed: {e2}")
            creds = None

    deposited = False
    if creds:
        try:
            resp = client.update_balance_allowance(
                params=BalanceAllowanceParams(asset_type="COLLATERAL")
            )
            print(f"update_balance_allowance response: {resp}")
        except Exception as e:
            print(f"update_balance_allowance error: {e}")

        # Check balance after
        try:
            bal = client.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            print(f"CLOB balance after API call: {bal}")
            balance_val = float(
                (isinstance(bal, dict) and bal.get("balance", "0")) or "0"
            )
            if balance_val > 0:
                print("SUCCESS: Deposit completed via API")
                deposited = True
        except Exception as e:
            print(f"Balance check: {e}")

    if deposited:
        return

    # ── Approach 2: Direct on-chain deposit ───────────────────────
    print("\n--- Approach 2: Direct on-chain deposit ---")

    # The API response showed these exchange addresses:
    # CTF Exchange V2: 0xE111180000d2663C0091e4f400237545B87B996B
    # NegRiskAdapter:  0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296
    # NegRisk CTF:     0xe2222d279d744050d28e00520010520000310F59

    exchanges = [
        ("CTF Exchange V2", "0xE111180000d2663C0091e4f400237545B87B996B"),
        ("CTF Exchange V1", "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"),
    ]

    deposit_amount = 1_000_000  # 1 USDC
    nonce = w3.eth.get_transaction_count(wallet_cs)

    for exch_name, exch_addr in exchanges:
        print(f"\nTrying {exch_name} ({exch_addr})...")
        ctf = Web3.to_checksum_address(exch_addr)

        # Check/set allowance
        current_allowance = token.functions.allowance(wallet_cs, ctf).call()
        if current_allowance < deposit_amount:
            print(f"  Approving (nonce={nonce})...")
            try:
                approve_txn = token.functions.approve(
                    ctf, 2**256 - 1
                ).build_transaction(
                    {
                        "from": wallet_cs,
                        "nonce": nonce,
                        "gas": 100_000,
                        "gasPrice": w3.eth.gas_price,
                    }
                )
                signed = account.sign_transaction(approve_txn)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                print(f"  Approve tx: {tx_hash.hex()}")
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
                print(f"  Status: {receipt['status']}")
                if receipt["status"] != 1:
                    print("  Approve reverted, skipping this exchange")
                    continue
                nonce += 1
            except Exception as e:
                print(f"  Approve error: {e}")
                continue

        # Try deposit
        deposit_abi = [
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
        ctf_contract = w3.eth.contract(address=ctf, abi=deposit_abi)
        try:
            deposit_txn = ctf_contract.functions.deposit(
                usdc_addr, deposit_amount
            ).build_transaction(
                {
                    "from": wallet_cs,
                    "nonce": nonce,
                    "gas": 300_000,
                    "gasPrice": w3.eth.gas_price,
                }
            )
            signed = account.sign_transaction(deposit_txn)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            print(f"  Deposit tx: {tx_hash.hex()}")
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
            print(f"  Status: {receipt['status']}")
            if receipt["status"] == 1:
                print(f"SUCCESS: Deposited 1 USDC to {exch_name}")
                return
            else:
                print("  Deposit reverted")
                nonce += 1
        except Exception as e:
            print(f"  Deposit error: {e}")

    print("\nAll automated approaches failed.")
    print("The CTF Exchange V2 appears to not have a standard on-chain deposit function.")
    print("Options:")
    print("  1. Deposit manually via https://polymarket.com (connect wallet, click Deposit)")
    print("  2. Use the Polymarket SDK which handles deposit via meta-transaction")
    sys.exit(1)


if __name__ == "__main__":
    main()
