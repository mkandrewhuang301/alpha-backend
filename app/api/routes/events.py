from fastapi import APIRouter, Query
from typing import Optional
from app.services.kalshi import get_events, get_event

router = APIRouter()


@router.get("/")
async def list_events(
    limit: int = Query(default=200, ge=1, le=200),
    cursor: Optional[str] = None,
    with_nested_markets: bool = False,
    with_milestones: bool = False,
    status: Optional[str] = Query(default=None, regex="^(open|closed|settled)$"),
    series_ticker: Optional[str] = None,
):
    data = await get_events(
        limit=limit,
        cursor=cursor,
        with_nested_markets=with_nested_markets,
        with_milestones=with_milestones,
        status=status,
        series_ticker=series_ticker,
    )
    return data


@router.get("/{event_ticker}")
async def get_event_by_ticker(
    event_ticker: str,
    with_nested_markets: bool = False,
):
    data = await get_event(
        event_ticker=event_ticker,
        with_nested_markets=with_nested_markets,
    )
    return data
