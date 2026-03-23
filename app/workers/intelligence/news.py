"""
NewsAPI ingestion worker — polls /v2/everything every 5 minutes.

Pipeline:
    NewsAPI REST → Redis dedup → PostgreSQL bulk insert → enqueue NLP batch

Safe to fail: outer try/except prevents arq process crash.
"""

import logging
from datetime import datetime, timezone

import httpx

from app.core.config import NEWSAPI_KEY
from app.workers.intelligence.base import (
    bulk_insert_intelligence,
    check_redis_dedup,
    content_hash,
)

logger = logging.getLogger(__name__)

NEWSAPI_BASE_URL = "https://newsapi.org/v2/everything"
MAX_ARTICLES_PER_POLL = 50


NEWS_TAG_CURSOR_KEY = "intel_news_tag_cursor"
TERMS_PER_QUERY = 5  # NewsAPI query length limit


async def _fetch_query_terms(pool, redis) -> list[str]:
    """
    Load PlatformTag labels as NewsAPI query terms, rotating through all tags
    across poll cycles so every category eventually gets covered.

    Uses a Redis cursor to track offset. On each 5-min poll, advances by
    TERMS_PER_QUERY. Wraps back to 0 when we've exhausted all tags.

    Uses label (human-readable) instead of slug — "US Politics" matches
    news articles far better than the slug "us-politics".
    """
    offset = int(await redis.get(NEWS_TAG_CURSOR_KEY) or 0)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT label FROM platform_tags
            WHERE force_hide = false
              AND type IN ('category', 'tag')
            ORDER BY label
            LIMIT $1 OFFSET $2
            """,
            TERMS_PER_QUERY,
            offset,
        )

    labels = [r["label"] for r in rows]

    if labels:
        # Advance cursor for next cycle
        await redis.set(NEWS_TAG_CURSOR_KEY, offset + TERMS_PER_QUERY)
    else:
        # Wrapped around — reset to beginning
        await redis.set(NEWS_TAG_CURSOR_KEY, 0)
        # Re-fetch from start so this cycle isn't empty
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT label FROM platform_tags
                WHERE force_hide = false
                  AND type IN ('category', 'tag')
                ORDER BY label
                LIMIT $1
                """,
                TERMS_PER_QUERY,
            )
        labels = [r["label"] for r in rows]

    return labels


async def ingest_news(ctx: dict) -> None:
    """
    arq cron task: poll NewsAPI for articles matching prediction market topics.
    Dedup via Redis hash → batch INSERT → enqueue NLP batch processing.

    Rotates through all platform tag labels across cycles (TERMS_PER_QUERY per
    5-min poll) so the full taxonomy is covered over time.
    """
    try:
        pool = ctx["asyncpg_pool"]
        redis = ctx["redis"]

        if not NEWSAPI_KEY:
            logger.warning("[news] NEWSAPI_KEY not set, skipping")
            return

        # Build query from active platform tags (rotates each cycle)
        terms = await _fetch_query_terms(pool, redis)
        if not terms:
            logger.info("[news] No active platform tags for query, skipping")
            return

        query = " OR ".join(f'"{t}"' if " " in t else t for t in terms)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                NEWSAPI_BASE_URL,
                params={
                    "q": query,
                    "sortBy": "publishedAt",
                    "pageSize": MAX_ARTICLES_PER_POLL,
                    "apiKey": NEWSAPI_KEY,
                    "language": "en",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        articles = data.get("articles", [])
        if not articles:
            logger.info("[news] No new articles from NewsAPI")
            return

        # Dedup + build insert batch
        items_to_insert = []
        for article in articles:
            try:
                title = article.get("title") or ""
                url = article.get("url") or ""
                if not title or title == "[Removed]":
                    continue

                c_hash = content_hash("news", url, title)
                if await check_redis_dedup(redis, c_hash):
                    continue

                raw_text = article.get("description") or article.get("content") or title
                published = article.get("publishedAt")
                if published:
                    pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                else:
                    pub_dt = datetime.now(timezone.utc)

                items_to_insert.append({
                    "source_domain": "news",
                    "source_name": "newsapi",
                    "title": title,
                    "raw_text": raw_text,
                    "url": url,
                    "author": article.get("author"),
                    "published_at": pub_dt,
                    "metadata": {
                        "source_api": "newsapi",
                        "source_name": article.get("source", {}).get("name"),
                        "image_url": article.get("urlToImage"),
                    },
                    "impact_level": "low",
                    "content_hash": c_hash,
                })
            except Exception as e:
                logger.warning("[news] Skipping article: %s", e)
                continue

        if not items_to_insert:
            logger.info("[news] All articles deduped, nothing to insert")
            return

        # Batch insert
        inserted_ids = await bulk_insert_intelligence(pool, items_to_insert)

        # Enqueue NLP batch processing (batches of 10)
        if inserted_ids:
            from arq import ArqRedis
            arq_redis = ArqRedis(pool_or_conn=redis.connection_pool)
            for i in range(0, len(inserted_ids), 10):
                batch = [str(uid) for uid in inserted_ids[i:i + 10]]
                await arq_redis.enqueue_job(
                    "run_process_intelligence_nlp_batch",
                    batch,
                )
            logger.info(
                "[news] Enqueued NLP for %d articles in %d batches",
                len(inserted_ids),
                (len(inserted_ids) + 9) // 10,
            )

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            logger.warning("[news] NewsAPI rate limited, will retry next cycle")
        else:
            logger.error("[news] NewsAPI HTTP error: %s", e)
    except Exception as exc:
        logger.error("[news] ingest_news failed: %s", exc, exc_info=True)
