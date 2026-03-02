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

import json as _json
import logging
import time
import uuid
from datetime import datetime, timezone

from app.core.database import get_asyncpg_pool
from app.models.kalshi import (
    KalshiEvent,
    KalshiMarket,
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

async def _load_ext_id_map(pool, table: str) -> dict[str, uuid.UUID]:
    """Load a {ext_id: id} mapping for all non-deleted rows of the given table for our exchange."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT ext_id, id FROM {table} WHERE exchange = $1 AND is_deleted = FALSE",
            EXCHANGE,
        )
    return {r["ext_id"]: r["id"] for r in rows}




# ---------------------------------------------------------------------------
# 1. Sync Series
# ---------------------------------------------------------------------------

async def sync_series() -> int:
    """
    Fetch all Kalshi series and batch-upsert into the series table via asyncpg.
    Returns the number of series processed.
    """
    logger.info("[kalshi.ingest] Syncing series...")

    try:
        resp = await get_series_list()
    except Exception as exc:
        logger.error("[kalshi.ingest] Failed to fetch series: %s", exc, exc_info=True)
        return 0

    series_list = resp.series or []
    logger.info("[kalshi.ingest] Fetched %d series from Kalshi API", len(series_list))

    if not series_list:
        logger.warning("[kalshi.ingest] Kalshi returned empty series list — nothing to upsert")
        return 0

    # Build rows for batch upsert
    now = datetime.now(timezone.utc)
    rows = []
    for s in series_list:
        rows.append((
            str(uuid.uuid4()),       # id
            EXCHANGE,                # exchange
            s.ticker,                # ext_id
            s.title,                 # title
            s.description,           # description
            s.category,              # category
            _json.dumps(s.tags or []),  # tags (JSONB)
            s.image_url,             # image_url
            s.frequency,             # frequency
            False,                   # is_deleted
            now,                     # updated_at
        ))

    logger.info("[kalshi.ingest] Prepared %d series rows for batch upsert", len(rows))

    pool = await get_asyncpg_pool()
    BATCH_SIZE = 500
    count = 0

    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        try:
            async with pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO series (id, exchange, ext_id, title, description, category, tags, image_url, frequency, is_deleted, updated_at)
                    VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11)
                    ON CONFLICT ON CONSTRAINT uq_series_exchange_extid DO UPDATE SET
                        title = EXCLUDED.title,
                        description = EXCLUDED.description,
                        category = EXCLUDED.category,
                        tags = EXCLUDED.tags,
                        image_url = EXCLUDED.image_url,
                        frequency = EXCLUDED.frequency,
                        updated_at = EXCLUDED.updated_at
                    """,
                    batch,
                )
            count += len(batch)
            logger.info("[kalshi.ingest] Series upsert progress: %d / %d", count, len(rows))
        except Exception as exc:
            logger.error("[kalshi.ingest] Error in series batch upsert (batch %d-%d): %s", i, i + len(batch), exc, exc_info=True)

    logger.info("[kalshi.ingest] Series sync complete: %d processed", count)
    return count


# ---------------------------------------------------------------------------
# 2. Sync Events
# ---------------------------------------------------------------------------

async def sync_events() -> int:
    """
    Fetch all Kalshi events (paginated) and batch-upsert into the events table via asyncpg.
    Returns the number of events processed.
    """
    logger.info("[kalshi.ingest] Syncing events...")

    pool = await get_asyncpg_pool()

    # Pre-load series ext_id → UUID map for FK resolution
    series_map = await _load_ext_id_map(pool, "series")
    logger.info("[kalshi.ingest] Loaded %d series for FK lookup", len(series_map))

    now = datetime.now(timezone.utc)
    total_fetched = 0
    total_upserted = 0
    cursor = None
    page = 0
    BATCH_SIZE = 500

    while True:
        page += 1
        try:
            resp = await get_events(limit=200, cursor=cursor)
        except Exception as exc:
            logger.error("[kalshi.ingest] Failed to fetch events page %d: %s", page, exc)
            break

        raw_events = resp.events or []
        if not raw_events:
            break

        events = [KalshiEvent(**e.to_dict()) for e in raw_events]
        total_fetched += len(events)
        logger.info("[kalshi.ingest] Fetched events page %d: %d events (total fetched: %d)", page, len(events), total_fetched)

        # Build rows for batch upsert
        rows = []
        for e in events:
            series_id = None
            if e.series_ticker:
                series_id = series_map.get(e.series_ticker)

            platform_metadata = {
                "strike_date": str(e.strike_date) if e.strike_date else None,
                "strike_period": e.strike_period,
            }

            rows.append((
                str(uuid.uuid4()),                    # id
                str(series_id) if series_id else None, # series_id
                EXCHANGE,                              # exchange
                e.event_ticker,                        # ext_id
                e.title,                               # title
                e.sub_title,                           # description
                e.category,                            # category
                e.normalized_status,                   # status
                e.mutually_exclusive or False,          # mutually_exclusive
                e.close_time,                          # close_time
                e.expected_expiration_time,             # expected_expiration_time
                _json.dumps(platform_metadata),        # platform_metadata
                False,                                 # is_deleted
                now,                                   # updated_at
            ))

        # Batch upsert
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            try:
                async with pool.acquire() as conn:
                    await conn.executemany(
                        """
                        INSERT INTO events (id, series_id, exchange, ext_id, title, description, category,
                                           status, mutually_exclusive, close_time, expected_expiration_time,
                                           platform_metadata, is_deleted, updated_at)
                        VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7,
                                $8::market_status, $9, $10, $11,
                                $12::jsonb, $13, $14)
                        ON CONFLICT ON CONSTRAINT uq_events_exchange_extid DO UPDATE SET
                            series_id = EXCLUDED.series_id,
                            title = EXCLUDED.title,
                            description = EXCLUDED.description,
                            category = EXCLUDED.category,
                            status = EXCLUDED.status,
                            mutually_exclusive = EXCLUDED.mutually_exclusive,
                            close_time = EXCLUDED.close_time,
                            expected_expiration_time = EXCLUDED.expected_expiration_time,
                            updated_at = EXCLUDED.updated_at
                        """,
                        batch,
                    )
                total_upserted += len(batch)
            except Exception as exc:
                logger.error("[kalshi.ingest] Error in events batch upsert: %s", exc, exc_info=True)

        logger.info("[kalshi.ingest] Events upsert progress: %d upserted", total_upserted)

        cursor = resp.cursor
        if not cursor:
            break

    logger.info("[kalshi.ingest] Events sync complete: %d fetched, %d upserted", total_fetched, total_upserted)
    return total_upserted


# ---------------------------------------------------------------------------
# 3. Sync Markets + Outcomes
# ---------------------------------------------------------------------------

def _build_outcome_rows(
    market_id: str,
    market: KalshiMarket,
) -> list[tuple]:
    """Build outcome rows for a market (no DB calls needed)."""
    if market.normalized_market_type == "binary":
        outcomes = [
            ("yes", "Yes", "yes"),
            ("no", "No", "no"),
        ]
    else:
        outcomes = [
            (market.ticker, market.title, "other"),
        ]

    rows = []
    for asset_id, title, side in outcomes:
        is_winner = None
        if market.result:
            result_lower = market.result.lower()
            is_winner = (asset_id == result_lower)

        rows.append((
            str(uuid.uuid4()),   # id
            market_id,           # market_id (already a string UUID)
            asset_id,            # execution_asset_id
            title,               # title
            side,                # side
            is_winner,           # is_winner
            _json.dumps({}),     # platform_metadata
        ))
    return rows


async def sync_markets() -> int:
    """
    Fetch all Kalshi markets (paginated) and batch-upsert into markets + market_outcomes via asyncpg.
    Must run AFTER sync_events() so event_id foreign keys resolve.
    Returns the number of markets processed.
    """
    logger.info("[kalshi.ingest] Syncing markets...")

    pool = await get_asyncpg_pool()

    # Pre-load event ext_id → UUID map for FK resolution
    event_map = await _load_ext_id_map(pool, "events")
    logger.info("[kalshi.ingest] Loaded %d events for FK lookup", len(event_map))

    now = datetime.now(timezone.utc)
    total_fetched = 0
    total_upserted = 0
    total_outcomes = 0
    skipped_no_event = 0
    cursor = None
    page = 0
    BATCH_SIZE = 500

    while True:
        page += 1
        try:
            resp = await get_markets(limit=1000, cursor=cursor)
        except Exception as exc:
            logger.error("[kalshi.ingest] Failed to fetch markets page %d: %s", page, exc)
            break

        raw_markets = resp.markets or []
        if not raw_markets:
            break

        markets = [KalshiMarket(**m.to_dict()) for m in raw_markets]
        total_fetched += len(markets)
        logger.info("[kalshi.ingest] Fetched markets page %d: %d markets (total fetched: %d)", page, len(markets), total_fetched)

        # Build rows for market upsert, collecting outcome rows along the way
        market_rows = []
        outcome_rows = []

        for m in markets:
            if not m.event_ticker:
                skipped_no_event += 1
                continue

            event_id = event_map.get(m.event_ticker)
            if event_id is None:
                skipped_no_event += 1
                continue

            market_uuid = str(uuid.uuid4())

            platform_metadata = {
                "strike_type": m.strike_type,
                "floor_strike": m.floor_strike,
                "cap_strike": m.cap_strike,
                "risk_limit_cents": m.risk_limit_cents,
                "settlement_timer_seconds": m.settlement_timer_seconds,
                "can_close_early": m.can_close_early,
                "response_price_units": m.response_price_units,
            }

            market_rows.append((
                market_uuid,                         # id
                str(event_id),                       # event_id
                EXCHANGE,                            # exchange
                m.ticker,                            # ext_id
                m.title,                             # title
                m.subtitle,                          # subtitle
                m.normalized_market_type,            # type
                m.normalized_status,                 # status
                m.open_time,                         # open_time
                m.close_time,                        # close_time
                m.expiration_time,                   # resolve_time
                m.result,                            # result
                m.rules_primary,                     # rules_primary
                m.rules_secondary,                   # rules_secondary
                _json.dumps(platform_metadata),      # platform_metadata
                False,                               # is_deleted
                now,                                 # updated_at
            ))

            # We can't use market_uuid for outcomes on conflict (existing rows keep their id),
            # so we'll resolve market IDs after the market upsert below.

        # Batch upsert markets
        for i in range(0, len(market_rows), BATCH_SIZE):
            batch = market_rows[i : i + BATCH_SIZE]
            try:
                async with pool.acquire() as conn:
                    await conn.executemany(
                        """
                        INSERT INTO markets (id, event_id, exchange, ext_id, title, subtitle, type, status,
                                            open_time, close_time, resolve_time, result, rules_primary,
                                            rules_secondary, platform_metadata, is_deleted, updated_at)
                        VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7::market_type, $8::market_status,
                                $9, $10, $11, $12, $13, $14, $15::jsonb, $16, $17)
                        ON CONFLICT ON CONSTRAINT uq_markets_exchange_extid DO UPDATE SET
                            title = EXCLUDED.title,
                            subtitle = EXCLUDED.subtitle,
                            type = EXCLUDED.type,
                            status = EXCLUDED.status,
                            open_time = EXCLUDED.open_time,
                            close_time = EXCLUDED.close_time,
                            resolve_time = EXCLUDED.resolve_time,
                            result = EXCLUDED.result,
                            rules_primary = EXCLUDED.rules_primary,
                            rules_secondary = EXCLUDED.rules_secondary,
                            platform_metadata = EXCLUDED.platform_metadata,
                            updated_at = EXCLUDED.updated_at
                        """,
                        batch,
                    )
                total_upserted += len(batch)
            except Exception as exc:
                logger.error("[kalshi.ingest] Error in markets batch upsert: %s", exc, exc_info=True)

        logger.info("[kalshi.ingest] Markets upsert progress: %d upserted", total_upserted)

        # Now load market ext_id → id map for this page to build outcome rows
        # (need actual DB ids, not the uuid4 we generated, since ON CONFLICT keeps existing id)
        market_id_map = await _load_ext_id_map(pool, "markets")

        for m in markets:
            if not m.event_ticker or m.event_ticker not in event_map:
                continue
            mid = market_id_map.get(m.ticker)
            if mid:
                outcome_rows.extend(_build_outcome_rows(str(mid), m))

        # Batch upsert outcomes
        for i in range(0, len(outcome_rows), BATCH_SIZE):
            batch = outcome_rows[i : i + BATCH_SIZE]
            try:
                async with pool.acquire() as conn:
                    await conn.executemany(
                        """
                        INSERT INTO market_outcomes (id, market_id, execution_asset_id, title, side, is_winner, platform_metadata)
                        VALUES ($1::uuid, $2::uuid, $3, $4, $5::trade_side, $6, $7::jsonb)
                        ON CONFLICT ON CONSTRAINT uq_market_outcomes_market_asset DO UPDATE SET
                            title = EXCLUDED.title,
                            is_winner = EXCLUDED.is_winner
                        """,
                        batch,
                    )
                total_outcomes += len(batch)
            except Exception as exc:
                logger.error("[kalshi.ingest] Error in outcomes batch upsert: %s", exc, exc_info=True)

        logger.info("[kalshi.ingest] Outcomes upsert progress: %d upserted", total_outcomes)

        cursor = resp.cursor
        if not cursor:
            break

    logger.info(
        "[kalshi.ingest] Markets sync complete: %d fetched, %d markets upserted, %d outcomes upserted, %d skipped (no event)",
        total_fetched, total_upserted, total_outcomes, skipped_no_event,
    )
    return total_upserted


# ---------------------------------------------------------------------------
# Full sync entrypoint
# ---------------------------------------------------------------------------

async def run_kalshi_full_sync() -> None:
    """
    Run a complete Kalshi data sync: series → events → markets → outcomes.
    This is called on startup and on a recurring schedule via arq.
    Each sync stage gets its own session to avoid long-running transactions.

    Ordering is enforced: series MUST succeed before events, events before markets.
    If series sync fails or returns 0, events will not have valid series_id FKs.
    """
    logger.info("[kalshi.ingest] Starting full sync...")

    try:
        # 1. Series — must complete successfully before events
        series_count = await sync_series()

        if series_count == 0:
            logger.error(
                "[kalshi.ingest] Series sync returned 0 rows — aborting full sync. "
                "Events and markets depend on series being present."
            )
            return

        # 2. Events — must complete successfully before markets
        events_count = await sync_events()

        if events_count == 0:
            logger.warning(
                "[kalshi.ingest] Events sync returned 0 rows — skipping markets sync."
            )
            return

        # 3. Markets + outcomes
        await sync_markets()

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

    pool = await get_asyncpg_pool()
    event_map = await _load_ext_id_map(pool, "events")
    now = datetime.now(timezone.utc)
    BATCH_SIZE = 500

    try:
        cursor = None
        total_markets = 0
        total_outcomes = 0

        while True:
            try:
                resp = await get_markets(limit=1000, cursor=cursor, min_updated_ts=since_ts)
            except Exception as exc:
                logger.error("[kalshi.ingest] Reconciliation fetch failed: %s", exc)
                break

            raw_batch = [KalshiMarket(**m.to_dict()) for m in (resp.markets or [])]
            if not raw_batch:
                break

            # Build market rows
            market_rows = []
            valid_markets = []
            for m in raw_batch:
                if not m.event_ticker:
                    continue
                event_id = event_map.get(m.event_ticker)
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

                market_rows.append((
                    str(uuid.uuid4()),
                    str(event_id),
                    EXCHANGE,
                    m.ticker,
                    m.title,
                    m.subtitle,
                    m.normalized_market_type,
                    m.normalized_status,
                    m.open_time,
                    m.close_time,
                    m.expiration_time,
                    m.result,
                    m.rules_primary,
                    m.rules_secondary,
                    _json.dumps(platform_metadata),
                    False,
                    now,
                ))
                valid_markets.append(m)

            # Batch upsert markets
            for i in range(0, len(market_rows), BATCH_SIZE):
                batch = market_rows[i : i + BATCH_SIZE]
                try:
                    async with pool.acquire() as conn:
                        await conn.executemany(
                            """
                            INSERT INTO markets (id, event_id, exchange, ext_id, title, subtitle, type, status,
                                                open_time, close_time, resolve_time, result, rules_primary,
                                                rules_secondary, platform_metadata, is_deleted, updated_at)
                            VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7::market_type, $8::market_status,
                                    $9, $10, $11, $12, $13, $14, $15::jsonb, $16, $17)
                            ON CONFLICT ON CONSTRAINT uq_markets_exchange_extid DO UPDATE SET
                                title = EXCLUDED.title,
                                subtitle = EXCLUDED.subtitle,
                                type = EXCLUDED.type,
                                status = EXCLUDED.status,
                                open_time = EXCLUDED.open_time,
                                close_time = EXCLUDED.close_time,
                                resolve_time = EXCLUDED.resolve_time,
                                result = EXCLUDED.result,
                                rules_primary = EXCLUDED.rules_primary,
                                rules_secondary = EXCLUDED.rules_secondary,
                                platform_metadata = EXCLUDED.platform_metadata,
                                updated_at = EXCLUDED.updated_at
                            """,
                            batch,
                        )
                    total_markets += len(batch)
                except Exception as exc:
                    logger.error("[kalshi.ingest] Reconciliation market upsert error: %s", exc, exc_info=True)

            # Build and upsert outcome rows
            if valid_markets:
                market_id_map = await _load_ext_id_map(pool, "markets")
                outcome_rows = []
                for m in valid_markets:
                    mid = market_id_map.get(m.ticker)
                    if mid:
                        outcome_rows.extend(_build_outcome_rows(str(mid), m))

                for i in range(0, len(outcome_rows), BATCH_SIZE):
                    batch = outcome_rows[i : i + BATCH_SIZE]
                    try:
                        async with pool.acquire() as conn:
                            await conn.executemany(
                                """
                                INSERT INTO market_outcomes (id, market_id, execution_asset_id, title, side, is_winner, platform_metadata)
                                VALUES ($1::uuid, $2::uuid, $3, $4, $5::trade_side, $6, $7::jsonb)
                                ON CONFLICT ON CONSTRAINT uq_market_outcomes_market_asset DO UPDATE SET
                                    title = EXCLUDED.title,
                                    is_winner = EXCLUDED.is_winner
                                """,
                                batch,
                            )
                        total_outcomes += len(batch)
                    except Exception as exc:
                        logger.error("[kalshi.ingest] Reconciliation outcome upsert error: %s", exc, exc_info=True)

            cursor = resp.cursor
            if not cursor:
                break

        _last_reconciliation_ts = now_ts
        logger.info("[kalshi.ingest] State reconciliation complete: %d markets, %d outcomes updated", total_markets, total_outcomes)

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
