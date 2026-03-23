"""
Semantic search service — pgvector cosine similarity across markets, events,
and external intelligence.

Features:
    - 5-second asyncio.wait_for() timeout per vector query (graceful degradation)
    - Hybrid filtering: vector similarity + SQL WHERE status IN ('unopened', 'active')
    - Optional RAG synthesis via GPT-4o (opt-in with synthesize=True)
    - Merges and re-ranks results from all three sources
"""

import asyncio
import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.nlp_embeddings import generate_embedding
from app.services.nlp_rag import synthesize_rag_answer

logger = logging.getLogger(__name__)

VECTOR_QUERY_TIMEOUT = 5.0  # seconds per pgvector query


async def _search_markets(
    db: AsyncSession,
    query_embedding: list[float],
    limit: int,
    include_resolved: bool,
) -> list[dict]:
    """Search markets by embedding cosine similarity."""
    status_filter = "" if include_resolved else "AND status IN ('unopened', 'active')"

    result = await db.execute(
        text(f"""
            SELECT id, title, subtitle, status, exchange,
                   1 - (embedding <=> :embedding::vector) AS similarity
            FROM markets
            WHERE is_deleted = false
              AND embedding IS NOT NULL
              {status_filter}
            ORDER BY embedding <=> :embedding::vector
            LIMIT :limit
        """),
        {"embedding": str(query_embedding), "limit": limit},
    )
    rows = result.mappings().all()
    return [
        {
            "id": str(row["id"]),
            "type": "market",
            "title": row["title"],
            "subtitle": row.get("subtitle"),
            "status": row["status"],
            "exchange": row["exchange"],
            "similarity": float(row["similarity"]),
        }
        for row in rows
    ]


async def _search_events(
    db: AsyncSession,
    query_embedding: list[float],
    limit: int,
    include_resolved: bool,
) -> list[dict]:
    """Search events by embedding cosine similarity."""
    status_filter = "" if include_resolved else "AND status IN ('unopened', 'active')"

    result = await db.execute(
        text(f"""
            SELECT id, title, description, status, exchange,
                   1 - (embedding <=> :embedding::vector) AS similarity
            FROM events
            WHERE is_deleted = false
              AND embedding IS NOT NULL
              {status_filter}
            ORDER BY embedding <=> :embedding::vector
            LIMIT :limit
        """),
        {"embedding": str(query_embedding), "limit": limit},
    )
    rows = result.mappings().all()
    return [
        {
            "id": str(row["id"]),
            "type": "event",
            "title": row["title"],
            "description": row.get("description"),
            "status": row["status"],
            "exchange": row["exchange"],
            "similarity": float(row["similarity"]),
        }
        for row in rows
    ]


async def _search_intelligence(
    db: AsyncSession,
    query_embedding: list[float],
    limit: int,
) -> list[dict]:
    """Search external intelligence by embedding cosine similarity."""
    result = await db.execute(
        text("""
            SELECT id, title, raw_text, source_domain, source_name,
                   url, impact_level, published_at,
                   1 - (embedding <=> :embedding::vector) AS similarity
            FROM external_intelligence
            WHERE is_deleted = false
              AND embedding IS NOT NULL
            ORDER BY embedding <=> :embedding::vector
            LIMIT :limit
        """),
        {"embedding": str(query_embedding), "limit": limit},
    )
    rows = result.mappings().all()
    return [
        {
            "id": str(row["id"]),
            "type": "intelligence",
            "title": row["title"],
            "text": row["raw_text"][:500] if row["raw_text"] else "",
            "source_domain": row["source_domain"],
            "source_name": row["source_name"],
            "url": row.get("url"),
            "impact_level": row["impact_level"],
            "published_at": row["published_at"].isoformat() if row.get("published_at") else None,
            "similarity": float(row["similarity"]),
        }
        for row in rows
    ]


async def semantic_search(
    query: str,
    db: AsyncSession,
    limit: int = 10,
    include_resolved: bool = False,
    synthesize: bool = False,
) -> dict:
    """
    Run semantic search across markets, events, and intelligence.

    Returns:
        {
            "markets": [...],
            "events": [...],
            "intelligence": [...],
            "narrative": str | None,  (only if synthesize=True)
            "citations": [...]        (only if synthesize=True)
        }
    """
    # Generate query embedding
    try:
        query_embedding = await generate_embedding(query)
    except Exception as e:
        logger.error("[search] Failed to generate query embedding: %s", e)
        return {
            "markets": [],
            "events": [],
            "intelligence": [],
            "narrative": None,
            "citations": [],
        }

    # Three parallel pgvector queries with 5s timeout each
    async def _safe_query(coro, label: str):
        try:
            return await asyncio.wait_for(coro, timeout=VECTOR_QUERY_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("[search] %s query timed out after %ss", label, VECTOR_QUERY_TIMEOUT)
            return []
        except Exception as e:
            logger.error("[search] %s query failed: %s", label, e)
            return []

    markets, events, intelligence = await asyncio.gather(
        _safe_query(_search_markets(db, query_embedding, limit, include_resolved), "markets"),
        _safe_query(_search_events(db, query_embedding, limit, include_resolved), "events"),
        _safe_query(_search_intelligence(db, query_embedding, limit), "intelligence"),
    )

    result = {
        "markets": markets,
        "events": events,
        "intelligence": intelligence,
        "narrative": None,
        "citations": [],
    }

    # Optional RAG synthesis
    if synthesize:
        # Merge top results for context
        all_items = sorted(
            [
                *[{"id": m["id"], "type": "market", "title": m["title"],
                   "text": m.get("subtitle", ""), "source_name": m.get("exchange", ""),
                   "similarity": m["similarity"]} for m in markets],
                *[{"id": e["id"], "type": "event", "title": e["title"],
                   "text": e.get("description", ""), "source_name": e.get("exchange", ""),
                   "similarity": e["similarity"]} for e in events],
                *[{"id": i["id"], "type": "intelligence", "title": i["title"],
                   "text": i.get("text", ""), "source_name": i.get("source_name", ""),
                   "similarity": i["similarity"]} for i in intelligence],
            ],
            key=lambda x: x["similarity"],
            reverse=True,
        )

        try:
            rag_result = await synthesize_rag_answer(query, all_items[:10])
            result["narrative"] = rag_result["narrative"]
            result["citations"] = rag_result["citations"]
        except Exception as e:
            logger.error("[search] RAG synthesis failed, returning raw results: %s", e)

    return result
