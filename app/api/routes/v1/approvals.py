"""
Polymarket approval endpoints — gasless via proxy wallet relayer.

GET  /api/v1/polymarket/approvals/status          — check on-chain approval state
GET  /api/v1/polymarket/approvals/signing-message — build struct hash for iOS to sign
POST /api/v1/polymarket/approvals/execute         — submit signed tx to relayer (gasless)

Flow:
  1. iOS calls GET /approvals/status — see what's missing
  2. If not all_approved: GET /approvals/signing-message — get struct_hash + fields
  3. iOS calls Privy personal_sign(struct_hash) — returns signature
  4. iOS calls POST /approvals/execute with signature + fields from step 2
  5. Backend posts to Polymarket relayer — gasless, no MATIC needed
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import get_authenticated_eoa
from app.services.approvals import (
    get_approval_status,
    get_approval_signing_message,
    submit_approval_to_relayer,
)
from app.services.polymarket import derive_proxy_wallet

logger = logging.getLogger(__name__)

router = APIRouter()


class ApprovalStatusResponse(BaseModel):
    proxy_wallet: str
    usdc_for_ctf_exchange: bool
    usdc_for_neg_risk_exchange: bool
    usdc_for_neg_risk_adapter: bool
    ctf_for_ctf_exchange: bool
    ctf_for_neg_risk_exchange: bool
    ctf_for_neg_risk_adapter: bool
    all_approved: bool


class ApprovalSigningMessageResponse(BaseModel):
    struct_hash: str      # iOS signs this with Privy personal_sign
    eoa_address: str
    proxy_wallet: str
    nonce: str
    gas_limit: str
    encoded_data: str     # Return unchanged in execute request
    relay_address: str


class ApprovalExecuteRequest(BaseModel):
    eoa_address: str
    signature: str        # From Privy personal_sign(struct_hash)
    nonce: str            # From signing-message response
    gas_limit: str        # From signing-message response
    encoded_data: str     # From signing-message response
    relay_address: str    # From signing-message response


class ApprovalExecuteResponse(BaseModel):
    transaction_hash: str
    state: str


@router.get("/polymarket/approvals/status", response_model=ApprovalStatusResponse)
async def approval_status(eoa_address: str = Depends(get_authenticated_eoa)):
    """Check which Polymarket approvals are set on-chain for this user's proxy wallet."""
    try:
        status = await get_approval_status(eoa_address)
        proxy = derive_proxy_wallet(eoa_address)
        return ApprovalStatusResponse(
            proxy_wallet=proxy,
            usdc_for_ctf_exchange=status.usdc_for_ctf_exchange,
            usdc_for_neg_risk_exchange=status.usdc_for_neg_risk_exchange,
            usdc_for_neg_risk_adapter=status.usdc_for_neg_risk_adapter,
            ctf_for_ctf_exchange=status.ctf_for_ctf_exchange,
            ctf_for_neg_risk_exchange=status.ctf_for_neg_risk_exchange,
            ctf_for_neg_risk_adapter=status.ctf_for_neg_risk_adapter,
            all_approved=status.all_approved,
        )
    except Exception as e:
        logger.error("Failed to check approval status for %s: %s", eoa_address, e, exc_info=True)
        raise HTTPException(status_code=502, detail="Failed to read approval status from Polygon")


@router.get("/polymarket/approvals/signing-message", response_model=ApprovalSigningMessageResponse)
async def approval_signing_message(eoa_address: str = Depends(get_authenticated_eoa)):
    """
    Build the struct hash for iOS to sign via Privy personal_sign.

    iOS signs struct_hash with personal_sign (not signTypedData).
    Returns all fields needed for POST /approvals/execute.
    """
    try:
        msg = await get_approval_signing_message(eoa_address)
        return ApprovalSigningMessageResponse(
            struct_hash=msg.struct_hash,
            eoa_address=msg.eoa_address,
            proxy_wallet=msg.proxy_wallet,
            nonce=msg.nonce,
            gas_limit=msg.gas_limit,
            encoded_data=msg.encoded_data,
            relay_address=msg.relay_address,
        )
    except Exception as e:
        logger.error("Failed to build approval signing message for %s: %s", eoa_address, e, exc_info=True)
        raise HTTPException(status_code=502, detail="Failed to build approval signing message")


@router.post("/polymarket/approvals/execute", response_model=ApprovalExecuteResponse)
async def approval_execute(
    body: ApprovalExecuteRequest,
    authenticated_eoa: str = Depends(get_authenticated_eoa),
):
    """
    Submit signed approval transaction to Polymarket relayer — gasless, no MATIC needed.

    iOS signs the struct_hash from GET /approvals/signing-message using Privy personal_sign,
    then calls this endpoint with the signature and all other fields unchanged.
    """
    if body.eoa_address.lower() != authenticated_eoa.lower():
        raise HTTPException(status_code=403, detail="eoa_address does not match authenticated user")
    try:
        result = await submit_approval_to_relayer(
            eoa_address=body.eoa_address,
            signature=body.signature,
            nonce=body.nonce,
            gas_limit=body.gas_limit,
            encoded_data=body.encoded_data,
            relay_address=body.relay_address,
        )
        return ApprovalExecuteResponse(
            transaction_hash=result["transaction_hash"],
            state=result["state"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    except Exception as e:
        logger.error("Failed to execute approvals for %s: %s", body.eoa_address, e, exc_info=True)
        raise HTTPException(status_code=502, detail="Failed to submit approvals to relayer")
