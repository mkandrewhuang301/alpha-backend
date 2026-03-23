"""
Personalized intelligence feed — ranked by portfolio exposure, credibility, and recency.

Ranking formula:
    score = (portfolio_exposure * 0.4) + (credibility * 0.3) + (recency * 0.3)

Recency: exponential decay, half-life = 6 hours
Portfolio exposure: derived from user's active orders on matched markets
Credibility: from source_credibility table

Fallback: if user has no positions/activity, return high-impact global intelligence.
"""

import logging
import math
from datetime import datetime, timezone
from uuid import UUID

logger = logging.getLogger(__name__)

RECENCY_HALF_LIFE_HOURS = 6.0


def _recency_score(published_at: datetime) -> float:
    """Exponential decay score based on age. 1.0 = just published, 0.5 = 6h ago."""
    now = datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    age_hours = max(0, (now - published_at).total_seconds() / 3600)
    return math.exp(-0.693 * age_hours / RECENCY_HALF_LIFE_HOURS)  # ln(2) ≈ 0.693


async def get_user_interest_tags(user_id: str, pool) -> list[str]:
    """
    Build interest model from user activity:
    1. Open positions (UserOrder WHERE status IN live/partial/matched) → market → event → categories
    2. Markets shared in user's groups (GroupMessage WHERE type='market')
    """
    async with pool.acquire() as conn:
        # Get categories from active orders
        order_rows = await conn.fetch(
            """
            SELECT DISTINCT unnest(m.categories) AS tag
            FROM user_orders uo
            JOIN markets m ON m.id = uo.market_id
            WHERE uo.user_id = $1
              AND uo.status IN ('live', 'partial', 'matched_pending')
              AND m.categories IS NOT NULL
            """,
            UUID(user_id),
        )

        # Get categories from markets shared in user's groups
        group_rows = await conn.fetch(
            """
            SELECT DISTINCT unnest(m.categories) AS tag
            FROM group_messages gm
            JOIN group_memberships gms ON gms.group_id = gm.group_id
            JOIN markets m ON m.id = (gm.metadata->>'market_id')::uuid
            WHERE gms.user_id = $1
              AND gm.type = 'market'
              AND m.categories IS NOT NULL
            LIMIT 100
            """,
            UUID(user_id),
        )

    tags = set()
    for row in order_rows:
        if row["tag"]:
            tags.add(row["tag"])
    for row in group_rows:
        if row["tag"]:
            tags.add(row["tag"])

    return list(tags)


async def ranked_feed(
    user_id: str,
    pool,
    cursor: str | None = None,
    limit: int = 20,
    domain_filter: str | None = None,
    impact_filter: str | None = None,
) -> tuple[list[dict], str | None]:
    """
    Get personalized intelligence feed ranked by exposure + credibility + recency.

    Returns (items, next_cursor).
    """
    # Build user interest tags
    interest_tags = await get_user_interest_tags(user_id, pool)

    # Base query filters
    conditions = ["ei.is_deleted = false", "ei.nlp_status IN ('complete', 'partial')"]
    params = []
    param_idx = 1

    if domain_filter:
        conditions.append(f"ei.source_domain = ${param_idx}::source_domain_type")
        params.append(domain_filter)
        param_idx += 1

    if impact_filter:
        conditions.append(f"ei.impact_level = ${param_idx}::impact_level_type")
        params.append(impact_filter)
        param_idx += 1

    if cursor:
        conditions.append(f"ei.created_at < ${param_idx}::timestamptz")
        params.append(cursor)
        param_idx += 1

    where_clause = " AND ".join(conditions)

    async with pool.acquire() as conn:
        # Load credibility scores for ranking
        cred_rows = await conn.fetch(
            "SELECT source_domain, source_name, credibility_score FROM source_credibility"
        )
        cred_map = {
            (r["source_domain"], r["source_name"]): float(r["credibility_score"])
            for r in cred_rows
        }

        # Fetch intelligence items with their matched tags
        query = f"""
            SELECT ei.id, ei.title, ei.raw_text, ei.source_domain, ei.source_name,
                   ei.url, ei.impact_level, ei.published_at, ei.metadata,
                   ei.created_at,
                   COALESCE(
                       array_agg(DISTINCT unnest_tag) FILTER (WHERE unnest_tag IS NOT NULL),
                       ARRAY[]::text[]
                   ) AS matched_tags
            FROM external_intelligence ei
            LEFT JOIN intelligence_market_mapping imm ON imm.intelligence_id = ei.id
            LEFT JOIN LATERAL unnest(imm.matched_tags) AS unnest_tag ON true
            WHERE {where_clause}
            GROUP BY ei.id
            ORDER BY ei.created_at DESC
            LIMIT ${param_idx}
        """
        params.append(limit + 1)  # fetch one extra for cursor

        rows = await conn.fetch(query, *params)

    # Determine next cursor
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]
    next_cursor = rows[-1]["created_at"].isoformat() if rows and has_more else None

    # Score and rank
    items = []
    for row in rows:
        # Portfolio exposure: check if any matched tags overlap with user interests
        matched = set(row["matched_tags"] or [])
        interest_overlap = len(matched & set(interest_tags))
        exposure_score = min(1.0, interest_overlap / 3.0) if interest_tags else 0.0

        # Credibility score
        cred_key = (row["source_domain"], row["source_name"])
        credibility = cred_map.get(cred_key, 0.5)

        # Recency score
        recency = _recency_score(row["published_at"])

        # Combined score
        score = (exposure_score * 0.4) + (credibility * 0.3) + (recency * 0.3)

        metadata = row["metadata"] or {}

        items.append(
            {
                "id": str(row["id"]),
                "title": row["title"],
                "summary": metadata.get("summary", row["raw_text"][:200] if row["raw_text"] else ""),
                "source_domain": row["source_domain"],
                "source_name": row["source_name"],
                "url": row.get("url"),
                "impact_level": row["impact_level"],
                "published_at": row["published_at"].isoformat() if row["published_at"] else None,
                "matched_tags": list(matched),
                "relevance_score": round(score, 3),
            }
        )

    # Sort by relevance score (descending), then by published_at
    items.sort(key=lambda x: x["relevance_score"], reverse=True)

    # Fallback: if no personalized results and no filters, return high-impact global
    if not items and not interest_tags and not domain_filter:
        return await _global_fallback(pool, limit), None

    return items, next_cursor


async def _global_fallback(pool, limit: int) -> list[dict]:
    """Return high-impact global intelligence when user has no activity."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, raw_text, source_domain, source_name,
                   url, impact_level, published_at, metadata
            FROM external_intelligence
            WHERE is_deleted = false
              AND nlp_status IN ('complete', 'partial')
              AND impact_level IN ('high', 'medium')
            ORDER BY published_at DESC
            LIMIT $1
            """,
            limit,
        )

    return [
        {
            "id": str(r["id"]),
            "title": r["title"],
            "summary": (r["metadata"] or {}).get("summary", r["raw_text"][:200] if r["raw_text"] else ""),
            "source_domain": r["source_domain"],
            "source_name": r["source_name"],
            "url": r.get("url"),
            "impact_level": r["impact_level"],
            "published_at": r["published_at"].isoformat() if r["published_at"] else None,
            "matched_tags": [],
            "relevance_score": 0.0,
        }
        for r in rows
    ]
