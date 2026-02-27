"""
Events endpoints — data served from PostgreSQL (populated by kalshi_ingest worker).

GET /events            - list events, filterable by exchange, status, series_ticker
GET /events/{ticker}   - single event by Kalshi ticker (ext_id)
"""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
    ticker: str                         # ext_id — Kalshi event_ticker or Polymarket slug
    series_ticker: Optional[str] = None # Denormalized from parent Series.ext_id for FE convenience
    title: str
    description: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None
    mutually_exclusive: Optional[bool] = None
    close_time: Optional[datetime] = None
    expected_expiration_time: Optional[datetime] = None


class EventListResponse(BaseModel):
    events: List[EventResponse]
    count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_response(row: Event) -> EventResponse:
    series_ticker = row.series.ext_id if row.series else None
    return EventResponse(
        id=row.id,
        exchange=row.exchange,
        ticker=row.ext_id,
        series_ticker=series_ticker,
        title=row.title,
        description=row.description,
        category=row.category,
        status=row.status,
        mutually_exclusive=row.mutually_exclusive,
        close_time=row.close_time,
        expected_expiration_time=row.expected_expiration_time,
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
    series_ticker: Optional[str] = Query(default=None, description="Filter by parent series ticker"),
    limit: int = Query(default=200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> EventListResponse:
    """
    List events. Supports filtering by exchange, status, and series_ticker.
    Status values use our normalized enum ('active', 'closed', 'resolved') not Kalshi's raw strings.
    """
    stmt = (
        select(Event)
        .options(selectinload(Event.series))
        .where(Event.is_deleted == False)
    )

    if exchange:
        stmt = stmt.where(Event.exchange == exchange)
    if status:
        stmt = stmt.where(Event.status == status)
    if series_ticker:
        # Join to Series to filter by series ticker
        stmt = stmt.join(Series, Event.series_id == Series.id).where(
            Series.ext_id == series_ticker,
            Series.exchange == exchange,
        )

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
    """Fetch a single event by its native exchange ticker."""
    result = await db.execute(
        select(Event)
        .options(selectinload(Event.series))
        .where(
            Event.ext_id == event_ticker,
            Event.exchange == exchange,
            Event.is_deleted == False,
        )
    )
    row = result.scalar_one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail=f"Event '{event_ticker}' not found on {exchange}")

    return _row_to_response(row)
