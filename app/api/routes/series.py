"""
Series endpoints — data served from PostgreSQL (populated by kalshi_ingest worker).

GET /series            - list all series, filterable by exchange and category
GET /series/{ticker}   - single series by Kalshi ticker (ext_id)
"""

from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.db import Series

router = APIRouter()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class SeriesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    exchange: str
    ticker: str                     # ext_id — native exchange identifier (Kalshi ticker, Polymarket slug)
    title: str
    description: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    image_url: Optional[str] = None
    frequency: Optional[str] = None


class SeriesListResponse(BaseModel):
    series: List[SeriesResponse]
    count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_response(row: Series) -> SeriesResponse:
    return SeriesResponse(
        id=row.id,
        exchange=row.exchange,
        ticker=row.ext_id,
        title=row.title,
        description=row.description,
        category=row.category,
        tags=row.tags or [],
        image_url=row.image_url,
        frequency=row.frequency,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/", response_model=SeriesListResponse)
async def list_series(
    exchange: Optional[str] = Query(default="kalshi", description="Exchange filter: 'kalshi' | 'polymarket'"),
    category: Optional[str] = Query(default=None, description="Category filter, e.g. 'Politics', 'Sports'"),
    db: AsyncSession = Depends(get_db),
) -> SeriesListResponse:
    """List all series stored in the database, defaulting to Kalshi."""
    stmt = select(Series).where(Series.is_deleted == False)

    if exchange:
        stmt = stmt.where(Series.exchange == exchange)
    if category:
        stmt = stmt.where(Series.category == category)

    stmt = stmt.order_by(Series.title)

    result = await db.execute(stmt)
    rows = result.scalars().all()

    return SeriesListResponse(
        series=[_row_to_response(r) for r in rows],
        count=len(rows),
    )


@router.get("/{ticker}", response_model=SeriesResponse)
async def get_series_by_ticker(
    ticker: str,
    exchange: Optional[str] = Query(default="kalshi"),
    db: AsyncSession = Depends(get_db),
) -> SeriesResponse:
    """Fetch a single series by its native exchange ticker."""
    result = await db.execute(
        select(Series).where(
            Series.ext_id == ticker,
            Series.exchange == exchange,
            Series.is_deleted == False,
        )
    )
    row = result.scalar_one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail=f"Series '{ticker}' not found on {exchange}")

    return _row_to_response(row)
