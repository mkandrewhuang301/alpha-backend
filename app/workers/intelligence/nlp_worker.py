"""
NLP batch worker — processes up to 10 intelligence articles per invocation.

Pipeline per batch:
    1. Load raw_text from DB
    2. Load active PlatformTag slugs (cached in Redis 5 min)
    3. Parallel NER via asyncio.gather() (10 concurrent function calls)
    4. Batch embedding (single embeddings.create() call for all texts)
    5. UPDATE external_intelligence SET embedding, impact_level, nlp_status, metadata
    6. Find matching Events by tag overlap → INSERT IntelligenceMarketMapping (event_id always set)
    7. If any impact_level='high', trigger notification dispatch

nlp_status tracking:
    pending  → complete  (both NER + embedding succeeded)
    pending  → partial   (one of NER or embedding failed)
    pending  → failed    (both failed)
"""

import asyncio
import json
import logging
from uuid import UUID

from app.services.nlp_embeddings import generate_embeddings_batch
from app.services.nlp_entities import extract_entities_and_match

logger = logging.getLogger(__name__)

TAGS_CACHE_KEY = "intel_tags_cache"
TAGS_CACHE_TTL = 300  # 5 minutes


async def _load_tags_cached(pool, redis) -> list[str]:
    """
    Load PlatformTag slugs for NER, prioritizing those that appear in
    event/market tags arrays so NER output reliably produces mappings.

    Strategy:
      1. Event-indexed slugs first (appear in events.tags or markets metadata) — these
         are the slugs that will actually produce IntelligenceMarketMapping rows.
      2. Remaining active slugs fill the rest up to 2000 total.

    Cached in Redis for 5 min.
    """
    cached = await redis.get(TAGS_CACHE_KEY)
    if cached:
        return json.loads(cached)

    async with pool.acquire() as conn:
        # Priority 1: slugs that appear in event tags arrays (guaranteed to produce mappings)
        indexed_rows = await conn.fetch(
            """
            SELECT DISTINCT unnest(tags) AS slug
            FROM events
            WHERE is_deleted = false
            """
        )
        indexed_slugs = [r["slug"] for r in indexed_rows]

        # Priority 2: remaining active platform_tags (for broad coverage, fill to 2000)
        remaining_cap = max(0, 2000 - len(indexed_slugs))
        if remaining_cap > 0:
            extra_rows = await conn.fetch(
                """
                SELECT DISTINCT slug FROM platform_tags
                WHERE force_hide = false
                  AND slug != ALL($1::varchar[])
                LIMIT $2
                """,
                indexed_slugs,
                remaining_cap,
            )
            extra_slugs = [r["slug"] for r in extra_rows]
        else:
            extra_slugs = []

    slugs = indexed_slugs + extra_slugs
    if slugs:
        await redis.set(TAGS_CACHE_KEY, json.dumps(slugs), ex=TAGS_CACHE_TTL)
    return slugs


async def _find_matching_events(pool, matched_slugs: list[str]) -> list:
    """
    Find Events whose tags overlap with matched slugs (GIN && operator).

    Returns a list of event UUIDs. Market-level specificity is reserved for
    future targeted signals (e.g., sharp-money on a single outcome) that
    require explicit market identification beyond tag overlap.
    """
    if not matched_slugs:
        return []

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id FROM events
            WHERE is_deleted = false
              AND tags && $1::varchar[]
            LIMIT 50
            """,
            matched_slugs,
        )
    return [r["id"] for r in rows]


async def _insert_mappings(
    pool,
    intelligence_id: UUID,
    event_ids: list,
    ner_result: dict,
) -> None:
    """
    Insert IntelligenceMarketMapping rows for matched events.

    Every row has event_id (required). market_id is always NULL for tag-based
    matches — it is reserved for future targeted market signals.
    """
    if not event_ids:
        return

    matched_slugs = [t["slug"] for t in ner_result.get("matched_tags", [])]
    sentiment = ner_result.get("sentiment_polarity", 0.0)

    tag_confidences = [t.get("confidence", 0.5) for t in ner_result.get("matched_tags", [])]
    avg_confidence = sum(tag_confidences) / len(tag_confidences) if tag_confidences else 0.5

    rows = [
        (intelligence_id, event_id, avg_confidence, sentiment, matched_slugs)
        for event_id in event_ids
    ]

    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO intelligence_market_mapping
                (id, intelligence_id, event_id, confidence_score,
                 sentiment_polarity, matched_tags)
            VALUES (gen_random_uuid(), $1, $2, $3, $4, $5)
            ON CONFLICT (intelligence_id, event_id, market_id) DO NOTHING
            """,
            rows,
        )


async def process_intelligence_nlp_batch(
    ctx: dict, intelligence_ids: list[str]
) -> None:
    """
    Batch process up to 10 intelligence articles: NER + embedding + mapping.
    Called as an arq task by ingestion workers.
    """
    try:
        pool = ctx["asyncpg_pool"]
        redis = ctx["redis"]

        uuids = [UUID(uid) for uid in intelligence_ids[:10]]

        # 1. Load raw texts from DB
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, raw_text, title, source_domain
                FROM external_intelligence
                WHERE id = ANY($1) AND nlp_status IN ('pending', 'partial')
                """,
                uuids,
            )

        if not rows:
            logger.info("[nlp] No pending articles found for batch")
            return

        articles = {r["id"]: dict(r) for r in rows}
        article_ids = list(articles.keys())
        texts = [articles[aid]["raw_text"] for aid in article_ids]

        # 2. Load active tags (cached)
        available_tags = await _load_tags_cached(pool, redis)

        # 3. Parallel NER via asyncio.gather()
        ner_tasks = [
            extract_entities_and_match(text, available_tags) for text in texts
        ]
        ner_results = await asyncio.gather(*ner_tasks, return_exceptions=True)

        # 4. Batch embedding
        try:
            embeddings = await generate_embeddings_batch(texts)
            embedding_ok = True
        except Exception as e:
            logger.error("[nlp] Batch embedding failed: %s", e)
            embeddings = [None] * len(texts)
            embedding_ok = False

        # 5. UPDATE each article + determine nlp_status
        high_impact_ids = []

        async with pool.acquire() as conn:
            for i, aid in enumerate(article_ids):
                ner_result = ner_results[i]
                ner_ok = not isinstance(ner_result, Exception)

                if isinstance(ner_result, Exception):
                    logger.warning("[nlp] NER failed for %s: %s", aid, ner_result)
                    ner_result = {
                        "matched_tags": [],
                        "sentiment_polarity": 0.0,
                        "impact_level": "low",
                        "summary": "",
                    }

                # Determine nlp_status
                if ner_ok and embedding_ok and embeddings[i] is not None:
                    status = "complete"
                elif ner_ok or (embedding_ok and embeddings[i] is not None):
                    status = "partial"
                else:
                    status = "failed"

                embedding_val = embeddings[i] if embedding_ok else None
                impact = ner_result.get("impact_level", "low")
                summary = ner_result.get("summary", "")

                # Build metadata update
                metadata_update = json.dumps({"summary": summary})

                await conn.execute(
                    """
                    UPDATE external_intelligence
                    SET embedding = $2::vector,
                        impact_level = $3::impact_level_type,
                        nlp_status = $4::nlp_status_type,
                        metadata = metadata || $5::jsonb,
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    aid,
                    str(embedding_val) if embedding_val else None,
                    impact,
                    status,
                    metadata_update,
                )

                if impact == "high":
                    high_impact_ids.append(aid)

        # 6. Find matching events/markets and insert mappings
        for i, aid in enumerate(article_ids):
            ner_result = ner_results[i]
            if isinstance(ner_result, Exception):
                continue

            matched_slugs = [t["slug"] for t in ner_result.get("matched_tags", [])]
            if not matched_slugs:
                continue

            event_ids = await _find_matching_events(pool, matched_slugs)
            await _insert_mappings(pool, aid, event_ids, ner_result)

        # 7. Trigger notification dispatch for high-impact items
        if high_impact_ids:
            try:
                from arq import ArqRedis

                arq_redis = ArqRedis(pool_or_conn=redis.connection_pool)
                for aid in high_impact_ids:
                    # Get the matched market/event IDs for this intelligence
                    async with pool.acquire() as conn:
                        mapping_rows = await conn.fetch(
                            """
                            SELECT event_id, market_id
                            FROM intelligence_market_mapping
                            WHERE intelligence_id = $1
                            """,
                            aid,
                        )
                    event_ids = [str(r["event_id"]) for r in mapping_rows if r["event_id"]]
                    market_ids = [str(r["market_id"]) for r in mapping_rows if r["market_id"]]

                    await arq_redis.enqueue_job(
                        "run_dispatch_intelligence_alerts",
                        str(aid),
                        "high",
                        event_ids,
                        market_ids,
                    )
            except Exception as e:
                logger.error("[nlp] Failed to enqueue notification dispatch: %s", e)

        logger.info(
            "[nlp] Processed %d articles: %d high-impact",
            len(article_ids),
            len(high_impact_ids),
        )

    except Exception as exc:
        logger.error("[nlp] process_intelligence_nlp_batch failed: %s", exc, exc_info=True)
