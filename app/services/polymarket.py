"""
Polymarket service layer.

Handles:
  - Wallet address derivation (proxy + safe) using CREATE2 math
  - Safe deployment via Polymarket relayer
  - CLOB API credential management
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

from cryptography.fernet import Fernet

from app.core.config import (
    POLYMARKET_RELAYER_URL,
    POLYMARKET_RELAYER_KEY,
    POLYMARKET_RELAYER_ADDRESS,
    POLYMARKET_API_KEY,
    POLYMARKET_API_SECRET,
    POLYMARKET_API_PASSPHRASE,
    CLOB_HOST,
    ENCRYPTION_KEY,
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


# ---------------------------------------------------------------------------
# CLOB API credential derivation
# ---------------------------------------------------------------------------

CLOB_AUTH_MESSAGE = "This message attests that I control the given wallet"


@dataclass
class ClobSigningMessage:
    """
    The EIP-712 ClobAuth message iOS needs to sign to derive CLOB API credentials.

    EIP-712 domain: { name: "ClobAuthDomain", version: "1", chainId: 137 }
    EIP-712 type:   ClobAuth { address: address, timestamp: string, nonce: uint256, message: string }
    """
    address: str
    timestamp: str
    nonce: int
    message: str


@dataclass
class ClobCredentials:
    api_key: str
    api_secret: str
    passphrase: str


def get_clob_signing_message(eoa_address: str) -> ClobSigningMessage:
    """
    Build the EIP-712 ClobAuth payload iOS needs to sign.

    iOS signs this with eth_signTypedData_v4, then calls POST /users/clob/credentials
    with the resulting signature + timestamp + nonce.
    """
    return ClobSigningMessage(
        address=Web3.to_checksum_address(eoa_address),
        timestamp=str(int(time.time())),
        nonce=0,
        message=CLOB_AUTH_MESSAGE,
    )


async def derive_clob_credentials(
    eoa_address: str,
    signature: str,
    timestamp: str,
    nonce: int = 0,
) -> ClobCredentials:
    """
    Derive Polymarket CLOB API credentials from a signed ClobAuth EIP-712 message.

    Sends Level 1 headers (POLY_ADDRESS/SIGNATURE/TIMESTAMP/NONCE) to the CLOB API.
    Tries GET /auth/derive-api-key first (returns existing key if present),
    falls back to POST /auth/api-key (creates a new key).

    Args:
        eoa_address: User's EOA address
        signature:   EIP-712 ClobAuth signature from iOS Magic signing
        timestamp:   Unix timestamp string (seconds) used when signing
        nonce:       Nonce used when signing (always 0 for first-time derivation)

    Returns:
        ClobCredentials with api_key, api_secret, passphrase
    """
    headers = {
        "POLY_ADDRESS": Web3.to_checksum_address(eoa_address),
        "POLY_SIGNATURE": signature,
        "POLY_TIMESTAMP": timestamp,
        "POLY_NONCE": str(nonce),
    }

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{CLOB_HOST}/auth/derive-api-key",
            headers=headers,
            timeout=15.0,
        )
        if resp.status_code != 200:
            logger.info("derive-api-key returned %s, trying POST /auth/api-key", resp.status_code)
            resp = await client.post(
                f"{CLOB_HOST}/auth/api-key",
                headers=headers,
                timeout=15.0,
            )
            resp.raise_for_status()

        data = resp.json()
        logger.info("CLOB credential response: %s", data)

    return ClobCredentials(
        api_key=data["apiKey"],
        api_secret=data["secret"],
        passphrase=data["passphrase"],
    )


def encrypt_credential(value: str) -> str:
    """Encrypt a CLOB credential value with Fernet symmetric encryption."""
    return Fernet(ENCRYPTION_KEY.encode()).encrypt(value.encode()).decode()


def decrypt_credential(encrypted: str) -> str:
    """Decrypt a Fernet-encrypted CLOB credential value."""
    return Fernet(ENCRYPTION_KEY.encode()).decrypt(encrypted.encode()).decode()


# ---------------------------------------------------------------------------
# User-level CLOB L2 authentication
# ---------------------------------------------------------------------------

def build_user_clob_headers(
    method: str,
    path: str,
    api_key: str,
    secret: str,
    passphrase: str,
    eoa_address: str,
    body: str = "",
) -> dict:
    """
    Build the 5 POLY_* headers required for L2-authenticated CLOB requests.

    Uses the user's stored CLOB credentials (api_key, secret, passphrase)
    derived from their EOA via EIP-712 ClobAuth signing.

    Timestamp is UNIX seconds (not milliseconds — different from builder headers).
    """
    timestamp = str(int(time.time()))
    sig = _build_hmac_signature(secret, timestamp, method, path, body)
    headers = {
        "POLY_ADDRESS": Web3.to_checksum_address(eoa_address),
        "POLY_API_KEY": api_key,
        "POLY_PASSPHRASE": passphrase,
        "POLY_TIMESTAMP": timestamp,
        "POLY_SIGNATURE": sig,
    }
    if method.upper() in ("POST", "DELETE", "PUT", "PATCH"):
        headers["Content-Type"] = "application/json"
    return headers


async def get_user_clob_credentials(eoa_address: str, db) -> ClobCredentials:
    """
    Load and decrypt a user's CLOB credentials from the DB.

    Args:
        eoa_address: User's EOA wallet address
        db:          AsyncSession from FastAPI dependency

    Raises:
        ValueError: If no account or credentials found for this EOA
    """
    from sqlalchemy import select
    from app.models.db import User, Account

    result = await db.execute(
        select(Account).join(User).where(
            User.eoa_address == eoa_address,
            Account.exchange == "polymarket",
        )
    )
    account = result.scalar_one_or_none()
    if not account or not account.encrypted_api_key:
        raise ValueError(f"No CLOB credentials found for {eoa_address} — call POST /users/clob/credentials first")

    return ClobCredentials(
        api_key=decrypt_credential(account.encrypted_api_key),
        api_secret=decrypt_credential(account.encrypted_api_secret),
        passphrase=decrypt_credential(account.encrypted_passphrase),
    )
