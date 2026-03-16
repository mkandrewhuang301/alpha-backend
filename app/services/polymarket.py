"""
Polymarket service layer.

Handles:
  - Wallet address derivation (proxy + safe) using CREATE2 math
  - Safe deployment via Polymarket relayer
  - CLOB API credential management (planned)
  - Order signing and submission (planned)
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass

import httpx
from web3 import Web3
from eth_abi import encode
from eth_abi.packed import encode_packed

from app.core.config import (
    POLYMARKET_RELAYER_URL,
    POLYMARKET_RELAYER_KEY,
    POLYMARKET_RELAYER_ADDRESS,
    POLYMARKET_API_KEY,
    POLYMARKET_API_SECRET,
    POLYMARKET_API_PASSPHRASE,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Polygon mainnet contract constants (from Polymarket's builder-relayer-client)
# ---------------------------------------------------------------------------

# Gnosis Safe — requires Polymarket builder relayer access to deploy
SAFE_FACTORY = "0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b"
SAFE_FACTORY_NAME = "Polymarket Contract Proxy Factory"
SAFE_INIT_CODE_HASH = "0x2bce2127ff07fb632d16c8347c4ebf501f4841168bed00d9e6ef715ddb6fcecf"

# Polymarket Proxy wallet — deploys automatically on first transaction, no relayer needed
PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
PROXY_INIT_CODE_HASH = "0xd21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
POLYGON_CHAIN_ID = 137


# ---------------------------------------------------------------------------
# Wallet derivation
# ---------------------------------------------------------------------------

def derive_proxy_wallet(eoa_address: str) -> str:
    """
    Derive the Polymarket Proxy wallet address from an EOA address.

    Uses CREATE2 deterministic address derivation — same EOA always produces
    the same proxy wallet address. No network call required.

    Use this until Polymarket builder relayer access is obtained.
    """
    checksummed = Web3.to_checksum_address(eoa_address)
    salt = Web3.keccak(encode_packed(["address"], [checksummed]))
    proxy_address = Web3.keccak(
        b'\xff' +
        bytes.fromhex(PROXY_FACTORY[2:]) +
        salt +
        bytes.fromhex(PROXY_INIT_CODE_HASH[2:])
    )[-20:]
    return Web3.to_checksum_address('0x' + proxy_address.hex())


def derive_safe(eoa_address: str) -> str:
    """
    Derive the Gnosis Safe address from an EOA address.

    Uses CREATE2 deterministic address derivation — same EOA always produces
    the same Safe address. No network call required.

    Note: Requires Polymarket builder relayer access to actually deploy.
    """
    checksummed = Web3.to_checksum_address(eoa_address)
    salt = Web3.keccak(encode(["address"], [checksummed]))
    safe_address = Web3.keccak(
        b'\xff' +
        bytes.fromhex(SAFE_FACTORY[2:]) +
        salt +
        bytes.fromhex(SAFE_INIT_CODE_HASH[2:])
    )[-20:]
    return Web3.to_checksum_address('0x' + safe_address.hex())


# ---------------------------------------------------------------------------
# Relayer — Safe deployment
# ---------------------------------------------------------------------------

def _relayer_headers() -> dict:
    """Auth headers for relayer GET requests."""
    return {
        "RELAYER_API_KEY": POLYMARKET_RELAYER_KEY,
        "RELAYER_API_KEY_ADDRESS": POLYMARKET_RELAYER_ADDRESS,
        "Content-Type": "application/json",
    }


def _build_hmac_signature(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    base64_secret = base64.urlsafe_b64decode(secret)
    message = timestamp + method + path
    if body:
        message += body.replace("'", '"')
    h = hmac.new(base64_secret, message.encode("utf-8"), hashlib.sha256)
    return base64.urlsafe_b64encode(h.digest()).decode("utf-8")


def _builder_headers(method: str, path: str, body: str = "") -> dict:
    """HMAC auth headers for relayer POST /submit."""
    timestamp = str(int(time.time() * 1000))  # milliseconds
    sig = _build_hmac_signature(POLYMARKET_API_SECRET.strip(), timestamp, method, path, body)
    return {
        "POLY_BUILDER_SIGNATURE": sig,
        "POLY_BUILDER_TIMESTAMP": timestamp,
        "POLY_BUILDER_API_KEY": POLYMARKET_API_KEY.strip(),
        "POLY_BUILDER_PASSPHRASE": POLYMARKET_API_PASSPHRASE.strip(),
        "Content-Type": "application/json",
    }


@dataclass
class SafeDeployPayload:
    """
    Everything the iOS app needs to construct and sign the EIP-712 SafeTx
    for Safe deployment, then call POST /users/safe/deploy.

    EIP-712 domain: { chainId: 137, verifyingContract: safe_address }
    EIP-712 type:   SafeTx (standard Gnosis Safe transaction type)
    """
    safe_address: str    # verifyingContract in EIP-712 domain
    nonce: str           # from relayer — always "0" for fresh deploy
    chain_id: int        # 137 (Polygon mainnet)
    # SafeTx fields — iOS builds these into the EIP-712 message
    to: str              # Safe factory address
    value: str           # "0"
    data: str            # "0x" (empty)
    operation: str       # "0" (Call)
    safe_txn_gas: str    # "0"
    base_gas: str        # "0"
    gas_price: str       # "0"
    gas_token: str       # zero address
    refund_receiver: str # zero address


async def get_safe_deploy_payload(eoa_address: str) -> SafeDeployPayload:
    """
    Build everything iOS needs to sign a Safe deployment transaction.

    Fetches the nonce from the relayer, then constructs the full EIP-712
    SafeTx payload that iOS Magic will sign.

    Args:
        eoa_address: User's EOA address (0x...)

    Returns:
        SafeDeployPayload with EIP-712 fields ready for iOS to sign
    """
    safe_address = derive_safe(eoa_address)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{POLYMARKET_RELAYER_URL}/relay-payload",
            params={"address": eoa_address, "type": "SAFE"},
            headers=_relayer_headers(),
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info("Relayer relay-payload response: %s", data)

    nonce = str(data.get("nonce", "0"))

    return SafeDeployPayload(
        safe_address=safe_address,
        nonce=nonce,
        chain_id=POLYGON_CHAIN_ID,
        to=SAFE_FACTORY,
        value="0",
        data="0x",
        operation="0",
        safe_txn_gas="0",
        base_gas="0",
        gas_price="0",
        gas_token=ZERO_ADDRESS,
        refund_receiver=ZERO_ADDRESS,
    )


@dataclass
class SafeDeployResult:
    safe_address: str
    transaction_hash: str


async def deploy_safe(eoa_address: str, signature: str) -> SafeDeployResult:
    """
    Submit a signed Safe deployment transaction to Polymarket's relayer.

    iOS signs the EIP-712 SafeTx payload from get_safe_deploy_payload(),
    then calls this with the resulting signature.

    Args:
        eoa_address: User's EOA address
        signature:   EIP-712 signature from iOS Magic signing

    Returns:
        SafeDeployResult with safe_address and transaction_hash
    """
    safe_address = derive_safe(eoa_address)

    body = {
        "from": eoa_address,
        "to": SAFE_FACTORY,
        "proxyWallet": safe_address,
        "data": "0x",
        "signature": signature,
        "signatureParams": {
            "paymentToken": ZERO_ADDRESS,
            "payment": "0",
            "paymentReceiver": ZERO_ADDRESS,
        },
        "type": "SAFE-CREATE",
    }

    async with httpx.AsyncClient() as client:
        body_str = json.dumps(body)
        resp = await client.post(
            f"{POLYMARKET_RELAYER_URL}/submit",
            content=body_str,
            headers=_builder_headers("POST", "/submit", body_str),
            timeout=15.0,
        )
        hdrs = _builder_headers("POST", "/submit", body_str)
        logger.info("=== RELAYER SUBMIT REQUEST ===")
        logger.info("Headers: %s", hdrs)
        logger.info("URL: %s/submit", POLYMARKET_RELAYER_URL)
        logger.info("Body: %s", json.dumps(body, indent=2))
        logger.info("=== RELAYER SUBMIT RESPONSE ===")
        logger.info("Status: %s", resp.status_code)
        logger.info("Body: %s", resp.text)
        resp.raise_for_status()
        submit_data = resp.json()
        transaction_id = submit_data["transactionID"]

        # Poll until mined (max 2 minutes)
        for _ in range(24):
            await asyncio.sleep(5)
            poll = await client.get(
                f"{POLYMARKET_RELAYER_URL}/transaction",
                params={"id": transaction_id},
                headers=_relayer_headers(),
                timeout=15.0,
            )
            poll.raise_for_status()
            poll_data = poll.json()
            if isinstance(poll_data, list):
                poll_data = poll_data[0] if poll_data else {}
            state = poll_data.get("state", "")
            logger.info("Poll response: %s", poll_data)

            if state == "STATE_MINED":
                return SafeDeployResult(
                    safe_address=safe_address,
                    transaction_hash=poll_data.get("transactionHash", ""),
                )
            if state in ("STATE_FAILED", "STATE_REJECTED"):
                error_msg = poll_data.get("errorMsg", "")
                if "already deployed" in error_msg:
                    return SafeDeployResult(
                        safe_address=safe_address,
                        transaction_hash=poll_data.get("transactionHash", ""),
                    )
                raise ValueError(f"Safe deployment failed: state={state}, error={error_msg}")

    raise TimeoutError("Safe deployment timed out after 2 minutes")
