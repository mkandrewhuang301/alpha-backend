"""
Shared taxonomy upsert helpers for ingest workers.

Provides slug normalization and PlatformTag upserts.
All taxonomy UI state flows through PlatformTag — there are no separate Category/Tag tables.
"""

import logging
import re
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


def slugify(value: str) -> str:
    """Convert a string to a URL-friendly slug."""
    slug = value.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


async def upsert_platform_tag(
    pool,
    exchange: str,
    tag_type: str,                        # "category" or "tag"
    slug: str,
    label: str,
    ext_id: Optional[str] = None,
    parent_ids: Optional[list] = None,    # list of parent slugs/IDs (multi-parent support)
    image_url: Optional[str] = None,
    is_carousel: bool = False,
    force_show: bool = False,
    force_hide: bool = False,
    platform_metadata: Optional[dict] = None,
) -> Optional[str]:
    """
    Upsert a PlatformTag row and return its slug (the critical linking key).

    The slug is the identifier that matches the strings stored in
    Series.categories, Series.tags, Event.categories, and Event.tags.

    parent_ids is a list of slug strings / IDs pointing to parent PlatformTags:
      - Kalshi tags:              parent_ids=[slugify(category_name)]   (single parent)
      - Polymarket subcategories: parent_ids=[slugify(parent_category)] (single parent)
      - Polymarket tags (Gamma):  parent_ids=[id1, id2, ...]            (multi-parent)
    """
    if not slug or not label:
        return None

    slug = slug.strip()
    label = label.strip()
    if not ext_id:
        ext_id = slug

    _parent_ids = parent_ids or []
    import json as _json
    _platform_metadata = _json.dumps(platform_metadata or {})
    internal_id = str(uuid.uuid4())

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO platform_tags
                    (id, exchange, type, ext_id, parent_ids, slug, label,
                     image_url, is_carousel, force_show, force_hide, platform_metadata, is_deleted)
                VALUES
                    ($1::uuid, $2::exchange_type, $3::platform_tag_type, $4, $5::text[], $6, $7,
                     $8, $9, $10, $11, $12::jsonb, FALSE)
                ON CONFLICT ON CONSTRAINT uq_platform_tags_exchange_slug_type
                DO UPDATE SET
                    label = EXCLUDED.label,
                    ext_id = EXCLUDED.ext_id,
                    parent_ids = EXCLUDED.parent_ids,
                    image_url = COALESCE(EXCLUDED.image_url, platform_tags.image_url),
                    is_carousel = EXCLUDED.is_carousel,
                    force_show = EXCLUDED.force_show,
                    force_hide = EXCLUDED.force_hide,
                    platform_metadata = EXCLUDED.platform_metadata
                """,
                internal_id,
                exchange,
                tag_type,
                ext_id,
                _parent_ids,
                slug,
                label,
                image_url,
                is_carousel,
                force_show,
                force_hide,
                _platform_metadata,
            )
        return slug
    except Exception as exc:
        logger.error(
            "[taxonomy] Failed to upsert platform_tag slug=%s exchange=%s type=%s: %s",
            slug, exchange, tag_type, exc,
        )
        return None
