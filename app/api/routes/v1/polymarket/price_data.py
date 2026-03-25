"""
Polymarket price data endpoint (v1) — sparkline chart data.

GET /polymarket/markets/{asset_id}/chart — unified {t, p} array for iOS sparkline charts.
Stitches historical CLOB API data with the live WS buffer from Redis.
"""

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel

from app.core.redis import get_redis
from app.services.polymarket.price_data import (
    get_stitched_chart,
    PriceDataUnavailable,
    VALID_INTERVALS,
)

router = APIRouter()


class PricePoint(BaseModel):
    t: int
    p: float


class ChartResponse(BaseModel):
    asset_id: str
    interval: str
    prices: list[PricePoint]
    count: int


@router.get(
    "/polymarket/markets/{asset_id}/chart",
    response_model=ChartResponse,
)
async def get_polymarket_chart(
    asset_id: str = Path(description="Polymarket execution_asset_id (ERC-1155 token ID)"),
    interval: str = Query(default="1d", description="Time interval: live (last hour), 1d, 1w, 1m, max"),
) -> ChartResponse:
    """
    Sparkline chart data for a Polymarket market outcome.

    Returns a continuous array of {t: timestamp, p: price} coordinates,
    stitching historical CLOB API data with the real-time WebSocket live buffer.
    Prices are normalized to $1.00 base (0.0–1.0).
    """
    if interval not in VALID_INTERVALS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid interval '{interval}'. Must be one of: {', '.join(sorted(VALID_INTERVALS))}",
        )

    redis = await get_redis()

    try:
        prices = await get_stitched_chart(redis, asset_id, interval)
    except PriceDataUnavailable as e:
        raise HTTPException(status_code=502, detail=str(e))

    return ChartResponse(
        asset_id=asset_id,
        interval=interval,
        prices=[PricePoint(t=p["t"], p=p["p"]) for p in prices],
        count=len(prices),
    )
