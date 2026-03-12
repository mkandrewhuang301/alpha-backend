"""
Versioned event endpoints (v1) — merge PostgreSQL metadata with live Redis prices.

Routes are mounted at /api/v1/{exchange}/... in main.py.

GET /{exchange}/categories          — distinct active event categories
GET /{exchange}/events              — event feed with category/sort/pagination + live prices
GET /{exchange}/events/{event_id}   — full event detail with all markets/outcomes + live ticker data
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.market_cache import MarketCacheManager
from app.core.redis import get_redis
from app.services import events as event_service

router = APIRouter()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class TickerData(BaseModel):
    price: Optional[str] = None
    bid: Optional[str] = None
    bid_size: Optional[str] = None
    ask: Optional[str] = None
    ask_size: Optional[str] = None


class OutcomePreview(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    execution_asset_id: str
    title: str
    side: str
    price: Optional[str] = None


class EventFeedItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ext_id: str
    title: str
    category: Optional[str] = None
    status: Optional[str] = None
    close_time: Optional[datetime] = None
    volume_24h: Optional[Decimal] = None
    image_url: Optional[str] = None
    yes_price: Optional[str] = None
    no_price: Optional[str] = None


class EventFeedResponse(BaseModel):
    events: list[EventFeedItem]
    count: int
    offset: int
    limit: int


class OutcomeDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    execution_asset_id: str
    title: str
    side: str
    is_winner: Optional[bool] = None
    ticker_data: Optional[TickerData] = None


class MarketDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ext_id: str
    title: str
    subtitle: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None
    open_time: Optional[datetime] = None
    close_time: Optional[datetime] = None
    outcomes: list[OutcomeDetail] = []


class EventDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ext_id: str
    title: str
    description: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None
    close_time: Optional[datetime] = None
    expected_expiration_time: Optional[datetime] = None
    volume_24h: Optional[Decimal] = None
    image_url: Optional[str] = None
    markets: list[MarketDetail] = []


class TrendingEventItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ext_id: str
    title: str
    category: Optional[str] = None
    status: Optional[str] = None
    close_time: Optional[datetime] = None
    volume_24h: Optional[float] = None
    image_url: Optional[str] = None
    event_image_url: Optional[str] = None
    featured_image_url: Optional[str] = None
    competition: Optional[str] = None


class TrendingEventsResponse(BaseModel):
    events: list[TrendingEventItem]
    count: int


class CategoryListResponse(BaseModel):
    categories: list[str]
    count: int


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/{exchange}/events/trending", response_model=TrendingEventsResponse)
async def get_trending_events(
    exchange: str = Path(description="Exchange name: 'kalshi' or 'polymarket'"),
    limit: int = Query(default=20, ge=1, le=100, description="Number of trending events"),
    db: AsyncSession = Depends(get_db),
) -> TrendingEventsResponse:
    """Trending events ranked by rolling 24h volume from the exchange-specific Redis ZSET leaderboard."""
    redis = await get_redis()
    items = await event_service.get_trending_events(redis, db, exchange=exchange, limit=limit)
    return TrendingEventsResponse(events=items, count=len(items))


@router.get("/{exchange}/categories", response_model=CategoryListResponse)
async def list_categories(
    exchange: str = Path(description="Exchange name: 'kalshi' or 'polymarket'"),
    db: AsyncSession = Depends(get_db),
) -> CategoryListResponse:
    """Return distinct categories for active events on an exchange."""
    categories = await event_service.list_categories(exchange, db)
    return CategoryListResponse(categories=categories, count=len(categories))


@router.get("/{exchange}/events", response_model=EventFeedResponse)
async def list_events(
    exchange: str = Path(description="Exchange name"),
    category: Optional[str] = Query(default=None, description="Filter by category"),
    series_ticker: Optional[str] = Query(default=None, description="Filter by series ticker (e.g. KXHIGHNY)"),
    sort: str = Query(default="volume", description="Sort: 'volume', 'closing_soon', 'newest'"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> EventFeedResponse:
    """Event feed with category/series filter, sorting, and pagination. Merges lead market yes/no prices from Redis."""
    redis = await get_redis()
    cache = MarketCacheManager(redis)

    items, count = await event_service.list_events_feed(
        exchange=exchange,
        db=db,
        cache=cache,
        category=category,
        series_ticker=series_ticker,
        sort=sort,
        limit=limit,
        offset=offset,
    )

    return EventFeedResponse(
        events=[EventFeedItem(**item) for item in items],
        count=count,
        offset=offset,
        limit=limit,
    )


@router.get("/{exchange}/events/{event_ext_id}", response_model=EventDetailResponse)
async def get_event_detail(
    exchange: str = Path(description="Exchange name"),
    event_ext_id: str = Path(description="Event ticker / ext_id"),
    db: AsyncSession = Depends(get_db),
) -> EventDetailResponse:
    """Full event detail with all markets, outcomes, and live ticker data from Redis."""
    redis = await get_redis()
    cache = MarketCacheManager(redis)

    data = await event_service.get_event_detail(
        exchange=exchange,
        event_ext_id=event_ext_id,
        db=db,
        cache=cache,
    )

    if data is None:
        raise HTTPException(status_code=404, detail=f"Event '{event_ext_id}' not found on {exchange}")

    return EventDetailResponse(**data)
