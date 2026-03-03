"""
Markets endpoints — data served from PostgreSQL (populated by workers.kalshi.ingest).

GET /markets              - list markets, filterable by exchange, status, event_ticker, series_ticker
GET /markets/{ticker}     - single market by Kalshi ticker (ext_id), includes outcomes
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
from app.models.db import Event, Market, MarketOutcome, Series

router = APIRouter()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class OutcomeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    execution_asset_id: str     # "yes" / "no" for Kalshi binary; ERC-1155 token ID for Polymarket
    title: str                  # Human-readable: "Yes", "No", "Kamala Harris"
    side: str                   # "yes" | "no" | "other"
    is_winner: Optional[bool] = None


class MarketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    exchange: str
    ticker: str                         # ext_id — Kalshi ticker or Polymarket conditionId
    event_ticker: Optional[str] = None  # Denormalized from parent Event.ext_id
    series_ticker: Optional[str] = None # Denormalized from grandparent Series.ext_id
    title: str
    subtitle: Optional[str] = None
    type: Optional[str] = None          # "binary" | "categorical" | "scalar"
    status: Optional[str] = None
    open_time: Optional[datetime] = None
    close_time: Optional[datetime] = None
    resolve_time: Optional[datetime] = None
    result: Optional[str] = None
    outcomes: Optional[List[OutcomeResponse]] = None


class MarketListResponse(BaseModel):
    markets: List[MarketResponse]
    count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _outcome_to_response(o: MarketOutcome) -> OutcomeResponse:
    return OutcomeResponse(
        id=o.id,
        execution_asset_id=o.execution_asset_id,
        title=o.title,
        side=o.side,
        is_winner=o.is_winner,
    )


def _row_to_response(row: Market, include_outcomes: bool = False) -> MarketResponse:
    event_ticker = row.event.ext_id if row.event else None
    series_ticker = (
        row.event.series.ext_id
        if row.event and row.event.series
        else None
    )
    outcomes = None
    if include_outcomes and row.outcomes is not None:
        outcomes = [_outcome_to_response(o) for o in row.outcomes]

    return MarketResponse(
        id=row.id,
        exchange=row.exchange,
        ticker=row.ext_id,
        event_ticker=event_ticker,
        series_ticker=series_ticker,
        title=row.title,
        subtitle=row.subtitle,
        type=row.type,
        status=row.status,
        open_time=row.open_time,
        close_time=row.close_time,
        resolve_time=row.resolve_time,
        result=row.result,
        outcomes=outcomes,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/", response_model=MarketListResponse)
async def list_markets(
    exchange: Optional[str] = Query(default="kalshi"),
    status: Optional[str] = Query(
        default=None,
        description="Normalized status: 'active' | 'closed' | 'resolved' | 'suspended' | 'unopened'",
    ),
    event_ticker: Optional[str] = Query(default=None, description="Filter by parent event ticker"),
    series_ticker: Optional[str] = Query(default=None, description="Filter by grandparent series ticker"),
    limit: int = Query(default=200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> MarketListResponse:
    """
    List markets from the database. Supports filtering by exchange, status,
    event_ticker, and series_ticker.

    Status values use our normalized enum ('active', 'closed', 'resolved')
    not Kalshi's raw status strings.
    """
    stmt = (
        select(Market)
        .options(
            selectinload(Market.event).selectinload(Event.series),
        )
        .where(Market.is_deleted == False)
    )

    if exchange:
        stmt = stmt.where(Market.exchange == exchange)
    if status:
        stmt = stmt.where(Market.status == status)
    if event_ticker:
        stmt = stmt.join(Event, Market.event_id == Event.id).where(
            Event.ext_id == event_ticker,
        )
    elif series_ticker:
        # series_ticker requires joining up through events → series
        stmt = (
            stmt
            .join(Event, Market.event_id == Event.id)
            .join(Series, Event.series_id == Series.id)
            .where(Series.ext_id == series_ticker)
        )

    stmt = stmt.order_by(Market.close_time.desc().nullslast()).limit(limit)

    result = await db.execute(stmt)
    rows = result.scalars().all()

    return MarketListResponse(
        markets=[_row_to_response(r, include_outcomes=False) for r in rows],
        count=len(rows),
    )


@router.get("/{ticker}", response_model=MarketResponse)
async def get_market_by_ticker(
    ticker: str,
    exchange: Optional[str] = Query(default="kalshi"),
    db: AsyncSession = Depends(get_db),
) -> MarketResponse:
    """
    Fetch a single market by its native exchange ticker.
    Includes all outcomes (yes/no for binary markets).
    """
    result = await db.execute(
        select(Market)
        .options(
            selectinload(Market.event).selectinload(Event.series),
            selectinload(Market.outcomes),
        )
        .where(
            Market.ext_id == ticker,
            Market.exchange == exchange,
            Market.is_deleted == False,
        )
    )
    row = result.scalar_one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail=f"Market '{ticker}' not found on {exchange}")

    return _row_to_response(row, include_outcomes=True)
