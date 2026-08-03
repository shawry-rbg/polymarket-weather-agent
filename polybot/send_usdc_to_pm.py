"""
Send USDC from bot wallet to the Polymarket deposit wallet (Gnosis Safe).

Usage: modal run polybot/send_usdc_to_pm.py
"""
import sys
import os


def main():
    from eth_account import Account
    from web3 import Web3

    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not private_key:
        print("Error: POLYMARKET_PRIVATE_KEY not set")
        sys.exit(1)

    w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
    account = Account.from_key(private_key)
    bot_wallet = Web3.to_checksum_address(account.address)

    # The PM deposit wallet (Gnosis Safe) that's connected on Polymarket
    pm_wallet = Web3.to_checksum_address("0xAB2ddbd4BF2c8a256584Ca6c4eCa7D51810263CA")

    usdc = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")

    erc20_abi = [
        {
            "constant": True,
            "inputs": [{"name": "account", "type": "address"}],
            "name": "balanceOf",
            "outputs": [{"name": "", "type": "uint256"}],
            "type": "function",
        },
        {
            "constant": False,
            "inputs": [
                {"name": "to", "type": "address"},
                {"name": "amount", "type": "uint256"},
            ],
            "name": "transfer",
            "outputs": [{"name": "", "type": "bool"}],
            "type": "function",
        },
    ]
    token = w3.eth.contract(address=usdc, abi=erc20_abi)

    bot_balance = token.functions.balanceOf(bot_wallet).call()
    pm_balance = token.functions.balanceOf(pm_wallet).call()
    print(f"Bot wallet USDC: {bot_balance / 1e6:.4f}")
    print(f"PM wallet USDC:  {pm_balance / 1e6:.4f}")

    # Send 1 USDC to PM wallet
    send_amount = 1_000_000  # 1 USDC

    if bot_balance < send_amount:
        print(f"Error: Bot wallet only has {bot_balance / 1e6:.4f} USDC")
        sys.exit(1)

    print(f"\nSending 1 USDC from bot wallet to PM wallet...")
    nonce = w3.eth.get_transaction_count(bot_wallet)
    txn = token.functions.transfer(pm_wallet, send_amount).build_transaction({
        "from": bot_wallet,
        "nonce": nonce,
        "gas": 100_000,
        "gasPrice": w3.eth.gas_price,
    })
    signed = account.sign_transaction(txn)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"Tx: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"Status: {receipt['status']}")

    if receipt["status"] == 1:
        new_pm = token.functions.balanceOf(pm_wallet).call()
        new_bot = token.functions.balanceOf(bot_wallet).call()
        print(f"\nNew balances:")
        print(f"  Bot wallet: {new_bot / 1e6:.4f} USDC")
        print(f"  PM wallet:  {new_pm / 1e6:.4f} USDC")
        print("SUCCESS")
    else:
        print("ERROR: Transfer reverted")
        sys.exit(1)


if __name__ == "__main__":
    main()
