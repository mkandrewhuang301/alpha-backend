"""
Polymarket token approval service.

Handles the one-time on-chain approvals required before trading on Polymarket.
Uses the Polymarket builder relayer for gasless execution — no MATIC needed.

Flow:
  GET  /approvals/status        — checks on-chain which approvals are set
  GET  /approvals/signing-message — returns struct hash for iOS to sign via Privy
  POST /approvals/execute       — takes Privy signature, submits to relayer gaslessly

Ported from TypeScript:
  https://github.com/Polymarket/builder-relayer-client/blob/main/src/builder/proxy.ts
  https://github.com/Polymarket/builder-relayer-client/blob/main/src/encode/proxy.ts
"""

import asyncio
import json
import logging
import ssl
import time
from dataclasses import dataclass

import certifi
import httpx
from eth_abi import encode
from web3 import AsyncWeb3
from web3.providers import AsyncHTTPProvider

from app.core.config import (
    POLYGON_RPC_URL,
    POLYMARKET_RELAYER_URL,
    POLYMARKET_RELAYER_KEY,
    POLYMARKET_RELAYER_ADDRESS,
    POLYMARKET_API_KEY,
    POLYMARKET_API_SECRET,
    POLYMARKET_API_PASSPHRASE,
)
from app.services.polymarket import derive_proxy_wallet, _build_hmac_signature

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Polygon contract addresses
# ---------------------------------------------------------------------------

USDC_ADDRESS        = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon
CTF_ADDRESS         = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Conditional Token Framework
CTF_EXCHANGE        = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE   = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
NEG_RISK_ADAPTER    = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# Proxy wallet relayer contracts (Polygon mainnet)
# Source: https://github.com/Polymarket/builder-relayer-client/blob/main/src/config/index.ts
PROXY_FACTORY       = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
RELAY_HUB           = "0xD216153c06E857cD7f72665E0aF1d7D82172F494"

MAX_UINT256 = 2**256 - 1
POLYGON_CHAIN_ID = 137
DEFAULT_GAS_LIMIT = 500_000  # Relay hub checks gasleft() >= gasLimit*2; keep below 750k for 1.55M relay TX

# ---------------------------------------------------------------------------
# Minimal ABIs
# ---------------------------------------------------------------------------

USDC_ABI = [
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "allowance",
        "type": "function",
        "inputs": [
            {"name": "owner",   "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]

CTF_ABI = [
    {
        "name": "setApprovalForAll",
        "type": "function",
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "outputs": [],
    },
    {
        "name": "isApprovedForAll",
        "type": "function",
        "inputs": [
            {"name": "account",  "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
    },
]

# ProxyWalletFactory ABI — proxy(tuple[]) function
# Source: https://github.com/Polymarket/builder-relayer-client/blob/main/src/abis/proxyFactory.ts
PROXY_FACTORY_ABI = [
    {
        "constant": False,
        "inputs": [
            {
                "components": [
                    {"name": "typeCode", "type": "uint8"},
                    {"name": "to",      "type": "address"},
                    {"name": "value",   "type": "uint256"},
                    {"name": "data",    "type": "bytes"},
                ],
                "name": "calls",
                "type": "tuple[]",
            }
        ],
        "name": "proxy",
        "outputs": [{"name": "returnValues", "type": "bytes[]"}],
        "payable": True,
        "stateMutability": "payable",
        "type": "function",
    }
]


def _get_w3() -> AsyncWeb3:
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    return AsyncWeb3(AsyncHTTPProvider(
        POLYGON_RPC_URL.strip(),
        request_kwargs={"ssl": ssl_ctx},
    ))


# ---------------------------------------------------------------------------
# On-chain approval status check
# ---------------------------------------------------------------------------

@dataclass
class ApprovalStatus:
    usdc_for_ctf_exchange: bool
    usdc_for_neg_risk_exchange: bool
    usdc_for_neg_risk_adapter: bool
    ctf_for_ctf_exchange: bool
    ctf_for_neg_risk_exchange: bool
    ctf_for_neg_risk_adapter: bool
    all_approved: bool


async def get_approval_status(eoa_address: str) -> ApprovalStatus:
    """Check which Polymarket approvals are set on-chain for the user's proxy wallet."""
    proxy = derive_proxy_wallet(eoa_address)
    w3 = _get_w3()

    usdc = w3.eth.contract(address=AsyncWeb3.to_checksum_address(USDC_ADDRESS), abi=USDC_ABI)
    ctf  = w3.eth.contract(address=AsyncWeb3.to_checksum_address(CTF_ADDRESS),  abi=CTF_ABI)
    proxy_cs = AsyncWeb3.to_checksum_address(proxy)

    results = await asyncio.gather(
        usdc.functions.allowance(proxy_cs, AsyncWeb3.to_checksum_address(CTF_EXCHANGE)).call(),
        usdc.functions.allowance(proxy_cs, AsyncWeb3.to_checksum_address(NEG_RISK_EXCHANGE)).call(),
        usdc.functions.allowance(proxy_cs, AsyncWeb3.to_checksum_address(NEG_RISK_ADAPTER)).call(),
        ctf.functions.isApprovedForAll(proxy_cs, AsyncWeb3.to_checksum_address(CTF_EXCHANGE)).call(),
        ctf.functions.isApprovedForAll(proxy_cs, AsyncWeb3.to_checksum_address(NEG_RISK_EXCHANGE)).call(),
        ctf.functions.isApprovedForAll(proxy_cs, AsyncWeb3.to_checksum_address(NEG_RISK_ADAPTER)).call(),
        return_exceptions=True,
    )

    def _bool(r):
        if isinstance(r, Exception):
            logger.warning("Approval check failed: %s", r)
            return False
        if isinstance(r, bool):
            return r
        return int(r) > 0

    usdc_ctf      = _bool(results[0])
    usdc_neg_ex   = _bool(results[1])
    usdc_neg      = _bool(results[2])
    ctf_ctf       = _bool(results[3])
    ctf_neg_ex    = _bool(results[4])
    ctf_neg_adapt = _bool(results[5])

    return ApprovalStatus(
        usdc_for_ctf_exchange=usdc_ctf,
        usdc_for_neg_risk_exchange=usdc_neg_ex,
        usdc_for_neg_risk_adapter=usdc_neg,
        ctf_for_ctf_exchange=ctf_ctf,
        ctf_for_neg_risk_exchange=ctf_neg_ex,
        ctf_for_neg_risk_adapter=ctf_neg_adapt,
        all_approved=all([usdc_ctf, usdc_neg_ex, usdc_neg, ctf_ctf, ctf_neg_ex, ctf_neg_adapt]),
    )


# ---------------------------------------------------------------------------
# Proxy transaction encoding
# Ported from: src/encode/proxy.ts + src/abis/proxyFactory.ts
# ---------------------------------------------------------------------------

def _encode_approval_calldata(w3: AsyncWeb3) -> list[tuple]:
    """
    Build the list of (typeCode, to, value, data) tuples for all 5 approvals.
    typeCode=0 means a standard Call (not DelegateCall).
    """
    usdc = w3.eth.contract(address=AsyncWeb3.to_checksum_address(USDC_ADDRESS), abi=USDC_ABI)
    ctf  = w3.eth.contract(address=AsyncWeb3.to_checksum_address(CTF_ADDRESS),  abi=CTF_ABI)

    # typeCode=1 means Call (0=Invalid, 1=Call, 2=DelegateCall per Polymarket SDK types.ts)
    calls = [
        (1, USDC_ADDRESS, 0, usdc.encode_abi("approve", [AsyncWeb3.to_checksum_address(CTF_EXCHANGE),        MAX_UINT256])),
        (1, USDC_ADDRESS, 0, usdc.encode_abi("approve", [AsyncWeb3.to_checksum_address(NEG_RISK_ADAPTER),    MAX_UINT256])),
        (1, USDC_ADDRESS, 0, usdc.encode_abi("approve", [AsyncWeb3.to_checksum_address(NEG_RISK_EXCHANGE),   MAX_UINT256])),
        (1, CTF_ADDRESS,  0, ctf.encode_abi("setApprovalForAll", [AsyncWeb3.to_checksum_address(CTF_EXCHANGE),     True])),
        (1, CTF_ADDRESS,  0, ctf.encode_abi("setApprovalForAll", [AsyncWeb3.to_checksum_address(NEG_RISK_EXCHANGE), True])),
        (1, CTF_ADDRESS,  0, ctf.encode_abi("setApprovalForAll", [AsyncWeb3.to_checksum_address(NEG_RISK_ADAPTER),  True])),
    ]
    return calls


def _encode_proxy_tx_data(calls: list[tuple]) -> str:
    """
    Encode calls array as proxy(tuple[]) calldata.
    Ported from encodeProxyTransactionData in src/encode/proxy.ts.
    """
    w3 = AsyncWeb3()
    factory = w3.eth.contract(
        address=AsyncWeb3.to_checksum_address(PROXY_FACTORY),
        abi=PROXY_FACTORY_ABI,
    )
    return factory.encode_abi("proxy", [calls])


# ---------------------------------------------------------------------------
# Struct hash — ported from createStructHash in src/builder/proxy.ts
# ---------------------------------------------------------------------------

def _create_struct_hash(
    from_address: str,
    to: str,
    data: str,
    tx_fee: str,
    gas_price: str,
    gas_limit: str,
    nonce: str,
    relay_hub: str,
    relay: str,
) -> str:
    """
    Build the keccak256 struct hash the user must sign for a proxy relay transaction.

    Ported from createStructHash() in proxy.ts:
      keccak256(concat(["rlx:", from, to, data, txFee(32), gasPrice(32), gasLimit(32), nonce(32), relayHub, relay]))
    """
    from eth_utils import keccak, to_bytes

    def addr_bytes(addr: str) -> bytes:
        return bytes.fromhex(addr.lower().replace("0x", "").zfill(40))

    def uint256_bytes(val: str) -> bytes:
        return int(val).to_bytes(32, "big")

    prefix   = b"rlx:"
    f_bytes  = addr_bytes(from_address)
    to_bytes_ = addr_bytes(to)
    data_bytes = bytes.fromhex(data.replace("0x", ""))
    fee_bytes  = uint256_bytes(tx_fee)
    gp_bytes   = uint256_bytes(gas_price)
    gl_bytes   = uint256_bytes(gas_limit)
    n_bytes    = uint256_bytes(nonce)
    hub_bytes  = addr_bytes(relay_hub)
    relay_bytes = addr_bytes(relay)

    payload = prefix + f_bytes + to_bytes_ + data_bytes + fee_bytes + gp_bytes + gl_bytes + n_bytes + hub_bytes + relay_bytes
    return "0x" + keccak(payload).hex()


# ---------------------------------------------------------------------------
# Signing message — what iOS signs with Privy
# ---------------------------------------------------------------------------

@dataclass
class ApprovalSigningMessage:
    struct_hash: str    # Raw hash — iOS signs with personal_sign (eth_sign)
    eoa_address: str
    proxy_wallet: str
    nonce: str
    gas_limit: str
    encoded_data: str   # Full proxy() calldata — returned unchanged in execute
    relay_address: str  # Relay address from relayer payload


async def get_approval_signing_message(eoa_address: str) -> ApprovalSigningMessage:
    """
    Build the struct hash for iOS to sign via Privy personal_sign.

    Returns the hash + all fields needed to call POST /approvals/execute.
    """
    proxy = derive_proxy_wallet(eoa_address)

    # Fetch nonce + relay address from relayer
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{POLYMARKET_RELAYER_URL}/relay-payload",
            params={"address": eoa_address, "type": "PROXY"},
            headers={
                "RELAYER_API_KEY": POLYMARKET_RELAYER_KEY,
                "RELAYER_API_KEY_ADDRESS": POLYMARKET_RELAYER_ADDRESS,
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        payload = resp.json()

    logger.info("relay-payload response for PROXY: %s", payload)
    nonce = str(payload.get("nonce", "0"))
    relay_address = str(payload.get("address", RELAY_HUB))

    # Encode all 5 approval calls into proxy() calldata
    w3 = AsyncWeb3()
    calls = _encode_approval_calldata(w3)
    encoded_data = _encode_proxy_tx_data(calls)

    gas_limit = str(DEFAULT_GAS_LIMIT)

    struct_hash = _create_struct_hash(
        from_address=eoa_address,
        to=PROXY_FACTORY,
        data=encoded_data,
        tx_fee="0",
        gas_price="0",
        gas_limit=gas_limit,
        nonce=nonce,
        relay_hub=RELAY_HUB,
        relay=relay_address,
    )

    return ApprovalSigningMessage(
        struct_hash=struct_hash,
        eoa_address=eoa_address,
        proxy_wallet=proxy,
        nonce=nonce,
        gas_limit=gas_limit,
        encoded_data=encoded_data,
        relay_address=relay_address,
    )


# ---------------------------------------------------------------------------
# Relayer submission
# ---------------------------------------------------------------------------

def _builder_headers(method: str, path: str, body: str = "") -> dict:
    import base64, hashlib, hmac as _hmac
    timestamp = str(int(time.time() * 1000))
    sig = _build_hmac_signature(POLYMARKET_API_SECRET.strip(), timestamp, method, path, body)
    return {
        "POLY_BUILDER_SIGNATURE": sig,
        "POLY_BUILDER_TIMESTAMP": timestamp,
        "POLY_BUILDER_API_KEY": POLYMARKET_API_KEY.strip(),
        "POLY_BUILDER_PASSPHRASE": POLYMARKET_API_PASSPHRASE.strip(),
        "Content-Type": "application/json",
    }


async def submit_approval_to_relayer(
    eoa_address: str,
    signature: str,
    nonce: str,
    gas_limit: str,
    encoded_data: str,
    relay_address: str,
) -> dict:
    """
    Submit signed proxy approval transaction to Polymarket relayer.

    Ported from buildProxyTransactionRequest + client._post_request in proxy.ts.
    """
    proxy = derive_proxy_wallet(eoa_address)

    body = {
        "type": "PROXY",
        "from": eoa_address,
        "to": PROXY_FACTORY,
        "proxyWallet": proxy,
        "data": encoded_data,
        "nonce": nonce,
        "signature": signature,
        "signatureParams": {
            "gasPrice": "0",
            "gasLimit": gas_limit,
            "relayerFee": "0",
            "relayHub": RELAY_HUB,
            "relay": relay_address,
        },
        "metadata": "Set all token approvals for trading",
    }

    body_str = json.dumps(body, separators=(",", ":"))
    headers = _builder_headers("POST", "/submit", body_str)

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{POLYMARKET_RELAYER_URL}/submit",
            content=body_str,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        transaction_id = data["transactionID"]

        # Poll until mined (max 2 minutes)
        for _ in range(24):
            await asyncio.sleep(5)
            poll = await client.get(
                f"{POLYMARKET_RELAYER_URL}/transaction",
                params={"id": transaction_id},
                headers={
                    "RELAYER_API_KEY": POLYMARKET_API_KEY,
                    "RELAYER_API_KEY_ADDRESS": POLYMARKET_API_SECRET,
                },
                timeout=15.0,
            )
            poll.raise_for_status()
            poll_data = poll.json()
            if isinstance(poll_data, list):
                poll_data = poll_data[0] if poll_data else {}

            state = poll_data.get("state", "")
            logger.info("Approval relay poll: %s", poll_data)

            if state in ("STATE_MINED", "STATE_CONFIRMED"):
                return {
                    "transaction_hash": poll_data.get("transactionHash", ""),
                    "state": state,
                }
            if state in ("STATE_FAILED", "STATE_INVALID"):
                raise ValueError(f"Approval relay failed: state={state}, error={poll_data.get('errorMsg', '')}")

    raise TimeoutError("Approval relay timed out after 2 minutes")
