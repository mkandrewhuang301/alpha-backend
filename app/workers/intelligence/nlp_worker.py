"""
NLP batch worker — processes up to 10 intelligence articles per invocation.

Pipeline per batch:
    1. Load raw_text + metadata from DB
    2. Load active PlatformTag slugs (cached in Redis 5 min)
    3. Tiered NER: try local keyword match first, GPT-4o only for unresolved
    4. Batch embedding (single embeddings.create() call for all texts)
    5. UPDATE external_intelligence SET embedding, impact_level, nlp_status, metadata
    6. Unified matching: GIN tag overlap + embedding cosine similarity → combined confidence
    7. INSERT IntelligenceMarketMapping with price_at_publish
    8. If any impact_level='high', trigger notification dispatch
    9. Publish to SSE channel for real-time feed

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
from app.services.nlp_entities import extract_entities_and_match, tier1_keyword_match

logger = logging.getLogger(__name__)

TAGS_CACHE_KEY = "intel_tags_cache"
TAGS_CACHE_TTL = 300  # 5 minutes

# Matching thresholds
COSINE_SIMILARITY_THRESHOLD = 0.5   # minimum for embedding-only matches
COMBINED_CONFIDENCE_THRESHOLD = 0.3  # minimum to create a mapping


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


async def _find_matches(
    pool,
    intelligence_embedding: list[float] | None,
    matched_slugs: list[str],
) -> list[dict]:
    """
    Unified matching engine: combines GIN tag overlap + embedding cosine
    similarity into a single confidence score per event/market.

    Returns list of dicts:
        {"event_id": UUID, "market_id": UUID | None, "confidence": float, "tag_score": float, "cosine_sim": float}

    Scoring:
        - Both signals:     confidence = (tag_score * 0.5) + (cosine_sim * 0.5)
        - Tags only:        confidence = tag_score * 0.7
        - Embedding only:   confidence = cosine_sim * 0.6
    """
    candidates = {}  # key: (event_id, market_id) → {"tag_score", "cosine_sim"}

    async with pool.acquire() as conn:
        # --- Signal 1: GIN tag overlap ---
        if matched_slugs:
            tag_rows = await conn.fetch(
                """
                SELECT id AS event_id,
                       array_length(
                           ARRAY(SELECT unnest(tags) INTERSECT SELECT unnest($1::varchar[])),
                           1
                       ) AS overlap_count
                FROM events
                WHERE is_deleted = false
                  AND tags && $1::varchar[]
                LIMIT 50
                """,
                matched_slugs,
            )
            num_slugs = len(matched_slugs)
            for row in tag_rows:
                overlap = row["overlap_count"] or 0
                tag_score = min(1.0, overlap / max(num_slugs, 1))
                key = (row["event_id"], None)
                candidates[key] = {"tag_score": tag_score, "cosine_sim": 0.0}

        # --- Signal 2: Embedding cosine similarity ---
        if intelligence_embedding is not None:
            embedding_str = str(intelligence_embedding)

            # Search events by embedding
            event_sim_rows = await conn.fetch(
                """
                SELECT id AS event_id,
                       1 - (embedding <=> $1::vector) AS similarity
                FROM events
                WHERE is_deleted = false
                  AND embedding IS NOT NULL
                  AND 1 - (embedding <=> $1::vector) >= $2
                ORDER BY embedding <=> $1::vector
                LIMIT 30
                """,
                embedding_str,
                COSINE_SIMILARITY_THRESHOLD,
            )
            for row in event_sim_rows:
                key = (row["event_id"], None)
                if key in candidates:
                    candidates[key]["cosine_sim"] = float(row["similarity"])
                else:
                    candidates[key] = {"tag_score": 0.0, "cosine_sim": float(row["similarity"])}

            # Search markets by embedding (creates market-level mappings)
            market_sim_rows = await conn.fetch(
                """
                SELECT m.id AS market_id, m.event_id,
                       1 - (m.embedding <=> $1::vector) AS similarity
                FROM markets m
                WHERE m.is_deleted = false
                  AND m.embedding IS NOT NULL
                  AND 1 - (m.embedding <=> $1::vector) >= $2
                ORDER BY m.embedding <=> $1::vector
                LIMIT 30
                """,
                embedding_str,
                COSINE_SIMILARITY_THRESHOLD,
            )
            for row in market_sim_rows:
                key = (row["event_id"], row["market_id"])
                if key in candidates:
                    candidates[key]["cosine_sim"] = max(
                        candidates[key]["cosine_sim"], float(row["similarity"])
                    )
                else:
                    candidates[key] = {"tag_score": 0.0, "cosine_sim": float(row["similarity"])}

    # --- Compute combined confidence ---
    results = []
    for (event_id, market_id), scores in candidates.items():
        tag_score = scores["tag_score"]
        cosine_sim = scores["cosine_sim"]

        if tag_score > 0 and cosine_sim > 0:
            # Both signals — full weight
            confidence = (tag_score * 0.5) + (cosine_sim * 0.5)
        elif tag_score > 0:
            # Tags only — capped lower without semantic confirmation
            confidence = tag_score * 0.7
        else:
            # Embedding only — slightly discounted
            confidence = cosine_sim * 0.6

        if confidence >= COMBINED_CONFIDENCE_THRESHOLD:
            results.append({
                "event_id": event_id,
                "market_id": market_id,
                "confidence": round(confidence, 3),
                "tag_score": round(tag_score, 3),
                "cosine_sim": round(cosine_sim, 3),
            })

    # Sort by confidence descending, limit to top 50
    results.sort(key=lambda x: x["confidence"], reverse=True)
    return results[:50]


async def _insert_mappings(
    pool,
    intelligence_id: UUID,
    matches: list[dict],
    ner_result: dict,
) -> None:
    """
    Insert IntelligenceMarketMapping rows for matched events/markets.
    Stores combined confidence, sentiment, matched tags, and price_at_publish.
    """
    if not matches:
        return

    matched_slugs = [t["slug"] for t in ner_result.get("matched_tags", [])]
    sentiment = ner_result.get("sentiment_polarity", 0.0)

    async with pool.acquire() as conn:
        # Gather current prices from Redis tickers for price_at_publish snapshot.
        # Prices are stored in Redis as HSET ticker:{exchange}:{asset_id}.
        # For efficiency, we batch-fetch via the primary "yes" outcome per market.
        price_map = {}  # market_id or event_id → price (float)

        all_entity_ids = list({
            m["market_id"] or m["event_id"] for m in matches
        })
        if all_entity_ids:
            # Get market → exchange + yes outcome asset_id for Redis lookup
            outcome_rows = await conn.fetch(
                """
                SELECT m.id AS market_id, m.event_id, m.exchange,
                       mo.execution_asset_id
                FROM markets m
                JOIN market_outcomes mo ON mo.market_id = m.id AND mo.side = 'yes'
                WHERE (m.id = ANY($1) OR m.event_id = ANY($1))
                  AND m.is_deleted = false
                """,
                all_entity_ids,
            )
            # Build Redis pipeline for price lookups
            if outcome_rows:
                from app.core.redis import get_redis
                price_redis = await get_redis()
                pipe = price_redis.pipeline(transaction=False)
                lookup_keys = []
                for row in outcome_rows:
                    key = f"ticker:{row['exchange']}:{row['execution_asset_id']}"
                    pipe.hget(key, "price")
                    lookup_keys.append((row["market_id"], row["event_id"]))
                results = await pipe.execute()
                for i, val in enumerate(results):
                    if val is not None:
                        try:
                            price = float(val)
                            mid, eid = lookup_keys[i]
                            price_map[mid] = price
                            if eid not in price_map:
                                price_map[eid] = price
                        except (ValueError, TypeError):
                            pass

        rows = []
        for match in matches:
            event_id = match["event_id"]
            market_id = match["market_id"]
            confidence = match["confidence"]

            # Look up price_at_publish (None if not available in Redis)
            price = price_map.get(market_id) if market_id else price_map.get(event_id)

            rows.append((
                intelligence_id,
                event_id,
                market_id,
                confidence,
                sentiment,
                matched_slugs,
                price,
            ))

        await conn.executemany(
            """
            INSERT INTO intelligence_market_mapping
                (id, intelligence_id, event_id, market_id, confidence_score,
                 sentiment_polarity, matched_tags, price_at_publish)
            VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (intelligence_id, event_id, market_id) DO NOTHING
            """,
            rows,
        )


async def _publish_to_sse(redis, intelligence_id: UUID, title: str, summary: str,
                          source_domain: str, impact_level: str,
                          matched_tags: list[str]) -> None:
    """Publish intelligence item to Redis Pub/Sub for SSE delivery."""
    try:
        payload = json.dumps({
            "id": str(intelligence_id),
            "title": title,
            "summary": summary,
            "source_domain": source_domain,
            "impact_level": impact_level,
            "matched_tags": matched_tags,
        })
        await redis.publish("intel:feed:global", payload)
    except Exception as e:
        logger.warning("[nlp] Failed to publish SSE event: %s", e)


async def process_intelligence_nlp_batch(
    ctx: dict, intelligence_ids: list[str]
) -> None:
    """
    Batch process up to 10 intelligence articles: NER + embedding + matching.
    Called as an arq task by ingestion workers.
    """
    try:
        pool = ctx["asyncpg_pool"]
        redis = ctx["redis"]

        uuids = [UUID(uid) for uid in intelligence_ids[:10]]

        # 1. Load raw texts + metadata from DB
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, raw_text, title, source_domain, metadata
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

        # 3. Tiered NER: try local keyword match first, GPT-4o for unresolved
        ner_results = [None] * len(article_ids)
        gpt_indices = []

        for i, aid in enumerate(article_ids):
            article = articles[aid]
            metadata = article.get("metadata") or {}
            tier1_result = tier1_keyword_match(
                article["raw_text"], metadata, available_tags
            )
            if tier1_result is not None:
                ner_results[i] = tier1_result
                logger.debug("[nlp] Tier 1 match for %s: %d tags", aid,
                             len(tier1_result.get("matched_tags", [])))
            else:
                gpt_indices.append(i)

        # GPT-4o NER only for articles that need it
        if gpt_indices:
            gpt_texts = [texts[i] for i in gpt_indices]
            gpt_tasks = [
                extract_entities_and_match(t, available_tags) for t in gpt_texts
            ]
            gpt_results = await asyncio.gather(*gpt_tasks, return_exceptions=True)
            for j, idx in enumerate(gpt_indices):
                ner_results[idx] = gpt_results[j]

        logger.info(
            "[nlp] NER: %d tier1, %d GPT-4o",
            len(article_ids) - len(gpt_indices),
            len(gpt_indices),
        )

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
                ner_ok = not isinstance(ner_result, (Exception, type(None)))

                if isinstance(ner_result, Exception) or ner_result is None:
                    logger.warning("[nlp] NER failed for %s: %s", aid, ner_result)
                    ner_result = {
                        "matched_tags": [],
                        "sentiment_polarity": 0.0,
                        "impact_level": "low",
                        "summary": "",
                    }
                    ner_ok = False

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

                # Store processed result back for matching step
                ner_results[i] = ner_result
                if impact == "high":
                    high_impact_ids.append(aid)

        # 6. Unified matching: GIN tags + embedding cosine similarity
        for i, aid in enumerate(article_ids):
            ner_result = ner_results[i]
            if isinstance(ner_result, Exception):
                continue

            matched_slugs = [t["slug"] for t in ner_result.get("matched_tags", [])]
            embedding_val = embeddings[i] if embedding_ok else None

            # Skip if we have neither tags nor embedding
            if not matched_slugs and embedding_val is None:
                continue

            matches = await _find_matches(pool, embedding_val, matched_slugs)
            await _insert_mappings(pool, aid, matches, ner_result)

            # Publish to SSE for real-time feed
            if matches:
                summary = ner_result.get("summary", "")
                await _publish_to_sse(
                    redis, aid, articles[aid]["title"], summary,
                    articles[aid]["source_domain"],
                    ner_result.get("impact_level", "low"),
                    matched_slugs,
                )

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
            "[nlp] Processed %d articles: %d high-impact, %d tier1-NER, %d GPT-NER",
            len(article_ids),
            len(high_impact_ids),
            len(article_ids) - len(gpt_indices),
            len(gpt_indices),
        )

    except Exception as exc:
        logger.error("[nlp] process_intelligence_nlp_batch failed: %s", exc, exc_info=True)
