"""
Kalshi Market Data Ingestion Worker

Syncs the full Kalshi market hierarchy into PostgreSQL:
    Series → Events → Markets → MarketOutcomes (yes/no for binary)

Ingestion order MUST be: series → events → markets → outcomes.
Markets reference events; events reference series. Violating this order
will produce foreign key errors.

Jobs:
    - run_kalshi_full_sync: full backfill every 15 min (via arq cron)
    - kalshi_state_reconciliation: delta sync every 1 min
    - aggregate_ohlcv: Redis ticks → 1m candles every 1 min
"""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory
from app.models.db import Event, Market, MarketOutcome, Series
from app.models.kalshi import (
    KalshiEvent,
    KalshiMarket,
    KalshiSeries,
)
from app.services.kalshi import (
    get_events,
    get_markets,
    get_series_list,
)

logger = logging.getLogger(__name__)

EXCHANGE = "kalshi"

# Timestamp of the last state reconciliation run (epoch seconds)
_last_reconciliation_ts: int = 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _get_series_id_by_ext(session: AsyncSession, series_ticker: str) -> Optional[uuid.UUID]:
    """Look up our internal series UUID by Kalshi series ticker."""
    result = await session.execute(
        select(Series.id).where(
            Series.exchange == EXCHANGE,
            Series.ext_id == series_ticker,
            Series.is_deleted == False,
        )
    )
    row = result.scalar_one_or_none()
    return row


async def _get_event_id_by_ext(session: AsyncSession, event_ticker: str) -> Optional[uuid.UUID]:
    """Look up our internal event UUID by Kalshi event ticker."""
    result = await session.execute(
        select(Event.id).where(
            Event.exchange == EXCHANGE,
            Event.ext_id == event_ticker,
            Event.is_deleted == False,
        )
    )
    return result.scalar_one_or_none()


async def _get_market_id_by_ext(session: AsyncSession, market_ticker: str) -> Optional[uuid.UUID]:
    """Look up our internal market UUID by Kalshi market ticker."""
    result = await session.execute(
        select(Market.id).where(
            Market.exchange == EXCHANGE,
            Market.ext_id == market_ticker,
            Market.is_deleted == False,
        )
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# 1. Sync Series
# ---------------------------------------------------------------------------

async def sync_series(session: AsyncSession) -> int:
    """
    Fetch all Kalshi series and upsert into the series table.
    Returns the number of series processed.
    """
    logger.info("[kalshi.ingest] Syncing series...")

    raw = await get_series_list()
    if raw.get("error"):
        logger.error("[kalshi.ingest] Failed to fetch series: %s", raw.get("detail"))
        return 0

    series_list = [KalshiSeries(**s) for s in raw.get("series", [])]
    count = 0

    for s in series_list:
        try:
            stmt = (
                pg_insert(Series)
                .values(
                    id=uuid.uuid4(),
                    exchange=EXCHANGE,
                    ext_id=s.ticker,
                    title=s.title,
                    description=s.description,
                    category=s.category,
                    tags=s.tags or [],
                    image_url=s.image_url,
                    frequency=s.frequency,
                    is_deleted=False,
                )
                .on_conflict_do_update(
                    constraint="uq_series_exchange_extid",
                    set_={
                        "title": s.title,
                        "description": s.description,
                        "category": s.category,
                        "tags": s.tags or [],
                        "image_url": s.image_url,
                        "frequency": s.frequency,
                        "updated_at": datetime.now(timezone.utc),
                    },
                )
            )
            await session.execute(stmt)
            count += 1
        except Exception as exc:
            logger.error("[kalshi.ingest] Error upserting series %s: %s", s.ticker, exc)

    await session.commit()
    logger.info("[kalshi.ingest] Series sync complete: %d processed", count)
    return count


# ---------------------------------------------------------------------------
# 2. Sync Events
# ---------------------------------------------------------------------------

async def sync_events(session: AsyncSession) -> int:
    """
    Fetch all Kalshi events (paginated) and upsert into the events table.
    Returns the number of events processed.
    """
    logger.info("[kalshi.ingest] Syncing events...")
    count = 0
    cursor = None

    while True:
        raw = await get_events(limit=200, cursor=cursor)
        if raw.get("error"):
            logger.error("[kalshi.ingest] Failed to fetch events: %s", raw.get("detail"))
            break

        batch = [KalshiEvent(**e) for e in raw.get("events", [])]
        if not batch:
            break

        for e in batch:
            try:
                series_id = None
                if e.series_ticker:
                    series_id = await _get_series_id_by_ext(session, e.series_ticker)
                    if series_id is None:
                        logger.warning(
                            "[kalshi.ingest] Series %s not found for event %s — skipping series_id",
                            e.series_ticker, e.event_ticker,
                        )

                stmt = (
                    pg_insert(Event)
                    .values(
                        id=uuid.uuid4(),
                        series_id=series_id,
                        exchange=EXCHANGE,
                        ext_id=e.event_ticker,
                        title=e.title,
                        description=e.sub_title,
                        category=e.category,
                        status=e.normalized_status,
                        mutually_exclusive=e.mutually_exclusive or False,
                        close_time=e.close_time,
                        expected_expiration_time=e.expected_expiration_time,
                        platform_metadata={},
                        is_deleted=False,
                    )
                    .on_conflict_do_update(
                        constraint="uq_events_exchange_extid",
                        set_={
                            "series_id": series_id,
                            "title": e.title,
                            "description": e.sub_title,
                            "category": e.category,
                            "status": e.normalized_status,
                            "mutually_exclusive": e.mutually_exclusive or False,
                            "close_time": e.close_time,
                            "expected_expiration_time": e.expected_expiration_time,
                            "updated_at": datetime.now(timezone.utc),
                        },
                    )
                )
                await session.execute(stmt)
                count += 1
            except Exception as exc:
                logger.error("[kalshi.ingest] Error upserting event %s: %s", e.event_ticker, exc)

        await session.commit()

        cursor = raw.get("cursor")
        if not cursor:
            break

    logger.info("[kalshi.ingest] Events sync complete: %d processed", count)
    return count


# ---------------------------------------------------------------------------
# 3. Sync Markets + Outcomes
# ---------------------------------------------------------------------------

async def _upsert_outcomes_for_market(
    session: AsyncSession,
    market_id: uuid.UUID,
    market: KalshiMarket,
) -> None:
    """
    Create yes/no outcomes for a binary Kalshi market if they don't already exist.
    For non-binary markets, outcomes are inserted with side='other' and named by ticker.
    """
    if market.normalized_market_type == "binary":
        outcomes = [
            {
                "execution_asset_id": "yes",
                "title": "Yes",
                "side": "yes",
            },
            {
                "execution_asset_id": "no",
                "title": "No",
                "side": "no",
            },
        ]
    else:
        # Categorical/scalar: single placeholder until full outcome data is available
        outcomes = [
            {
                "execution_asset_id": market.ticker,
                "title": market.title,
                "side": "other",
            }
        ]

    for o in outcomes:
        is_winner = None
        if market.result and o["execution_asset_id"] == market.result.lower():
            is_winner = True
        elif market.result and o["execution_asset_id"] != market.result.lower():
            is_winner = False

        stmt = (
            pg_insert(MarketOutcome)
            .values(
                id=uuid.uuid4(),
                market_id=market_id,
                execution_asset_id=o["execution_asset_id"],
                title=o["title"],
                side=o["side"],
                is_winner=is_winner,
                platform_metadata={},
            )
            .on_conflict_do_update(
                constraint="uq_market_outcomes_market_asset",
                set_={
                    "title": o["title"],
                    "is_winner": is_winner,
                },
            )
        )
        await session.execute(stmt)


async def sync_markets(session: AsyncSession) -> int:
    """
    Fetch all Kalshi markets (paginated) and upsert into markets + market_outcomes tables.
    Must run AFTER sync_events() so event_id foreign keys resolve.
    Returns the number of markets processed.
    """
    logger.info("[kalshi.ingest] Syncing markets...")
    count = 0
    cursor = None

    while True:
        raw = await get_markets(limit=1000, cursor=cursor)
        if raw.get("error"):
            logger.error("[kalshi.ingest] Failed to fetch markets: %s", raw.get("detail"))
            break

        batch = [KalshiMarket(**m) for m in raw.get("markets", [])]
        if not batch:
            break

        for m in batch:
            try:
                if not m.event_ticker:
                    logger.warning("[kalshi.ingest] Market %s has no event_ticker — skipping", m.ticker)
                    continue

                event_id = await _get_event_id_by_ext(session, m.event_ticker)
                if event_id is None:
                    logger.warning(
                        "[kalshi.ingest] Event %s not found for market %s — skipping",
                        m.event_ticker, m.ticker,
                    )
                    continue

                # Capture exchange-specific metadata in JSONB
                platform_metadata = {
                    "strike_type": m.strike_type,
                    "floor_strike": m.floor_strike,
                    "cap_strike": m.cap_strike,
                    "risk_limit_cents": m.risk_limit_cents,
                    "settlement_timer_seconds": m.settlement_timer_seconds,
                    "can_close_early": m.can_close_early,
                    "response_price_units": m.response_price_units,
                }

                stmt = (
                    pg_insert(Market)
                    .values(
                        id=uuid.uuid4(),
                        event_id=event_id,
                        exchange=EXCHANGE,
                        ext_id=m.ticker,
                        title=m.title,
                        subtitle=m.subtitle,
                        type=m.normalized_market_type,
                        status=m.normalized_status,
                        open_time=m.open_time,
                        close_time=m.close_time,
                        resolve_time=m.expiration_time,
                        result=m.result,
                        rules_primary=m.rules_primary,
                        rules_secondary=m.rules_secondary,
                        platform_metadata=platform_metadata,
                        is_deleted=False,
                    )
                    .on_conflict_do_update(
                        constraint="uq_markets_exchange_extid",
                        set_={
                            "title": m.title,
                            "subtitle": m.subtitle,
                            "type": m.normalized_market_type,
                            "status": m.normalized_status,
                            "open_time": m.open_time,
                            "close_time": m.close_time,
                            "resolve_time": m.expiration_time,
                            "result": m.result,
                            "rules_primary": m.rules_primary,
                            "rules_secondary": m.rules_secondary,
                            "platform_metadata": platform_metadata,
                            "updated_at": datetime.now(timezone.utc),
                        },
                    )
                )
                await session.execute(stmt)
                # Flush within the transaction so the next SELECT can see the upserted row
                await session.flush()

                # Fetch the market_id (upserted or existing)
                market_id = await _get_market_id_by_ext(session, m.ticker)
                if market_id:
                    await _upsert_outcomes_for_market(session, market_id, m)

                count += 1
            except Exception as exc:
                logger.error("[kalshi.ingest] Error upserting market %s: %s", m.ticker, exc)
                await session.rollback()

        await session.commit()

        cursor = raw.get("cursor")
        if not cursor:
            break

    logger.info("[kalshi.ingest] Markets sync complete: %d processed", count)
    return count


# ---------------------------------------------------------------------------
# Full sync entrypoint
# ---------------------------------------------------------------------------

async def run_kalshi_full_sync() -> None:
    """
    Run a complete Kalshi data sync: series → events → markets → outcomes.
    This is called on startup and on a recurring schedule via arq.
    Each sync stage gets its own session to avoid long-running transactions.
    """
    logger.info("[kalshi.ingest] Starting full sync...")

    try:
        async with async_session_factory() as session:
            await sync_series(session)

        async with async_session_factory() as session:
            await sync_events(session)

        async with async_session_factory() as session:
            await sync_markets(session)

        logger.info("[kalshi.ingest] Full sync complete.")
    except Exception as exc:
        logger.error("[kalshi.ingest] Full sync failed: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# State reconciliation (delta sync)
# ---------------------------------------------------------------------------

async def kalshi_state_reconciliation(ctx: dict) -> None:
    """
    Delta sync: fetch only markets updated since the last reconciliation run.
    Uses min_updated_ts param to get recently changed markets and upserts them.
    Runs every 1 minute via arq cron.
    """
    global _last_reconciliation_ts

    now_ts = int(time.time())
    # On first run, look back 2 minutes
    since_ts = _last_reconciliation_ts if _last_reconciliation_ts > 0 else now_ts - 120

    logger.info("[kalshi.ingest] State reconciliation since ts=%d...", since_ts)

    try:
        cursor = None
        total = 0

        while True:
            raw = await get_markets(limit=1000, cursor=cursor, min_updated_ts=since_ts)
            if raw.get("error"):
                logger.error("[kalshi.ingest] Reconciliation fetch failed: %s", raw.get("detail"))
                break

            batch = [KalshiMarket(**m) for m in raw.get("markets", [])]
            if not batch:
                break

            async with async_session_factory() as session:
                for m in batch:
                    try:
                        if not m.event_ticker:
                            continue

                        event_id = await _get_event_id_by_ext(session, m.event_ticker)
                        if event_id is None:
                            continue

                        platform_metadata = {
                            "strike_type": m.strike_type,
                            "floor_strike": m.floor_strike,
                            "cap_strike": m.cap_strike,
                            "risk_limit_cents": m.risk_limit_cents,
                            "settlement_timer_seconds": m.settlement_timer_seconds,
                            "can_close_early": m.can_close_early,
                            "response_price_units": m.response_price_units,
                        }

                        stmt = (
                            pg_insert(Market)
                            .values(
                                id=uuid.uuid4(),
                                event_id=event_id,
                                exchange=EXCHANGE,
                                ext_id=m.ticker,
                                title=m.title,
                                subtitle=m.subtitle,
                                type=m.normalized_market_type,
                                status=m.normalized_status,
                                open_time=m.open_time,
                                close_time=m.close_time,
                                resolve_time=m.expiration_time,
                                result=m.result,
                                rules_primary=m.rules_primary,
                                rules_secondary=m.rules_secondary,
                                platform_metadata=platform_metadata,
                                is_deleted=False,
                            )
                            .on_conflict_do_update(
                                constraint="uq_markets_exchange_extid",
                                set_={
                                    "title": m.title,
                                    "subtitle": m.subtitle,
                                    "type": m.normalized_market_type,
                                    "status": m.normalized_status,
                                    "open_time": m.open_time,
                                    "close_time": m.close_time,
                                    "resolve_time": m.expiration_time,
                                    "result": m.result,
                                    "rules_primary": m.rules_primary,
                                    "rules_secondary": m.rules_secondary,
                                    "platform_metadata": platform_metadata,
                                    "updated_at": datetime.now(timezone.utc),
                                },
                            )
                        )
                        await session.execute(stmt)
                        await session.flush()

                        market_id = await _get_market_id_by_ext(session, m.ticker)
                        if market_id:
                            await _upsert_outcomes_for_market(session, market_id, m)

                        total += 1
                    except Exception as exc:
                        logger.error("[kalshi.ingest] Reconciliation error for %s: %s", m.ticker, exc)
                        await session.rollback()

                await session.commit()

            cursor = raw.get("cursor")
            if not cursor:
                break

        _last_reconciliation_ts = now_ts
        logger.info("[kalshi.ingest] State reconciliation complete: %d markets updated", total)

    except Exception as exc:
        logger.error("[kalshi.ingest] State reconciliation failed: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# OHLCV aggregation (Redis ticks → Postgres)
# ---------------------------------------------------------------------------

async def aggregate_ohlcv(ctx: dict) -> None:
    """
    Read latest tick prices from Redis HSET keys (market:{ticker}),
    compute 1-minute OHLCV snapshots, and bulk insert to the market_ticks table.

    Tick data format in Redis (set by WebSocket firehose):
        HSET market:{ticker} best_bid best_ask last_price volume ts
    """
    redis = ctx.get("redis")
    pool = ctx.get("asyncpg_pool")

    if not redis or not pool:
        logger.warning("[kalshi.ingest] OHLCV aggregation skipped — redis or pool not available")
        return

    try:
        # Scan for all market:* keys in Redis
        tick_keys = []
        async for key in redis.scan_iter(match="market:*", count=500):
            tick_keys.append(key)

        if not tick_keys:
            return

        now = datetime.now(timezone.utc)
        rows = []

        for key in tick_keys:
            data = await redis.hgetall(key)
            if not data:
                continue

            ticker = key.replace("market:", "")
            last_price = float(data.get("last_price", 0))
            best_bid = float(data.get("best_bid", 0))
            best_ask = float(data.get("best_ask", 0))
            volume = int(data.get("volume", 0))

            # For 1m candles from a single snapshot: OHLC are all the same price
            rows.append((
                ticker,
                now,
                last_price,  # open
                last_price,  # high
                last_price,  # low
                last_price,  # close
                volume,
                best_bid,
                best_ask,
            ))

        if rows:
            async with pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO market_ticks (ticker, ts, open, high, low, close, volume, best_bid, best_ask)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (ticker, ts) DO UPDATE SET
                        high = GREATEST(market_ticks.high, EXCLUDED.high),
                        low = LEAST(market_ticks.low, EXCLUDED.low),
                        close = EXCLUDED.close,
                        volume = EXCLUDED.volume,
                        best_bid = EXCLUDED.best_bid,
                        best_ask = EXCLUDED.best_ask
                    """,
                    rows,
                )

            logger.info("[kalshi.ingest] OHLCV aggregation: %d tickers written", len(rows))

    except Exception as exc:
        logger.error("[kalshi.ingest] OHLCV aggregation failed: %s", exc, exc_info=True)
