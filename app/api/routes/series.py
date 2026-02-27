from fastapi import APIRouter
from typing import Optional
from app.services.kalshi import get_series_list, get_series_by_ticker

router = APIRouter()


@router.get("/")
async def list_series(
    category: Optional[str] = None,
    tags: Optional[str] = None,
    include_product_metadata: bool = False,
    include_volume: bool = False,
):
    data = await get_series_list(
        category=category,
        tags=tags,
        include_product_metadata=include_product_metadata,
        include_volume=include_volume,
    )
    return data


@router.get("/{series_ticker}")
async def get_series_by_ticker(
    series_ticker: str,
    include_volume: bool = False,
):
    data = await get_series_by_ticker(
        series_ticker=series_ticker,
        include_volume=include_volume,
    )
    return data
