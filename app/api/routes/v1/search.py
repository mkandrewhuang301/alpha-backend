"""
Semantic search endpoint — natural language search across markets, events,
and intelligence articles using pgvector cosine similarity.

GET /api/v1/search?q=...&limit=10&include_resolved=false&synthesize=false

Authenticated via get_current_user(). Requires INTELLIGENCE_ENABLED=true
for intelligence results (markets/events always searchable once embeddings exist).
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import OPENAI_API_KEY
from app.core.database import get_db
from app.models.db import User
from app.services.auth import get_current_user
from app.services.search import semantic_search

router = APIRouter()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class SearchResultItem(BaseModel):
    id: str
    type: str  # "market", "event", "intelligence"
    title: str
    similarity: float
    # Market/Event fields
    status: Optional[str] = None
    exchange: Optional[str] = None
    subtitle: Optional[str] = None
    description: Optional[str] = None
    # Intelligence fields
    source_domain: Optional[str] = None
    source_name: Optional[str] = None
    url: Optional[str] = None
    impact_level: Optional[str] = None
    published_at: Optional[str] = None
    text: Optional[str] = None


class CitationItem(BaseModel):
    id: str
    title: str
    type: str


class SearchResponse(BaseModel):
    markets: list[SearchResultItem]
    events: list[SearchResultItem]
    intelligence: list[SearchResultItem]
    narrative: Optional[str] = None
    citations: list[CitationItem] = []


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/search", response_model=SearchResponse)
async def search(
    q: str = Query(..., min_length=3, max_length=500, description="Search query"),
    limit: int = Query(10, ge=1, le=50, description="Results per category"),
    include_resolved: bool = Query(False, description="Include resolved markets/events"),
    synthesize: bool = Query(False, description="Generate GPT-4o narrative (costs ~$0.01-0.03)"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Semantic search across markets, events, and intelligence.

    Vectorizes the query and searches using pgvector cosine similarity.
    Optionally generates a GPT-4o narrative synthesis with citations.
    """
    if not OPENAI_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Search requires OPENAI_API_KEY to be configured",
        )

    result = await semantic_search(
        query=q,
        db=db,
        limit=limit,
        include_resolved=include_resolved,
        synthesize=synthesize,
    )
    return result
