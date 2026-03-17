"""
Group endpoints.

POST   /api/v1/groups                              create group
GET    /api/v1/groups                              my groups
GET    /api/v1/groups/discover                     public channels
GET    /api/v1/groups/{group_id}                   get group
PUT    /api/v1/groups/{group_id}                   update group (owner/admin)
DELETE /api/v1/groups/{group_id}                   soft delete (owner only)
POST   /api/v1/groups/{group_id}/join              join public group
POST   /api/v1/groups/join/{code}                  join via invite code
POST   /api/v1/groups/{group_id}/leave             leave group
POST   /api/v1/groups/{group_id}/invite            generate invite link
GET    /api/v1/groups/{group_id}/members           list members
PUT    /api/v1/groups/{group_id}/members/{user_id} update member role
DELETE /api/v1/groups/{group_id}/members/{user_id} kick member
PUT    /api/v1/groups/{group_id}/read              reset unread for caller
"""

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.limiter import _get_user_key, limiter

from app.core.database import get_db
from app.models.db import Group, GroupMembership, User
from app.services.auth import get_current_user
from app.services.groups import (
    ROLE_ORDER,
    add_member,
    assert_min_role,
    create_group,
    get_group_or_404_authorized,
    get_membership,
    remove_member,
    reset_unread,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/groups")

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class CreateGroupRequest(BaseModel):
    name: str
    slug: Optional[str] = None
    description: Optional[str] = None
    avatar_url: Optional[str] = None
    access_type: str = "private"

    @field_validator("access_type")
    @classmethod
    def _check_access_type(cls, v: str) -> str:
        if v not in ("private", "public"):
            raise ValueError("access_type must be 'private' or 'public'")
        return v

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Name cannot be empty")
        if len(v) > 100:
            raise ValueError("Name too long (max 100 chars)")
        return v.strip()


class UpdateGroupRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    avatar_url: Optional[str] = None


class UpdateMemberRoleRequest(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        if v not in ("admin", "member", "follower"):
            raise ValueError("role must be admin, member, or follower")
        return v


class GroupResponse(BaseModel):
    id: str
    name: str
    slug: str
    description: Optional[str]
    avatar_url: Optional[str]
    access_type: str
    owner_id: str
    invite_code: Optional[str]
    max_members: Optional[int]
    member_count: int
    created_at: Optional[str]


class MemberResponse(BaseModel):
    user_id: str
    username: Optional[str]
    display_name: Optional[str]
    avatar_url: Optional[str]
    is_verified: bool
    role: str
    joined_at: Optional[str]


def _group_to_response(group: Group, include_invite: bool = False) -> GroupResponse:
    return GroupResponse(
        id=str(group.id),
        name=group.name,
        slug=group.slug,
        description=group.description,
        avatar_url=group.avatar_url,
        access_type=group.access_type,
        owner_id=str(group.owner_id),
        invite_code=group.invite_code if include_invite else None,
        max_members=group.max_members,
        member_count=group.member_count,
        created_at=group.created_at.isoformat() if group.created_at else None,
    )


# ---------------------------------------------------------------------------
# Create / list / discover
# ---------------------------------------------------------------------------

@router.post("", response_model=GroupResponse, status_code=201)
async def create_new_group(
    body: CreateGroupRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GroupResponse:
    from app.core.redis import get_redis
    from app.workers.taxonomy import slugify

    redis = await get_redis()
    slug = body.slug or slugify(body.name)
    group = await create_group(
        name=body.name,
        slug=slug,
        access_type=body.access_type,
        owner_id=current_user.id,
        db=db,
        redis=redis,
        description=body.description,
        avatar_url=body.avatar_url,
    )
    return _group_to_response(group, include_invite=True)


@router.get("", response_model=list[GroupResponse])
async def get_my_groups(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[GroupResponse]:
    """Return all groups the current user is a member of."""
    stmt = (
        select(Group)
        .join(GroupMembership, GroupMembership.group_id == Group.id)
        .where(
            GroupMembership.user_id == current_user.id,
            Group.is_deleted.is_(False),
        )
        .order_by(Group.created_at.desc())
    )
    result = await db.execute(stmt)
    groups = result.scalars().all()
    return [_group_to_response(g, include_invite=True) for g in groups]


@router.get("/discover", response_model=list[GroupResponse])
async def discover_groups(
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(None, description="Compound cursor: '{member_count}:{group_id}' from previous page's last group"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[GroupResponse]:
    """
    Public groups ordered by member_count desc.
    Cursor pagination: pass '{member_count}:{group_id}' from the last group on the
    previous page to get the next page. Handles ties in member_count correctly.
    """
    from sqlalchemy import desc, or_, and_

    stmt = (
        select(Group)
        .where(
            Group.access_type == "public",
            Group.is_deleted.is_(False),
        )
        .order_by(desc(Group.member_count), desc(Group.id))
        .limit(limit)
    )
    if cursor:
        try:
            count_str, gid_str = cursor.split(":", 1)
            cursor_count = int(count_str)
            cursor_gid = UUID(gid_str)
            stmt = stmt.where(
                or_(
                    Group.member_count < cursor_count,
                    and_(Group.member_count == cursor_count, Group.id < cursor_gid),
                )
            )
        except (ValueError, AttributeError):
            pass  # Invalid cursor — return first page

    result = await db.execute(stmt)
    groups = result.scalars().all()
    return [_group_to_response(g) for g in groups]


# ---------------------------------------------------------------------------
# Get / Update / Delete
# ---------------------------------------------------------------------------

@router.get("/{group_id}", response_model=GroupResponse)
async def get_group(
    group_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GroupResponse:
    group, membership = await get_group_or_404_authorized(group_id, current_user.id, db)
    return _group_to_response(group, include_invite=membership is not None)


@router.put("/{group_id}", response_model=GroupResponse)
async def update_group(
    group_id: UUID,
    body: UpdateGroupRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GroupResponse:
    group, membership = await get_group_or_404_authorized(group_id, current_user.id, db)
    await assert_min_role(group_id, current_user.id, "admin", db, membership=membership)

    if body.name is not None:
        group.name = body.name.strip()
    if body.description is not None:
        group.description = body.description
    if body.avatar_url is not None:
        group.avatar_url = body.avatar_url

    await db.commit()
    await db.refresh(group)
    return _group_to_response(group, include_invite=True)


@router.delete("/{group_id}", status_code=200)
async def delete_group(
    group_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    group, membership = await get_group_or_404_authorized(group_id, current_user.id, db)
    await assert_min_role(group_id, current_user.id, "owner", db, membership=membership)

    group.is_deleted = True
    await db.commit()
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Join / Leave / Invite
# ---------------------------------------------------------------------------

@router.post("/{group_id}/join", status_code=200)
async def join_public_group(
    group_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    group = await db.get(Group, group_id)
    if not group or group.is_deleted:
        raise HTTPException(status_code=404, detail="Group not found")
    if group.access_type != "public":
        raise HTTPException(status_code=403, detail="This is a private group. Use an invite link.")

    # Check max_members
    if group.max_members is not None and group.member_count >= group.max_members:
        raise HTTPException(status_code=409, detail="Group is full")

    from app.core.redis import get_redis
    redis = await get_redis()
    membership = await add_member(group_id, current_user.id, "member", db, redis)
    return {"status": "joined", "role": membership.role}


@router.post("/join/{code}", status_code=200)
@limiter.limit("10/minute")
async def join_via_invite(
    request: Request,
    code: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GroupResponse:
    """Join a private group via invite code."""
    stmt = select(Group).where(
        Group.invite_code == code,
        Group.is_deleted.is_(False),
    )
    result = await db.execute(stmt)
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Invalid or expired invite link")

    if group.max_members is not None and group.member_count >= group.max_members:
        raise HTTPException(status_code=409, detail="Group is full")

    # Already a member? Return current state.
    existing = await get_membership(group.id, current_user.id, db)
    if existing:
        return _group_to_response(group, include_invite=True)

    from app.core.redis import get_redis
    redis = await get_redis()
    await add_member(group.id, current_user.id, "member", db, redis)
    await db.refresh(group)
    return _group_to_response(group, include_invite=True)


@router.post("/{group_id}/leave", status_code=200)
async def leave_group(
    group_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.core.redis import get_redis
    redis = await get_redis()
    await remove_member(group_id, current_user.id, db, redis)
    return {"status": "left"}


@router.post("/{group_id}/invite")
async def get_or_regenerate_invite(
    group_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return the current invite link (or generate one for public groups)."""
    group, membership = await get_group_or_404_authorized(group_id, current_user.id, db)
    await assert_min_role(group_id, current_user.id, "admin", db, membership=membership)

    if not group.invite_code:
        import secrets
        group.invite_code = secrets.token_hex(16)
        await db.commit()
        await db.refresh(group)

    return {
        "invite_code": group.invite_code,
        "invite_url": f"alpha://group/join/{group.invite_code}",
    }


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

@router.get("/{group_id}/members", response_model=list[MemberResponse])
async def list_members(
    group_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MemberResponse]:
    await get_group_or_404_authorized(group_id, current_user.id, db)

    stmt = (
        select(GroupMembership, User)
        .join(User, User.id == GroupMembership.user_id)
        .where(GroupMembership.group_id == group_id)
        .order_by(GroupMembership.joined_at)
    )
    result = await db.execute(stmt)
    rows = result.fetchall()

    return [
        MemberResponse(
            user_id=str(m.user_id),
            username=u.username,
            display_name=u.display_name,
            avatar_url=u.avatar_url,
            is_verified=u.is_verified or False,
            role=m.role,
            joined_at=m.joined_at.isoformat() if m.joined_at else None,
        )
        for m, u in rows
    ]


@router.put("/{group_id}/members/{target_user_id}", status_code=200)
async def update_member_role(
    group_id: UUID,
    target_user_id: UUID,
    body: UpdateMemberRoleRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _, m = await get_group_or_404_authorized(group_id, current_user.id, db)
    caller_membership = await assert_min_role(group_id, current_user.id, "admin", db, membership=m)

    target_membership = await get_membership(group_id, target_user_id, db)
    if not target_membership:
        raise HTTPException(status_code=404, detail="User is not a member of this group")
    if target_membership.role == "owner":
        raise HTTPException(status_code=400, detail="Cannot change the owner's role")

    # Admins can't change other admins (only owner can)
    if (
        target_membership.role == "admin"
        and caller_membership.role != "owner"
    ):
        raise HTTPException(status_code=403, detail="Only the group owner can change admin roles")

    target_membership.role = body.role
    await db.commit()
    return {"status": "updated", "role": body.role}


@router.delete("/{group_id}/members/{target_user_id}", status_code=200)
async def kick_member(
    group_id: UUID,
    target_user_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _, m = await get_group_or_404_authorized(group_id, current_user.id, db)
    caller_membership = await assert_min_role(group_id, current_user.id, "admin", db, membership=m)

    target_membership = await get_membership(group_id, target_user_id, db)
    if not target_membership:
        raise HTTPException(status_code=404, detail="User is not a member")
    if target_membership.role == "owner":
        raise HTTPException(status_code=400, detail="Cannot kick the group owner")
    if target_membership.role == "admin" and caller_membership.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can kick admins")
    if target_user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Use /leave to leave the group")

    from app.core.redis import get_redis
    redis = await get_redis()
    await remove_member(group_id, target_user_id, db, redis)
    return {"status": "kicked"}


# ---------------------------------------------------------------------------
# Read receipt
# ---------------------------------------------------------------------------

@router.put("/{group_id}/read", status_code=200)
async def mark_group_read(
    group_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Reset unread count for the current user in this group."""
    _, membership = await get_group_or_404_authorized(group_id, current_user.id, db)

    from app.core.redis import get_redis
    redis = await get_redis()
    await reset_unread(group_id, current_user.id, redis)

    # Also update last_read_at in the DB
    from datetime import datetime, timezone
    if membership:
        membership.last_read_at = datetime.now(timezone.utc)
        await db.commit()

    return {"status": "read"}
