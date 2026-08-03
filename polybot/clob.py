"""Defines the py-clob-client-v2 live-order adapter used by `polybot.clob`.

This adapter intentionally re-initializes auth per call so we do not
share state between balance checks, order signing, and execution.
"""
from __future__ import annotations

import os
import json
import logging
import time
import dataclasses
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# USDC contract on Polygon (for balance checks)
USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A79449AA84174"
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
_GAS_QUEUE_PATH = "/polybot-data/gas_queue.jsonl"
POLYGONSCAN_GAS_API = "https://api.polygonscan.com/api"
POLYGONSCAN_API_KEY = os.environ.get("POLYGONSCAN_API_KEY", "")
# Fallback: public gas station
GAS_STATION_URL = "https://gasstation.polygon.technology/v2"
MATIC_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price?ids=matic-network&vs_currencies=usd"
GAS_TRADE_THRESHOLD_PCT = 0.05  # Queue if gas > 5% of trade size
MAX_BATCH_SIZE = 3
GAS_CACHE_TTL = 60  # Cache gas price for 60 seconds
GAS_QUEUE_PATH = _GAS_QUEUE_PATH
POLY_1271_SIGNER_TYPE = 2
BYTES32_ZERO = "0x0000000000000000000000000000000000000000000000000000000000000000"
EMPTY_HASH = "0x0000000000000000000000000000000000000000000000000000000000000000"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Bright Data proxy config (credit-saving mode)
BRIGHTDATA_API_KEY = os.environ.get("BRIGHTDATA_API_KEY", "")
BRIGHTDATA_PROXY_URL = os.environ.get("BRIGHTDATA_PROXY_URL", "")
BRIGHTDATA_PROXY_AUTH = (
    (os.environ.get("BRIGHTDATA_API_KEY", ""), "")
    if os.environ.get("BRIGHTDATA_API_KEY")
    else None
)
USE_PROXY_FOR = ["/clob/order", "/balance-allowance", "/submit", "/api/v1/"]


def _should_use_proxy(url: str) -> bool:
    """Only use proxy for endpoints that need geo-bypass/restricted access."""
    if not BRIGHTDATA_PROXY_URL:
        return False
    return any(endpoint in url for endpoint in USE_PROXY_FOR)


def _get_proxy_dict() -> dict | None:
    """Return proxy dict only when configured."""
    if not BRIGHTDATA_PROXY_URL:
        return None
    return {
        "http": BRIGHTDATA_PROXY_URL,
        "https": BRIGHTDATA_PROXY_URL,
    }


# Module-level gas cache
_gas_cache: dict = {"price_gwei": 30.0, "matic_usd": 0.50, "ts": 0}


def get_wallet_address() -> Optional[str]:
    """Derive the wallet address from the private key."""
    try:
        from eth_account import Account
        private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        if private_key:
            # Only raise if Account itself is unavailable
            return Account.from_key(private_key).address
    except Exception:
        pass
    return None


def _build_sdk_client():
    """Lazy-init a CLOB client using py-clob-client-v2 only."""
    from py_clob_client_v2 import ClobClient
    from py_clob_client_v2.constants import POLYGON

    private_key = os.environ.get("PK") or os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not private_key:
        raise RuntimeError("PK/POLYMARKET_PRIVATE_KEY not set")
    wallet = os.environ.get("PM_WALLET_ADDRESS", "0xAB2ddbd4BF2c8a256584Ca6c4eCa7D51810263CA")
    host = (os.environ.get("POLYMARKET_HOST") or os.environ.get("POLY_HOST") or "https://clob.polymarket.com").rstrip("/")
    chain_id = int(os.environ.get("POLYMARKET_CHAIN_ID", os.environ.get("POLY_CHAIN_ID", str(POLYGON))))
    client = ClobClient(host, key=private_key, chain_id=chain_id)

    api_key = os.environ.get("CLOB_API_KEY")
    api_secret = os.environ.get("CLOB_SECRET")
    api_pass = os.environ.get("CLOB_PASS_PHRASE") or os.environ.get("CLOB_API_PASSPHRASE")
    if api_key and api_secret and api_pass:
        try:
            from py_clob_client_v2 import ApiCreds
            client.creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_pass)
            return client
        except Exception as e:
            print(f"[SDK] Failed to attach explicit ApiCreds: key_set={bool(api_key)} secret_set={bool(api_secret)} pass_set={bool(api_pass)} err={e}")
    try:
        client.create_or_derive_api_key()
    except Exception as e:
        print(f"[SDK] API key derivation skipped/failed: {e}")

    try:
        proxy_url = os.environ.get("POLY_PROXY_URL") or os.environ.get("PROXY_URL", "")
        if proxy_url:
            _attach_proxy_session(client, proxy_url)
    except Exception as e:
        print(f"[SDK] proxy setup skipped/failed: {e}")
    return client


def _attach_proxy_session(client, proxy_url: str):
    session = requests.Session()
    session.proxies.update({"https": proxy_url, "http": proxy_url})
    for proto in ("https://", "http://"):
        session.proxies.setdefault(proto, proxy_url)
    try:
        login = client.creds or client.create_or_derive_api_key()
    except Exception as e:
        print(f"[SDK] proxy session auth skipped/failed: {e}")
        login = client.creds
    if login and getattr(login, "api_key", None):
        session.headers.update({"x-api-key": login.api_key})
    session.headers.update({"User-Agent": "polybot/1.0"})
    client._session = session


def _safe_checksum(addr: Optional[str]) -> Optional[str]:
    """Normally return a checksummed address, fall back to raw if normalization fails."""
    if addr is None:
        return None
    try:
        from eth_utils.address import to_checksum_address
        return to_checksum_address(addr)
    except Exception:
        return addr


def _derive_order_from_market(
    market_id: str,
    side: str,
    price: float,
    size: float,
    wallet: str,
    host: str,
    chain_id: int,
    token_id: Optional[str] = None,
    fee_rate_bps: int = 0,
    nonce: Optional[int] = None,
    expiration: Optional[int] = None,
):
    """Build and sign an order using py-clob-client-v2 primitives."""
    from py_clob_client_v2 import (
        ClobClient,
        OrderArgs,
        OrderType,
        RoundConfig,
        SigningMethod,
    )
    from py_clob_client_v2.builders import build_order_amts

    if token_id is None:
        token_id = market_id

    price = round(float(price), 6)
    size = round(float(size), 6)
    if size <= 0:
        raise ValueError("size must be positive")

    client = ClobClient(host=host, key=wallet, chain_id=chain_id)

    # If explicit API creds are provided, attach them.
    api_key = os.environ.get("CLOB_API_KEY")
    api_secret = os.environ.get("CLOB_SECRET")
    api_pass = os.environ.get("CLOB_PASS_PHRASE") or os.environ.get("CLOB_API_PASSPHRASE")
    if api_key and api_secret and api_pass:
        try:
            from py_clob_client_v2 import ApiCreds
            client.creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_pass)
        except Exception:
            pass

    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")

    round_config = RoundConfig(price=price, size=size, amount=price * size)
    maker_amount, taker_amount = build_order_amts(
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        fee_rate_bps=int(fee_rate_bps),
    )

    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=size,
        side=side,
        fee_rate_bps=int(fee_rate_bps),
        nonce=int(nonce or int(time.time() * 1000)),
        expiration=int(expiration) if expiration else None,
        maker_amount=int(maker_amount),
        taker_amount=int(taker_amount),
    )

    signed = client.create_order(
        order_args=order_args,
        signing_method=SigningMethod.METAMASK,
        private_key=wallet,
    )
    return signed


def _normalize_submit_result(resp, market_id, side, valid_price, size):
    """Normalize V2 SDK submit response into a legacy-ish shape."""
    if isinstance(resp, dict):
        raw = resp
    elif hasattr(resp, "__dict__"):
        raw = {
            k: v for k, v in vars(resp).items()
            if not k.startswith("_") and k != "error_message"
        }
    else:
        raw = {"order": str(resp)}

    return {
        "status": "success",
        "market_id": market_id,
        "side": side,
        "price": valid_price,
        "size": size,
        "order_id": raw.get("id") or raw.get("orderID") or raw.get("order_id"),
        "raw": raw,
    }


async def place_order(
    market_id: str,
    side: str,
    price: float,
    size: float,
    *,
    token_id: Optional[str] = None,
    wallet: Optional[str] = None,
    host: Optional[str] = None,
    chain_id: Optional[int] = None,
    fee_rate_bps: int = 0,
    nonce: Optional[int] = None,
    expiration: Optional[int] = None,
    signing_key: Optional[str] = None,
):
    wallet = wallet or os.environ.get("PM_WALLET_ADDRESS", "")
    host = host or (os.environ.get("POLYMARKET_HOST") or os.environ.get("POLY_HOST") or "https://clob.polymarket.com").rstrip("/")
    chain_id = int(chain_id or os.environ.get("POLYMARKET_CHAIN_ID", os.environ.get("POLY_CHAIN_ID", "137")))
    signing_key = signing_key or os.environ.get("PK") or os.environ.get("POLYMARKET_PRIVATE_KEY", "")

    if not wallet:
        raise RuntimeError("wallet not set")
    if not signing_key:
        raise RuntimeError("signing_key not set")

    signed = _derive_order_from_market(
        market_id=market_id,
        side=side,
        price=price,
        size=size,
        wallet=signing_key,
        host=host,
        chain_id=chain_id,
        token_id=token_id,
        fee_rate_bps=fee_rate_bps,
        nonce=nonce,
        expiration=expiration,
    )
    if signed is None:
        return None

    client = _build_sdk_client()
    try:
        resp = client.post_order(signed)
    except TypeError:
        resp = client.create_and_post_order(signed)

    return _normalize_submit_result(resp, market_id, side, price, size)


async def execute_trade(
    market_id: str,
    side: str,
    price: float,
    size: float,
    **kwargs,
) -> dict:
    result = await place_order(market_id=market_id, side=side, price=price, size=size, **kwargs)
    if result is None:
        return {"status": "skipped", "reason": "signed_order_none"}
    if isinstance(result, dict) and result.get("status") == "success":
        return {"status": "success", "order_id": result.get("order_id"), "market_id": market_id, "side": side, "price": price, "size": size, "raw": result.get("raw")}
    return {"status": "error", "result": result}
