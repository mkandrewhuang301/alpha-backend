"""
Personalized intelligence feed endpoint.

GET /api/v1/intelligence/feed?cursor=...&limit=20&domain=sports&impact=high

Authenticated, cursor-paginated. Returns intelligence ranked by portfolio
exposure, source credibility, and recency.
"""

from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.config import INTELLIGENCE_ENABLED
from app.core.database import get_asyncpg_pool
from app.models.db import User
from app.services.auth import get_current_user
from app.services.feed import ranked_feed

router = APIRouter()


# ---------------------------------------------------------------------------
# Enum validators for query params
# ---------------------------------------------------------------------------

class SourceDomainFilter(str, Enum):
    news = "news"
    social = "social"
    sports = "sports"
    crypto = "crypto"
    weather = "weather"


class ImpactLevelFilter(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class FeedItem(BaseModel):
    id: str
    title: str
    summary: str
    source_domain: str
    source_name: str
    url: Optional[str] = None
    impact_level: str
    published_at: Optional[str] = None
    matched_tags: list[str] = []
    relevance_score: float


class FeedResponse(BaseModel):
    items: list[FeedItem]
    next_cursor: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/intelligence/feed", response_model=FeedResponse)
async def get_intelligence_feed(
    cursor: Optional[str] = Query(None, description="Pagination cursor (ISO timestamp)"),
    limit: int = Query(20, ge=1, le=50, description="Number of items to return"),
    domain: Optional[SourceDomainFilter] = Query(None, description="Filter by source domain"),
    impact: Optional[ImpactLevelFilter] = Query(None, description="Filter by impact level"),
    current_user: User = Depends(get_current_user),
):
    """
    Personalized intelligence feed ranked by portfolio exposure,
    source credibility, and recency.
    """
    if not INTELLIGENCE_ENABLED:
        return FeedResponse(items=[], next_cursor=None)

    pool = await get_asyncpg_pool()

    items, next_cursor = await ranked_feed(
        user_id=str(current_user.id),
        pool=pool,
        cursor=cursor,
        limit=limit,
        domain_filter=domain.value if domain else None,
        impact_filter=impact.value if impact else None,
    )

    return FeedResponse(items=items, next_cursor=next_cursor)
