"""
User authentication and registration routes.

POST /api/v1/users/register
    - Verifies Magic DID token from iOS
    - Creates User row (eoa_address, email)
    - Derives Polymarket Safe address
    - Creates Account row (exchange=polymarket, wallet_address)
    - Returns user info + wallet address

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
from app.services.magic import verify_did_token
from app.services.polymarket import (
    derive_proxy_wallet,
    derive_safe,
    get_safe_deploy_payload,
    deploy_safe,
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
    did_token: str


class RegisterResponse(BaseModel):
    user_id: UUID
    eoa_address: str
    wallet_address: str  # Polymarket Safe address (derived from EOA)
    email: str
    is_new_user: bool


class SafePayloadResponse(BaseModel):
    safe_address: str    # EIP-712 verifyingContract
    nonce: str
    chain_id: int        # 137 (Polygon)
    to: str              # Safe factory — iOS puts this in SafeTx.to
    value: str
    data: str
    operation: str
    safe_txn_gas: str
    base_gas: str
    gas_price: str
    gas_token: str
    refund_receiver: str


class SafeDeployRequest(BaseModel):
    eoa_address: str
    signature: str       # EIP-712 signature from iOS Magic signing


class SafeDeployResponse(BaseModel):
    safe_address: str
    transaction_hash: str


class ClobSigningMessageResponse(BaseModel):
    address: str      # EOA address (checksum)
    timestamp: str    # Unix seconds — iOS must use this exact value when signing
    nonce: int        # Always 0 for first-time credential derivation
    message: str      # Static attestation string
    chain_id: int     # 137 (Polygon)


class ClobCredentialsRequest(BaseModel):
    eoa_address: str
    signature: str    # EIP-712 ClobAuth signature from iOS Magic signing
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
    Register or log in a user via Magic DID token.

    Called by iOS immediately after Magic login. Idempotent —
    safe to call on every login. Returns same response for new
    and returning users.
    """
    # 1. Verify DID token with Magic API → extract eoa_address + email
    try:
        user_info = verify_did_token(body.did_token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    eoa_address = user_info.eoa_address
    email = user_info.email

    # 2. Derive Polymarket Safe address from EOA (pure math, no network call)
    wallet_address = derive_safe(eoa_address)

    # 3. Check if user already exists
    result = await db.execute(
        select(User).where(User.eoa_address == eoa_address)
    )
    existing_user = result.scalar_one_or_none()

    if existing_user:
        # Returning user — just return their info
        logger.info("Returning user login: eoa=%s", eoa_address)
        return RegisterResponse(
            user_id=existing_user.id,
            eoa_address=eoa_address,
            wallet_address=wallet_address,
            email=existing_user.email,
            is_new_user=False,
        )

    # 4. New user — create User row
    new_user = User(
        email=email,
        eoa_address=eoa_address,
    )
    db.add(new_user)
    await db.flush()  # get the generated user.id before creating Account

    # 5. Create Polymarket Account row
    account = Account(
        user_id=new_user.id,
        exchange="polymarket",
        ext_account_id=wallet_address,  # wallet address is the Polymarket account ID
        safe_address=wallet_address,
    )
    db.add(account)
    await db.commit()

    logger.info("New user registered: eoa=%s wallet=%s", eoa_address, wallet_address)

    return RegisterResponse(
        user_id=new_user.id,
        eoa_address=eoa_address,
        wallet_address=wallet_address,
        email=email,
        is_new_user=True,
    )


@router.get("/users/safe/payload", response_model=SafePayloadResponse)
async def get_safe_payload(eoa_address: str):
    """
    Fetch the Safe deployment payload from Polymarket's relayer.

    iOS calls this endpoint, receives the payload, signs it with Magic,
    then immediately calls POST /users/safe/deploy with the signature.

    Args:
        eoa_address: User's EOA address from Magic (0x...)
    """
    try:
        payload = await get_safe_deploy_payload(eoa_address)
    except Exception as e:
        logger.error("Failed to fetch relay payload for eoa=%s: %s", eoa_address, e)
        raise HTTPException(status_code=502, detail=f"Relayer error: {e}")

    return SafePayloadResponse(
        safe_address=payload.safe_address,
        nonce=payload.nonce,
        chain_id=payload.chain_id,
        to=payload.to,
        value=payload.value,
        data=payload.data,
        operation=payload.operation,
        safe_txn_gas=payload.safe_txn_gas,
        base_gas=payload.base_gas,
        gas_price=payload.gas_price,
        gas_token=payload.gas_token,
        refund_receiver=payload.refund_receiver,
    )


@router.post("/users/safe/deploy", response_model=SafeDeployResponse)
async def deploy_safe_endpoint(
    body: SafeDeployRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Submit a signed Safe deployment to Polymarket's relayer.

    iOS signs the relay payload with Magic, then calls this endpoint.
    Backend submits to relayer, polls until mined, stores safe_address.

    Flow:
        1. Submit signed tx to relayer
        2. Poll until STATE_MINED
        3. Update Account row with confirmed safe_address
        4. Return { safe_address, transaction_hash }
    """
    try:
        result = await deploy_safe(
            eoa_address=body.eoa_address,
            signature=body.signature,
        )
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Safe deployment timed out")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Safe deployment failed for eoa=%s: %s", body.eoa_address, e)
        raise HTTPException(status_code=502, detail=f"Relayer error: {e}")

    # Update Account row with deployed safe_address
    account_result = await db.execute(
        select(Account).join(User).where(
            User.eoa_address == body.eoa_address,
            Account.exchange == "polymarket",
        )
    )
    account = account_result.scalar_one_or_none()
    if account:
        account.safe_address = result.safe_address
        account.ext_account_id = result.safe_address
        await db.commit()

    logger.info("Safe deployed: eoa=%s safe=%s tx=%s",
                body.eoa_address, result.safe_address, result.transaction_hash)

    return SafeDeployResponse(
        safe_address=result.safe_address,
        transaction_hash=result.transaction_hash,
    )


@router.get("/users/clob/signing-message", response_model=ClobSigningMessageResponse)
async def get_clob_signing_message_endpoint(eoa_address: str):
    """
    Return the EIP-712 ClobAuth payload iOS needs to sign to derive CLOB API credentials.

    iOS must sign this EXACT payload (same timestamp, nonce, message) using
    eth_signTypedData_v4, then call POST /users/clob/credentials with the signature.

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

    iOS signs the ClobAuth EIP-712 payload from GET /users/clob/signing-message,
    then calls this endpoint. Backend derives credentials from Polymarket's CLOB API,
    encrypts them with Fernet, and stores them in the Account row.

    Flow:
        1. Send POLY_ADDRESS/SIGNATURE/TIMESTAMP/NONCE headers to CLOB API
        2. Receive { apiKey, secret, passphrase }
        3. Encrypt each value and store in Account.platform_metadata
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

    # Encrypt and store in dedicated Account columns
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
