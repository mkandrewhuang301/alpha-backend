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
    get_event_metadata,
    get_events,
    get_markets,
    get_markets_raw,
    get_series_list,
    get_tags_for_series_categories,
)
from app.workers.taxonomy import (
    upsert_platform_tag,
    slugify,
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
        settlement_sources = [
            {"name": ss.name, "url": ss.url}
            for ss in (s.settlement_sources or [])
        ]
        # categories: ARRAY(text) — list of category slugs
        categories = [slugify(s.category)] if s.category else []
        # tags: ARRAY(text) — list of tag slugs
        tags = [slugify(t) if isinstance(t, str) else slugify(str(t)) for t in (s.tags or [])]
        rows.append((
            str(uuid.uuid4()),                         # id
            EXCHANGE,                                  # exchange
            s.ticker,                                  # ext_id
            s.title,                                   # title
            s.description,                             # description (from product_metadata)
            categories,                                # categories ARRAY(text)
            tags,                                      # tags ARRAY(text)
            s.image_url,                               # image_url (from product_metadata)
            s.frequency,                               # frequency
            _json.dumps(settlement_sources),           # settlement_sources (JSONB)
            s.contract_url,                            # contract_url
            _json.dumps(s.additional_prohibitions or []),  # additional_prohibitions (JSONB)
            s.fee_type,                                # fee_type
            s.fee_multiplier,                          # fee_multiplier
            s.volume or 0,                             # volume_24h (use volume from API)
            s.volume or 0,                             # total_volume
            False,                                     # is_deleted
            now,                                       # updated_at
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
                    INSERT INTO series (id, exchange, ext_id, title, description, categories, tags,
                                       image_url, frequency, settlement_sources, contract_url,
                                       additional_prohibitions, fee_type, fee_multiplier,
                                       volume_24h, total_volume, is_deleted, updated_at)
                    VALUES ($1::uuid, $2, $3, $4, $5, $6::text[], $7::text[], $8, $9,
                            $10::jsonb, $11, $12::jsonb, $13, $14::numeric,
                            $15::numeric, $16::numeric, $17, $18)
                    ON CONFLICT ON CONSTRAINT uq_series_exchange_extid DO UPDATE SET
                        title = EXCLUDED.title,
                        description = EXCLUDED.description,
                        categories = EXCLUDED.categories,
                        tags = EXCLUDED.tags,
                        image_url = EXCLUDED.image_url,
                        frequency = EXCLUDED.frequency,
                        settlement_sources = EXCLUDED.settlement_sources,
                        contract_url = EXCLUDED.contract_url,
                        additional_prohibitions = EXCLUDED.additional_prohibitions,
                        fee_type = EXCLUDED.fee_type,
                        fee_multiplier = EXCLUDED.fee_multiplier,
                        volume_24h = EXCLUDED.volume_24h,
                        total_volume = EXCLUDED.total_volume,
                        updated_at = EXCLUDED.updated_at
                    """,
                    batch,
                )
            count += len(batch)
            logger.info("[kalshi.ingest] Series upsert progress: %d / %d", count, len(rows))
        except Exception as exc:
            logger.error("[kalshi.ingest] Error in series batch upsert (batch %d-%d): %s", i, i + len(batch), exc, exc_info=True)

    logger.info("[kalshi.ingest] Series sync complete: %d processed", count)

    # Upsert PlatformTags for each unique category encountered
    seen_categories: set[str] = set()
    for s in series_list:
        if s.category and s.category not in seen_categories:
            seen_categories.add(s.category)
            await upsert_platform_tag(
                pool, EXCHANGE, "category",
                slug=slugify(s.category),
                label=s.category,
            )
    logger.info(
        "[kalshi.ingest] Upserted %d category platform_tags from series", len(seen_categories),
    )

    # Use SearchAPI to get all tags per category, upsert each tag with parent_id
    # linking it to its parent category slug.
    tags_by_category = await get_tags_for_series_categories()
    tag_count = 0
    for category_name, tag_labels in tags_by_category.items():
        category_slug = slugify(category_name)
        for tag_label in tag_labels:
            if not tag_label:
                continue
            await upsert_platform_tag(
                pool, EXCHANGE, "tag",
                slug=slugify(tag_label),
                label=tag_label,
                parent_id=category_slug,
            )
            tag_count += 1
    logger.info("[kalshi.ingest] Upserted %d tag platform_tags from SearchAPI", tag_count)

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
    yes_sub_titles, no_sub_titles = [], []
    types, statuses, open_times, close_times, resolve_times = [], [], [], [], []
    results, rules_primaries, rules_secondaries = [], [], []
    image_urls, color_codes = [], []
    fractional_tradings, response_price_units_list, strike_types = [], [], []
    open_interests, volumes, volumes_24h, total_volumes, liquidities = [], [], [], [], []
    platform_metas = []
    is_deleteds, updated_ats = [], []

    for r in rows:
        ids.append(r[0]); event_ids.append(r[1]); exchanges.append(r[2])
        ext_ids.append(r[3]); titles.append(r[4]); subtitles.append(r[5])
        yes_sub_titles.append(r[6]); no_sub_titles.append(r[7])
        types.append(r[8]); statuses.append(r[9]); open_times.append(r[10])
        close_times.append(r[11]); resolve_times.append(r[12]); results.append(r[13])
        rules_primaries.append(r[14]); rules_secondaries.append(r[15])
        image_urls.append(r[16]); color_codes.append(r[17])
        fractional_tradings.append(r[18]); response_price_units_list.append(r[19])
        strike_types.append(r[20])
        open_interests.append(r[21]); volumes.append(r[22])
        volumes_24h.append(r[23]); total_volumes.append(r[24]); liquidities.append(r[25])
        platform_metas.append(r[26]); is_deleteds.append(r[27]); updated_ats.append(r[28])

    query = """
        INSERT INTO markets (id, event_id, exchange, ext_id, title, subtitle,
                            yes_sub_title, no_sub_title,
                            type, status, open_time, close_time, resolve_time,
                            result, rules_primary, rules_secondary,
                            image_url, color_code,
                            fractional_trading_enabled, response_price_units, strike_type,
                            open_interest, volume, volume_24h, total_volume, liquidity,
                            platform_metadata, is_deleted, updated_at)
        SELECT
            unnest($1::uuid[]), unnest($2::uuid[]), unnest($3::exchange_type[]), unnest($4::text[]),
            unnest($5::text[]), unnest($6::text[]),
            unnest($7::text[]), unnest($8::text[]),
            unnest($9::market_type[]), unnest($10::market_status[]),
            unnest($11::timestamptz[]), unnest($12::timestamptz[]), unnest($13::timestamptz[]),
            unnest($14::text[]), unnest($15::text[]), unnest($16::text[]),
            unnest($17::text[]), unnest($18::text[]),
            unnest($19::boolean[]), unnest($20::text[]), unnest($21::text[]),
            unnest($22::numeric[]), unnest($23::numeric[]),
            unnest($24::numeric[]), unnest($25::numeric[]), unnest($26::numeric[]),
            unnest($27::jsonb[]), unnest($28::boolean[]), unnest($29::timestamptz[])
        ON CONFLICT ON CONSTRAINT uq_markets_exchange_extid DO UPDATE SET
            title = EXCLUDED.title,
            subtitle = EXCLUDED.subtitle,
            yes_sub_title = EXCLUDED.yes_sub_title,
            no_sub_title = EXCLUDED.no_sub_title,
            type = EXCLUDED.type,
            status = EXCLUDED.status,
            open_time = EXCLUDED.open_time,
            close_time = EXCLUDED.close_time,
            resolve_time = EXCLUDED.resolve_time,
            result = EXCLUDED.result,
            rules_primary = EXCLUDED.rules_primary,
            rules_secondary = EXCLUDED.rules_secondary,
            image_url = COALESCE(EXCLUDED.image_url, markets.image_url),
            color_code = COALESCE(EXCLUDED.color_code, markets.color_code),
            fractional_trading_enabled = EXCLUDED.fractional_trading_enabled,
            response_price_units = EXCLUDED.response_price_units,
            strike_type = EXCLUDED.strike_type,
            open_interest = EXCLUDED.open_interest,
            volume = EXCLUDED.volume,
            volume_24h = EXCLUDED.volume_24h,
            total_volume = EXCLUDED.total_volume,
            liquidity = EXCLUDED.liquidity,
            platform_metadata = EXCLUDED.platform_metadata,
            updated_at = EXCLUDED.updated_at
    """

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                query,
                ids, event_ids, exchanges, ext_ids, titles, subtitles,
                yes_sub_titles, no_sub_titles,
                types, statuses, open_times, close_times, resolve_times,
                results, rules_primaries, rules_secondaries,
                image_urls, color_codes,
                fractional_tradings, response_price_units_list, strike_types,
                open_interests, volumes, volumes_24h, total_volumes, liquidities,
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
                    INSERT INTO markets (id, event_id, exchange, ext_id, title, subtitle,
                                        yes_sub_title, no_sub_title,
                                        type, status, open_time, close_time, resolve_time,
                                        result, rules_primary, rules_secondary,
                                        image_url, color_code,
                                        fractional_trading_enabled, response_price_units, strike_type,
                                        open_interest, volume, volume_24h, total_volume, liquidity,
                                        platform_metadata, is_deleted, updated_at)
                    VALUES ($1::uuid, $2::uuid, $3::exchange_type, $4, $5, $6,
                            $7, $8,
                            $9::market_type, $10::market_status,
                            $11, $12, $13,
                            $14, $15, $16,
                            $17, $18,
                            $19, $20, $21,
                            $22::numeric, $23::numeric,
                            $24::numeric, $25::numeric, $26::numeric,
                            $27::jsonb, $28, $29)
                    ON CONFLICT ON CONSTRAINT uq_markets_exchange_extid DO UPDATE SET
                        title = EXCLUDED.title, subtitle = EXCLUDED.subtitle,
                        yes_sub_title = EXCLUDED.yes_sub_title, no_sub_title = EXCLUDED.no_sub_title,
                        type = EXCLUDED.type, status = EXCLUDED.status,
                        open_time = EXCLUDED.open_time, close_time = EXCLUDED.close_time,
                        resolve_time = EXCLUDED.resolve_time, result = EXCLUDED.result,
                        rules_primary = EXCLUDED.rules_primary, rules_secondary = EXCLUDED.rules_secondary,
                        image_url = COALESCE(EXCLUDED.image_url, markets.image_url),
                        color_code = COALESCE(EXCLUDED.color_code, markets.color_code),
                        fractional_trading_enabled = EXCLUDED.fractional_trading_enabled,
                        response_price_units = EXCLUDED.response_price_units,
                        strike_type = EXCLUDED.strike_type,
                        open_interest = EXCLUDED.open_interest, volume = EXCLUDED.volume,
                        volume_24h = EXCLUDED.volume_24h, total_volume = EXCLUDED.total_volume,
                        liquidity = EXCLUDED.liquidity,
                        platform_metadata = EXCLUDED.platform_metadata, updated_at = EXCLUDED.updated_at
                    """,
                    *r,
                )
            upserted += 1
        except Exception as row_exc:
            logger.error(
                "[kalshi.ingest] SKIPPED market ext_id=%s type=%s status=%s — row upsert failed: %s",
                r[3], r[8], r[9], row_exc,
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


def _build_market_row(
    m: KalshiMarket,
    event_id: uuid.UUID,
    now: datetime,
    market_metadata: dict[str, dict] | None = None,
) -> tuple:
    """Build a single market upsert row tuple from a KalshiMarket and its resolved event_id.

    Args:
        market_metadata: optional {market_ticker: {image_url, color_code}} from event metadata
    """
    platform_metadata = {
        "floor_strike": m.floor_strike,
        "cap_strike": m.cap_strike,
        "risk_limit_cents": m.risk_limit_cents,
        "settlement_timer_seconds": m.settlement_timer_seconds,
        "can_close_early": m.can_close_early,
        "early_close_condition": m.early_close_condition,
        "price_level_structure": m.price_level_structure,
        "is_provisional": m.is_provisional,
    }

    # Get per-market display metadata from event metadata if available
    meta = (market_metadata or {}).get(m.ticker, {})
    image_url = meta.get("image_url")
    color_code = meta.get("color_code")

    # Parse fixed-point volume fields when available
    volume_24h = None
    if hasattr(m, "volume_24h") and m.volume_24h is not None:
        volume_24h = m.volume_24h

    return (
        str(uuid.uuid4()),                   # 0: id
        str(event_id),                       # 1: event_id
        EXCHANGE,                            # 2: exchange
        m.ticker,                            # 3: ext_id
        m.title,                             # 4: title
        m.subtitle,                          # 5: subtitle
        m.yes_sub_title,                     # 6: yes_sub_title
        m.no_sub_title,                      # 7: no_sub_title
        m.normalized_market_type,            # 8: type
        m.normalized_status,                 # 9: status
        m.open_time,                         # 10: open_time
        m.close_time,                        # 11: close_time
        m.expiration_time,                   # 12: resolve_time
        m.result,                            # 13: result
        m.rules_primary,                     # 14: rules_primary
        m.rules_secondary,                   # 15: rules_secondary
        image_url,                           # 16: image_url
        color_code,                          # 17: color_code
        m.fractional_trading_enabled or False,  # 18: fractional_trading_enabled
        m.response_price_units,              # 19: response_price_units
        m.strike_type,                       # 20: strike_type
        m.open_interest,                     # 21: open_interest
        m.volume,                            # 22: volume
        volume_24h,                          # 23: volume_24h
        m.volume,                            # 24: total_volume (same as volume on initial sync)
        m.liquidity,                         # 25: liquidity
        _json.dumps(platform_metadata),      # 26: platform_metadata
        False,                               # 27: is_deleted
        now,                                 # 28: updated_at
    )


def _build_event_row(
    e: KalshiEvent,
    series_id: uuid.UUID | None,
    now: datetime,
    event_meta=None,
) -> tuple:
    """Build a single event upsert row from a KalshiEvent.

    Uses the new schema:
      - series_ids: ARRAY(uuid) — single-element for Kalshi events
      - categories: ARRAY(text) — slug strings cascaded from parent series
      - tags: ARRAY(text) — empty for Kalshi (no structured tags in API)
    """
    platform_metadata = {
        "strike_date": str(e.strike_date) if e.strike_date else None,
        "strike_period": e.strike_period,
        "collateral_return_type": e.collateral_return_type,
        "available_on_brokers": e.available_on_brokers,
        "product_metadata": e.product_metadata,
        # Denormalized series ticker for API responses (avoid series join)
        "series_ticker": e.series_ticker,
    }

    # Extract display metadata from event metadata response if available
    event_image_url = None
    image_url = None
    featured_image_url = None
    settlement_sources_json = _json.dumps([])
    competition = None
    competition_scope = None

    if event_meta:
        event_image_url = event_meta.image_url
        image_url = event_meta.image_url
        featured_image_url = event_meta.featured_image_url
        settlement_sources_json = _json.dumps([
            {"name": ss.name, "url": ss.url}
            for ss in (event_meta.settlement_sources or [])
        ])
        competition = event_meta.competition
        competition_scope = event_meta.competition_scope

    # series_ids: single-element UUID list for Kalshi (parent series)
    series_ids = [series_id] if series_id else []

    # categories: slug strings cascaded from the parent series category
    categories = [slugify(e.category)] if e.category else []

    # tags: Kalshi API doesn't return structured event-level tags
    tags: list[str] = []

    return (
        str(uuid.uuid4()),           # 0: id
        series_ids,                  # 1: series_ids (list of uuid.UUID)
        EXCHANGE,                    # 2: exchange
        e.event_ticker,              # 3: ext_id
        e.title,                     # 4: title
        e.sub_title,                 # 5: description
        e.sub_title,                 # 6: sub_title
        categories,                  # 7: categories ARRAY(text)
        tags,                        # 8: tags ARRAY(text)
        e.normalized_status,         # 9: status
        e.mutually_exclusive or False,  # 10: mutually_exclusive
        e.close_time,                # 11: close_time
        e.expected_expiration_time,  # 12: expected_expiration_time
        event_image_url,             # 13: event_image_url
        image_url,                   # 14: image_url
        featured_image_url,          # 15: featured_image_url
        settlement_sources_json,     # 16: settlement_sources (JSONB)
        competition,                 # 17: competition
        competition_scope,           # 18: competition_scope
        0,                           # 19: total_volume (aggregated later)
        0,                           # 20: open_interest (aggregated later)
        _json.dumps(platform_metadata),  # 21: platform_metadata
        False,                       # 22: is_deleted
        now,                         # 23: updated_at
    )


async def _upsert_events_returning(pool, rows: list[tuple]) -> dict[str, uuid.UUID]:
    """
    Upsert event rows using row-by-row INSERT with RETURNING.

    Returns a {ext_id: id} mapping for immediate market FK resolution.

    Row tuple format (24 elements):
      0: id (str UUID)
      1: series_ids (list[uuid.UUID])
      2: exchange (str)
      3: ext_id (str)
      4: title (str)
      5: description (str)
      6: sub_title (str)
      7: categories (list[str])
      8: tags (list[str])
      9: status (str)
      10: mutually_exclusive (bool)
      11: close_time (datetime|None)
      12: expected_expiration_time (datetime|None)
      13: event_image_url (str|None)
      14: image_url (str|None)
      15: featured_image_url (str|None)
      16: settlement_sources (str JSON)
      17: competition (str|None)
      18: competition_scope (str|None)
      19: total_volume (number)
      20: open_interest (number)
      21: platform_metadata (str JSON)
      22: is_deleted (bool)
      23: updated_at (datetime)
    """
    if not rows:
        return {}

    _INSERT_SQL = """
        INSERT INTO events (
            id, series_ids, exchange, ext_id, title, description,
            sub_title, categories, tags,
            status, mutually_exclusive, close_time, expected_expiration_time,
            event_image_url, image_url, featured_image_url, settlement_sources,
            competition, competition_scope,
            total_volume, open_interest,
            platform_metadata, is_deleted, updated_at
        )
        VALUES (
            $1::uuid, $2::uuid[], $3::exchange_type, $4, $5, $6,
            $7, $8::text[], $9::text[],
            $10::market_status, $11, $12, $13,
            $14, $15, $16, $17::jsonb,
            $18, $19,
            $20::numeric, $21::numeric,
            $22::jsonb, $23, $24
        )
        ON CONFLICT ON CONSTRAINT uq_events_exchange_extid DO UPDATE SET
            series_ids = EXCLUDED.series_ids,
            title = EXCLUDED.title,
            description = EXCLUDED.description,
            sub_title = EXCLUDED.sub_title,
            categories = EXCLUDED.categories,
            tags = EXCLUDED.tags,
            status = EXCLUDED.status,
            mutually_exclusive = EXCLUDED.mutually_exclusive,
            close_time = EXCLUDED.close_time,
            expected_expiration_time = EXCLUDED.expected_expiration_time,
            event_image_url = COALESCE(EXCLUDED.event_image_url, events.event_image_url),
            image_url = COALESCE(EXCLUDED.image_url, events.image_url),
            featured_image_url = COALESCE(EXCLUDED.featured_image_url, events.featured_image_url),
            settlement_sources = CASE
                WHEN EXCLUDED.settlement_sources::text != '[]' THEN EXCLUDED.settlement_sources
                ELSE events.settlement_sources
            END,
            competition = COALESCE(EXCLUDED.competition, events.competition),
            competition_scope = COALESCE(EXCLUDED.competition_scope, events.competition_scope),
            total_volume = EXCLUDED.total_volume,
            open_interest = EXCLUDED.open_interest,
            platform_metadata = EXCLUDED.platform_metadata,
            updated_at = EXCLUDED.updated_at
        RETURNING ext_id, id
    """

    result_map: dict[str, uuid.UUID] = {}
    try:
        async with pool.acquire() as conn:
            for r in rows:
                try:
                    row = await conn.fetchrow(
                        _INSERT_SQL,
                        uuid.UUID(r[0]),  # id
                        r[1],             # series_ids (list[uuid.UUID])
                        r[2],             # exchange
                        r[3],             # ext_id
                        r[4],             # title
                        r[5],             # description
                        r[6],             # sub_title
                        r[7],             # categories
                        r[8],             # tags
                        r[9],             # status
                        r[10],            # mutually_exclusive
                        r[11],            # close_time
                        r[12],            # expected_expiration_time
                        r[13],            # event_image_url
                        r[14],            # image_url
                        r[15],            # featured_image_url
                        r[16],            # settlement_sources JSON string
                        r[17],            # competition
                        r[18],            # competition_scope
                        r[19],            # total_volume
                        r[20],            # open_interest
                        r[21],            # platform_metadata JSON string
                        r[22],            # is_deleted
                        r[23],            # updated_at
                    )
                    if row:
                        result_map[row["ext_id"]] = row["id"]
                except Exception as row_exc:
                    logger.error(
                        "[kalshi.ingest] SKIPPED event ext_id=%s — upsert failed: %s",
                        r[3], row_exc,
                    )
    except Exception as exc:
        logger.error("[kalshi.ingest] Event upsert connection failed (%d rows): %s", len(rows), exc, exc_info=True)

    return result_map


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
        non_mve_events: list[KalshiEvent] = []

        for e in events:
            if _is_mve(e.event_ticker) or _is_mve(e.series_ticker):
                continue

            series_id = None
            if e.series_ticker:
                series_id = series_map.get(e.series_ticker)

            event_rows.append(_build_event_row(e, series_id, now))
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
# DEV_MODE: restricted sync for sandbox / free-tier environments
# ---------------------------------------------------------------------------

async def _upsert_single_series(pool, s, now: datetime) -> None:
    """Upsert a single series row (used in dev sync for individual series)."""
    settlement_sources = [
        {"name": ss.name, "url": ss.url}
        for ss in (s.settlement_sources or [])
    ]
    raw_category = getattr(s, "category", None)
    categories = [slugify(raw_category)] if raw_category else []
    raw_tags = getattr(s, "tags", None) or []
    tags = [slugify(t) if isinstance(t, str) else slugify(str(t)) for t in raw_tags]

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO series (id, exchange, ext_id, title, description, categories,
                               tags, image_url, frequency, settlement_sources,
                               contract_url, additional_prohibitions,
                               fee_type, fee_multiplier, volume_24h, total_volume,
                               is_deleted, updated_at)
            VALUES ($1::uuid, $2, $3, $4, $5, $6::text[], $7::text[], $8, $9,
                    $10::jsonb, $11, $12::jsonb,
                    $13, $14::numeric, $15::numeric, $16::numeric,
                    $17, $18)
            ON CONFLICT ON CONSTRAINT uq_series_exchange_extid DO UPDATE SET
                title = EXCLUDED.title,
                description = EXCLUDED.description,
                categories = EXCLUDED.categories,
                tags = EXCLUDED.tags,
                image_url = EXCLUDED.image_url,
                frequency = EXCLUDED.frequency,
                settlement_sources = EXCLUDED.settlement_sources,
                contract_url = EXCLUDED.contract_url,
                additional_prohibitions = EXCLUDED.additional_prohibitions,
                fee_type = EXCLUDED.fee_type,
                fee_multiplier = EXCLUDED.fee_multiplier,
                volume_24h = EXCLUDED.volume_24h,
                total_volume = EXCLUDED.total_volume,
                updated_at = EXCLUDED.updated_at
            """,
            str(uuid.uuid4()),
            EXCHANGE,
            s.ticker,
            s.title,
            getattr(s, "description", None),
            categories,
            tags,
            getattr(s, "image_url", None),
            getattr(s, "frequency", None),
            _json.dumps(settlement_sources),
            getattr(s, "contract_url", None),
            _json.dumps(getattr(s, "additional_prohibitions", None) or []),
            getattr(s, "fee_type", None),
            getattr(s, "fee_multiplier", None),
            getattr(s, "volume", None) or 0,
            getattr(s, "volume", None) or 0,
            False,
            now,
        )


async def run_kalshi_dev_sync() -> None:
    """
    DEV_MODE backfill: sync the explicit curated set of Kalshi series defined
    in dev_config.DEV_EXPLICIT_SERIES_FLAT.

    Steps:
      1. Fetch all series from Kalshi API, filter to DEV_EXPLICIT_SERIES_FLAT tickers.
      2. Upsert selected series.
      3. For each series, paginate through open events (with nested markets).
      4. Fetch event metadata (images, colors, settlement sources) for each event.
      5. Upsert events, markets, and outcomes with full display metadata.
      6. Populate dev_config.DEV_TARGET_SERIES and DEV_TARGET_MARKETS.
    """
    import app.core.dev_config as dev_cfg
    from app.core.dev_config import DEV_EXPLICIT_SERIES, DEV_EXPLICIT_SERIES_FLAT

    logger.info(
        "[kalshi.ingest] DEV_MODE: Starting explicit series sync (%d tickers across %d categories)...",
        len(DEV_EXPLICIT_SERIES_FLAT),
        len(DEV_EXPLICIT_SERIES),
    )

    pool = await get_asyncpg_pool()
    now = datetime.now(timezone.utc)

    # ---- Step 1: Fetch all series, filter to explicit list ----
    try:
        resp = await get_series_list()
    except Exception as exc:
        logger.error("[kalshi.ingest] DEV: Failed to fetch series list: %s", exc)
        return

    all_series = resp.series or []
    if not all_series:
        logger.warning("[kalshi.ingest] DEV: No series returned from Kalshi API")
        return

    explicit_set = set(DEV_EXPLICIT_SERIES_FLAT)
    selected_series = [s for s in all_series if s.ticker in explicit_set]
    found_tickers = {s.ticker for s in selected_series}
    missing = explicit_set - found_tickers
    if missing:
        logger.warning(
            "[kalshi.ingest] DEV: %d explicit series not found in Kalshi API: %s",
            len(missing), sorted(missing),
        )

    for cat, tickers in DEV_EXPLICIT_SERIES.items():
        found_in_cat = [t for t in tickers if t in found_tickers]
        logger.info(
            "[kalshi.ingest] DEV: Category '%s' — %d/%d series found: %s",
            cat, len(found_in_cat), len(tickers), found_in_cat,
        )

    # ---- Step 2: Upsert selected series + platform tags ----
    for s in selected_series:
        try:
            await _upsert_single_series(pool, s, now)
            logger.info("[kalshi.ingest] DEV: Upserted series %s (%s)", s.ticker, s.category)
        except Exception as exc:
            logger.error("[kalshi.ingest] DEV: Failed to sync series %s: %s", s.ticker, exc)

    # Pre-load series map for FK resolution
    series_map = await _load_ext_id_map(pool, "series")

    # Upsert PlatformTags for each unique category encountered in selected series
    seen_categories: set[str] = set()
    for s in selected_series:
        if s.category and s.category not in seen_categories:
            seen_categories.add(s.category)
            await upsert_platform_tag(
                pool, EXCHANGE, "category",
                slug=slugify(s.category),
                label=s.category,
            )

    # Use SearchAPI to get all tags per category, upsert each with parent_id link
    tags_by_category = await get_tags_for_series_categories()
    for category_name, tag_labels in tags_by_category.items():
        category_slug = slugify(category_name)
        for tag_label in tag_labels:
            if not tag_label:
                continue
            await upsert_platform_tag(
                pool, EXCHANGE, "tag",
                slug=slugify(tag_label),
                label=tag_label,
                parent_id=category_slug,
            )

    target_series_tickers = [s.ticker for s in selected_series]

    # ---- Step 3: Sync events + markets per series ----
    new_target_markets: list[str] = []
    total_events = 0
    total_markets = 0
    BATCH_SIZE = 200

    for series_ticker in target_series_tickers:
        logger.info("[kalshi.ingest] DEV: Fetching events for series %s", series_ticker)
        cursor = None
        page = 0

        while True:
            page += 1
            try:
                # Fetch events WITHOUT nested markets to avoid SDK validation
                # errors on closed/settled markets with null price fields.
                resp = await get_events(
                    series_ticker=series_ticker,
                    with_nested_markets=False,
                    limit=200,
                    cursor=cursor,
                    status="open",
                )
            except Exception as exc:
                logger.error("[kalshi.ingest] DEV: Failed to fetch events for %s page %d: %s", series_ticker, page, exc)
                break

            raw_events = resp.events or []
            if not raw_events:
                break

            events = [KalshiEvent(**e.to_dict()) for e in raw_events]

            # ---- Fetch event metadata for display data ----
            event_metadata_cache: dict[str, object] = {}
            market_metadata_cache: dict[str, dict] = {}

            for e in events:
                if _is_mve(e.event_ticker):
                    continue
                meta = await get_event_metadata(e.event_ticker)
                if meta:
                    event_metadata_cache[e.event_ticker] = meta
                    # Build per-market metadata lookup
                    for md in (meta.market_details or []):
                        market_metadata_cache[md.market_ticker] = {
                            "image_url": md.image_url,
                            "color_code": md.color_code,
                        }

            # Build event rows
            event_rows = []
            non_mve_events: list[KalshiEvent] = []
            for e in events:
                if _is_mve(e.event_ticker) or _is_mve(e.series_ticker):
                    continue
                series_id = series_map.get(e.series_ticker) if e.series_ticker else None
                event_meta = event_metadata_cache.get(e.event_ticker)
                event_rows.append(_build_event_row(e, series_id, now, event_meta))
                non_mve_events.append(e)

            # Upsert events (batched), get back {ext_id: id}
            page_event_map: dict[str, uuid.UUID] = {}
            for i in range(0, len(event_rows), BATCH_SIZE):
                batch = event_rows[i : i + BATCH_SIZE]
                batch_map = await _upsert_events_returning(pool, batch)
                page_event_map.update(batch_map)
            total_events += len(page_event_map)

            # Fetch markets separately per event using raw bypass to avoid SDK
            # validation errors when closed/settled markets have null price fields.
            market_rows = []
            valid_markets: list[KalshiMarket] = []
            for e in non_mve_events:
                event_id = page_event_map.get(e.event_ticker)
                if event_id is None:
                    continue
                try:
                    raw_markets, _ = await get_markets_raw(event_ticker=e.event_ticker, limit=200)
                    for m in raw_markets:
                        if _is_mve(m.ticker):
                            continue
                        market_rows.append(_build_market_row(m, event_id, now, market_metadata_cache))
                        valid_markets.append(m)
                        if m.normalized_status == "active":
                            new_target_markets.append(m.ticker)
                except Exception as mkt_exc:
                    logger.warning("[kalshi.ingest] DEV: Could not fetch markets for %s: %s", e.event_ticker, mkt_exc)

            # Upsert markets
            for i in range(0, len(market_rows), BATCH_SIZE):
                batch = market_rows[i : i + BATCH_SIZE]
                upserted = await _upsert_markets_batch(pool, batch)
                total_markets += upserted

            # Build and upsert outcomes
            if valid_markets:
                valid_tickers = [m.ticker for m in valid_markets]
                market_id_map = await _load_ext_id_map_for_keys(pool, "markets", valid_tickers)
                outcome_rows = []
                for m in valid_markets:
                    mid = market_id_map.get(m.ticker)
                    if mid:
                        outcome_rows.extend(_build_outcome_rows(str(mid), m))
                for i in range(0, len(outcome_rows), BATCH_SIZE):
                    batch = outcome_rows[i : i + BATCH_SIZE]
                    await _upsert_outcomes_batch(pool, batch)

            cursor = resp.cursor
            if not cursor:
                break

    # ---- Step 4: Populate DEV_TARGET_SERIES and DEV_TARGET_MARKETS ----
    dev_cfg.DEV_TARGET_SERIES.clear()
    dev_cfg.DEV_TARGET_SERIES.extend(target_series_tickers)
    dev_cfg.DEV_TARGET_MARKETS.clear()
    dev_cfg.DEV_TARGET_MARKETS.extend(new_target_markets)

    logger.info(
        "[kalshi.ingest] DEV_MODE sync complete: %d explicit series, "
        "%d events, %d markets upserted, %d open market tickers in subscription list",
        len(target_series_tickers),
        total_events, total_markets, len(new_target_markets),
    )


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
        # Scan for all ticker:kalshi:*-yes keys (only yes side to avoid double-counting)
        tick_keys = []
        async for key in redis.scan_iter(match="ticker:kalshi:*-yes", count=500):
            tick_keys.append(key)

        if not tick_keys:
            return

        now = datetime.now(timezone.utc)
        rows = []

        for key in tick_keys:
            data = await redis.hgetall(key)
            if not data:
                continue

            # Extract market ticker from key: ticker:kalshi:{market_ticker}-yes
            # Remove prefix "ticker:kalshi:" and suffix "-yes"
            raw = key[len("ticker:kalshi:"):]
            ticker = raw[:-len("-yes")]

            price = float(data.get("price", 0))
            best_bid = float(data.get("bid", 0))
            best_ask = float(data.get("ask", 0))
            volume = int(data.get("volume_24h", 0))

            # For 1m candles from a single snapshot: OHLC are all the same price
            rows.append((
                ticker,
                now,
                price,     # open
                price,     # high
                price,     # low
                price,     # close
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


# ---------------------------------------------------------------------------
# Event volume aggregation
# ---------------------------------------------------------------------------

async def aggregate_event_volumes(ctx: dict) -> None:
    """
    Sum volume_24h from each event's child markets (stored in platform_metadata)
    and write the total to events.volume_24h.

    Runs every 5 minutes via arq cron.
    """
    pool = ctx.get("asyncpg_pool")
    if not pool:
        pool = await get_asyncpg_pool()

    try:
        async with pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE events e
                SET volume_24h = sub.total_vol,
                    updated_at = NOW()
                FROM (
                    SELECT
                        m.event_id,
                        COALESCE(SUM((m.platform_metadata->>'volume_24h')::numeric), 0) AS total_vol
                    FROM markets m
                    WHERE m.is_deleted = FALSE
                      AND m.platform_metadata->>'volume_24h' IS NOT NULL
                    GROUP BY m.event_id
                ) sub
                WHERE e.id = sub.event_id
                  AND e.is_deleted = FALSE
            """)
        logger.info("[kalshi.ingest] Event volume aggregation complete: %s", result)
    except Exception as exc:
        logger.error("[kalshi.ingest] Event volume aggregation failed: %s", exc, exc_info=True)
