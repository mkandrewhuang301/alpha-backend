"""
User positions, orders, and trades routes.

GET /api/v1/users/positions  — open positions on Polymarket
GET /api/v1/users/orders     — open (live) orders
GET /api/v1/users/trades     — filled trade history

All endpoints authenticate with the user's stored CLOB credentials (derived
via EIP-712 ClobAuth signing). Credentials are decrypted server-side and
used to sign HMAC requests to the Polymarket CLOB API.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.services.positions import get_positions, get_open_orders, get_trades

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/users/positions")
async def user_positions(eoa_address: str):
    """
    Fetch the user's open positions on Polymarket.

    Uses the public Polymarket Data API — no CLOB credentials required.
    Returns position objects (conditionId, size, currentValue, etc.)
    """
    try:
        return await get_positions(eoa_address)
    except Exception as e:
        logger.error("Failed to fetch positions for %s: %s", eoa_address, e)
        raise HTTPException(status_code=502, detail="Failed to fetch positions from Polymarket")


@router.get("/users/orders")
async def user_orders(
    eoa_address: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch the user's open orders on Polymarket.

    Returns orders with status 'live' — orders that haven't been filled or cancelled.
    """
    try:
        return await get_open_orders(eoa_address, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Failed to fetch orders for %s: %s", eoa_address, e)
        raise HTTPException(status_code=502, detail="Failed to fetch orders from Polymarket")


@router.get("/users/trades")
async def user_trades(
    eoa_address: str,
    market: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch the user's trade history on Polymarket.

    Optional query param:
        market — conditionId to filter trades for a specific market
    """
    try:
        return await get_trades(eoa_address, db, market=market)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Failed to fetch trades for %s: %s", eoa_address, e)
        raise HTTPException(status_code=502, detail="Failed to fetch trades from Polymarket")
