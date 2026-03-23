"""
Group domain service — group CRUD, membership management, message assembly.

All business logic lives here. Route handlers are thin wrappers that call these functions.
"""

import logging
import secrets
import uuid as _uuid
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException
from pydantic import AnyHttpUrl, BaseModel, field_validator
from sqlalchemy import select, update, func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import (
    Group,
    GroupMembership,
    GroupMessage,
    Market,
    UserOrder,
)

logger = logging.getLogger(__name__)

# Role precedence: higher = more privileged
ROLE_ORDER = {"owner": 4, "admin": 3, "member": 2, "follower": 1}

# Redis key helpers
def _member_set_key(group_id: UUID) -> str:
    return f"group_members:{group_id}"

def _unread_key(group_id: UUID, user_id: UUID) -> str:
    return f"unread:{group_id}:{user_id}"


# ---------------------------------------------------------------------------
# Metadata schemas (Pydantic) — validated in create_message
# ---------------------------------------------------------------------------

class TradeSnapshot(BaseModel):
    market_title: str
    exchange: str
    side: str
    price: float
    size: float
    status: str

class TradeMetadata(BaseModel):
    source: str  # "user_order" | "public_trade"
    record_id: UUID
    snapshot: TradeSnapshot
    resolved: bool = False
    profit_pct: Optional[float] = None

    @field_validator("source")
    @classmethod
    def _check_source(cls, v: str) -> str:
        if v not in ("user_order", "public_trade"):
            raise ValueError("source must be 'user_order' or 'public_trade'")
        return v


class MarketSnapshot(BaseModel):
    title: str
    yes_price: float
    no_price: float
    close_time: Optional[str] = None
    image_url: Optional[str] = None


class MarketMetadata(BaseModel):
    exchange: str
    market_id: UUID
    shared_yes_price: float
    snapshot: MarketSnapshot


class ImageMetadata(BaseModel):
    url: AnyHttpUrl
    width: Optional[int] = None
    height: Optional[int] = None
    mime_type: Optional[str] = None


class ArticleMetadata(BaseModel):
    url: AnyHttpUrl
    title: str
    source: str
    image_url: Optional[AnyHttpUrl] = None


class ProfileMetadata(BaseModel):
    user_id: UUID
    username: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None


class SystemMetadata(BaseModel):
    event: str  # "price_alert"|"resolution"|"member_joined"|"member_left"
    market_id: Optional[UUID] = None
    detail: str


_METADATA_SCHEMAS: dict[str, type[BaseModel]] = {
    "image": ImageMetadata,
    "trade": TradeMetadata,
    "market": MarketMetadata,
    "article": ArticleMetadata,
    "profile": ProfileMetadata,
    "system": SystemMetadata,
}


def _validate_metadata(msg_type: str, metadata: dict) -> dict:
    """Validate and normalise message metadata by type. Returns clean dict."""
    if msg_type == "text":
        return {}
    schema = _METADATA_SCHEMAS.get(msg_type)
    if schema is None:
        return {}
    return schema(**metadata).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Authorization helpers
# ---------------------------------------------------------------------------

async def get_membership(
    group_id: UUID,
    user_id: UUID,
    db: AsyncSession,
) -> Optional[GroupMembership]:
    stmt = select(GroupMembership).where(
        GroupMembership.group_id == group_id,
        GroupMembership.user_id == user_id,
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_group_or_404_authorized(
    group_id: UUID,
    user_id: UUID,
    db: AsyncSession,
) -> tuple[Group, Optional[GroupMembership]]:
    """
    Returns (group, membership) if the user can see it.
    - Private groups: raises 404 if user is not a member (avoids leaking existence).
    - Public groups: returns (group, None) if user is not a member.
    Always fetches membership so callers can skip a second query for role checks.
    """
    group = await db.get(Group, group_id)
    if not group or group.is_deleted:
        raise HTTPException(status_code=404, detail="Group not found")

    membership = await get_membership(group_id, user_id, db)
    if group.access_type == "private" and not membership:
        raise HTTPException(status_code=404, detail="Group not found")

    return group, membership


async def assert_min_role(
    group_id: UUID,
    user_id: UUID,
    min_role: str,
    db: AsyncSession,
    membership: Optional[GroupMembership] = None,
) -> GroupMembership:
    """Raises HTTP 403 if user's role is below min_role. Returns the membership.

    Pass `membership` if already fetched (e.g. from get_group_or_404_authorized)
    to avoid a redundant DB query.
    """
    if membership is None:
        membership = await get_membership(group_id, user_id, db)
    if not membership:
        raise HTTPException(status_code=403, detail="Not a member of this group")
    if ROLE_ORDER.get(membership.role, 0) < ROLE_ORDER.get(min_role, 0):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return membership


# ---------------------------------------------------------------------------
# Group CRUD
# ---------------------------------------------------------------------------

async def create_group(
    name: str,
    slug: str,
    access_type: str,
    owner_id: UUID,
    db: AsyncSession,
    redis,
    description: Optional[str] = None,
    avatar_url: Optional[str] = None,
) -> Group:
    """Create a new group, add the owner as first member, init Redis member set."""
    # Normalize slug (same function used across the taxonomy)
    from app.workers.taxonomy import slugify as _slugify
    slug = _slugify(slug)

    # Check slug uniqueness (no is_deleted filter — DB constraint is absolute)
    existing = await db.execute(
        select(Group).where(Group.slug == slug)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A group with this slug already exists")

    invite_code = secrets.token_hex(16) if access_type == "private" else None
    max_members = 50 if access_type == "private" else None  # None = unlimited for public

    group = Group(
        name=name,
        slug=slug,
        description=description,
        avatar_url=avatar_url,
        access_type=access_type,
        owner_id=owner_id,
        invite_code=invite_code,
        max_members=max_members,
        member_count=1,
    )
    db.add(group)
    await db.flush()  # Get the generated ID

    membership = GroupMembership(
        group_id=group.id,
        user_id=owner_id,
        role="owner",
    )
    db.add(membership)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A group with this slug already exists")
    await db.refresh(group)

    # Initialize Redis member set
    key = _member_set_key(group.id)
    await redis.sadd(key, str(owner_id))
    await redis.expire(key, 86400)  # 24hr TTL

    return group


# ---------------------------------------------------------------------------
# Membership management
# ---------------------------------------------------------------------------

async def _get_or_refresh_member_set(group_id: UUID, db: AsyncSession, redis) -> set[str]:
    """
    Return the Redis group_members set for fan-out, refreshing from DB if missing.
    """
    key = _member_set_key(group_id)
    members = await redis.smembers(key)
    if members:
        return members

    # Cache miss — rebuild from DB
    stmt = select(GroupMembership.user_id).where(
        GroupMembership.group_id == group_id
    )
    result = await db.execute(stmt)
    user_ids = [str(row[0]) for row in result.fetchall()]
    if user_ids:
        await redis.sadd(key, *user_ids)
        await redis.expire(key, 86400)
    return set(user_ids)


async def add_member(
    group_id: UUID,
    user_id: UUID,
    role: str,
    db: AsyncSession,
    redis,
) -> GroupMembership:
    """Add a user to a group. Raises 409 if already a member."""
    existing = await get_membership(group_id, user_id, db)
    if existing:
        raise HTTPException(status_code=409, detail="Already a member of this group")

    # Lock the group row to prevent concurrent joins from exceeding max_members.
    # SELECT FOR UPDATE ensures two concurrent requests serialize here.
    locked_group = await db.execute(
        select(Group).where(Group.id == group_id).with_for_update()
    )
    group_row = locked_group.scalar_one_or_none()
    if not group_row:
        raise HTTPException(status_code=404, detail="Group not found")
    if group_row.max_members is not None and group_row.member_count >= group_row.max_members:
        raise HTTPException(status_code=409, detail="Group is full")

    membership = GroupMembership(group_id=group_id, user_id=user_id, role=role)
    db.add(membership)

    # Increment denormalized member_count
    await db.execute(
        update(Group)
        .where(Group.id == group_id)
        .values(member_count=Group.member_count + 1)
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Already a member of this group")

    # Update Redis member set
    key = _member_set_key(group_id)
    await redis.sadd(key, str(user_id))
    await redis.expire(key, 86400)

    return membership


async def remove_member(
    group_id: UUID,
    user_id: UUID,
    db: AsyncSession,
    redis,
) -> None:
    """Remove a user from a group. Raises 404 if not a member."""
    membership = await get_membership(group_id, user_id, db)
    if not membership:
        raise HTTPException(status_code=404, detail="Not a member of this group")
    if membership.role == "owner":
        raise HTTPException(status_code=400, detail="Owner cannot leave the group")

    await db.delete(membership)
    await db.execute(
        update(Group)
        .where(Group.id == group_id)
        .values(member_count=func.greatest(Group.member_count - 1, 0))
    )
    await db.commit()

    # Update Redis member set
    key = _member_set_key(group_id)
    await redis.srem(key, str(user_id))

    # Clear unread counter
    await redis.delete(_unread_key(group_id, user_id))


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

async def create_message(
    group_id: UUID,
    sender_id: UUID,
    msg_type: str,
    content: Optional[str],
    metadata: dict,
    db: AsyncSession,
    redis,
    reply_to_id: Optional[UUID] = None,
) -> GroupMessage:
    """
    Validate + insert a new group message, then fan-out unread INCR to all members.
    """
    # Content validation
    if msg_type == "text":
        if not content or not content.strip():
            raise HTTPException(status_code=422, detail="Text messages require content")
        if len(content) > 2000:
            raise HTTPException(status_code=422, detail="Message too long (max 2000 chars)")
        content = content.replace("\x00", "").strip()

    # Metadata validation by type
    clean_metadata = _validate_metadata(msg_type, metadata)

    # Trade message: verify the user_order belongs to the sender
    if msg_type == "trade":
        trade_meta = TradeMetadata(**metadata)
        if trade_meta.source == "user_order":
            order = await db.get(UserOrder, trade_meta.record_id)
            if not order or order.user_id != sender_id:
                raise HTTPException(
                    status_code=403,
                    detail="Cannot share a trade that doesn't belong to you",
                )

    # Market message: verify the market exists and is not deleted
    if msg_type == "market":
        market_meta = MarketMetadata(**metadata)
        market = await db.get(Market, market_meta.market_id)
        if not market or market.is_deleted:
            raise HTTPException(status_code=404, detail="Market not found")

    message = GroupMessage(
        group_id=group_id,
        sender_id=sender_id,
        type=msg_type,
        content=content,
        msg_metadata=clean_metadata,
        reply_to_id=reply_to_id,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)

    # Fan-out unread INCR to all members except the sender
    try:
        member_ids = await _get_or_refresh_member_set(group_id, db, redis)
        if member_ids:
            pipe = redis.pipeline()
            for uid in member_ids:
                if uid != str(sender_id):
                    try:
                        unread_key = _unread_key(group_id, _uuid.UUID(uid))
                    except ValueError:
                        logger.warning("Skipping malformed member ID in fan-out: %r", uid)
                        continue
                    pipe.incr(unread_key)
                    pipe.expire(unread_key, 30 * 86400)  # 30-day TTL
            await pipe.execute()
    except Exception as exc:
        logger.warning("Unread fan-out failed for group %s: %s", group_id, exc)

    # Fire-and-forget: enqueue NLP processing for article messages
    if msg_type == "article":
        try:
            from app.core.config import INTELLIGENCE_ENABLED
            if INTELLIGENCE_ENABLED:
                from arq import ArqRedis
                arq_redis = ArqRedis(pool_or_conn=redis.connection_pool)
                await arq_redis.enqueue_job(
                    "run_process_article_intelligence",
                    str(message.id),
                    str(group_id),
                )
        except Exception as exc:
            logger.warning("Article NLP enqueue failed for msg %s: %s", message.id, exc)

    return message


async def list_messages(
    group_id: UUID,
    db: AsyncSession,
    cursor: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """
    Cursor-based message pagination (cursor = ISO timestamp).
    Returns list of message dicts with sender profile joined in Python.
    """
    from sqlalchemy import and_

    stmt = (
        select(GroupMessage)
        .where(
            and_(
                GroupMessage.group_id == group_id,
                GroupMessage.is_deleted.is_(False),
            )
        )
        .order_by(GroupMessage.created_at.desc())
        .limit(limit)
    )
    if cursor:
        from datetime import datetime, timezone
        try:
            cursor_dt = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
            stmt = stmt.where(GroupMessage.created_at < cursor_dt)
        except ValueError:
            pass  # Invalid cursor — ignore

    result = await db.execute(stmt)
    messages = result.scalars().all()

    # Collect unique sender IDs for batch profile lookup
    sender_ids = list({m.sender_id for m in messages if m.sender_id is not None})
    sender_map: dict[UUID, Any] = {}
    if sender_ids:
        from app.models.db import User
        users_stmt = select(
            User.id, User.username, User.display_name, User.avatar_url, User.is_verified
        ).where(User.id.in_(sender_ids))
        users_result = await db.execute(users_stmt)
        for row in users_result.fetchall():
            sender_map[row.id] = {
                "id": str(row.id),
                "username": row.username,
                "display_name": row.display_name,
                "avatar_url": row.avatar_url,
                "is_verified": row.is_verified,
            }

    output = []
    for msg in messages:
        output.append({
            "id": str(msg.id),
            "group_id": str(msg.group_id),
            "sender": sender_map.get(msg.sender_id) if msg.sender_id else None,
            "type": msg.type,
            "content": msg.content,
            "metadata": msg.msg_metadata or {},
            "reply_to_id": str(msg.reply_to_id) if msg.reply_to_id else None,
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
        })

    return output


async def get_unread_counts(
    group_ids: list[UUID],
    user_id: UUID,
    redis,
) -> dict[str, int]:
    """Return {group_id_str: unread_count} for all supplied groups."""
    if not group_ids:
        return {}

    keys = [_unread_key(gid, user_id) for gid in group_ids]
    values = await redis.mget(*keys)
    return {
        str(gid): int(v or 0)
        for gid, v in zip(group_ids, values)
    }


async def reset_unread(group_id: UUID, user_id: UUID, redis) -> None:
    """Delete the unread counter for a group+user (user opened the group)."""
    await redis.delete(_unread_key(group_id, user_id))
