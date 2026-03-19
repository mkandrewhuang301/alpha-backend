"""
Polymarket positions, orders, and trades service.

Fetches user-specific data from the Polymarket CLOB API using L2 HMAC auth.
All functions decrypt the user's stored credentials and sign the request before calling CLOB.
"""

import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import CLOB_HOST
from app.services.polymarket import build_user_clob_headers, get_user_clob_credentials

logger = logging.getLogger(__name__)


POLYMARKET_DATA_API = "https://data-api.polymarket.com"


async def get_positions(eoa_address: str) -> list[dict]:
    """
    Fetch open positions for a user from Polymarket Data API.

    This is a public endpoint — no HMAC auth required. Positions are tied
    to the user's EOA address on Polygon.

    Returns a list of position objects (conditionId, size, currentValue, etc.)
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{POLYMARKET_DATA_API}/positions",
            params={"user": eoa_address},
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json()


async def get_open_orders(eoa_address: str, db: AsyncSession) -> list[dict]:
    """
    Fetch open (live) orders for a user from Polymarket CLOB.

    Returns orders that haven't been fully filled or cancelled.
    """
    creds = await get_user_clob_credentials(eoa_address, db)
    headers = build_user_clob_headers(
        method="GET",
        path="/data/orders",
        api_key=creds.api_key,
        secret=creds.api_secret,
        passphrase=creds.passphrase,
        eoa_address=eoa_address,
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{CLOB_HOST}/data/orders", headers=headers, timeout=15.0)
        resp.raise_for_status()
        return resp.json()


async def get_trades(eoa_address: str, db: AsyncSession, market: str | None = None) -> list[dict]:
    """
    Fetch trade history for a user from Polymarket CLOB.

    Args:
        eoa_address: User's EOA wallet address
        db:          DB session
        market:      Optional conditionId to filter trades for a specific market
    """
    creds = await get_user_clob_credentials(eoa_address, db)
    headers = build_user_clob_headers(
        method="GET",
        path="/data/trades",
        api_key=creds.api_key,
        secret=creds.api_secret,
        passphrase=creds.passphrase,
        eoa_address=eoa_address,
    )
    params = {}
    if market:
        params["market"] = market

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{CLOB_HOST}/data/trades", headers=headers, params=params, timeout=15.0)
        resp.raise_for_status()
        return resp.json()
