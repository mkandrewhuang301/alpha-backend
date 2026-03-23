"""
Intelligence ↔ Groups integration workers.

Two tasks:
    process_article_intelligence(ctx, message_id, group_id)
        — Extract URL/title from article GroupMessage, run NLP, link markets

    inject_intelligence_system_messages(ctx)
        — 5-min cron: find high-impact intelligence, match against group markets,
          inject system messages (deduped via Redis 24h TTL)

Redis keys:
    intel_group_dedup:{group_id}:{intelligence_id} — STRING "1", 24h TTL
"""

import json
import logging
from uuid import UUID, uuid4

from app.workers.intelligence.base import (
    bulk_insert_intelligence,
    check_redis_dedup,
    content_hash,
)

logger = logging.getLogger(__name__)

GROUP_INTEL_DEDUP_TTL = 86400  # 24 hours


async def process_article_intelligence(
    ctx: dict, message_id: str, group_id: str
) -> None:
    """
    arq task: process an article shared in a group through the NLP pipeline.

    1. Extract URL + title from GroupMessage.metadata
    2. Check ExternalIntelligence by content_hash — reuse if exists, else INSERT + NLP
    3. Update GroupMessage.metadata with linked_markets
    """
    try:
        pool = ctx["asyncpg_pool"]
        redis = ctx["redis"]

        # Load the group message
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT metadata FROM group_messages
                WHERE id = $1 AND type = 'article'
                """,
                UUID(message_id),
            )

        if not row or not row["metadata"]:
            logger.debug("[group_intel] Message %s not found or no metadata", message_id)
            return

        metadata = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])
        url = metadata.get("url", "")
        title = metadata.get("title", "")

        if not title:
            logger.debug("[group_intel] Article message %s has no title", message_id)
            return

        # Check if this article already exists in intelligence
        c_hash = content_hash("news", url, title)

        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id FROM external_intelligence WHERE content_hash = $1",
                c_hash,
            )

        if existing:
            intelligence_id = existing["id"]
        else:
            # Check dedup and insert
            if await check_redis_dedup(redis, c_hash):
                logger.debug("[group_intel] Article deduped: %s", title[:50])
                return

            from datetime import datetime, timezone

            items = [
                {
                    "source_domain": "news",
                    "source_name": "user_shared",
                    "title": title,
                    "raw_text": metadata.get("description", title),
                    "url": url,
                    "author": metadata.get("author"),
                    "published_at": datetime.now(timezone.utc),
                    "metadata": {
                        "source_api": "user_shared",
                        "group_id": group_id,
                        "message_id": message_id,
                    },
                    "impact_level": "low",
                    "content_hash": c_hash,
                }
            ]

            inserted_ids = await bulk_insert_intelligence(pool, items)
            if not inserted_ids:
                return

            intelligence_id = inserted_ids[0]

            # Enqueue NLP processing
            from arq import ArqRedis

            arq_redis = ArqRedis(pool_or_conn=redis.connection_pool)
            await arq_redis.enqueue_job(
                "run_process_intelligence_nlp_batch",
                [str(intelligence_id)],
            )

        # Update GroupMessage metadata with linked markets
        async with pool.acquire() as conn:
            mapping_rows = await conn.fetch(
                """
                SELECT market_id FROM intelligence_market_mapping
                WHERE intelligence_id = $1 AND market_id IS NOT NULL
                """,
                intelligence_id,
            )

        if mapping_rows:
            linked_markets = [str(r["market_id"]) for r in mapping_rows]
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE group_messages
                    SET metadata = metadata || $2::jsonb
                    WHERE id = $1
                    """,
                    UUID(message_id),
                    json.dumps({"linked_markets": linked_markets}),
                )

            logger.info(
                "[group_intel] Linked %d markets to article message %s",
                len(linked_markets),
                message_id,
            )

    except Exception as exc:
        logger.error(
            "[group_intel] process_article_intelligence failed: %s",
            exc,
            exc_info=True,
        )


async def inject_intelligence_system_messages(ctx: dict) -> None:
    """
    arq cron (5 min): inject high-impact intelligence as system messages
    into groups where relevant markets have been shared.
    """
    try:
        pool = ctx["asyncpg_pool"]
        redis = ctx["redis"]

        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)

        # Find high-impact intelligence from last 5 min
        async with pool.acquire() as conn:
            intel_rows = await conn.fetch(
                """
                SELECT ei.id, ei.title, ei.metadata, ei.impact_level,
                       array_agg(DISTINCT imm.market_id) FILTER (WHERE imm.market_id IS NOT NULL)
                           AS matched_market_ids
                FROM external_intelligence ei
                JOIN intelligence_market_mapping imm ON imm.intelligence_id = ei.id
                WHERE ei.created_at >= $1
                  AND ei.impact_level IN ('high', 'medium')
                  AND ei.is_deleted = false
                GROUP BY ei.id
                """,
                cutoff,
            )

        if not intel_rows:
            return

        for intel in intel_rows:
            market_ids = [mid for mid in (intel["matched_market_ids"] or []) if mid]
            if not market_ids:
                continue

            # Find groups where these markets have been shared
            async with pool.acquire() as conn:
                group_rows = await conn.fetch(
                    """
                    SELECT DISTINCT group_id
                    FROM group_messages
                    WHERE type = 'market'
                      AND (metadata->>'market_id')::uuid = ANY($1)
                    """,
                    market_ids,
                )

            for group_row in group_rows:
                group_id = group_row["group_id"]
                intel_id = intel["id"]

                # Dedup: check if we already injected this intel into this group
                dedup_key = f"intel_group_dedup:{group_id}:{intel_id}"
                exists = await redis.get(dedup_key)
                if exists:
                    continue

                await redis.set(dedup_key, "1", ex=GROUP_INTEL_DEDUP_TTL)

                # Inject system message
                metadata = intel["metadata"] or {}
                summary = metadata.get("summary", intel["title"])

                msg_metadata = {
                    "event": "intelligence_alert",
                    "intelligence_id": str(intel_id),
                    "impact_level": intel["impact_level"],
                    "title": intel["title"],
                    "summary": summary[:200],
                    "matched_market_ids": [str(mid) for mid in market_ids],
                }

                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO group_messages
                            (id, group_id, sender_id, type, content, metadata)
                        VALUES ($1, $2, NULL, 'system', $3, $4)
                        """,
                        uuid4(),
                        group_id,
                        f"Intelligence Alert: {intel['title'][:100]}",
                        json.dumps(msg_metadata),
                    )

                logger.info(
                    "[group_intel] Injected system message in group=%s for intel=%s",
                    group_id,
                    intel_id,
                )

    except Exception as exc:
        logger.error(
            "[group_intel] inject_intelligence_system_messages failed: %s",
            exc,
            exc_info=True,
        )
