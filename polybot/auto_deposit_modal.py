import os
import sys
import time
from web3 import Web3

# Lazy address resolution to avoid import-time address validation failures.
def _usdc_address() -> str:
    try:
        env_addr = os.environ.get("USDC_ADDRESS")
        if env_addr:
            return Web3.to_checksum_address(env_addr)
    except Exception:
        pass
    try:
        from eth_utils.address import to_checksum_address
        return to_checksum_address('0x2791bca1f2de4661ed88a30c99a79449aa84174')
    except Exception:
        pass
    return '0x2791bca1f2de4661ed88a30c99a79449aa84174'


def _connect_rpc():
    rpc_urls = [
        'https://polygon-mainnet.g.alchemy.com/v2/demo',
        'https://rpc-mainnet.maticvigil.com',
        'https://polygon-rpc.com',
        'https://matic-mainnet.chainstacklabs.com'
    ]
    for url in rpc_urls:
        w3 = Web3(Web3.HTTPProvider(url))
        if w3.is_connected():
            return w3
    return None


def get_balance_modal(funder_address: str) -> float:
    """Get USDC balance of the Polymarket deposit wallet."""
    w3 = _connect_rpc()
    if not w3:
        print("❌ RPC not connected")
        return 0.0
    usdc_abi = [
        {'constant': True, 'inputs': [{'name': 'owner', 'type': 'address'}], 'name': 'balanceOf',
         'outputs': [{'name': '', 'type': 'uint256'}], 'type': 'function'}
    ]
    usdc_address = _usdc_address()
    usdc = w3.eth.contract(address=usdc_address, abi=usdc_abi)
    balance = usdc.functions.balanceOf(funder_address).call()
    return balance / 10**6


def deposit_to_polymarket_modal(amount_usdc: float = 1.0) -> bool:
    """Deposit USDC from Phantom wallet to Polymarket."""
    w3 = _connect_rpc()
    if not w3:
        print("❌ RPC not connected")
        return False
    pk = os.environ.get("PK")
    funder = os.environ.get("FUNDER")
    if not pk or not funder:
        print("❌ PK and FUNDER required")
        return False
    account = w3.eth.account.from_key(pk)
    amount = int(amount_usdc * 10**6)
    funder_checksum = funder
    try:
        funder_checksum = Web3.to_checksum_address(funder)
    except Exception:
        pass
    usdc_address = _usdc_address()
    usdc_abi = [
        {'constant': False, 'inputs': [{'name': 'spender', 'type': 'address'}, {'name': 'amount', 'type': 'uint256'}],
         'name': 'approve', 'outputs': [{'name': '', 'type': 'bool'}], 'type': 'function'},
        {'constant': True, 'inputs': [{'name': 'owner', 'type': 'address'}], 'name': 'balanceOf',
         'outputs': [{'name': '', 'type': 'uint256'}], 'type': 'function'}
    ]
    usdc = w3.eth.contract(address=usdc_address, abi=usdc_abi)
    # Check EOA balance
    eoa_balance = usdc.functions.balanceOf(account.address).call()
    print(f"EOA balance: {eoa_balance/10**6:.2f} USDC")
    if eoa_balance < amount:
        print(f"❌ Insufficient balance. Need {amount_usdc} USDC")
        return False
    # Approve
    print(f"🔄 Approving {amount_usdc} USDC for deposit...")
    tx = usdc.functions.approve(funder_checksum, amount).build_transaction({
        'from': account.address,
        'nonce': w3.eth.get_transaction_count(account.address),
        'gas': 100000,
        'gasPrice': w3.eth.gas_price
    })
    signed_tx = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
    print(f"✅ Approval tx: {tx_hash.hex()}")
    w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"🔄 Transferring {amount_usdc} USDC to Polymarket wallet...")
    tx2 = usdc.functions.transfer(funder_checksum, amount).build_transaction({
        'from': account.address,
        'nonce': w3.eth.get_transaction_count(account.address),
        'gas': 100000,
        'gasPrice': w3.eth.gas_price
    })
    signed_tx2 = account.sign_transaction(tx2)
    tx_hash2 = w3.eth.send_raw_transaction(signed_tx2.rawTransaction)
    print(f"✅ Transfer tx: {tx_hash2.hex()}")
    w3.eth.wait_for_transaction_receipt(tx_hash2)
    # Verify
    new_balance = usdc.functions.balanceOf(funder_checksum).call()
    print(f"✅ Polymarket wallet now has: {new_balance/10**6:.2f} USDC")
    return True


def check_and_fund():
    """Check balance and fund if needed."""
    pk = os.environ.get("PK")
    funder = os.environ.get("FUNDER")
    if not pk or not funder:
        print("❌ PK and FUNDER required")
        return False
    balance = get_balance_modal(funder)
    print(f"📊 Polymarket wallet balance: {balance:.2f} USDC")
    if balance < 0.50:
        print("⚠️ Balance below $0.50. Depositing $1.00...")
        return deposit_to_polymarket_modal(1.0)
    else:
        print("✅ Balance sufficient for trading.")
        return True


if __name__ == "__main__":
    check_and_fund()
