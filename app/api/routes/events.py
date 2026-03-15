"""
Events endpoints — data served from PostgreSQL (populated by workers).

GET /events            - list events, filterable by exchange, status, series_ticker, categories, tags
GET /events/{ticker}   - single event by exchange ticker (ext_id)
"""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.db import Event, Series

router = APIRouter()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class EventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    exchange: str
    ticker: str                             # ext_id — Kalshi event_ticker or Polymarket event id
    series_ticker: Optional[str] = None    # Denormalized from platform_metadata["series_ticker"]
    title: str
    description: Optional[str] = None
    categories: Optional[List[str]] = None  # ARRAY(text) slug strings
    tags: Optional[List[str]] = None        # ARRAY(text) slug strings
    status: Optional[str] = None
    mutually_exclusive: Optional[bool] = None
    close_time: Optional[datetime] = None
    expected_expiration_time: Optional[datetime] = None
    image_url: Optional[str] = None
    featured_image_url: Optional[str] = None
    volume_24h: Optional[float] = None


class EventListResponse(BaseModel):
    events: List[EventResponse]
    count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_response(row: Event) -> EventResponse:
    series_ticker = None
    if row.platform_metadata and isinstance(row.platform_metadata, dict):
        series_ticker = row.platform_metadata.get("series_ticker")

    return EventResponse(
        id=row.id,
        exchange=row.exchange,
        ticker=row.ext_id,
        series_ticker=series_ticker,
        title=row.title,
        description=row.description,
        categories=list(row.categories) if row.categories else [],
        tags=list(row.tags) if row.tags else [],
        status=row.status,
        mutually_exclusive=row.mutually_exclusive,
        close_time=row.close_time,
        expected_expiration_time=row.expected_expiration_time,
        image_url=row.image_url,
        featured_image_url=row.featured_image_url,
        volume_24h=float(row.volume_24h) if row.volume_24h else 0.0,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/", response_model=EventListResponse)
async def list_events(
    exchange: Optional[str] = Query(default="kalshi"),
    status: Optional[str] = Query(
        default=None,
        description="Filter by normalized status: 'active' | 'closed' | 'resolved' | 'suspended'",
    ),
    series_ticker: Optional[str] = Query(default=None, description="Filter by parent series ticker (ext_id)"),
    categories: Optional[List[str]] = Query(
        default=None,
        description="Filter by one or more category slugs (AND logic — event must match all). "
                    "Example: ?categories=politics&categories=elections",
    ),
    tags: Optional[List[str]] = Query(
        default=None,
        description="Filter by one or more tag slugs (AND logic — event must match all). "
                    "Example: ?tags=bitcoin&tags=crypto",
    ),
    limit: int = Query(default=200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> EventListResponse:
    """
    List events. Supports filtering by exchange, status, series_ticker, categories, and tags.

    `categories` and `tags` accept multiple values and use PostgreSQL GIN @> (contains all)
    operator — only events matching every specified slug are returned.
    """
    stmt = select(Event).where(Event.is_deleted == False)

    if exchange:
        stmt = stmt.where(Event.exchange == exchange)
    if status:
        stmt = stmt.where(Event.status == status)

    if series_ticker:
        series_subq = (
            select(Series.id)
            .where(Series.ext_id == series_ticker, Series.is_deleted == False)
            .scalar_subquery()
        )
        stmt = stmt.where(func.array_position(Event.series_ids, series_subq).isnot(None))

    if categories:
        # @> contains-all: event.categories @> ARRAY['politics', 'elections']
        stmt = stmt.where(Event.categories.contains(categories))

    if tags:
        # @> contains-all: event.tags @> ARRAY['bitcoin', 'crypto']
        stmt = stmt.where(Event.tags.contains(tags))

    stmt = stmt.order_by(Event.close_time.desc().nullslast()).limit(limit)

    result = await db.execute(stmt)
    rows = result.scalars().all()

    return EventListResponse(
        events=[_row_to_response(r) for r in rows],
        count=len(rows),
    )


@router.get("/{event_ticker}", response_model=EventResponse)
async def get_event_by_ticker(
    event_ticker: str,
    exchange: Optional[str] = Query(default="kalshi"),
    db: AsyncSession = Depends(get_db),
) -> EventResponse:
    """Fetch a single event by its native exchange ticker (ext_id)."""
    result = await db.execute(
        select(Event).where(
            Event.ext_id == event_ticker,
            Event.exchange == exchange,
            Event.is_deleted == False,
        )
    )
    row = result.scalar_one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail=f"Event '{event_ticker}' not found on {exchange}")

    return _row_to_response(row)
