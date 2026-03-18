"""
Privy service layer.

Handles:
  - Access token verification (ES256 JWT signed by Privy)
  - Identity token parsing to extract email + EOA wallet address
  - Falls back to Privy REST API if wallet not in identity token

Token flow from iOS:
  1. User logs in via Privy Swift SDK
  2. iOS calls createEthereumWallet() explicitly after login
  3. iOS calls getAccessToken() + getIdentityToken()
  4. Both tokens sent to POST /api/v1/users/register
"""

import json
import logging
from dataclasses import dataclass

import httpx
import jwt
from jwt import PyJWKClient

from app.core.config import PRIVY_APP_ID, PRIVY_APP_SECRET, PRIVY_VERIFICATION_KEY

logger = logging.getLogger(__name__)

# Privy REST API base
PRIVY_API_BASE = "https://auth.privy.io/api/v1"


@dataclass
class PrivyUserInfo:
    privy_did: str    # "did:privy:..." — Privy's internal user ID
    email: str
    eoa_address: str  # 0x... — Ethereum EOA from embedded wallet


def _decode_token(token: str) -> dict:
    """
    Verify and decode a Privy JWT (access or identity token).

    Uses the PEM verification key from the Privy dashboard (ES256).
    Validates issuer, audience, and expiry.

    Raises:
        ValueError: If token is invalid, expired, or signature check fails
    """
    if not PRIVY_VERIFICATION_KEY:
        raise ValueError("PRIVY_VERIFICATION_KEY is not set in environment")

    try:
        payload = jwt.decode(
            token,
            PRIVY_VERIFICATION_KEY,
            algorithms=["ES256"],
            audience=PRIVY_APP_ID,
            issuer="privy.io",
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise ValueError("Privy token has expired")
    except jwt.InvalidAudienceError:
        raise ValueError("Privy token audience mismatch — check PRIVY_APP_ID")
    except jwt.InvalidIssuerError:
        raise ValueError("Privy token issuer mismatch")
    except jwt.PyJWTError as e:
        raise ValueError(f"Invalid Privy token: {e}")


def _parse_linked_accounts(identity_payload: dict) -> tuple[str | None, str | None]:
    """
    Parse email and EOA wallet address from identity token linked_accounts.

    linked_accounts is a stringified JSON array in the JWT payload.
    Returns (email, eoa_address) — either may be None if not present.
    """
    raw = identity_payload.get("linked_accounts", "[]")
    try:
        accounts = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to parse linked_accounts: %r", raw)
        return None, None

    email = None
    eoa_address = None

    for account in accounts:
        if account.get("type") == "email" and not email:
            email = account.get("address")
        if (
            account.get("type") == "wallet"
            and account.get("chain_type") == "ethereum"
            and account.get("wallet_client_type") == "privy"
            and not eoa_address
        ):
            eoa_address = account.get("address")

    return email, eoa_address


async def _fetch_user_from_api(privy_did: str) -> tuple[str | None, str | None]:
    """
    Fetch email + EOA from Privy REST API as fallback.

    Used when wallet address is not yet in the identity token
    (e.g., wallet was just created and identity token hasn't refreshed).

    Returns (email, eoa_address) — either may be None.
    """
    user_id = privy_did.replace("did:privy:", "")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{PRIVY_API_BASE}/users/{privy_did}",
            auth=(PRIVY_APP_ID, PRIVY_APP_SECRET),
            headers={"privy-app-id": PRIVY_APP_ID},
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.warning("Privy REST API returned %s for user %s", resp.status_code, user_id)
            return None, None

        data = resp.json()
        linked = data.get("linked_accounts", [])

    email = next(
        (a.get("address") for a in linked if a.get("type") == "email"),
        None,
    )
    eoa_address = next(
        (
            a.get("address")
            for a in linked
            if a.get("type") == "wallet"
            and a.get("chainType") == "ethereum"
            and a.get("walletClientType") == "privy"
        ),
        None,
    )
    return email, eoa_address


async def verify_privy_tokens(access_token: str, identity_token: str) -> PrivyUserInfo:
    """
    Verify Privy access + identity tokens and extract user info.

    1. Verifies access token signature → gets Privy DID
    2. Verifies identity token signature → parses linked_accounts for email + EOA
    3. If EOA not in identity token, falls back to Privy REST API

    Args:
        access_token:   Short-lived JWT from privy.user.getAccessToken()
        identity_token: Longer-lived JWT from privy.user.getIdentityToken()

    Returns:
        PrivyUserInfo with privy_did, email, eoa_address

    Raises:
        ValueError: If tokens are invalid, expired, or wallet address cannot be found
    """
    # Verify access token → get Privy DID
    access_payload = _decode_token(access_token)
    privy_did = access_payload.get("sub", "")
    if not privy_did:
        raise ValueError("Privy access token missing sub claim")

    # Verify identity token → parse linked_accounts
    identity_payload = _decode_token(identity_token)
    email, eoa_address = _parse_linked_accounts(identity_payload)

    # Fallback: wallet just created, identity token hasn't refreshed yet
    if not eoa_address:
        logger.info("EOA not in identity token for %s — fetching from Privy API", privy_did)
        api_email, api_eoa = await _fetch_user_from_api(privy_did)
        if api_eoa:
            eoa_address = api_eoa
        if not email and api_email:
            email = api_email

    if not eoa_address:
        raise ValueError("No Ethereum embedded wallet found for user — ensure createEthereumWallet() was called before register")

    logger.info("Privy auth verified: did=%s eoa=%s", privy_did, eoa_address)
    return PrivyUserInfo(
        privy_did=privy_did,
        email=email or "",
        eoa_address=eoa_address,
    )
