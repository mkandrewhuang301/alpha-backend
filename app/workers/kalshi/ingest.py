"""
Kalshi Market Data Ingestion Worker

Syncs the full Kalshi market hierarchy into PostgreSQL:
    Series → Events + Markets → MarketOutcomes (yes/no for binary)

Ingestion order MUST be: series → events+markets → outcomes.
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

# Multivariate event (MVE) series prefixes to skip — these are combo/parlay
# markets that bloat the DB without adding value for Alpha's use case.
_MVE_PREFIXES = ("KXMVE",)

# Timestamp of the last state reconciliation run (epoch seconds)
_last_reconciliation_ts: int = 0


def _is_mve(ticker: str | None) -> bool:
    """Return True if a ticker belongs to a multivariate event series."""
    if not ticker:
        return False
    return ticker.startswith(_MVE_PREFIXES)


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


async def _load_ext_id_map_for_keys(pool, table: str, ext_ids: list[str]) -> dict[str, uuid.UUID]:
    """Load a {ext_id: id} mapping for specific ext_ids only. Much faster than loading the entire table."""
    if not ext_ids:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT ext_id, id FROM {table} WHERE exchange = $1 AND ext_id = ANY($2::text[]) AND is_deleted = FALSE",
            EXCHANGE,
            ext_ids,
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
# 2. Sync Events + Markets (single pass with nested markets)
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


async def _upsert_markets_batch(pool, rows: list[tuple]) -> int:
    """
    Upsert a batch of market rows using a single multi-row INSERT via UNNEST.
    Returns the number of rows affected. Falls back to row-by-row on batch failure.
    """
    if not rows:
        return 0

    # Transpose rows into column arrays for UNNEST
    ids, event_ids, exchanges, ext_ids, titles, subtitles = [], [], [], [], [], []
    types, statuses, open_times, close_times, resolve_times = [], [], [], [], []
    results, rules_primaries, rules_secondaries, platform_metas = [], [], [], []
    is_deleteds, updated_ats = [], []

    for r in rows:
        ids.append(r[0]); event_ids.append(r[1]); exchanges.append(r[2])
        ext_ids.append(r[3]); titles.append(r[4]); subtitles.append(r[5])
        types.append(r[6]); statuses.append(r[7]); open_times.append(r[8])
        close_times.append(r[9]); resolve_times.append(r[10]); results.append(r[11])
        rules_primaries.append(r[12]); rules_secondaries.append(r[13])
        platform_metas.append(r[14]); is_deleteds.append(r[15]); updated_ats.append(r[16])

    query = """
        INSERT INTO markets (id, event_id, exchange, ext_id, title, subtitle, type, status,
                            open_time, close_time, resolve_time, result, rules_primary,
                            rules_secondary, platform_metadata, is_deleted, updated_at)
        SELECT
            unnest($1::uuid[]), unnest($2::uuid[]), unnest($3::exchange_type[]), unnest($4::text[]),
            unnest($5::text[]), unnest($6::text[]), unnest($7::market_type[]), unnest($8::market_status[]),
            unnest($9::timestamptz[]), unnest($10::timestamptz[]), unnest($11::timestamptz[]),
            unnest($12::text[]), unnest($13::text[]), unnest($14::text[]),
            unnest($15::jsonb[]), unnest($16::boolean[]), unnest($17::timestamptz[])
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
    """

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                query,
                ids, event_ids, exchanges, ext_ids, titles, subtitles,
                types, statuses, open_times, close_times, resolve_times,
                results, rules_primaries, rules_secondaries,
                platform_metas, is_deleteds, updated_ats,
            )
        return len(rows)
    except Exception as exc:
        logger.error("[kalshi.ingest] Batch market upsert failed (%d rows), falling back to row-by-row: %s", len(rows), exc)

    # Fallback: row-by-row to identify the bad rows
    upserted = 0
    for r in rows:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO markets (id, event_id, exchange, ext_id, title, subtitle, type, status,
                                        open_time, close_time, resolve_time, result, rules_primary,
                                        rules_secondary, platform_metadata, is_deleted, updated_at)
                    VALUES ($1::uuid, $2::uuid, $3::exchange_type, $4, $5, $6, $7::market_type, $8::market_status,
                            $9, $10, $11, $12, $13, $14, $15::jsonb, $16, $17)
                    ON CONFLICT ON CONSTRAINT uq_markets_exchange_extid DO UPDATE SET
                        title = EXCLUDED.title, subtitle = EXCLUDED.subtitle, type = EXCLUDED.type,
                        status = EXCLUDED.status, open_time = EXCLUDED.open_time, close_time = EXCLUDED.close_time,
                        resolve_time = EXCLUDED.resolve_time, result = EXCLUDED.result,
                        rules_primary = EXCLUDED.rules_primary, rules_secondary = EXCLUDED.rules_secondary,
                        platform_metadata = EXCLUDED.platform_metadata, updated_at = EXCLUDED.updated_at
                    """,
                    *r,
                )
            upserted += 1
        except Exception as row_exc:
            logger.error(
                "[kalshi.ingest] SKIPPED market ext_id=%s type=%s status=%s — row upsert failed: %s",
                r[3], r[6], r[7], row_exc,
            )
    return upserted


async def _upsert_outcomes_batch(pool, rows: list[tuple]) -> int:
    """
    Upsert a batch of outcome rows using UNNEST. Falls back to row-by-row on failure.
    """
    if not rows:
        return 0

    ids, market_ids, asset_ids, titles, sides, is_winners, platform_metas = (
        [], [], [], [], [], [], [],
    )
    for r in rows:
        ids.append(r[0]); market_ids.append(r[1]); asset_ids.append(r[2])
        titles.append(r[3]); sides.append(r[4]); is_winners.append(r[5])
        platform_metas.append(r[6])

    query = """
        INSERT INTO market_outcomes (id, market_id, execution_asset_id, title, side, is_winner, platform_metadata)
        SELECT
            unnest($1::uuid[]), unnest($2::uuid[]), unnest($3::text[]),
            unnest($4::text[]), unnest($5::trade_side[]), unnest($6::boolean[]),
            unnest($7::jsonb[])
        ON CONFLICT ON CONSTRAINT uq_market_outcomes_market_asset DO UPDATE SET
            title = EXCLUDED.title,
            is_winner = EXCLUDED.is_winner
    """

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                query,
                ids, market_ids, asset_ids, titles, sides, is_winners, platform_metas,
            )
        return len(rows)
    except Exception as exc:
        logger.error("[kalshi.ingest] Batch outcome upsert failed (%d rows), falling back to row-by-row: %s", len(rows), exc)

    upserted = 0
    for r in rows:
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO market_outcomes (id, market_id, execution_asset_id, title, side, is_winner, platform_metadata)
                    VALUES ($1::uuid, $2::uuid, $3, $4, $5::trade_side, $6, $7::jsonb)
                    ON CONFLICT ON CONSTRAINT uq_market_outcomes_market_asset DO UPDATE SET
                        title = EXCLUDED.title, is_winner = EXCLUDED.is_winner
                    """,
                    *r,
                )
            upserted += 1
        except Exception as row_exc:
            logger.error(
                "[kalshi.ingest] SKIPPED outcome market_id=%s asset_id=%s side=%s — row upsert failed: %s",
                r[1], r[2], r[4], row_exc,
            )
    return upserted


def _build_market_row(m: KalshiMarket, event_id: uuid.UUID, now: datetime) -> tuple:
    """Build a single market upsert row tuple from a KalshiMarket and its resolved event_id."""
    platform_metadata = {
        "strike_type": m.strike_type,
        "floor_strike": m.floor_strike,
        "cap_strike": m.cap_strike,
        "risk_limit_cents": m.risk_limit_cents,
        "settlement_timer_seconds": m.settlement_timer_seconds,
        "can_close_early": m.can_close_early,
        "response_price_units": m.response_price_units,
    }

    return (
        str(uuid.uuid4()),                   # id
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
    )


async def _upsert_events_returning(pool, rows: list[tuple]) -> dict[str, uuid.UUID]:
    """
    Upsert event rows using UNNEST and return a {ext_id: id} mapping via RETURNING.
    This lets us get event IDs immediately for nested market FK resolution.
    """
    if not rows:
        return {}

    ids, series_ids, exchanges, ext_ids, titles, descriptions = [], [], [], [], [], []
    categories, statuses, mutually_exclusives = [], [], []
    close_times, expected_expiration_times, platform_metas = [], [], []
    is_deleteds, updated_ats = [], []

    for r in rows:
        ids.append(r[0]); series_ids.append(r[1]); exchanges.append(r[2])
        ext_ids.append(r[3]); titles.append(r[4]); descriptions.append(r[5])
        categories.append(r[6]); statuses.append(r[7]); mutually_exclusives.append(r[8])
        close_times.append(r[9]); expected_expiration_times.append(r[10])
        platform_metas.append(r[11]); is_deleteds.append(r[12]); updated_ats.append(r[13])

    query = """
        INSERT INTO events (id, series_id, exchange, ext_id, title, description, category,
                           status, mutually_exclusive, close_time, expected_expiration_time,
                           platform_metadata, is_deleted, updated_at)
        SELECT
            unnest($1::uuid[]), unnest($2::uuid[]), unnest($3::exchange_type[]), unnest($4::text[]),
            unnest($5::text[]), unnest($6::text[]), unnest($7::text[]),
            unnest($8::market_status[]), unnest($9::boolean[]), unnest($10::timestamptz[]),
            unnest($11::timestamptz[]), unnest($12::jsonb[]), unnest($13::boolean[]),
            unnest($14::timestamptz[])
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
        RETURNING ext_id, id
    """

    try:
        async with pool.acquire() as conn:
            result_rows = await conn.fetch(
                query,
                ids, series_ids, exchanges, ext_ids, titles, descriptions,
                categories, statuses, mutually_exclusives,
                close_times, expected_expiration_times,
                platform_metas, is_deleteds, updated_ats,
            )
        return {r["ext_id"]: r["id"] for r in result_rows}
    except Exception as exc:
        logger.error("[kalshi.ingest] Batch event upsert failed (%d rows): %s", len(rows), exc, exc_info=True)
        return {}


async def sync_events_and_markets() -> tuple[int, int]:
    """
    Fetch all Kalshi events with nested markets (paginated) and batch-upsert
    events, markets, and outcomes in a single pass.

    Uses with_nested_markets=True so markets arrive attached to their parent
    event — eliminating the need to paginate through all ~500k markets separately.

    Returns (events_upserted, markets_upserted).
    """
    logger.info("[kalshi.ingest] Syncing events + markets (nested)...")

    pool = await get_asyncpg_pool()

    # Pre-load series ext_id → UUID map for FK resolution
    series_map = await _load_ext_id_map(pool, "series")
    logger.info("[kalshi.ingest] Loaded %d series for FK lookup", len(series_map))

    now = datetime.now(timezone.utc)
    total_events_fetched = 0
    total_events_upserted = 0
    total_markets_upserted = 0
    total_outcomes_upserted = 0
    cursor = None
    page = 0
    BATCH_SIZE = 500

    while True:
        page += 1
        try:
            resp = await get_events(limit=200, cursor=cursor, with_nested_markets=True)
        except Exception as exc:
            logger.error("[kalshi.ingest] Failed to fetch events page %d: %s", page, exc)
            break

        raw_events = resp.events or []
        if not raw_events:
            break

        events = [KalshiEvent(**e.to_dict()) for e in raw_events]
        total_events_fetched += len(events)
        logger.info(
            "[kalshi.ingest] Fetched events page %d: %d events (total fetched: %d)",
            page, len(events), total_events_fetched,
        )

        # --- Build event rows (skip MVE) ---
        event_rows = []
        # Track which events are non-MVE so we process their markets
        non_mve_events: list[KalshiEvent] = []

        for e in events:
            if _is_mve(e.event_ticker) or _is_mve(e.series_ticker):
                continue

            series_id = None
            if e.series_ticker:
                series_id = series_map.get(e.series_ticker)

            platform_metadata = {
                "strike_date": str(e.strike_date) if e.strike_date else None,
                "strike_period": e.strike_period,
            }

            event_rows.append((
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
            non_mve_events.append(e)

        # Upsert events and get back {ext_id: id} mapping
        page_event_map: dict[str, uuid.UUID] = {}
        for i in range(0, len(event_rows), BATCH_SIZE):
            batch = event_rows[i : i + BATCH_SIZE]
            batch_map = await _upsert_events_returning(pool, batch)
            page_event_map.update(batch_map)
            total_events_upserted += len(batch_map)

        logger.info(
            "[kalshi.ingest] Events upsert progress: %d upserted (page %d: %d)",
            total_events_upserted, page, len(page_event_map),
        )

        # --- Build market rows from nested markets ---
        market_rows = []
        valid_markets: list[KalshiMarket] = []

        for e in non_mve_events:
            event_id = page_event_map.get(e.event_ticker)
            if event_id is None:
                logger.warning(
                    "[kalshi.ingest] SKIPPED markets for event %s — event_id not found after upsert",
                    e.event_ticker,
                )
                continue

            nested_markets = e.markets or []
            for m in nested_markets:
                market_rows.append(_build_market_row(m, event_id, now))
                valid_markets.append(m)

        # Batch upsert markets
        for i in range(0, len(market_rows), BATCH_SIZE):
            batch = market_rows[i : i + BATCH_SIZE]
            upserted = await _upsert_markets_batch(pool, batch)
            total_markets_upserted += upserted

        # --- Build and upsert outcomes ---
        if valid_markets:
            valid_tickers = [m.ticker for m in valid_markets]
            market_id_map = await _load_ext_id_map_for_keys(pool, "markets", valid_tickers)

            outcome_rows = []
            for m in valid_markets:
                mid = market_id_map.get(m.ticker)
                if mid:
                    outcome_rows.extend(_build_outcome_rows(str(mid), m))
                else:
                    logger.warning(
                        "[kalshi.ingest] SKIPPED outcomes for market %s — market_id not found after upsert",
                        m.ticker,
                    )

            for i in range(0, len(outcome_rows), BATCH_SIZE):
                batch = outcome_rows[i : i + BATCH_SIZE]
                upserted = await _upsert_outcomes_batch(pool, batch)
                total_outcomes_upserted += upserted

        logger.info(
            "[kalshi.ingest] Page %d complete: %d markets, %d outcomes upserted",
            page, total_markets_upserted, total_outcomes_upserted,
        )

        cursor = resp.cursor
        if not cursor:
            break

    logger.info(
        "[kalshi.ingest] Events+markets sync complete: %d events fetched, %d events upserted, "
        "%d markets upserted, %d outcomes upserted",
        total_events_fetched, total_events_upserted,
        total_markets_upserted, total_outcomes_upserted,
    )
    return total_events_upserted, total_markets_upserted


# ---------------------------------------------------------------------------
# Full sync entrypoint
# ---------------------------------------------------------------------------

async def run_kalshi_full_sync() -> None:
    """
    Run a complete Kalshi data sync: series → events+markets+outcomes.
    This is called on startup and on a recurring schedule via arq.

    Ordering is enforced: series MUST succeed before events+markets.
    If series sync fails or returns 0, events will not have valid series_id FKs.
    """
    logger.info("[kalshi.ingest] Starting full sync...")

    try:
        # 1. Series — must complete successfully before events+markets
        series_count = await sync_series()

        if series_count == 0:
            logger.error(
                "[kalshi.ingest] Series sync returned 0 rows — aborting full sync. "
                "Events and markets depend on series being present."
            )
            return

        # 2. Events + markets + outcomes (single pass with nested markets)
        events_count, markets_count = await sync_events_and_markets()

        if events_count == 0:
            logger.warning("[kalshi.ingest] Events+markets sync returned 0 events.")

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
        skipped_no_event_ticker = 0
        skipped_event_fetch_failed = 0

        while True:
            try:
                resp = await get_markets(limit=1000, cursor=cursor, min_updated_ts=since_ts)
            except Exception as exc:
                logger.error("[kalshi.ingest] Reconciliation fetch failed: %s", exc)
                break

            raw_batch = [KalshiMarket(**m.to_dict()) for m in (resp.markets or [])]
            if not raw_batch:
                break

            # Build market rows (skip MVE)
            market_rows = []
            valid_markets = []
            for m in raw_batch:
                if _is_mve(m.event_ticker) or _is_mve(m.series_ticker):
                    continue
                if not m.event_ticker:
                    skipped_no_event_ticker += 1
                    continue
                event_id = event_map.get(m.event_ticker)
                if event_id is None:
                    skipped_event_fetch_failed += 1
                    logger.warning(
                        "[kalshi.ingest] SKIPPED market %s in reconciliation — event %s could not be fetched",
                        m.ticker, m.event_ticker,
                    )
                    continue

                market_rows.append(_build_market_row(m, event_id, now))
                valid_markets.append(m)

            # Batch upsert markets
            for i in range(0, len(market_rows), BATCH_SIZE):
                batch = market_rows[i : i + BATCH_SIZE]
                upserted = await _upsert_markets_batch(pool, batch)
                total_markets += upserted

            # Build and upsert outcome rows
            if valid_markets:
                valid_tickers = [m.ticker for m in valid_markets]
                market_id_map = await _load_ext_id_map_for_keys(pool, "markets", valid_tickers)
                outcome_rows = []
                for m in valid_markets:
                    mid = market_id_map.get(m.ticker)
                    if mid:
                        outcome_rows.extend(_build_outcome_rows(str(mid), m))
                    else:
                        logger.warning(
                            "[kalshi.ingest] SKIPPED outcomes for market %s in reconciliation — market_id not found after upsert",
                            m.ticker,
                        )

                for i in range(0, len(outcome_rows), BATCH_SIZE):
                    batch = outcome_rows[i : i + BATCH_SIZE]
                    upserted = await _upsert_outcomes_batch(pool, batch)
                    total_outcomes += upserted

            cursor = resp.cursor
            if not cursor:
                break

        _last_reconciliation_ts = now_ts
        logger.info(
            "[kalshi.ingest] State reconciliation complete: %d markets, %d outcomes updated, "
            "%d skipped (no event_ticker), %d skipped (event fetch failed)",
            total_markets, total_outcomes, skipped_no_event_ticker, skipped_event_fetch_failed,
        )

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
