"""
Polymarket Market Data Ingestion Worker

Syncs Polymarket series, events, markets, and outcomes from the Gamma API into PostgreSQL.

Data source: https://gamma-api.polymarket.com
Auth: None required — Gamma API is public read-only

Identifier strategy (per entity):
    Series:  ext_id = Polymarket series id  (stable, used for background polling)
             platform_metadata["slug"]   = series slug  (URL-friendly for FE sharing/linking)
             platform_metadata["ticker"] = series ticker (short machine-readable key)
    Event:   ext_id = Polymarket event id   (stable Gamma event ID)
             platform_metadata["slug"]   = event slug
    Market:  ext_id = conditionId           (stable hex string)
             platform_metadata["market_slug"] = market slug / marketSlug field

Hierarchy mapping:
    Polymarket series → series  (ext_id = series id, category = JSONB list of labels)
    Polymarket event  → events  (ext_id = event id, slug in platform_metadata)
    Polymarket market → markets (ext_id = conditionId, market_slug in platform_metadata)
    Polymarket token  → market_outcomes (execution_asset_id = clobTokenId ERC-1155)

Zipping outcomes:
    Gamma API returns outcomes as a JSON-stringified array (e.g. '["Yes","No"]')
    and clobTokenIds as a JSON-stringified array (e.g. '["0x123","0x456"]').
    We zip them together to create one MarketOutcome row per (outcome, token) pair.
"""

import asyncio
import json as _json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import httpx

from app.core.config import DEV_MODE
from app.core.database import get_asyncpg_pool
from app.models.polymarket import map_polymarket_status
from app.workers.taxonomy import (
    upsert_platform_tag,
    slugify,
)

logger = logging.getLogger(__name__)

EXCHANGE = "polymarket"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"

_HTTP_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Gamma API fetch helpers
# ---------------------------------------------------------------------------

async def _fetch_series_by_slug(slug: str) -> list[dict]:
    """Fetch Polymarket series matching a slug from the Gamma /series endpoint."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{GAMMA_API_BASE}/series",
                params={"slug": slug},
            )
            if resp.status_code != 200:
                logger.warning(
                    "[polymarket.ingest] Gamma /series returned %d for slug=%s",
                    resp.status_code, slug,
                )
                return []
            data = resp.json()
            return data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    except Exception as exc:
        logger.error("[polymarket.ingest] Error fetching series slug=%s: %s", slug, exc)
        return []


async def _fetch_series_events(series_id: str) -> list[dict]:
    """Fetch events belonging to a series via the Gamma /events endpoint."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{GAMMA_API_BASE}/events",
                params={"series_id": series_id, "active": "true"},
            )
            if resp.status_code != 200:
                logger.warning(
                    "[polymarket.ingest] Gamma /events returned %d for series_id=%s",
                    resp.status_code, series_id,
                )
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as exc:
        logger.error("[polymarket.ingest] Error fetching events for series_id=%s: %s", series_id, exc)
        return []


async def _fetch_active_series(limit: int = 100, offset: int = 0) -> list[dict]:
    """Fetch a page of active Polymarket series from the Gamma API."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{GAMMA_API_BASE}/series",
                params={"active": "true", "limit": limit, "offset": offset},
            )
            if resp.status_code != 200:
                logger.warning(
                    "[polymarket.ingest] Gamma /series returned %d", resp.status_code
                )
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as exc:
        logger.error("[polymarket.ingest] Error fetching active series: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Field parsing helpers
# ---------------------------------------------------------------------------

def _parse_json_field(value) -> list:
    """Parse a field that may be a JSON string, list, or None."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = _json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def _infer_market_type(outcomes: list[str]) -> str:
    """Infer market type from the outcomes array."""
    if len(outcomes) == 2 and set(o.lower() for o in outcomes) == {"yes", "no"}:
        return "binary"
    return "categorical"


def _infer_series_format_type(series_dict: dict) -> str:
    """
    Heuristically classify a series' dominant market format.

    Returns one of: 'binary', 'categorical', 'scalar'

    Used only for DEV_MODE diversity selection — not stored in DB.

    Rules (in priority order):
      negRisk=true    → categorical  (Polymarket grouped mutual-exclusion markets)
      slug/title has price/range keywords → scalar  (crypto price ladders, score ranges)
      culture/award/nba/nfl in category   → categorical  (who wins the trophy/award)
      default                             → binary  (yes/no question)
    """
    if series_dict.get("negRisk") or series_dict.get("neg_risk"):
        return "categorical"

    slug = (series_dict.get("slug") or "").lower()
    title = (series_dict.get("title") or "").lower()

    scalar_keywords = ("price", "hit", "reach", "above", "below", "range", "level", "high", "low")
    if any(kw in slug or kw in title for kw in scalar_keywords):
        return "scalar"

    cats_lower = [c.lower() for c in _extract_category_labels(series_dict)]
    categorical_cat_hints = {"culture", "entertainment", "music", "tv", "film", "award", "nba", "nfl", "sports"}
    if any(any(h in cat for h in categorical_cat_hints) for cat in cats_lower):
        return "categorical"

    return "binary"


def _select_diverse_series(
    series_page: list[dict],
    max_total: int = 20,
    max_per_bucket: int = 2,
) -> list[dict]:
    """
    Select a diverse subset of series across categories and market format types.

    Groups series into (primary_category_slug, format_type) buckets, then does
    max_per_bucket rounds of round-robin selection across all buckets — picking
    one from each bucket per round — until max_total series are collected.

    This guarantees representation across both category and format dimensions
    without over-indexing on any single bucket.
    """
    from collections import defaultdict

    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in series_page:
        cats = _extract_category_labels(s)
        primary_cat = slugify(cats[0]) if cats else "other"
        fmt = _infer_series_format_type(s)
        buckets[(primary_cat, fmt)].append(s)

    # Sort within each bucket by 24h volume descending so round-robin always
    # picks the highest-volume series per (category, format) slot first.
    for bk in buckets:
        buckets[bk].sort(
            key=lambda s: float(s.get("volume24hr") or s.get("volume") or 0),
            reverse=True,
        )

    selected: list[dict] = []
    bucket_counters: dict[tuple[str, str], int] = defaultdict(int)
    # Sort buckets for deterministic ordering (category asc, then format asc)
    bucket_keys = sorted(buckets.keys())

    for _round in range(max_per_bucket):
        for bk in bucket_keys:
            idx = bucket_counters[bk]
            items = buckets[bk]
            if idx < len(items):
                selected.append(items[idx])
                bucket_counters[bk] += 1
                if len(selected) >= max_total:
                    return selected

    return selected


def _infer_side(outcome_label: str) -> str:
    """Map outcome label to trade_side enum value."""
    label = outcome_label.lower().strip()
    if label == "yes":
        return "yes"
    if label == "no":
        return "no"
    return "other"


def _extract_category_labels(series_dict: dict) -> list[str]:
    """
    Extract category label strings from a series dict.

    Handles both:
    - categories as list of dicts: [{"id": 1, "label": "Politics"}, ...]
    - categories as list of strings: ["Politics", ...]
    """
    categories_raw = series_dict.get("categories") or []
    labels = []
    for cat in categories_raw:
        if isinstance(cat, dict):
            label = cat.get("label") or str(cat.get("id", ""))
        else:
            label = str(cat)
        if label:
            labels.append(label)
    return labels


# ---------------------------------------------------------------------------
# Series upsert
# ---------------------------------------------------------------------------

async def _upsert_series(pool, series_dict: dict) -> Optional[uuid.UUID]:
    """
    Upsert a Polymarket series row.

    Identifier strategy:
      - ext_id = series id (stable, used for background polling by ingest workers)
      - platform_metadata["slug"]   = slug (URL-friendly, for FE sharing/linking)
      - platform_metadata["ticker"] = ticker (short key)
      - category = JSONB list of category labels (e.g. ["Politics", "Elections"])
    """
    series_id_str = str(series_dict.get("id", "")).strip()
    if not series_id_str:
        logger.warning("[polymarket.ingest] Skipping series with no id: %s", series_dict.get("title"))
        return None

    slug = series_dict.get("slug") or ""
    ticker = series_dict.get("ticker") or ""
    title = series_dict.get("title") or slug or series_id_str
    description = series_dict.get("description")
    image_url = series_dict.get("image") or series_dict.get("icon")
    frequency = series_dict.get("recurrence")

    # categories: ARRAY(text) — slug strings from API category labels
    category_labels = _extract_category_labels(series_dict)
    categories = [slugify(label) for label in category_labels if label]

    # tags: ARRAY(text) — slug strings from API tag list
    tags_raw = series_dict.get("tags") or []
    tags = []
    for t in tags_raw:
        if isinstance(t, dict):
            tag_slug = t.get("slug") or slugify(t.get("label") or str(t.get("id", "")))
        else:
            tag_slug = slugify(str(t)) if t else ""
        if tag_slug:
            tags.append(tag_slug)

    # Volume metrics
    volume_24h = Decimal(str(series_dict.get("volume24hr") or 0))
    total_volume = Decimal(str(series_dict.get("volume") or 0))

    # platform_metadata stores slug + ticker for FE sharing/linking
    platform_metadata = _json.dumps({
        "slug": slug,
        "ticker": ticker,
    })

    now = datetime.now(timezone.utc)
    internal_id = str(uuid.uuid4())

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO series (
                    id, exchange, ext_id, title, description,
                    categories, tags, image_url, frequency,
                    volume_24h, total_volume,
                    platform_metadata, is_deleted, updated_at
                )
                VALUES (
                    $1::uuid, $2::exchange_type, $3, $4, $5,
                    $6::text[], $7::text[], $8, $9,
                    $10::numeric, $11::numeric,
                    $12::jsonb, FALSE, $13
                )
                ON CONFLICT ON CONSTRAINT uq_series_exchange_extid DO UPDATE SET
                    title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    categories = EXCLUDED.categories,
                    tags = EXCLUDED.tags,
                    image_url = COALESCE(EXCLUDED.image_url, series.image_url),
                    frequency = EXCLUDED.frequency,
                    volume_24h = EXCLUDED.volume_24h,
                    total_volume = EXCLUDED.total_volume,
                    platform_metadata = EXCLUDED.platform_metadata,
                    updated_at = EXCLUDED.updated_at
                RETURNING id
                """,
                internal_id,
                EXCHANGE,
                series_id_str,          # ext_id = stable Polymarket series id
                title,
                description,
                categories,             # categories ARRAY(text)
                tags,                   # tags ARRAY(text)
                image_url,
                frequency,
                volume_24h,
                total_volume,
                platform_metadata,
                now,
            )
        db_series_id = uuid.UUID(str(row["id"]))
    except Exception as exc:
        logger.error("[polymarket.ingest] Failed to upsert series id=%s: %s", series_id_str, exc)
        return None

    # Upsert PlatformTags for each category
    for label in category_labels:
        if label:
            await upsert_platform_tag(
                pool, EXCHANGE, "category",
                slug=slugify(label),
                label=label,
            )

    return db_series_id


# ---------------------------------------------------------------------------
# Event upsert
# ---------------------------------------------------------------------------

async def _upsert_event(
    pool,
    event_dict: dict,
    series_id: Optional[uuid.UUID],
) -> Optional[uuid.UUID]:
    """
    Upsert a Polymarket event row.

    Identifier strategy:
      - ext_id = event id  (stable Gamma event ID for background polling)
      - platform_metadata["slug"] = slug (URL-friendly, for FE sharing/linking)
    """
    ext_id = str(event_dict.get("id", "")).strip()
    if not ext_id:
        logger.warning("[polymarket.ingest] Skipping event with no id: %s", event_dict.get("title"))
        return None

    slug = event_dict.get("slug") or ""
    title = event_dict.get("title") or event_dict.get("question") or ext_id
    description = event_dict.get("description")
    image_url = event_dict.get("image") or event_dict.get("featuredImage")

    active = event_dict.get("active", True)
    closed = event_dict.get("closed", False)
    archived = event_dict.get("archived", False)
    if closed or archived:
        raw_status = "closed"
    elif active:
        raw_status = "open"
    else:
        raw_status = "closed"
    status = map_polymarket_status(raw_status)

    end_date_str = (
        event_dict.get("endDate")
        or event_dict.get("end_date")
        or event_dict.get("endDateIso")
    )
    close_time: Optional[datetime] = None
    if end_date_str:
        try:
            close_time = datetime.fromisoformat(
                str(end_date_str).rstrip("Z")
            ).replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            pass

    # Prefer volume24hr (rolling 24h) over volume (all-time total).
    # Gamma events expose both; the reconciliation cron keeps this fresh.
    vol_raw = event_dict.get("volume24hr") or event_dict.get("volume") or 0
    volume_24h = Decimal(str(vol_raw)) if vol_raw else Decimal(0)

    neg_risk = event_dict.get("negRisk") or event_dict.get("neg_risk") or False
    platform_metadata = _json.dumps({
        "slug": slug,           # FE sharing/linking identifier
        "neg_risk": neg_risk,
    })

    # series_ids: ARRAY(uuid) — include the parent series if provided
    series_ids: list[uuid.UUID] = [series_id] if series_id else []

    # categories: ARRAY(text) — combine event category + subcategory as slugs.
    # Polymarket's category/subcategory fields are often null; fall back to using
    # the first tag slug as the category so events remain filterable.
    raw_category = event_dict.get("category")
    raw_subcategory = event_dict.get("subcategory") or event_dict.get("subCategory")
    categories: list[str] = []
    if raw_category:
        categories.append(slugify(raw_category))
    if raw_subcategory and slugify(raw_subcategory) not in categories:
        categories.append(slugify(raw_subcategory))

    # tags: ARRAY(text) — extract slug strings from tag objects
    tags_raw = event_dict.get("tags") or []
    tags: list[str] = []
    for tag_dict in tags_raw:
        if isinstance(tag_dict, dict):
            tag_slug = tag_dict.get("slug") or ""
            if not tag_slug:
                tag_label = tag_dict.get("label") or ""
                tag_slug = slugify(tag_label) if tag_label else ""
            if tag_slug:
                tags.append(tag_slug)
        elif isinstance(tag_dict, str) and tag_dict:
            tags.append(slugify(tag_dict))

    # If category/subcategory both null (common on Polymarket), derive categories
    # from tags so events are still categorizable. Use first tag as primary category.
    if not categories and tags:
        categories = tags[:1]

    internal_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO events (
                    id, series_ids, exchange, ext_id, title, description,
                    categories, tags, status, close_time, volume_24h, image_url,
                    platform_metadata, is_deleted, updated_at
                )
                VALUES (
                    $1::uuid, $2::uuid[], $3::exchange_type, $4, $5, $6,
                    $7::text[], $8::text[], $9::market_status, $10, $11::numeric, $12,
                    $13::jsonb, FALSE, $14
                )
                ON CONFLICT ON CONSTRAINT uq_events_exchange_extid DO UPDATE SET
                    series_ids = EXCLUDED.series_ids,
                    title = EXCLUDED.title,
                    description = EXCLUDED.description,
                    categories = EXCLUDED.categories,
                    tags = EXCLUDED.tags,
                    status = EXCLUDED.status,
                    close_time = EXCLUDED.close_time,
                    volume_24h = EXCLUDED.volume_24h,
                    image_url = COALESCE(EXCLUDED.image_url, events.image_url),
                    platform_metadata = EXCLUDED.platform_metadata,
                    updated_at = EXCLUDED.updated_at
                RETURNING id
                """,
                internal_id,
                series_ids,             # series_ids ARRAY(uuid)
                EXCHANGE,
                ext_id,                 # ext_id = stable Polymarket event id
                title,
                description,
                categories,             # categories ARRAY(text)
                tags,                   # tags ARRAY(text)
                status,
                close_time,
                volume_24h,
                image_url,
                platform_metadata,
                now,
            )
        db_event_id = uuid.UUID(str(row["id"]))
    except Exception as exc:
        logger.error("[polymarket.ingest] Failed to upsert event id=%s: %s", ext_id, exc)
        return None

    # Upsert PlatformTags for categories.
    # Top-level category has no parent; subcategory links to parent via parent_ids.
    if raw_category:
        await upsert_platform_tag(
            pool, EXCHANGE, "category",
            slug=slugify(raw_category),
            label=raw_category,
        )
    if raw_subcategory:
        await upsert_platform_tag(
            pool, EXCHANGE, "category",
            slug=slugify(raw_subcategory),
            label=raw_subcategory,
            parent_ids=[slugify(raw_category)] if raw_category else [],
        )

    # Upsert PlatformTags for tags
    for tag_dict in tags_raw:
        if isinstance(tag_dict, dict):
            tag_slug = tag_dict.get("slug") or ""
            tag_label = tag_dict.get("label") or ""
            tag_ext_id = str(tag_dict.get("id", "")) if tag_dict.get("id") else tag_slug
            if tag_slug and tag_label:
                await upsert_platform_tag(
                    pool, EXCHANGE, "tag",
                    slug=tag_slug,
                    label=tag_label,
                    ext_id=tag_ext_id,
                )

    return db_event_id


# ---------------------------------------------------------------------------
# Market + outcomes upsert
# ---------------------------------------------------------------------------

async def _upsert_market_with_outcomes(
    pool,
    market_dict: dict,
    event_id: uuid.UUID,
) -> list[str]:
    """
    Upsert a Polymarket market and all its outcome token rows.

    Identifier strategy:
      - ext_id = conditionId (stable hex string for background polling)
      - platform_metadata["market_slug"] = marketSlug (URL-friendly, for FE sharing/linking)
      - platform_metadata["question_id"] = questionId
      - platform_metadata["clob_token_ids"] = clobTokenIds array

    Returns clobTokenIds for the WebSocket subscription list.
    """
    condition_id = (
        market_dict.get("conditionId")
        or market_dict.get("condition_id")
        or ""
    ).strip()
    if not condition_id:
        logger.warning(
            "[polymarket.ingest] Skipping market with no conditionId: %s",
            market_dict.get("question"),
        )
        return []

    market_slug = (
        market_dict.get("marketSlug")
        or market_dict.get("market_slug")
        or market_dict.get("slug")
        or ""
    )
    question = market_dict.get("question") or condition_id
    description = market_dict.get("description")

    active = market_dict.get("active", True)
    closed = market_dict.get("closed", False)
    archived = market_dict.get("archived", False)
    if closed or archived:
        raw_status = "closed"
    elif active:
        raw_status = "open"
    else:
        raw_status = "closed"
    status = map_polymarket_status(raw_status)

    outcomes_raw = _parse_json_field(market_dict.get("outcomes"))
    token_ids_raw = _parse_json_field(
        market_dict.get("clobTokenIds") or market_dict.get("clob_token_ids")
    )
    market_type = _infer_market_type(outcomes_raw)

    end_date_str = market_dict.get("endDateIso") or market_dict.get("end_date_iso")
    close_time: Optional[datetime] = None
    if end_date_str:
        try:
            close_time = datetime.fromisoformat(
                str(end_date_str).rstrip("Z")
            ).replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            pass

    volume = market_dict.get("volume")
    volume_val = Decimal(str(volume)) if volume else Decimal(0)
    liquidity = market_dict.get("liquidity")
    liquidity_val = Decimal(str(liquidity)) if liquidity else Decimal(0)

    neg_risk = market_dict.get("negRisk") or market_dict.get("neg_risk") or False
    platform_metadata = _json.dumps({
        "market_slug": market_slug,         # FE sharing/linking identifier
        "neg_risk": neg_risk,
        "neg_risk_market_id": market_dict.get("negRiskMarketId") or market_dict.get("neg_risk_market_id"),
        "question_id": market_dict.get("questionId") or market_dict.get("question_id"),
        "clob_token_ids": token_ids_raw,    # Full token list for WS subscription rebuild
    })

    internal_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO markets (
                    id, event_id, exchange, ext_id, title, subtitle,
                    type, status, close_time,
                    volume, volume_24h, total_volume, liquidity,
                    platform_metadata, is_deleted, updated_at
                )
                VALUES (
                    $1::uuid, $2::uuid, $3::exchange_type, $4, $5, $6,
                    $7::market_type, $8::market_status, $9,
                    $10::numeric, $10::numeric, $10::numeric, $11::numeric,
                    $12::jsonb, FALSE, $13
                )
                ON CONFLICT ON CONSTRAINT uq_markets_exchange_extid DO UPDATE SET
                    title = EXCLUDED.title,
                    type = EXCLUDED.type,
                    status = EXCLUDED.status,
                    close_time = EXCLUDED.close_time,
                    volume = EXCLUDED.volume,
                    volume_24h = EXCLUDED.volume_24h,
                    total_volume = EXCLUDED.total_volume,
                    liquidity = EXCLUDED.liquidity,
                    platform_metadata = EXCLUDED.platform_metadata,
                    updated_at = EXCLUDED.updated_at
                RETURNING id
                """,
                internal_id,
                str(event_id),
                EXCHANGE,
                condition_id,       # ext_id = stable conditionId
                question,
                description,
                market_type,
                status,
                close_time,
                volume_val,
                liquidity_val,
                platform_metadata,
                now,
            )
        db_market_id = str(row["id"])
    except Exception as exc:
        logger.error(
            "[polymarket.ingest] Failed to upsert market conditionId=%s: %s",
            condition_id, exc,
        )
        return []

    # Upsert outcomes — zip outcomes array with clobTokenIds array
    token_ids_collected: list[str] = []
    outcome_rows: list[tuple] = []

    for outcome_label, token_id in zip(outcomes_raw, token_ids_raw):
        side = _infer_side(str(outcome_label))
        outcome_rows.append((
            str(uuid.uuid4()),      # id
            db_market_id,           # market_id
            str(token_id),          # execution_asset_id (ERC-1155 token ID)
            str(outcome_label),     # title
            side,                   # side
            None,                   # is_winner
            _json.dumps({}),        # platform_metadata
        ))
        token_ids_collected.append(str(token_id))

    if outcome_rows:
        try:
            async with pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO market_outcomes
                        (id, market_id, execution_asset_id, title, side, is_winner, platform_metadata)
                    VALUES ($1::uuid, $2::uuid, $3, $4, $5::trade_side, $6, $7::jsonb)
                    ON CONFLICT ON CONSTRAINT uq_market_outcomes_market_asset DO UPDATE SET
                        title = EXCLUDED.title,
                        is_winner = COALESCE(EXCLUDED.is_winner, market_outcomes.is_winner)
                    """,
                    outcome_rows,
                )
        except Exception as exc:
            logger.error(
                "[polymarket.ingest] Failed to upsert outcomes for conditionId=%s: %s",
                condition_id, exc,
            )

    return token_ids_collected


# ---------------------------------------------------------------------------
# Tag taxonomy fetch helpers
# ---------------------------------------------------------------------------

async def _fetch_all_tags(page_size: int = 100) -> list[dict]:
    """Fetch all Polymarket tags from GET /tags with pagination."""
    all_tags: list[dict] = []
    offset = 0
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            while True:
                resp = await client.get(
                    f"{GAMMA_API_BASE}/tags",
                    params={"limit": page_size, "offset": offset},
                )
                if resp.status_code != 200:
                    logger.warning(
                        "[polymarket.ingest] GET /tags returned %d at offset=%d",
                        resp.status_code, offset,
                    )
                    break
                data = resp.json()
                if not isinstance(data, list) or not data:
                    break
                all_tags.extend(data)
                if len(data) < page_size:
                    break
                offset += page_size
    except Exception as exc:
        logger.error("[polymarket.ingest] Error fetching /tags: %s", exc)
    return all_tags


async def _fetch_tag_children(tag_id: str) -> list[dict]:
    """
    Fetch child tags for a given parent tag ID from GET /tags/{id}/related-tags/tags.
    Retries up to 6 times with exponential backoff on HTTP 429 (rate-limited).
    Backoff sequence: 4s → 8s → 16s → 32s → 64s (capped at 60s).
    """
    backoff = 5.0
    max_retries = 6
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.get(
                    f"{GAMMA_API_BASE}/tags/{tag_id}/related-tags/tags"
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return data if isinstance(data, list) else []
                elif resp.status_code == 429:
                    if attempt < max_retries - 1:
                        wait = min(backoff, 60.0)
                        logger.warning(
                            "[polymarket.ingest] 429 for tag_id=%s attempt=%d/%d — waiting %.0fs",
                            tag_id, attempt + 1, max_retries, wait,
                        )
                        await asyncio.sleep(wait)
                        backoff *= 2.0
                    # else fall through to final return below
                else:
                    return []
        except Exception as exc:
            logger.warning(
                "[polymarket.ingest] tag children fetch error tag_id=%s: %s", tag_id, exc
            )
            return []
    logger.warning(
        "[polymarket.ingest] Rate-limited for tag_id=%s after %d attempts — skipping",
        tag_id, max_retries,
    )
    return []


async def _fetch_sports() -> list[dict]:
    """Fetch sports metadata from GET /sports."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{GAMMA_API_BASE}/sports")
            if resp.status_code != 200:
                logger.warning(
                    "[polymarket.ingest] GET /sports returned %d", resp.status_code
                )
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as exc:
        logger.error("[polymarket.ingest] Error fetching /sports: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Tag taxonomy upserts
# ---------------------------------------------------------------------------

async def _upsert_polymarket_tags(pool, tags: list[dict]) -> int:
    """
    Upsert Polymarket Gamma API tags into platform_tags (exchange='polymarket', type='tag').

    Each dict must have keys: id, label, slug, is_carousel, force_show,
    force_hide, parent_ids.

    Uses (exchange, slug, type) unique constraint. Tags with no slug fall back
    to slugify(label). Original Polymarket tag ID stored in platform_metadata.
    """
    if not tags:
        return 0

    count = 0
    for t in tags:
        tag_id = t.get("id")
        if not tag_id:
            continue
        label = t.get("label") or ""
        raw_slug = t.get("slug") or ""
        slug = raw_slug.strip() or slugify(label) if label else ""
        if not slug or not label:
            continue
        await upsert_platform_tag(
            pool,
            exchange=EXCHANGE,
            tag_type="tag",
            slug=slug,
            label=label,
            ext_id=str(tag_id),
            parent_ids=t.get("parent_ids", []),
            is_carousel=bool(t.get("is_carousel", False)),
            force_show=bool(t.get("force_show", False)),
            force_hide=bool(t.get("force_hide", False)),
            platform_metadata={"polymarket_id": str(tag_id)},
        )
        count += 1
    return count


async def _upsert_polymarket_sports(pool, sports: list[dict]) -> int:
    """
    Upsert sports metadata into sports_metadata (exchange='polymarket').

    The API 'tags' field is a comma-separated string of tag IDs — split into array.
    Uses (exchange, sport) unique constraint.
    """
    if not sports:
        return 0
    rows = []
    for sport in sports:
        sport_name = str(sport.get("sport") or sport.get("name") or "").strip()
        if not sport_name:
            continue
        tags_raw = sport.get("tags") or ""
        if isinstance(tags_raw, str):
            tag_ids = [t.strip() for t in tags_raw.split(",") if t.strip()]
        elif isinstance(tags_raw, list):
            tag_ids = [str(t).strip() for t in tags_raw if str(t).strip()]
        else:
            tag_ids = []
        rows.append((
            str(uuid.uuid4()),
            EXCHANGE,
            sport_name,
            sport.get("series") or sport.get("seriesSlug"),
            sport.get("resolutionOracleURI") or sport.get("resolution_oracle_uri"),
            tag_ids,
        ))
    if not rows:
        return 0
    try:
        async with pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO sports_metadata
                    (id, exchange, sport, series_slug, resolution_url, tag_ids, updated_at)
                VALUES ($1::uuid, $2::exchange_type, $3, $4, $5, $6::text[], NOW())
                ON CONFLICT ON CONSTRAINT uq_sports_metadata_exchange_sport DO UPDATE SET
                    series_slug = EXCLUDED.series_slug,
                    resolution_url = EXCLUDED.resolution_url,
                    tag_ids = EXCLUDED.tag_ids,
                    updated_at = NOW()
                """,
                rows,
            )
    except Exception as exc:
        logger.error(
            "[polymarket.ingest] Failed to upsert sports_metadata: %s", exc
        )
        return 0
    return len(rows)


async def run_polymarket_tag_sync(pool) -> dict:
    """
    Sync the complete Polymarket tag taxonomy with parent-child relationships.

    Steps:
        1. Fetch all tags from GET /tags (paginated)
        2. For each top-level tag, fetch children from GET /tags/{id}/related-tags/tags
           (concurrent, semaphore-limited to avoid API overload)
        3. Accumulate parent_ids for each child tag
        4. Upsert all tags into polymarket_tags (GIN-indexed parent_ids array)
        5. Fetch and upsert sports metadata from GET /sports

    Returns a stats dict.
    """
    logger.info("[polymarket.ingest] Starting tag taxonomy sync...")

    # Step 1: Fetch all top-level tags
    raw_tags = await _fetch_all_tags()
    logger.info("[polymarket.ingest] Fetched %d tags from /tags", len(raw_tags))

    # Build initial tag map: {id_str → tag_data_dict}
    tags_map: dict[str, dict] = {}
    for tag in raw_tags:
        tag_id = str(tag.get("id", "")).strip()
        if not tag_id:
            continue
        tags_map[tag_id] = {
            "id": tag_id,
            "label": tag.get("label"),
            "slug": tag.get("slug"),
            "is_carousel": bool(tag.get("isCarousel", False)),
            "force_show": bool(tag.get("forceShow", False)),
            "force_hide": bool(tag.get("forceHide", False)),
            "parent_ids": [],
        }

    # Step 2: Fetch children for each top-level tag (concurrently, 3 at a time).
    # The Gamma API rate-limits aggressively; _fetch_tag_children already retries on
    # 429 with backoff, so keeping concurrency low avoids wasteful retry storms.
    sem = asyncio.Semaphore(3)

    async def _fetch_children_guarded(parent_id: str):
        async with sem:
            children = await _fetch_tag_children(parent_id)
            return parent_id, children

    parent_ids = list(tags_map.keys())
    results = await asyncio.gather(
        *[_fetch_children_guarded(pid) for pid in parent_ids],
        return_exceptions=True,
    )

    for result in results:
        if isinstance(result, Exception):
            logger.warning("[polymarket.ingest] Tag children fetch error: %s", result)
            continue
        parent_id, children = result
        for child in children:
            child_id = str(child.get("id", "")).strip()
            if not child_id:
                continue
            if child_id not in tags_map:
                tags_map[child_id] = {
                    "id": child_id,
                    "label": child.get("label"),
                    "slug": child.get("slug"),
                    "is_carousel": bool(child.get("isCarousel", False)),
                    "force_show": bool(child.get("forceShow", False)),
                    "force_hide": bool(child.get("forceHide", False)),
                    "parent_ids": [],
                }
            if parent_id not in tags_map[child_id]["parent_ids"]:
                tags_map[child_id]["parent_ids"].append(parent_id)

    # Step 3: Upsert all tags
    tag_count = await _upsert_polymarket_tags(pool, list(tags_map.values()))
    logger.info("[polymarket.ingest] Upserted %d tags into platform_tags (polymarket)", tag_count)

    # Step 4: Fetch and upsert sports metadata
    sports = await _fetch_sports()
    sport_count = await _upsert_polymarket_sports(pool, sports)
    logger.info(
        "[polymarket.ingest] Upserted %d rows into sports_metadata (polymarket)", sport_count
    )

    return {"tags": tag_count, "sports": sport_count}


# ---------------------------------------------------------------------------
# Process a full series dict (series + its events + their markets)
# ---------------------------------------------------------------------------

async def _process_series(pool, series_dict: dict) -> list[str]:
    """
    Upsert one Polymarket series and all its nested events/markets/outcomes.
    Returns the list of collected clobTokenIds for WS subscription.
    """
    series_id_str = str(series_dict.get("id", "")).strip()
    slug = series_dict.get("slug", "")

    # 1. Upsert the series
    db_series_id = await _upsert_series(pool, series_dict)
    if db_series_id is None:
        return []

    all_token_ids: list[str] = []
    event_categories_seen: set[str] = set()

    # 2. Get events — prefer nested array, fall back to separate API call
    events = series_dict.get("events") or []
    if not events and series_id_str:
        events = await _fetch_series_events(series_id_str)

    # In DEV_MODE, cap at 4 events per series to avoid long sync times
    if DEV_MODE and len(events) > 4:
        events = events[:4]
        logger.info(
            "[polymarket.ingest] DEV_MODE: capped to 4 events for series id=%s slug=%s",
            series_id_str, slug,
        )

    # 3. Upsert each event and its markets
    for event_dict in events:
        # Events embedded in a series response may be minimal — enrich if needed
        if not event_dict.get("markets"):
            # Try to get full event data including markets
            event_id_str = str(event_dict.get("id", ""))
            if event_id_str:
                enriched = await _fetch_event_by_id(event_id_str)
                if enriched:
                    event_dict = enriched

        db_event_id = await _upsert_event(pool, event_dict, db_series_id)
        if db_event_id is None:
            continue

        # Collect categories from events to back-fill series categories
        raw_cat = event_dict.get("category")
        raw_subcat = event_dict.get("subcategory") or event_dict.get("subCategory")
        event_tags = [
            (t.get("slug") or slugify(t.get("label") or ""))
            for t in (event_dict.get("tags") or [])
            if isinstance(t, dict) and (t.get("slug") or t.get("label"))
        ]
        if raw_cat:
            event_categories_seen.add(slugify(raw_cat))
        elif raw_subcat:
            event_categories_seen.add(slugify(raw_subcat))
        elif event_tags:
            event_categories_seen.add(event_tags[0])

        markets = event_dict.get("markets") or []
        for market_dict in markets:
            token_ids = await _upsert_market_with_outcomes(pool, market_dict, db_event_id)
            all_token_ids.extend(token_ids)

    # Back-fill series categories from event categories if series has none
    if event_categories_seen:
        derived_categories = sorted(event_categories_seen)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE series
                    SET categories = $1::text[], updated_at = NOW()
                    WHERE id = $2::uuid
                      AND (categories IS NULL OR array_length(categories, 1) IS NULL)
                    """,
                    derived_categories, str(db_series_id),
                )
        except Exception as exc:
            logger.warning("[polymarket.ingest] Could not back-fill series categories: %s", exc)

    logger.info(
        "[polymarket.ingest] Processed series id=%s slug=%s events=%d tokens=%d",
        series_id_str, slug, len(events), len(all_token_ids),
    )
    return all_token_ids


async def _fetch_event_by_id(event_id: str) -> Optional[dict]:
    """Fetch a single Polymarket event by its Gamma id (with full market data)."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{GAMMA_API_BASE}/events/{event_id}")
            if resp.status_code == 200:
                return resp.json()
    except Exception as exc:
        logger.warning("[polymarket.ingest] Could not enrich event id=%s: %s", event_id, exc)
    return None


# ---------------------------------------------------------------------------
# DB helper functions (used by main.py at startup)
# ---------------------------------------------------------------------------

async def load_polymarket_token_ids_from_db() -> list[str]:
    """
    Load all known Polymarket execution_asset_ids (clobTokenIds) from the DB.

    Used in production startup so the WebSocket can subscribe immediately
    without waiting for the first arq full sync cycle to complete.
    """
    pool = await get_asyncpg_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT mo.execution_asset_id
                FROM market_outcomes mo
                JOIN markets m ON mo.market_id = m.id
                WHERE m.exchange = 'polymarket'
                  AND m.is_deleted = FALSE
                  AND m.status NOT IN ('resolved', 'canceled')
                ORDER BY mo.execution_asset_id
                """,
            )
        return [r["execution_asset_id"] for r in rows]
    except Exception as exc:
        logger.error("[polymarket.ingest] Failed to load token IDs from DB: %s", exc)
        return []


async def build_token_event_map() -> dict[str, str]:
    """
    Build a {token_id → event_ext_id} mapping from the DB.

    Used by the stream worker to map CLOB WebSocket token IDs back to their parent
    Polymarket event for Redis ZSET trending ZADD calls.
    """
    pool = await get_asyncpg_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT mo.execution_asset_id, e.ext_id AS event_ext_id
                FROM market_outcomes mo
                JOIN markets m ON mo.market_id = m.id
                JOIN events e ON m.event_id = e.id
                WHERE m.exchange = 'polymarket' AND m.is_deleted = FALSE
                """,
            )
        return {r["execution_asset_id"]: r["event_ext_id"] for r in rows}
    except Exception as exc:
        logger.error("[polymarket.ingest] Failed to build token→event map: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# State reconciliation (periodic delta sync for market status / resolution)
# ---------------------------------------------------------------------------

async def run_polymarket_state_reconciliation() -> dict:
    """
    Delta sync: check Polymarket markets for status/resolution changes AND refresh volume.

    Queries the Gamma API for all active markets and:
      1. Updates status/result in DB for resolved/closed markets.
      2. Refreshes volume_24h in DB (markets table) from Gamma API market.volume24hr.
      3. Writes volume_24h to Redis HSET for each token so the frontend sees live volume.
      4. Updates events_trending_24h_polymarket ZSET (summed per event for trending endpoint).

    The CLOB WebSocket does NOT transmit volume — this cron is the authoritative
    source of volume data for Redis. Runs every 2 min (DEV) / 2 min (prod).

    Returns a stats dict: {"checked": N, "updated": N, "volume_refreshed": N}.
    """
    logger.info("[polymarket.ingest] Starting state reconciliation...")
    pool = await get_asyncpg_pool()
    from app.core.redis import get_redis
    from app.core.market_cache import _make_key
    redis_conn = await get_redis()
    checked = 0
    updated = 0
    volume_refreshed = 0

    try:
        # Pull all non-resolved polymarket markets from our DB
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT m.id, m.ext_id, m.status, e.ext_id AS event_ext_id
                FROM markets m
                JOIN events e ON m.event_id = e.id
                WHERE m.exchange = 'polymarket'
                  AND m.is_deleted = FALSE
                  AND m.status NOT IN ('resolved', 'canceled')
                ORDER BY m.updated_at ASC
                LIMIT 200
                """
            )

        if not rows:
            logger.info("[polymarket.ingest] State reconciliation: no active markets found.")
            return {"checked": 0, "updated": 0}

        # Batch conditionIds into Gamma API requests (max 20 per call)
        condition_ids = [r["ext_id"] for r in rows]
        checked = len(condition_ids)
        db_status_map = {r["ext_id"]: r["status"] for r in rows}

        batch_size = 20
        updates: list[tuple] = []  # (condition_id, new_status, result)
        # Volume data collected from Gamma API response for Redis + DB refresh
        # Maps condition_id → (volume_24h_float, [token_id, ...])
        volume_data: dict[str, tuple[float, list[str]]] = {}
        # event_ext_id → summed volume (for trending ZSET)
        event_volumes: dict[str, float] = {}
        event_ext_id_map = {r["ext_id"]: r["event_ext_id"] for r in rows}

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            for i in range(0, len(condition_ids), batch_size):
                batch = condition_ids[i : i + batch_size]
                # Gamma API accepts multiple conditionIds via repeated param
                try:
                    resp = await client.get(
                        f"{GAMMA_API_BASE}/markets",
                        params=[("conditionId", cid) for cid in batch],
                    )
                    if resp.status_code != 200:
                        logger.warning(
                            "[polymarket.ingest] Gamma /markets batch returned %d",
                            resp.status_code,
                        )
                        continue
                    markets = resp.json()
                    if not isinstance(markets, list):
                        continue

                    for mkt in markets:
                        condition_id = mkt.get("conditionId") or mkt.get("condition_id") or ""
                        if not condition_id or condition_id not in db_status_map:
                            continue

                        active = mkt.get("active", True)
                        closed = mkt.get("closed", False)
                        archived = mkt.get("archived", False)

                        if closed or archived:
                            raw_status = "closed"
                        elif active:
                            raw_status = "open"
                        else:
                            raw_status = "closed"

                        new_status = map_polymarket_status(raw_status)

                        # Detect resolution: check if any token is a winner
                        tokens = mkt.get("tokens") or []
                        winning_outcome = next(
                            (t.get("outcome") for t in tokens if t.get("winner")),
                            None,
                        )
                        if winning_outcome:
                            new_status = "resolved"

                        if new_status != db_status_map.get(condition_id):
                            updates.append((condition_id, new_status, winning_outcome))

                        # --- Volume refresh ---
                        # Gamma API provides volume24hr (rolling 24h) and volume (total).
                        # We use volume24hr as the primary display metric.
                        vol24 = float(mkt.get("volume24hr") or mkt.get("volume") or 0)
                        token_ids_raw = (
                            mkt.get("clobTokenIds") or mkt.get("clob_token_ids") or []
                        )
                        if isinstance(token_ids_raw, str):
                            try:
                                token_ids_raw = _json.loads(token_ids_raw)
                            except Exception:
                                token_ids_raw = []
                        token_ids_list = [str(t) for t in token_ids_raw if t]
                        volume_data[condition_id] = (vol24, token_ids_list)

                        # Accumulate event-level volume for trending ZSET
                        event_ext_id = event_ext_id_map.get(condition_id)
                        if event_ext_id and vol24 > 0:
                            event_volumes[event_ext_id] = (
                                event_volumes.get(event_ext_id, 0.0) + vol24
                            )

                except Exception as exc:
                    logger.warning(
                        "[polymarket.ingest] Batch reconciliation error: %s", exc
                    )
                    continue

        # Apply status/resolution updates to DB
        if updates:
            now = datetime.now(timezone.utc)
            async with pool.acquire() as conn:
                await conn.executemany(
                    """
                    UPDATE markets
                    SET status = $2::market_status,
                        result = $3,
                        updated_at = $4
                    WHERE exchange = 'polymarket'
                      AND ext_id = $1
                      AND is_deleted = FALSE
                    """,
                    [(cid, status, result, now) for cid, status, result in updates],
                )
            updated = len(updates)
            logger.info(
                "[polymarket.ingest] State reconciliation: updated %d / %d markets",
                updated, checked,
            )

        # --- Refresh volume_24h in DB and Redis ---
        # DB: bulk-update markets.volume_24h from Gamma API data
        vol_db_rows = [
            (cid, Decimal(str(vol)), datetime.now(timezone.utc))
            for cid, (vol, _) in volume_data.items()
            if vol > 0
        ]
        if vol_db_rows:
            async with pool.acquire() as conn:
                await conn.executemany(
                    """
                    UPDATE markets
                    SET volume_24h = $2::numeric,
                        volume     = $2::numeric,
                        updated_at = $3
                    WHERE exchange = 'polymarket'
                      AND ext_id = $1
                      AND is_deleted = FALSE
                    """,
                    vol_db_rows,
                )

        # Redis: write volume_24h to HSET for each token (one pipeline)
        redis_vol_writes = [
            (cid, vol, tids)
            for cid, (vol, tids) in volume_data.items()
            if vol > 0 and tids
        ]
        if redis_vol_writes:
            pipe = redis_conn.pipeline(transaction=False)
            _TICKER_TTL = 48 * 3600  # 48h — same as stream.py TICKER_KEY_TTL
            for _cid, vol, tids in redis_vol_writes:
                vol_str = str(int(vol))
                for token_id in tids:
                    key = _make_key(EXCHANGE, token_id)
                    pipe.hset(key, mapping={"volume_24h": vol_str})
                    pipe.expire(key, _TICKER_TTL)
            await pipe.execute()
            volume_refreshed = sum(len(tids) for _, _, tids in redis_vol_writes)

        # Event-level volume_24h refresh: sum market volumes per event → events.volume_24h
        if event_volumes:
            vol_event_rows = [
                (evt_id, Decimal(str(vol)), datetime.now(timezone.utc))
                for evt_id, vol in event_volumes.items()
                if vol > 0
            ]
            if vol_event_rows:
                async with pool.acquire() as conn:
                    await conn.executemany(
                        """
                        UPDATE events
                        SET volume_24h = $2::numeric,
                            updated_at = $3
                        WHERE exchange = 'polymarket'
                          AND ext_id = $1
                          AND is_deleted = FALSE
                        """,
                        vol_event_rows,
                    )

        # Trending ZSET: update events_trending_24h_polymarket with summed event volumes
        if event_volumes:
            pipe2 = redis_conn.pipeline(transaction=False)
            for evt_id, vol in event_volumes.items():
                pipe2.zadd("events_trending_24h_polymarket", {evt_id: vol})
            await pipe2.execute()

        logger.info(
            "[polymarket.ingest] Volume refreshed: %d tokens across %d events (trending updated)",
            volume_refreshed, len(event_volumes),
        )

        if not updates:
            logger.info(
                "[polymarket.ingest] State reconciliation: %d markets checked, none changed.",
                checked,
            )

    except Exception as exc:
        logger.error("[polymarket.ingest] State reconciliation failed: %s", exc, exc_info=True)

    return {"checked": checked, "updated": updated, "volume_refreshed": volume_refreshed}


# ---------------------------------------------------------------------------
# Main sync entry points
# ---------------------------------------------------------------------------

async def run_polymarket_dev_sync() -> list[str]:
    """
    Sync a diverse set of active Polymarket series for DEV_MODE.

    Rather than hardcoded slugs (which go stale), fetches a large page of
    currently-active series from the Gamma API and selects a diverse subset
    across categories and market format types (binary / categorical / scalar).

    Selection budget: up to 20 series, at most 2 per (category, format) bucket,
    round-robined so every represented category gets a slot before any repeats.

    Falls back to existing DB token IDs if the sync yields 0 tokens (e.g. all
    active events for the selected series have since resolved between restarts).

    Populates POLYMARKET_DEV_TOKEN_IDS and POLYMARKET_DEV_EVENT_IDS.
    Returns the list of token IDs for WebSocket subscription.
    """
    from app.core.dev_config import (
        POLYMARKET_DEV_TOKEN_IDS,
        POLYMARKET_DEV_EVENT_IDS,
    )

    _DEV_CANDIDATE_POOL = 200   # How many active series to fetch for selection
    _DEV_SERIES_TARGET  = 20    # Max series to process
    _DEV_PER_BUCKET     = 2     # Max series per (category, format) bucket

    logger.info(
        "[polymarket.ingest] Starting DEV sync — fetching %d active series for diverse selection",
        _DEV_CANDIDATE_POOL,
    )
    pool = await get_asyncpg_pool()

    # 1. Fetch candidate pool of active series
    series_page = await _fetch_active_series(limit=_DEV_CANDIDATE_POOL, offset=0)
    if not series_page:
        logger.warning("[polymarket.ingest] Gamma API returned no active series — aborting DEV sync")
        return await load_polymarket_token_ids_from_db()

    # 2. Sort candidate pool by 24h volume descending before diversity selection.
    # This ensures that within each (category, format) bucket, the highest-volume
    # series are at the front and get picked first during round-robin.
    series_page.sort(
        key=lambda s: float(s.get("volume24hr") or s.get("volume") or 0),
        reverse=True,
    )

    # 3. Select a diverse subset across (category, format_type) buckets
    selected = _select_diverse_series(
        series_page,
        max_total=_DEV_SERIES_TARGET,
        max_per_bucket=_DEV_PER_BUCKET,
    )
    logger.info(
        "[polymarket.ingest] DEV sync: selected %d/%d series (diverse by category × format type)",
        len(selected), len(series_page),
    )
    for s in selected:
        cats = _extract_category_labels(s)
        fmt = _infer_series_format_type(s)
        vol = float(s.get("volume24hr") or s.get("volume") or 0)
        logger.info(
            "[polymarket.ingest]   → slug=%-40s  category=%-20s  format=%-12s  vol24h=$%.0f",
            s.get("slug", ""), (cats[0] if cats else "?"), fmt, vol,
        )

    # 4. Upsert each selected series + its events/markets/outcomes
    all_token_ids: list[str] = []
    all_event_ids: list[str] = []

    for series_dict in selected:
        try:
            token_ids = await _process_series(pool, series_dict)
            all_token_ids.extend(token_ids)
            for event_dict in (series_dict.get("events") or []):
                eid = str(event_dict.get("id", "")).strip()
                if eid:
                    all_event_ids.append(eid)
        except Exception as exc:
            logger.error(
                "[polymarket.ingest] Error processing series id=%s slug=%s: %s",
                series_dict.get("id"), series_dict.get("slug"), exc, exc_info=True,
            )

    # 5. Populate mutable dev config lists (dedup, preserve order)
    POLYMARKET_DEV_TOKEN_IDS.clear()
    POLYMARKET_DEV_TOKEN_IDS.extend(list(dict.fromkeys(all_token_ids)))

    POLYMARKET_DEV_EVENT_IDS.clear()
    POLYMARKET_DEV_EVENT_IDS.extend(list(dict.fromkeys(all_event_ids)))

    logger.info(
        "[polymarket.ingest] DEV sync complete: %d token IDs across %d events",
        len(POLYMARKET_DEV_TOKEN_IDS),
        len(POLYMARKET_DEV_EVENT_IDS),
    )

    # 6. If sync yielded nothing (all selected series have resolved markets),
    #    fall back to existing DB tokens so the WS still subscribes to something.
    if not POLYMARKET_DEV_TOKEN_IDS:
        logger.warning(
            "[polymarket.ingest] DEV sync yielded 0 token IDs — "
            "falling back to existing DB tokens for WS subscription"
        )
        db_tokens = await load_polymarket_token_ids_from_db()
        POLYMARKET_DEV_TOKEN_IDS.extend(db_tokens)
        logger.info(
            "[polymarket.ingest] DB fallback: %d token IDs loaded", len(db_tokens)
        )

    # 7. Tag taxonomy sync — skipped in DEV_MODE (5540 related-tag API calls,
    #    ~10 minutes, exhausts DB pool). Run via prod cron or manual endpoint.
    if not DEV_MODE:
        try:
            tag_stats = await run_polymarket_tag_sync(pool)
            logger.info(
                "[polymarket.ingest] Tag taxonomy synced: %d tags, %d sports",
                tag_stats["tags"], tag_stats["sports"],
            )
        except Exception as exc:
            logger.warning("[polymarket.ingest] Tag taxonomy sync failed (non-critical): %s", exc)
    else:
        logger.info("[polymarket.ingest] DEV_MODE — skipping tag taxonomy sync (too slow for dev)")

    return list(POLYMARKET_DEV_TOKEN_IDS)


async def run_polymarket_full_sync() -> dict:
    """
    Production full sync: page through all active Polymarket series via Gamma API.

    Equivalent to Kalshi's run_kalshi_full_sync — intended for arq cron scheduling.
    Returns stats dict with counts of processed entities.
    """
    logger.info("[polymarket.ingest] Starting production full sync...")
    pool = await get_asyncpg_pool()

    total_series = 0
    total_tokens = 0
    offset = 0
    page_size = 100

    while True:
        series_page = await _fetch_active_series(limit=page_size, offset=offset)
        if not series_page:
            break

        for series_dict in series_page:
            try:
                token_ids = await _process_series(pool, series_dict)
                total_tokens += len(token_ids)
                total_series += 1
            except Exception as exc:
                logger.error(
                    "[polymarket.ingest] Error syncing series id=%s: %s",
                    series_dict.get("id"), exc,
                )

        logger.info(
            "[polymarket.ingest] Progress: offset=%d series=%d tokens=%d",
            offset, total_series, total_tokens,
        )

        if len(series_page) < page_size:
            break
        offset += page_size

    logger.info(
        "[polymarket.ingest] Full sync complete: %d series, %d tokens",
        total_series, total_tokens,
    )
    return {"series": total_series, "tokens": total_tokens}
