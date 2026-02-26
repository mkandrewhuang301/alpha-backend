from fastapi import APIRouter
from app.services.kalshi import get_markets, get_market

router = APIRouter()

@router.get("/")
async def list_markets(status: str = None, series_ticker: str = None, event_ticker: str = None, limit: int = None):
    data = await get_markets(status=status, series_ticker=series_ticker, event_ticker=event_ticker, limit=limit)
    return data

@router.get("/{ticker}")
async def get_market_by_ticker(ticker: str):
    data = await get_market(ticker)
    return data