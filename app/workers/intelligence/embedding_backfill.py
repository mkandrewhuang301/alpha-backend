"""
Embedding backfill workers — populate VECTOR(1536) columns for existing
markets and events that were created before the intelligence engine.

Two arq cron tasks:
    backfill_market_embeddings  — every 10 min, batch 100 markets with NULL embedding
    backfill_event_embeddings   — every 10 min (offset 5), batch 100 events

Generates embedding from:
    Markets: title + rules_primary + subtitle
    Events: title + description + categories

Stops once all rows have embeddings (no-op cron).
"""

import logging

from app.services.nlp_embeddings import generate_embeddings_batch

logger = logging.getLogger(__name__)

BACKFILL_BATCH_SIZE = 100


async def backfill_market_embeddings(ctx: dict) -> None:
    """
    arq cron: backfill embedding column on markets table.
    Processes up to 100 markets per invocation.
    """
    try:
        pool = ctx["asyncpg_pool"]

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, subtitle,
                       platform_metadata->>'rules_primary' as rules_primary
                FROM markets
                WHERE embedding IS NULL AND is_deleted = false
                LIMIT $1
                """,
                BACKFILL_BATCH_SIZE,
            )

        if not rows:
            logger.debug("[backfill] No markets need embedding backfill")
            return

        # Build text for each market
        texts = []
        ids = []
        for row in rows:
            parts = [row["title"] or ""]
            if row.get("rules_primary"):
                parts.append(row["rules_primary"])
            if row.get("subtitle"):
                parts.append(row["subtitle"])
            texts.append(" ".join(parts).strip())
            ids.append(row["id"])

        # Batch generate embeddings
        embeddings = await generate_embeddings_batch(texts)

        # Update markets with embeddings
        async with pool.acquire() as conn:
            for i, market_id in enumerate(ids):
                await conn.execute(
                    """
                    UPDATE markets
                    SET embedding = $2::vector, updated_at = NOW()
                    WHERE id = $1
                    """,
                    market_id,
                    str(embeddings[i]),
                )

        logger.info("[backfill] Updated embeddings for %d markets", len(ids))

    except Exception as exc:
        logger.error(
            "[backfill] backfill_market_embeddings failed: %s", exc, exc_info=True
        )


async def backfill_event_embeddings(ctx: dict) -> None:
    """
    arq cron: backfill embedding column on events table.
    Processes up to 100 events per invocation.
    """
    try:
        pool = ctx["asyncpg_pool"]

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, description, categories
                FROM events
                WHERE embedding IS NULL AND is_deleted = false
                LIMIT $1
                """,
                BACKFILL_BATCH_SIZE,
            )

        if not rows:
            logger.debug("[backfill] No events need embedding backfill")
            return

        # Build text for each event
        texts = []
        ids = []
        for row in rows:
            parts = [row["title"] or ""]
            if row.get("description"):
                parts.append(row["description"])
            cats = row.get("categories")
            if cats:
                parts.append(" ".join(cats))
            texts.append(" ".join(parts).strip())
            ids.append(row["id"])

        # Batch generate embeddings
        embeddings = await generate_embeddings_batch(texts)

        # Update events with embeddings
        async with pool.acquire() as conn:
            for i, event_id in enumerate(ids):
                await conn.execute(
                    """
                    UPDATE events
                    SET embedding = $2::vector, updated_at = NOW()
                    WHERE id = $1
                    """,
                    event_id,
                    str(embeddings[i]),
                )

        logger.info("[backfill] Updated embeddings for %d events", len(ids))

    except Exception as exc:
        logger.error(
            "[backfill] backfill_event_embeddings failed: %s", exc, exc_info=True
        )
