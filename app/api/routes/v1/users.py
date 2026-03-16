"""
User authentication and registration routes.

POST /api/v1/users/register
    - Verifies Magic DID token from iOS
    - Creates User row (eoa_address, email)
    - Derives Polymarket proxy wallet address
    - Creates Account row (exchange=polymarket, wallet_address)
    - Returns user info + wallet address
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
from app.services.polymarket import derive_proxy_wallet, derive_safe, get_safe_deploy_payload, deploy_safe

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
    wallet_address: str  # Polymarket proxy wallet (derived from EOA)
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

    # 2. Derive Polymarket proxy wallet from EOA (pure math, no network call)
    wallet_address = derive_proxy_wallet(eoa_address)

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
