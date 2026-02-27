from fastapi import APIRouter
from typing import Optional
from app.services.kalshi import get_markets, get_market

router = APIRouter()

@router.get("/")
async def list_markets(
    status: Optional[str] = None,
    series_ticker: Optional[str] = None,
    event_ticker: Optional[str] = None,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
):
    data = await get_markets(status=status, series_ticker=series_ticker, event_ticker=event_ticker, limit=limit, cursor=cursor)
    return data

@router.get("/{ticker}")
async def get_market_by_ticker(ticker: str):
    data = await get_market(ticker)
    return data