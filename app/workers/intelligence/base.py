"""
Shared utilities for intelligence ingestion workers.

content_hash:           SHA256 dedup hash for articles
check_redis_dedup:      Redis-based dedup check with TTL
bulk_insert_intelligence: asyncpg UNNEST batch INSERT ... ON CONFLICT DO NOTHING
"""

import hashlib
import json
import logging
from datetime import datetime
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

DEDUP_TTL_SECONDS = 7 * 86400  # 7 days


def content_hash(source_domain: str, url: str | None, title: str) -> str:
    """Deterministic SHA256 hash for deduplication."""
    raw = f"{source_domain}|{url or ''}|{title}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


async def check_redis_dedup(redis, hash_val: str) -> bool:
    """
    Atomic check-and-set for content_hash deduplication.
    Returns True if duplicate (skip), False if new (proceed).

    Uses SET NX EX (atomic) instead of GET + SET to eliminate the race
    window where two concurrent workers both see the key missing and both
    proceed to insert + enqueue NLP.
    """
    key = f"intel_dedup:{hash_val}"
    set_result = await redis.set(key, "1", nx=True, ex=DEDUP_TTL_SECONDS)
    # redis-py: SET NX returns True if key was set (new), None if key existed (dup)
    return set_result is None


async def bulk_insert_intelligence(
    pool,
    items: list[dict],
) -> list[UUID]:
    """
    Batch INSERT into external_intelligence using asyncpg UNNEST.
    Returns list of inserted UUIDs (excludes conflicts on content_hash).

    Each item dict must have:
        source_domain, source_name, title, raw_text, url, author,
        published_at, metadata, impact_level, content_hash
    """
    if not items:
        return []

    ids = [uuid4() for _ in items]

    async with pool.acquire() as conn:
        # Build arrays for UNNEST
        result = await conn.fetch(
            """
            INSERT INTO external_intelligence (
                id, source_domain, source_name, title, raw_text, url,
                author, published_at, metadata, impact_level, content_hash
            )
            SELECT * FROM UNNEST(
                $1::uuid[], $2::source_domain_type[], $3::varchar[],
                $4::text[], $5::text[], $6::text[],
                $7::varchar[], $8::timestamptz[], $9::jsonb[], $10::impact_level_type[],
                $11::varchar[]
            )
            ON CONFLICT (content_hash) DO NOTHING
            RETURNING id
            """,
            ids,
            [i["source_domain"] for i in items],
            [i["source_name"] for i in items],
            [i["title"] for i in items],
            [i["raw_text"] for i in items],
            [i.get("url") for i in items],
            [i.get("author") for i in items],
            [i["published_at"] for i in items],
            [json.dumps(i.get("metadata", {})) for i in items],
            [i.get("impact_level", "low") for i in items],
            [i["content_hash"] for i in items],
        )

    inserted_ids = [row["id"] for row in result]
    logger.info(
        "[intelligence] Inserted %d/%d items (dedup skipped %d)",
        len(inserted_ids), len(items), len(items) - len(inserted_ids),
    )
    return inserted_ids
