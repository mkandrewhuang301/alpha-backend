"""
PlatformTags endpoints — taxonomy UI metadata for both exchanges.

GET /categories/platform-tags/       - list tags filterable by exchange, type, parent_ids
GET /categories/platform-tags/{id}   - single tag by UUID
"""

from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.db import PlatformTag

router = APIRouter()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class PlatformTagResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    exchange: str
    type: str                               # "category" | "tag"
    ext_id: str
    parent_ids: List[str] = []              # Parent slugs/IDs (multi-parent support)
    slug: str                               # The critical link to Event/Series ARRAY columns
    label: str                              # UI display text
    image_url: Optional[str] = None
    is_carousel: bool = False
    force_show: bool = False
    force_hide: bool = False
    platform_metadata: Optional[Dict[str, Any]] = None


class PlatformTagListResponse(BaseModel):
    platform_tags: List[PlatformTagResponse]
    count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _platform_tag_to_response(row: PlatformTag) -> PlatformTagResponse:
    return PlatformTagResponse(
        id=row.id,
        exchange=row.exchange,
        type=row.type,
        ext_id=row.ext_id,
        parent_ids=row.parent_ids or [],
        slug=row.slug,
        label=row.label,
        image_url=row.image_url,
        is_carousel=row.is_carousel,
        force_show=row.force_show,
        force_hide=row.force_hide,
        platform_metadata=row.platform_metadata,
    )


# ---------------------------------------------------------------------------
# PlatformTag routes
# ---------------------------------------------------------------------------

@router.get("/platform-tags/", response_model=PlatformTagListResponse)
async def list_platform_tags(
    exchange: Optional[str] = Query(default=None, description="Filter by exchange: 'kalshi' | 'polymarket'"),
    type: Optional[str] = Query(default=None, description="Filter by type: 'category' | 'tag'"),
    parent_id: Optional[str] = Query(default=None, description="Filter by parent slug/ID (containment). Pass empty string for top-level items with no parents."),
    is_carousel: Optional[bool] = Query(default=None, description="Filter to carousel items only"),
    db: AsyncSession = Depends(get_db),
) -> PlatformTagListResponse:
    """
    List platform tags for UI display.

    The `slug` field in each result matches slug strings stored in
    Series.categories, Series.tags, Event.categories, and Event.tags.

    - `type=category` — top-level category items (parent_ids=[]) or subcategories
    - `type=tag` — tag items; `parent_ids` links to the parent category slugs
    - `parent_id=<slug>` — filter to children of a given parent (array containment)
    - `parent_id=` (empty string) — filter to top-level items (parent_ids is empty)
    - `is_carousel=true` — items flagged for carousel display
    """
    stmt = select(PlatformTag).where(PlatformTag.is_deleted == False)

    if exchange:
        stmt = stmt.where(PlatformTag.exchange == exchange)
    if type:
        stmt = stmt.where(PlatformTag.type == type)
    if parent_id is not None:
        if parent_id == "":
            # Top-level: parent_ids is null or empty array
            stmt = stmt.where(
                (PlatformTag.parent_ids == None) | (PlatformTag.parent_ids == [])
            )
        else:
            # Children of a specific parent — GIN array containment
            stmt = stmt.where(
                PlatformTag.parent_ids.contains([parent_id])
            )
    if is_carousel is not None:
        stmt = stmt.where(PlatformTag.is_carousel == is_carousel)

    stmt = stmt.order_by(PlatformTag.label)

    result = await db.execute(stmt)
    rows = result.scalars().all()

    return PlatformTagListResponse(
        platform_tags=[_platform_tag_to_response(r) for r in rows],
        count=len(rows),
    )


@router.get("/platform-tags/{tag_id}", response_model=PlatformTagResponse)
async def get_platform_tag(
    tag_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> PlatformTagResponse:
    """Fetch a single platform tag by its internal UUID."""
    result = await db.execute(
        select(PlatformTag).where(
            PlatformTag.id == tag_id,
            PlatformTag.is_deleted == False,
        )
    )
    row = result.scalar_one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail=f"PlatformTag '{tag_id}' not found")

    return _platform_tag_to_response(row)
