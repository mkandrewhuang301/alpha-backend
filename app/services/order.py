"""
Polymarket order preparation and submission service.

Flow:
  1. iOS calls POST /polymarket/order/prepare — backend fetches market params,
     calculates amounts, returns full EIP-712 typed data for Privy to sign.
  2. iOS signs with Privy embedded wallet → gets signature.
  3. iOS calls POST /polymarket/order/submit — backend reconstructs order,
     adds HMAC auth headers, POSTs to Polymarket CLOB.

Scaling notes:
  - Shared httpx client (_clob_http) — reuses connections instead of opening
    a new TCP connection per request.
  - tick_size and neg_risk are cached in Redis (1h TTL) — they almost never
    change for a live market. fee_rate cached for 5 minutes.
  - Redis cache avoids 3 Polymarket API round-trips on every order prep.
"""

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from decimal import Decimal
from math import ceil, floor

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import CLOB_HOST
from app.core.redis import get_redis
from app.models.db import Account, User
from app.services.polymarket import (
    ClobCredentials,
    build_user_clob_headers,
    decrypt_credential,
    derive_proxy_wallet,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared httpx client — reused across requests (no new TCP connection per call)
# ---------------------------------------------------------------------------

_clob_http: httpx.AsyncClient | None = None


def _get_http() -> httpx.AsyncClient:
    """Return shared async httpx client, creating it on first call."""
    global _clob_http
    if _clob_http is None or _clob_http.is_closed:
        _clob_http = httpx.AsyncClient(base_url=CLOB_HOST, timeout=10.0)
    return _clob_http


# ---------------------------------------------------------------------------
# Contract addresses (Polygon mainnet, chainId 137)
# ---------------------------------------------------------------------------

CTF_EXCHANGE          = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # binary markets
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"  # multi-outcome markets
ZERO_ADDRESS          = "0x0000000000000000000000000000000000000000"
POLYGON_CHAIN_ID      = 137

# EIP-712 Order type definition (field order matters for signing)
ORDER_TYPE = [
    {"name": "salt",          "type": "uint256"},
    {"name": "maker",         "type": "address"},
    {"name": "signer",        "type": "address"},
    {"name": "taker",         "type": "address"},
    {"name": "tokenId",       "type": "uint256"},
    {"name": "makerAmount",   "type": "uint256"},
    {"name": "takerAmount",   "type": "uint256"},
    {"name": "expiration",    "type": "uint256"},
    {"name": "nonce",         "type": "uint256"},
    {"name": "feeRateBps",    "type": "uint256"},
    {"name": "side",          "type": "uint8"},
    {"name": "signatureType", "type": "uint8"},
]

# ---------------------------------------------------------------------------
# Amount rounding (mirrors py-clob-client order_builder/helpers.py exactly)
# ---------------------------------------------------------------------------

@dataclass
class RoundingConfig:
    price:  int  # decimal places for price
    size:   int  # decimal places for size
    amount: int  # decimal places for token amounts

ROUNDING_CONFIG: dict[str, RoundingConfig] = {
    "0.1":    RoundingConfig(price=1, size=2, amount=3),
    "0.01":   RoundingConfig(price=2, size=2, amount=4),
    "0.001":  RoundingConfig(price=3, size=2, amount=5),
    "0.0001": RoundingConfig(price=4, size=2, amount=6),
}


def _decimal_places(x: float) -> int:
    return abs(Decimal(str(x)).as_tuple().exponent)

def _round_down(x: float, decimals: int) -> float:
    return floor(x * (10 ** decimals)) / (10 ** decimals)

def _round_up(x: float, decimals: int) -> float:
    return ceil(x * (10 ** decimals)) / (10 ** decimals)

def _round_normal(x: float, decimals: int) -> float:
    return round(x * (10 ** decimals)) / (10 ** decimals)

def _to_6dec(x: float) -> int:
    """Convert float to 6-decimal integer (USDC units)."""
    f = (10 ** 6) * x
    if _decimal_places(f) > 0:
        f = _round_normal(f, 0)
    return int(f)


def _get_buy_amounts(price: float, size: float, tick_size: str) -> tuple[int, int]:
    """BUY: maker spends USDC, receives shares. Returns (makerAmount, takerAmount)."""
    cfg = ROUNDING_CONFIG[tick_size]
    raw_price = _round_normal(price, cfg.price)
    raw_taker = _round_down(size, cfg.size)        # shares to receive
    raw_maker = raw_taker * raw_price              # USDC to spend
    if _decimal_places(raw_maker) > cfg.amount:
        raw_maker = _round_up(raw_maker, cfg.amount + 4)
        if _decimal_places(raw_maker) > cfg.amount:
            raw_maker = _round_down(raw_maker, cfg.amount)
    return _to_6dec(raw_maker), _to_6dec(raw_taker)


def _get_sell_amounts(price: float, size: float, tick_size: str) -> tuple[int, int]:
    """SELL: maker provides shares, receives USDC. Returns (makerAmount, takerAmount)."""
    cfg = ROUNDING_CONFIG[tick_size]
    raw_price = _round_normal(price, cfg.price)
    raw_maker = _round_down(size, cfg.size)        # shares to sell
    raw_taker = raw_maker * raw_price              # USDC to receive
    if _decimal_places(raw_taker) > cfg.amount:
        raw_taker = _round_up(raw_taker, cfg.amount + 4)
        if _decimal_places(raw_taker) > cfg.amount:
            raw_taker = _round_down(raw_taker, cfg.amount)
    return _to_6dec(raw_maker), _to_6dec(raw_taker)


def _generate_salt() -> int:
    """
    Generate order salt — small integer matching py-clob-client convention.
    Polymarket rejects salts that are too large (uint256 max causes issues
    with some contract implementations).
    """
    return round(time.time() * random.random())


# ---------------------------------------------------------------------------
# Redis-cached CLOB market info fetchers
#
# tick_size and neg_risk are static per market (1h TTL — they never change
# mid-market in practice). fee_rate can change with Polymarket fee schedules
# (5m TTL).
#
# Cache keys:  clob:tick:{token_id}
#              clob:neg_risk:{token_id}
#              clob:fee:{token_id}
# ---------------------------------------------------------------------------

_TICK_TTL    = 3600  # 1 hour
_NEG_TTL     = 3600  # 1 hour
_FEE_TTL     = 300   # 5 minutes


async def _fetch_tick_size(token_id: str) -> str:
    redis = await get_redis()
    key = f"clob:tick:{token_id}"
    cached = await redis.get(key)
    if cached:
        return cached
    resp = await _get_http().get("/tick-size", params={"token_id": token_id})
    resp.raise_for_status()
    value = str(resp.json()["minimum_tick_size"])
    await redis.set(key, value, ex=_TICK_TTL)
    return value


async def _fetch_neg_risk(token_id: str) -> bool:
    redis = await get_redis()
    key = f"clob:neg_risk:{token_id}"
    cached = await redis.get(key)
    if cached is not None:
        return cached == "1"
    resp = await _get_http().get("/neg-risk", params={"token_id": token_id})
    resp.raise_for_status()
    value = resp.json().get("neg_risk", False)
    await redis.set(key, "1" if value else "0", ex=_NEG_TTL)
    return value


async def _fetch_fee_rate(token_id: str) -> int:
    redis = await get_redis()
    key = f"clob:fee:{token_id}"
    cached = await redis.get(key)
    if cached is not None:
        return int(cached)
    resp = await _get_http().get("/fee-rate", params={"token_id": token_id})
    resp.raise_for_status()
    value = int(resp.json().get("base_fee", 0))
    await redis.set(key, str(value), ex=_FEE_TTL)
    return value


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _get_credentials(eoa_address: str, db: AsyncSession) -> ClobCredentials:
    result = await db.execute(
        select(Account).join(User).where(
            User.eoa_address == eoa_address,
            Account.exchange == "polymarket",
        )
    )
    account = result.scalar_one_or_none()
    if not account:
        raise ValueError(f"No Polymarket account found for {eoa_address} — register first")
    if not account.encrypted_api_key:
        raise ValueError(f"No CLOB credentials for {eoa_address} — call POST /users/clob/credentials first")
    return ClobCredentials(
        api_key=decrypt_credential(account.encrypted_api_key),
        api_secret=decrypt_credential(account.encrypted_api_secret),
        passphrase=decrypt_credential(account.encrypted_passphrase),
    )


# ---------------------------------------------------------------------------
# Prepare order (called before iOS signs)
# ---------------------------------------------------------------------------

@dataclass
class PreparedOrder:
    """Everything iOS needs to sign the order and submit it back."""
    eip712:       dict  # Full EIP-712 typed data for eth_signTypedData_v4
    salt:         int   # Must match signed value — return unchanged in submit
    maker_amount: str   # 6-decimal USDC string
    taker_amount: str   # 6-decimal shares string
    fee_rate_bps: str   # basis points string
    neg_risk:     bool  # Which exchange contract was used
    tick_size:    str   # Market tick precision


async def prepare_order(
    eoa_address: str,
    token_id: str,
    price: float,
    size: float,
    side: str,  # "BUY" or "SELL"
) -> PreparedOrder:
    """
    Fetch market params (cached in Redis), calculate amounts, build EIP-712 payload.

    iOS must pass back salt, maker_amount, taker_amount, fee_rate_bps, neg_risk
    unchanged in the submit request — they must match what was signed.
    """
    if side not in ("BUY", "SELL"):
        raise ValueError(f"Invalid side: {side}. Must be BUY or SELL")

    tick_size, neg_risk, fee_rate_bps = await asyncio.gather(
        _fetch_tick_size(token_id),
        _fetch_neg_risk(token_id),
        _fetch_fee_rate(token_id),
    )

    if tick_size not in ROUNDING_CONFIG:
        raise ValueError(f"Unknown tick_size: {tick_size}")

    cfg = ROUNDING_CONFIG[tick_size]
    if abs(_round_normal(price, cfg.price) - price) > 1e-9:
        raise ValueError(f"Price {price} does not fit tick size {tick_size}")

    side_int = 0 if side == "BUY" else 1
    if side == "BUY":
        maker_amount, taker_amount = _get_buy_amounts(price, size, tick_size)
    else:
        maker_amount, taker_amount = _get_sell_amounts(price, size, tick_size)

    salt = _generate_salt()
    exchange_address = NEG_RISK_CTF_EXCHANGE if neg_risk else CTF_EXCHANGE

    eip712 = {
        "domain": {
            "name": "Polymarket CTF Exchange",
            "version": "1",
            "chainId": POLYGON_CHAIN_ID,
            "verifyingContract": exchange_address,
        },
        "types": {"Order": ORDER_TYPE},
        "primaryType": "Order",
        "message": {
            "salt":          str(salt),
            "maker":         derive_proxy_wallet(eoa_address),
            "signer":        eoa_address,
            "taker":         ZERO_ADDRESS,
            "tokenId":       token_id,
            "makerAmount":   str(maker_amount),
            "takerAmount":   str(taker_amount),
            "expiration":    "0",
            "nonce":         "0",
            "feeRateBps":    str(fee_rate_bps),
            "side":          str(side_int),
            "signatureType": "1",
        },
    }

    return PreparedOrder(
        eip712=eip712,
        salt=salt,
        maker_amount=str(maker_amount),
        taker_amount=str(taker_amount),
        fee_rate_bps=str(fee_rate_bps),
        neg_risk=neg_risk,
        tick_size=tick_size,
    )


# ---------------------------------------------------------------------------
# Submit order (called after iOS signs)
# ---------------------------------------------------------------------------

@dataclass
class OrderResult:
    order_id: str
    status:   str
    success:  bool


async def submit_order(
    eoa_address: str,
    token_id: str,
    signature: str,
    salt: int,
    maker_amount: str,
    taker_amount: str,
    fee_rate_bps: str,
    side: str,
    neg_risk: bool,
    db: AsyncSession,
    expiration: str = "0",
    nonce: str = "0",
    order_type: str = "GTC",
) -> OrderResult:
    """
    Reconstruct the signed order and POST to Polymarket CLOB with HMAC auth.

    salt, maker_amount, taker_amount, fee_rate_bps must match the prepare
    response exactly — they are part of the signed EIP-712 struct.
    """
    creds = await _get_credentials(eoa_address, db)
    side_str = side.upper()
    if side_str not in ("BUY", "SELL"):
        raise ValueError(f"Invalid side: {side}")

    order = {
        "salt":          salt,          # integer
        "maker":         derive_proxy_wallet(eoa_address),
        "signer":        eoa_address,
        "taker":         ZERO_ADDRESS,
        "tokenId":       token_id,
        "makerAmount":   maker_amount,  # string
        "takerAmount":   taker_amount,  # string
        "side":          side_str,      # "BUY"/"SELL" string for REST body
        "expiration":    expiration,
        "nonce":         nonce,
        "feeRateBps":    fee_rate_bps,
        "signatureType": 1,             # POLY_PROXY = 1 (EOA signs, proxy wallet holds funds)
        "signature":     signature,
    }

    body = {
        "order":     order,
        "owner":     creds.api_key,     # api_key UUID, not EOA address
        "orderType": order_type,
    }
    body_str = json.dumps(body, separators=(",", ":"))

    headers = build_user_clob_headers(
        method="POST",
        path="/order",
        api_key=creds.api_key,
        secret=creds.api_secret,
        passphrase=creds.passphrase,
        eoa_address=eoa_address,
        body=body_str,
    )

    resp = await _get_http().post("/order", content=body_str, headers=headers)
    if resp.status_code not in (200, 201):
        logger.error("Polymarket order rejected %s: %s", resp.status_code, resp.text)
        raise ValueError(f"Polymarket rejected order ({resp.status_code}): {resp.text}")

    data = resp.json()
    logger.info("Order submitted: eoa=%s order_id=%s status=%s", eoa_address, data.get("orderID"), data.get("status"))

    return OrderResult(
        order_id=data.get("orderID", ""),
        status=data.get("status", ""),
        success=data.get("success", True),
    )
