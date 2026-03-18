"""
User authentication and registration routes.

POST /api/v1/users/register
    - Verifies Privy access + identity tokens from iOS
    - Creates User row (eoa_address, email, privy_did)
    - Creates Account row (exchange=polymarket, wallet_address=eoa_address)
    - Returns user info + wallet address
    - Idempotent — safe to call on every login

GET  /api/v1/users/clob/signing-message
    - Returns the EIP-712 ClobAuth payload iOS needs to sign

POST /api/v1/users/clob/credentials
    - Accepts signature from iOS, derives CLOB API credentials, stores encrypted in DB

GET  /api/v1/users/clob/credentials
    - Returns stored CLOB API key (not secret/passphrase) to confirm credentials exist
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.db import User, Account
from app.services.privy import verify_privy_tokens
from app.services.polymarket import (
    get_clob_signing_message,
    derive_clob_credentials,
    encrypt_credential,
    decrypt_credential,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    access_token: str    # Short-lived JWT from privy.user.getAccessToken()
    identity_token: str  # Longer-lived JWT from privy.user.getIdentityToken()


class RegisterResponse(BaseModel):
    user_id: UUID
    eoa_address: str     # Privy embedded EOA wallet address
    wallet_address: str  # Same as eoa_address — used directly for Polymarket
    email: str
    is_new_user: bool


class ClobSigningMessageResponse(BaseModel):
    address: str      # EOA address (checksum)
    timestamp: str    # Unix seconds — iOS must use this exact value when signing
    nonce: int        # Always 0 for first-time credential derivation
    message: str      # Static attestation string
    chain_id: int     # 137 (Polygon)


class ClobCredentialsRequest(BaseModel):
    eoa_address: str
    signature: str    # EIP-712 ClobAuth signature from iOS signing
    timestamp: str    # Must match the timestamp returned by GET /users/clob/signing-message
    nonce: int = 0


class ClobCredentialsResponse(BaseModel):
    api_key: str
    has_credentials: bool


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/users/register", response_model=RegisterResponse)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Register or log in a user via Privy tokens.

    Called by iOS after Privy login + createEthereumWallet(). Idempotent —
    safe to call on every login. Returns same response for new and returning users.

    iOS must call createEthereumWallet() before this endpoint — the EOA address
    must be present in the identity token's linked_accounts.
    """
    # 1. Verify Privy tokens → extract privy_did, email, eoa_address
    try:
        user_info = await verify_privy_tokens(body.access_token, body.identity_token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    eoa_address = user_info.eoa_address
    email = user_info.email
    privy_did = user_info.privy_did

    # 2. Check if user already exists
    result = await db.execute(
        select(User).where(User.eoa_address == eoa_address)
    )
    existing_user = result.scalar_one_or_none()

    if existing_user:
        logger.info("Returning user login: eoa=%s", eoa_address)
        return RegisterResponse(
            user_id=existing_user.id,
            eoa_address=eoa_address,
            wallet_address=eoa_address,
            email=existing_user.email,
            is_new_user=False,
        )

    # 3. New user — create User row
    new_user = User(
        email=email,
        eoa_address=eoa_address,
    )
    db.add(new_user)
    await db.flush()

    # 4. Create Polymarket Account row — EOA is the trading address directly
    account = Account(
        user_id=new_user.id,
        exchange="polymarket",
        ext_account_id=eoa_address,
    )
    db.add(account)
    await db.commit()

    logger.info("New user registered: eoa=%s privy_did=%s", eoa_address, privy_did)

    return RegisterResponse(
        user_id=new_user.id,
        eoa_address=eoa_address,
        wallet_address=eoa_address,
        email=email,
        is_new_user=True,
    )


@router.get("/users/clob/signing-message", response_model=ClobSigningMessageResponse)
async def get_clob_signing_message_endpoint(eoa_address: str):
    """
    Return the EIP-712 ClobAuth payload iOS needs to sign to derive CLOB API credentials.

    iOS must sign this EXACT payload (same timestamp, nonce, message) using
    eth_signTypedData_v4 via Privy's embedded wallet, then call POST /users/clob/credentials.

    EIP-712 domain: { name: "ClobAuthDomain", version: "1", chainId: 137 }
    EIP-712 type:   ClobAuth { address: address, timestamp: string, nonce: uint256, message: string }
    """
    msg = get_clob_signing_message(eoa_address)
    return ClobSigningMessageResponse(
        address=msg.address,
        timestamp=msg.timestamp,
        nonce=msg.nonce,
        message=msg.message,
        chain_id=137,
    )


@router.post("/users/clob/credentials", response_model=ClobCredentialsResponse)
async def store_clob_credentials(
    body: ClobCredentialsRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Derive Polymarket CLOB API credentials from iOS signature and store them encrypted.

    iOS signs the ClobAuth EIP-712 payload from GET /users/clob/signing-message
    using Privy's embedded wallet, then calls this endpoint. Backend derives
    credentials from Polymarket's CLOB API, encrypts with Fernet, stores in Account row.

    Flow:
        1. Send POLY_ADDRESS/SIGNATURE/TIMESTAMP/NONCE headers to CLOB API
        2. Receive { apiKey, secret, passphrase }
        3. Encrypt each value and store in Account encrypted columns
        4. Return { api_key, has_credentials: true }
    """
    try:
        creds = await derive_clob_credentials(
            eoa_address=body.eoa_address,
            signature=body.signature,
            timestamp=body.timestamp,
            nonce=body.nonce,
        )
    except Exception as e:
        logger.error("CLOB credential derivation failed for eoa=%s: %s", body.eoa_address, e)
        raise HTTPException(status_code=502, detail=f"CLOB API error: {e}")

    account_result = await db.execute(
        select(Account).join(User).where(
            User.eoa_address == body.eoa_address,
            Account.exchange == "polymarket",
        )
    )
    account = account_result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Polymarket account not found — register first")

    account.encrypted_api_key = encrypt_credential(creds.api_key)
    account.encrypted_api_secret = encrypt_credential(creds.api_secret)
    account.encrypted_passphrase = encrypt_credential(creds.passphrase)
    await db.commit()

    logger.info("CLOB credentials stored for eoa=%s api_key=%s", body.eoa_address, creds.api_key)

    return ClobCredentialsResponse(api_key=creds.api_key, has_credentials=True)


@router.get("/users/clob/credentials", response_model=ClobCredentialsResponse)
async def get_clob_credentials(
    eoa_address: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Check whether CLOB credentials exist for a user and return the (unencrypted) api_key.

    Does NOT return secret or passphrase — those stay server-side.
    Returns 404 if no credentials have been stored yet.
    """
    account_result = await db.execute(
        select(Account).join(User).where(
            User.eoa_address == eoa_address,
            Account.exchange == "polymarket",
        )
    )
    account = account_result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Polymarket account not found")

    encrypted_key = account.encrypted_api_key
    if not encrypted_key:
        raise HTTPException(status_code=404, detail="No CLOB credentials stored — call POST /users/clob/credentials first")

    try:
        api_key = decrypt_credential(encrypted_key)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to decrypt stored credentials")

    return ClobCredentialsResponse(api_key=api_key, has_credentials=True)
