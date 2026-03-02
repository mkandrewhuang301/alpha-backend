"""
Event service — handles DB queries and Redis enrichment for event endpoints.

Routes call these functions; they handle the heavy lifting of querying,
joining, and merging live ticker data from Redis.
"""

from typing import Optional
from uuid import UUID

from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.market_cache import MarketCacheManager
from app.models.db import Event, Market


def _build_redis_asset_id(exchange: str, market_ext_id: str, execution_asset_id: str) -> str:
    """
    Build the Redis asset ID used in cache keys.
    Kalshi: {market_ticker}-{side}  (e.g. KXINFLATION-24-yes)
    Polymarket: token ID as-is
    """
    if exchange == "kalshi":
        return f"{market_ext_id}-{execution_asset_id}"
    return execution_asset_id


async def list_categories(
    exchange: str,
    db: AsyncSession,
) -> list[str]:
    """Return sorted distinct categories for active events on an exchange."""
    result = await db.execute(
        select(distinct(Event.category))
        .where(
            Event.exchange == exchange,
            Event.status == "active",
            Event.is_deleted == False,
            Event.category.isnot(None),
        )
        .order_by(Event.category)
    )
    return [row[0] for row in result.all()]


async def list_events_feed(
    exchange: str,
    db: AsyncSession,
    cache: MarketCacheManager,
    category: Optional[str] = None,
    sort: str = "volume",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """
    Fetch event feed with sorting, pagination, and live Redis prices for lead markets.

    Returns (items, count) where items are dicts ready for response serialization.
    """
    stmt = select(Event).where(Event.exchange == exchange, Event.is_deleted == False)

    if category:
        stmt = stmt.where(Event.category == category)

    if sort == "closing_soon":
        stmt = stmt.where(Event.status == "active").order_by(Event.close_time.asc().nullslast())
    elif sort == "newest":
        stmt = stmt.order_by(Event.created_at.desc())
    else:
        stmt = stmt.order_by(Event.volume_24h.desc().nullslast())

    stmt = stmt.offset(offset).limit(limit)
    result = await db.execute(stmt)
    events = result.scalars().all()

    if not events:
        return [], 0

    # Find lead market (first market) per event for yes/no prices
    event_ids = [e.id for e in events]
    market_stmt = (
        select(Market)
        .where(Market.event_id.in_(event_ids), Market.is_deleted == False)
        .order_by(Market.created_at.asc())
    )
    market_result = await db.execute(market_stmt)
    all_markets = market_result.scalars().all()

    lead_market: dict[UUID, Market] = {}
    for m in all_markets:
        if m.event_id not in lead_market:
            lead_market[m.event_id] = m

    # Batch-fetch Redis prices for lead markets
    redis_asset_ids: list[str] = []
    event_to_asset: dict[UUID, tuple[str, str]] = {}
    for eid, mkt in lead_market.items():
        yes_id = _build_redis_asset_id(exchange, mkt.ext_id, "yes")
        no_id = _build_redis_asset_id(exchange, mkt.ext_id, "no")
        redis_asset_ids.extend([yes_id, no_id])
        event_to_asset[eid] = (yes_id, no_id)

    ticker_data = await cache.get_tickers(exchange, redis_asset_ids)

    items: list[dict] = []
    for e in events:
        yes_price: Optional[str] = None
        no_price: Optional[str] = None
        asset_pair = event_to_asset.get(e.id)
        if asset_pair:
            yes_data = ticker_data.get(asset_pair[0], {})
            no_data = ticker_data.get(asset_pair[1], {})
            yes_price = yes_data.get("price")
            no_price = no_data.get("price")

        items.append({
            "id": e.id,
            "ext_id": e.ext_id,
            "title": e.title,
            "category": e.category,
            "status": e.status,
            "close_time": e.close_time,
            "volume_24h": e.volume_24h,
            "image_url": e.image_url,
            "yes_price": yes_price,
            "no_price": no_price,
        })

    return items, len(items)


async def get_event_detail(
    exchange: str,
    event_ext_id: str,
    db: AsyncSession,
    cache: MarketCacheManager,
) -> Optional[dict]:
    """
    Full event detail with all markets, outcomes, and live ticker data from Redis.

    Returns None if event not found.
    """
    result = await db.execute(
        select(Event)
        .options(selectinload(Event.markets).selectinload(Market.outcomes))
        .where(
            Event.ext_id == event_ext_id,
            Event.exchange == exchange,
            Event.is_deleted == False,
        )
    )
    event = result.scalar_one_or_none()
    if event is None:
        return None

    # Collect all outcome asset IDs for batch Redis fetch
    redis_asset_ids: list[str] = []
    outcome_asset_map: dict[UUID, str] = {}
    for mkt in event.markets:
        if mkt.is_deleted:
            continue
        for outcome in mkt.outcomes:
            asset_id = _build_redis_asset_id(exchange, mkt.ext_id, outcome.execution_asset_id)
            redis_asset_ids.append(asset_id)
            outcome_asset_map[outcome.id] = asset_id

    ticker_data = await cache.get_tickers(exchange, redis_asset_ids)

    # Build response dict
    market_details: list[dict] = []
    for mkt in event.markets:
        if mkt.is_deleted:
            continue
        outcome_details: list[dict] = []
        for outcome in mkt.outcomes:
            asset_id = outcome_asset_map.get(outcome.id)
            td = None
            if asset_id:
                raw = ticker_data.get(asset_id, {})
                if raw:
                    td = {
                        "price": raw.get("price"),
                        "bid": raw.get("bid"),
                        "bid_size": raw.get("bid_size"),
                        "ask": raw.get("ask"),
                        "ask_size": raw.get("ask_size"),
                    }
            outcome_details.append({
                "id": outcome.id,
                "execution_asset_id": outcome.execution_asset_id,
                "title": outcome.title,
                "side": outcome.side,
                "is_winner": outcome.is_winner,
                "ticker_data": td,
            })

        market_details.append({
            "id": mkt.id,
            "ext_id": mkt.ext_id,
            "title": mkt.title,
            "subtitle": mkt.subtitle,
            "type": mkt.type,
            "status": mkt.status,
            "open_time": mkt.open_time,
            "close_time": mkt.close_time,
            "outcomes": outcome_details,
        })

    return {
        "id": event.id,
        "ext_id": event.ext_id,
        "title": event.title,
        "description": event.description,
        "category": event.category,
        "status": event.status,
        "close_time": event.close_time,
        "expected_expiration_time": event.expected_expiration_time,
        "volume_24h": event.volume_24h,
        "image_url": event.image_url,
        "markets": market_details,
    }
