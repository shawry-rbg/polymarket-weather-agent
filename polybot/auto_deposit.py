import os
from web3 import Web3

# CORRECTED USDC ADDRESS (Polygon)
USDC_ADDRESS = '0x2791Bca1f2de4661ED88A30C99A79449AA84174'

def ensure_balance():
    w3 = Web3(Web3.HTTPProvider('https://polygon-rpc.com'))
    if not w3.is_connected():
        print("❌ Failed to connect to Polygon")
        return False
    
    pk = os.environ.get("PK")
    funder = os.environ.get("FUNDER")
    if not pk or not funder:
        print("❌ PK and FUNDER environment variables required")
        return False
    
    print(f"✅ Connected to Polygon. PK loaded. Funder: {funder[:10]}...")
    return True

if __name__ == "__main__":
    ensure_balance()
