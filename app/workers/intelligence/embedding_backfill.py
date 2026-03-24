"""
Embedding backfill workers — populate VECTOR(768) columns for existing
markets and events that were created before the intelligence engine.

Two arq cron tasks:
    backfill_market_embeddings  — every 10 min, batch 100 markets with NULL embedding
    backfill_event_embeddings   — every 10 min (offset 5), batch 100 events

Generates embedding from rich metadata:
    Markets: title + subtitle + rules_primary + categories + exchange + status
    Events: title + description + categories + tags + exchange

Uses batch UPDATE via UNNEST for efficiency (no per-row round-trips).
Stops once all rows have embeddings (no-op cron).
"""

import logging

from app.services.nlp_embeddings import generate_embeddings_batch

logger = logging.getLogger(__name__)

BACKFILL_BATCH_SIZE = 100


def _build_market_text(row: dict) -> str:
    """Build rich embedding text from market metadata."""
    parts = [row["title"] or ""]
    if row.get("subtitle"):
        parts.append(row["subtitle"])
    if row.get("rules_primary"):
        parts.append(row["rules_primary"])
    # Markets don't have categories — include event categories if available
    if row.get("event_categories"):
        parts.append(" ".join(row["event_categories"]))
    if row.get("exchange"):
        parts.append(f"Exchange: {row['exchange']}")
    if row.get("status"):
        parts.append(f"Status: {row['status']}")
    return " ".join(parts).strip()


def _build_event_text(row: dict) -> str:
    """Build rich embedding text from event metadata."""
    parts = [row["title"] or ""]
    if row.get("description"):
        parts.append(row["description"])
    cats = row.get("categories")
    if cats:
        parts.append(" ".join(cats))
    tags = row.get("tags")
    if tags:
        parts.append(" ".join(tags))
    if row.get("exchange"):
        parts.append(f"Exchange: {row['exchange']}")
    return " ".join(parts).strip()


async def backfill_market_embeddings(ctx: dict) -> None:
    """
    arq cron: backfill embedding column on markets table.
    Processes up to 100 markets per invocation using batch UPDATE.
    """
    try:
        pool = ctx["asyncpg_pool"]

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT m.id, m.title, m.subtitle, m.exchange, m.status,
                       m.platform_metadata->>'rules_primary' as rules_primary,
                       e.categories as event_categories
                FROM markets m
                LEFT JOIN events e ON e.id = m.event_id
                WHERE m.embedding IS NULL AND m.is_deleted = false
                LIMIT $1
                """,
                BACKFILL_BATCH_SIZE,
            )

        if not rows:
            logger.debug("[backfill] No markets need embedding backfill")
            return

        texts = []
        ids = []
        for row in rows:
            texts.append(_build_market_text(dict(row)))
            ids.append(row["id"])

        embeddings = await generate_embeddings_batch(texts)

        # Batch UPDATE via UNNEST — single round-trip
        embedding_strs = [str(e) for e in embeddings]
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE markets AS m
                SET embedding = data.emb::vector,
                    updated_at = NOW()
                FROM UNNEST($1::uuid[], $2::text[]) AS data(mid, emb)
                WHERE m.id = data.mid
                """,
                ids,
                embedding_strs,
            )

        logger.info("[backfill] Updated embeddings for %d markets", len(ids))

    except Exception as exc:
        logger.error(
            "[backfill] backfill_market_embeddings failed: %s", exc, exc_info=True
        )


async def backfill_event_embeddings(ctx: dict) -> None:
    """
    arq cron: backfill embedding column on events table.
    Processes up to 100 events per invocation using batch UPDATE.
    """
    try:
        pool = ctx["asyncpg_pool"]

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, description, categories, tags, exchange
                FROM events
                WHERE embedding IS NULL AND is_deleted = false
                LIMIT $1
                """,
                BACKFILL_BATCH_SIZE,
            )

        if not rows:
            logger.debug("[backfill] No events need embedding backfill")
            return

        texts = []
        ids = []
        for row in rows:
            texts.append(_build_event_text(dict(row)))
            ids.append(row["id"])

        embeddings = await generate_embeddings_batch(texts)

        # Batch UPDATE via UNNEST — single round-trip
        embedding_strs = [str(e) for e in embeddings]
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE events AS e
                SET embedding = data.emb::vector,
                    updated_at = NOW()
                FROM UNNEST($1::uuid[], $2::text[]) AS data(eid, emb)
                WHERE e.id = data.eid
                """,
                ids,
                embedding_strs,
            )

        logger.info("[backfill] Updated embeddings for %d events", len(ids))

    except Exception as exc:
        logger.error(
            "[backfill] backfill_event_embeddings failed: %s", exc, exc_info=True
        )
