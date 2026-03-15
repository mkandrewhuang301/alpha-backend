"""
Polymarket service layer.

Handles:
  - Wallet address derivation (proxy + safe) using CREATE2 math
  - Safe deployment via Polymarket relayer
  - CLOB API credential management (planned)
  - Order signing and submission (planned)
"""

import asyncio
import logging
from dataclasses import dataclass

import httpx
from web3 import Web3
from eth_abi import encode
from eth_abi.packed import encode_packed

from app.core.config import (
    POLYMARKET_RELAYER_URL,
    POLYMARKET_RELAYER_KEY,
    POLYMARKET_RELAYER_ADDRESS,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Polygon mainnet contract constants (from Polymarket's builder-relayer-client)
# ---------------------------------------------------------------------------

# Gnosis Safe — requires Polymarket builder relayer access to deploy
SAFE_FACTORY = "0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b"
SAFE_INIT_CODE_HASH = "0x2bce2127ff07fb632d16c8347c4ebf501f4841168bed00d9e6ef715ddb6fcecf"

# Polymarket Proxy wallet — deploys automatically on first transaction, no relayer needed
PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
PROXY_INIT_CODE_HASH = "0xd21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b"


# ---------------------------------------------------------------------------
# Wallet derivation
# ---------------------------------------------------------------------------

def derive_proxy_wallet(eoa_address: str) -> str:
    """
    Derive the Polymarket Proxy wallet address from an EOA address.

    Uses CREATE2 deterministic address derivation — same EOA always produces
    the same proxy wallet address. No network call required.

    Use this until Polymarket builder relayer access is obtained.

    Args:
        eoa_address: The user's EOA address (0x...)

    Returns:
        Checksummed proxy wallet address (0x...)
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
    Switch to this from derive_proxy_wallet once builder access is obtained.

    Args:
        eoa_address: The user's EOA address (0x...)

    Returns:
        Checksummed Safe address (0x...)
    """
    # encodeAbiParameters = standard ABI encoding (32-byte padded), NOT packed
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
    """Auth headers for all Polymarket relayer requests."""
    return {
        "RELAYER_API_KEY": POLYMARKET_RELAYER_KEY,
        "RELAYER_API_KEY_ADDRESS": POLYMARKET_RELAYER_ADDRESS,
        "Content-Type": "application/json",
    }


@dataclass
class RelayPayload:
    """Payload returned by the relayer that iOS must sign."""
    to: str
    data: str
    nonce: str
    gas_price: str
    operation: str
    safe_txn_gas: str
    base_gas: str
    gas_token: str
    refund_receiver: str
    safe_address: str


async def get_relay_payload(eoa_address: str) -> RelayPayload:
    """
    Fetch the Safe deployment payload from Polymarket's relayer.

    The returned payload must be signed by the user's EOA (via Magic on iOS).
    No on-chain interaction — just fetches what needs to be signed.

    Args:
        eoa_address: User's EOA address (0x...)

    Returns:
        RelayPayload containing all fields needed for iOS to sign + backend to submit
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

    return RelayPayload(
        to=data.get("to", ""),
        data=data.get("data", ""),
        nonce=str(data.get("nonce", "0")),
        gas_price=str(data.get("gasPrice", "0")),
        operation=str(data.get("operation", "0")),
        safe_txn_gas=str(data.get("safeTxnGas", "0")),
        base_gas=str(data.get("baseGas", "0")),
        gas_token=data.get("gasToken", "0x0000000000000000000000000000000000000000"),
        refund_receiver=data.get("refundReceiver", "0x0000000000000000000000000000000000000000"),
        safe_address=safe_address,
    )


@dataclass
class SafeDeployResult:
    safe_address: str
    transaction_hash: str


async def deploy_safe(eoa_address: str, signature: str, payload: dict) -> SafeDeployResult:
    """
    Submit a signed Safe deployment transaction to Polymarket's relayer.

    Polls until the Safe is confirmed on-chain, then returns the safe_address.

    Args:
        eoa_address: User's EOA address
        signature:   EIP-712 signature from iOS (Magic signed the relay payload)
        payload:     The relay payload dict originally returned by get_relay_payload()

    Returns:
        SafeDeployResult with safe_address and transaction_hash
    """
    safe_address = derive_safe(eoa_address)

    body = {
        "from": eoa_address,
        "to": payload["to"],
        "proxyWallet": safe_address,
        "data": payload["data"],
        "nonce": payload["nonce"],
        "signature": signature,
        "type": "SAFE",
        "signatureParams": {
            "gasPrice": payload["gas_price"],
            "operation": payload["operation"],
            "safeTxnGas": payload["safe_txn_gas"],
            "baseGas": payload["base_gas"],
            "gasToken": payload["gas_token"],
            "refundReceiver": payload["refund_receiver"],
        },
    }

    async with httpx.AsyncClient() as client:
        # Submit deployment transaction
        resp = await client.post(
            f"{POLYMARKET_RELAYER_URL}/submit",
            json=body,
            headers=_relayer_headers(),
            timeout=15.0,
        )
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
            state = poll_data.get("state", "")

            if state == "STATE_MINED":
                return SafeDeployResult(
                    safe_address=safe_address,
                    transaction_hash=poll_data.get("transactionHash", ""),
                )
            if state in ("STATE_FAILED", "STATE_REJECTED"):
                raise ValueError(f"Safe deployment failed: state={state}")

    raise TimeoutError("Safe deployment timed out after 2 minutes")
