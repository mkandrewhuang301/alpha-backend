"""
Order preparation and submission routes.

POST /api/v1/polymarket/order/prepare
    - Fetches market params (tick_size, neg_risk, fee_rate)
    - Calculates makerAmount / takerAmount with correct rounding
    - Returns full EIP-712 typed data payload for Privy to sign on iOS

POST /api/v1/polymarket/order/submit
    - Receives iOS signature + order fields
    - Reconstructs order struct, adds HMAC auth headers
    - Submits to Polymarket CLOB POST /order
    - Returns order_id + status

DELETE /api/v1/polymarket/order/{order_id}
    - Cancels an open order by order ID
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_authenticated_eoa
from app.core.database import get_db
from app.services.order import prepare_order, submit_order, PreparedOrder, OrderResult

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class PrepareOrderRequest(BaseModel):
    eoa_address: str
    token_id: str    # Polymarket ERC-1155 clobTokenId (large uint256 as string)
    price: float     # 0.0 to 1.0
    size: float      # number of shares
    side: str        # "BUY" or "SELL"


class PrepareOrderResponse(BaseModel):
    eip712: dict          # Full EIP-712 typed data — pass directly to Privy signTypedData
    salt: int             # Must be returned in submit request unchanged
    maker_amount: str     # 6-decimal USDC string
    taker_amount: str     # 6-decimal shares string
    fee_rate_bps: str     # basis points
    neg_risk: bool        # Which exchange contract was used
    tick_size: str        # Market tick precision


class SubmitOrderRequest(BaseModel):
    eoa_address: str
    token_id: str
    signature: str        # 0x-prefixed EIP-712 signature from Privy
    salt: int             # Must match value from prepare response
    maker_amount: str
    taker_amount: str
    fee_rate_bps: str
    side: str             # "BUY" or "SELL"
    neg_risk: bool
    expiration: str = "0"
    nonce: str = "0"
    order_type: str = "GTC"  # GTC | GTD | FOK | FAK


class SubmitOrderResponse(BaseModel):
    order_id: str
    status: str
    success: bool


class CancelOrderRequest(BaseModel):
    eoa_address: str
    order_id: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/polymarket/order/prepare", response_model=PrepareOrderResponse)
async def prepare_order_endpoint(
    body: PrepareOrderRequest,
    authenticated_eoa: str = Depends(get_authenticated_eoa),
):
    """
    Prepare an unsigned order for a Polymarket market.

    Requires: Authorization: Bearer <privy_access_token>
    """
    if body.eoa_address.lower() != authenticated_eoa.lower():
        raise HTTPException(status_code=403, detail="eoa_address does not match authenticated user")
    try:
        result = await prepare_order(
            eoa_address=body.eoa_address,
            token_id=body.token_id,
            price=body.price,
            size=body.size,
            side=body.side,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Order prepare failed for eoa=%s token=%s: %s", body.eoa_address, body.token_id, e)
        raise HTTPException(status_code=502, detail="Failed to fetch market params from Polymarket")

    return PrepareOrderResponse(
        eip712=result.eip712,
        salt=result.salt,
        maker_amount=result.maker_amount,
        taker_amount=result.taker_amount,
        fee_rate_bps=result.fee_rate_bps,
        neg_risk=result.neg_risk,
        tick_size=result.tick_size,
    )


@router.post("/polymarket/order/submit", response_model=SubmitOrderResponse)
async def submit_order_endpoint(
    body: SubmitOrderRequest,
    authenticated_eoa: str = Depends(get_authenticated_eoa),
    db: AsyncSession = Depends(get_db),
):
    """
    Submit a signed order to Polymarket CLOB.

    Requires: Authorization: Bearer <privy_access_token>
    """
    if body.eoa_address.lower() != authenticated_eoa.lower():
        raise HTTPException(status_code=403, detail="eoa_address does not match authenticated user")
    try:
        result = await submit_order(
            eoa_address=body.eoa_address,
            token_id=body.token_id,
            signature=body.signature,
            salt=body.salt,
            maker_amount=body.maker_amount,
            taker_amount=body.taker_amount,
            fee_rate_bps=body.fee_rate_bps,
            side=body.side,
            neg_risk=body.neg_risk,
            db=db,
            expiration=body.expiration,
            nonce=body.nonce,
            order_type=body.order_type,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Order submit failed for eoa=%s: %s", body.eoa_address, e)
        raise HTTPException(status_code=502, detail="Failed to submit order to Polymarket")

    return SubmitOrderResponse(
        order_id=result.order_id,
        status=result.status,
        success=result.success,
    )


@router.delete("/polymarket/order/{order_id}")
async def cancel_order_endpoint(
    order_id: str,
    eoa_address: str = Depends(get_authenticated_eoa),
    db: AsyncSession = Depends(get_db),
):
    """
    Cancel an open order by order ID.

    Requires the user's CLOB credentials for HMAC auth.
    Returns 200 on success.
    """
    import json
    from app.services.order import _get_credentials, _get_http
    from app.services.polymarket import build_user_clob_headers

    try:
        creds = await _get_credentials(eoa_address, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    body_str = json.dumps({"orderID": order_id}, separators=(",", ":"))
    headers = build_user_clob_headers(
        method="DELETE",
        path="/order",
        api_key=creds.api_key,
        secret=creds.api_secret,
        passphrase=creds.passphrase,
        eoa_address=eoa_address,
        body=body_str,
    )

    try:
        resp = await _get_http().request("DELETE", "/order", content=body_str, headers=headers)
        if resp.status_code not in (200, 204):
            raise HTTPException(status_code=502, detail=f"Polymarket cancel failed: {resp.text}")
        return {"cancelled": order_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Order cancel failed for order_id=%s eoa=%s: %s", order_id, eoa_address, e)
        raise HTTPException(status_code=502, detail="Failed to cancel order")
