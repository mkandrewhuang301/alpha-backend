"""
Candlestick endpoints (v1) — hybrid aggregation strategy.

Historical candles from Kalshi REST API, live candle from Redis WebSocket aggregation.

GET /kalshi/events/{event_ticker}/candlesticks — merged OHLCV array for charting
"""

import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel

from app.core.redis import get_redis
from app.services import candlesticks as candle_service

router = APIRouter()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class CandlestickItem(BaseModel):
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: int


class CandlestickResponse(BaseModel):
    event_ticker: str
    period_interval: int
    candlesticks: list[CandlestickItem]
    count: int


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get(
    "/kalshi/events/{event_ticker}/candlesticks",
    response_model=CandlestickResponse,
)
async def get_event_candlesticks(
    event_ticker: str = Path(description="Kalshi event ticker (e.g. KXNBA-26FEB26MIAPHI)"),
    start_ts: int = Query(description="Start Unix timestamp"),
    end_ts: Optional[int] = Query(default=None, description="End Unix timestamp (defaults to now)"),
    series_ticker: str = Query(description="Parent series ticker (e.g. KXNBA)"),
    market_ticker: Optional[str] = Query(default=None, description="Specific market ticker for live candle"),
) -> CandlestickResponse:
    """
    Merged candlestick data: Kalshi historical + Redis live candle.

    Returns 1-minute OHLCV candles with prices normalized to $1.00 base (0.0-1.0).
    The most recent candle may include sub-second live data from the WebSocket feed.
    """
    if end_ts is None:
        end_ts = int(time.time())

    if start_ts >= end_ts:
        raise HTTPException(status_code=400, detail="start_ts must be before end_ts")

    redis = await get_redis()

    # Determine market tickers for live candle lookup
    market_tickers: list[str] = []
    if market_ticker:
        market_tickers = [market_ticker]

    candles = await candle_service.get_merged_candlesticks(
        redis_conn=redis,
        event_ticker=event_ticker,
        series_ticker=series_ticker,
        market_tickers=market_tickers,
        start_ts=start_ts,
        end_ts=end_ts,
    )

    return CandlestickResponse(
        event_ticker=event_ticker,
        period_interval=1,
        candlesticks=[CandlestickItem(**c) for c in candles],
        count=len(candles),
    )
