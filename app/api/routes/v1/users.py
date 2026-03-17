"""
User and auth endpoints.

Auth:
  POST /api/v1/auth/register    {email, password, username}
  POST /api/v1/auth/login       {email, password} → JWT
  POST /api/v1/auth/refresh     {refresh_token} → new JWT

Users:
  GET    /api/v1/users/me
  PUT    /api/v1/users/me
  GET    /api/v1/users/search?q=
  GET    /api/v1/users/{username}
  POST   /api/v1/users/{user_id}/follow
  DELETE /api/v1/users/{user_id}/follow
  GET    /api/v1/users/me/following
  GET    /api/v1/users/me/followers
  GET    /api/v1/users/me/unread
"""

import logging
import re
from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import SUPABASE_ANON_KEY, SUPABASE_URL
from app.core.database import get_db
from app.core.limiter import _get_user_key, limiter
from app.models.db import Group, GroupMembership, User, UserFollow
from app.services.auth import get_current_user
from app.services.groups import get_unread_counts

logger = logging.getLogger(__name__)
router = APIRouter()

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,50}$")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    username: str

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v

    @field_validator("username")
    @classmethod
    def _check_username(cls, v: str) -> str:
        v = v.lower()
        if not _USERNAME_RE.match(v):
            raise ValueError("Username must be 3-50 chars, letters/numbers/underscores only")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str
    user_id: str


class UserProfileResponse(BaseModel):
    id: str
    email: str
    username: Optional[str]
    display_name: Optional[str]
    avatar_url: Optional[str]
    bio: Optional[str]
    is_verified: bool


class PublicProfileResponse(BaseModel):
    id: str
    username: Optional[str]
    display_name: Optional[str]
    avatar_url: Optional[str]
    bio: Optional[str]
    is_verified: bool


class UpdateProfileRequest(BaseModel):
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    bio: Optional[str] = None
    username: Optional[str] = None

    @field_validator("username")
    @classmethod
    def _check_username(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.lower()
        if not _USERNAME_RE.match(v):
            raise ValueError("Username must be 3-50 chars, letters/numbers/underscores only")
        return v

    @field_validator("bio")
    @classmethod
    def _check_bio(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v) > 500:
            raise ValueError("Bio must be 500 characters or less")
        return v


# ---------------------------------------------------------------------------
# Supabase Auth helpers
# ---------------------------------------------------------------------------

def _supabase_headers() -> dict:
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Content-Type": "application/json",
    }


async def _supabase_auth_post(path: str, body: dict) -> dict:
    """POST to Supabase Auth REST API. Raises HTTPException on error."""
    url = f"{SUPABASE_URL}/auth/v1{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=body, headers=_supabase_headers())

    if resp.status_code not in (200, 201):
        data = resp.json()
        msg = data.get("error_description") or data.get("msg") or data.get("error") or "Auth error"
        raise HTTPException(status_code=resp.status_code, detail=msg)

    return resp.json()


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@router.post("/auth/register", response_model=AuthResponse, status_code=201)
@limiter.limit("10/minute")
async def register(
    request: Request,
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> AuthResponse:
    """Register a new user with Supabase Auth and create a profile row in our DB."""
    # 1. Check username uniqueness BEFORE calling Supabase Auth.
    #    This prevents orphaned Supabase users: if we created the Supabase user first
    #    and then the DB insert failed, the user would be permanently locked out of Alpha.
    existing = await db.execute(
        select(User).where(User.username == body.username)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username already taken")

    # 2. Create user in Supabase Auth
    data = await _supabase_auth_post("/signup", {
        "email": body.email,
        "password": body.password,
        "data": {"username": body.username},
    })

    supabase_user = data.get("user") or {}
    supabase_uid = supabase_user.get("id")
    if not supabase_uid:
        raise HTTPException(status_code=500, detail="Supabase returned no user ID")

    session = data.get("session") or data  # Supabase v2 vs v1 shape
    access_token = session.get("access_token", "")
    refresh_token = session.get("refresh_token", "")
    expires_in = session.get("expires_in", 3600)

    # 3. Create profile row
    user = User(
        email=body.email,
        supabase_uid=supabase_uid,
        username=body.username,
    )
    db.add(user)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Email or username already registered")

    return AuthResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=expires_in,
        refresh_token=refresh_token,
        user_id=str(user.id),
    )


@router.post("/auth/login", response_model=AuthResponse)
@limiter.limit("10/minute")
async def login(request: Request, body: LoginRequest) -> AuthResponse:
    """Authenticate with Supabase Auth. Returns JWT."""
    data = await _supabase_auth_post(
        "/token?grant_type=password",
        {"email": body.email, "password": body.password},
    )

    user = data.get("user") or {}
    return AuthResponse(
        access_token=data.get("access_token", ""),
        token_type="bearer",
        expires_in=data.get("expires_in", 3600),
        refresh_token=data.get("refresh_token", ""),
        user_id=user.get("id", ""),
    )


@router.post("/auth/refresh", response_model=AuthResponse)
async def refresh_token(request: Request, body: RefreshRequest) -> AuthResponse:
    """Exchange a refresh token for a new access token."""
    data = await _supabase_auth_post(
        "/token?grant_type=refresh_token",
        {"refresh_token": body.refresh_token},
    )

    user = data.get("user") or {}
    return AuthResponse(
        access_token=data.get("access_token", ""),
        token_type="bearer",
        expires_in=data.get("expires_in", 3600),
        refresh_token=data.get("refresh_token", ""),
        user_id=user.get("id", ""),
    )


# ---------------------------------------------------------------------------
# Profile endpoints
# ---------------------------------------------------------------------------

@router.get("/users/me", response_model=UserProfileResponse)
async def get_me(current_user: User = Depends(get_current_user)) -> UserProfileResponse:
    return UserProfileResponse(
        id=str(current_user.id),
        email=current_user.email,
        username=current_user.username,
        display_name=current_user.display_name,
        avatar_url=current_user.avatar_url,
        bio=current_user.bio,
        is_verified=current_user.is_verified or False,
    )


@router.put("/users/me", response_model=UserProfileResponse)
async def update_me(
    body: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserProfileResponse:
    # Username uniqueness check
    if body.username and body.username != current_user.username:
        existing = await db.execute(
            select(User).where(User.username == body.username, User.id != current_user.id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Username already taken")

    if body.username is not None:
        current_user.username = body.username
    if body.display_name is not None:
        current_user.display_name = body.display_name
    if body.avatar_url is not None:
        current_user.avatar_url = body.avatar_url
    if body.bio is not None:
        current_user.bio = body.bio

    await db.commit()
    await db.refresh(current_user)

    return UserProfileResponse(
        id=str(current_user.id),
        email=current_user.email,
        username=current_user.username,
        display_name=current_user.display_name,
        avatar_url=current_user.avatar_url,
        bio=current_user.bio,
        is_verified=current_user.is_verified or False,
    )


@router.get("/users/search", response_model=list[PublicProfileResponse])
async def search_users(
    q: str = Query(..., min_length=1, max_length=50),
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[PublicProfileResponse]:
    """Prefix search on username using pg_trgm GIN index."""
    stmt = (
        select(User)
        .where(
            User.username.ilike(f"{q}%"),
            User.id != current_user.id,
        )
        .limit(limit)
    )
    result = await db.execute(stmt)
    users = result.scalars().all()
    return [
        PublicProfileResponse(
            id=str(u.id),
            username=u.username,
            display_name=u.display_name,
            avatar_url=u.avatar_url,
            bio=u.bio,
            is_verified=u.is_verified or False,
        )
        for u in users
    ]


@router.get("/users/{username}", response_model=PublicProfileResponse)
async def get_user_profile(
    username: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PublicProfileResponse:
    """Public profile — no PII."""
    stmt = select(User).where(User.username == username.lower())
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return PublicProfileResponse(
        id=str(user.id),
        username=user.username,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        bio=user.bio,
        is_verified=user.is_verified or False,
    )


# ---------------------------------------------------------------------------
# Follow / Unfollow
# ---------------------------------------------------------------------------

@router.post("/users/{user_id}/follow", status_code=201)
@limiter.limit("100/minute", key_func=_get_user_key)
async def follow_user(
    request: Request,
    user_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot follow yourself")

    # Check target user exists
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    # Idempotent — don't error if already following
    existing = await db.execute(
        select(UserFollow).where(
            UserFollow.follower_id == current_user.id,
            UserFollow.followed_id == user_id,
        )
    )
    if existing.scalar_one_or_none():
        return {"status": "already_following"}

    follow = UserFollow(follower_id=current_user.id, followed_id=user_id)
    db.add(follow)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        return {"status": "already_following"}
    return {"status": "following"}


@router.delete("/users/{user_id}/follow", status_code=200)
async def unfollow_user(
    user_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    existing = await db.execute(
        select(UserFollow).where(
            UserFollow.follower_id == current_user.id,
            UserFollow.followed_id == user_id,
        )
    )
    follow = existing.scalar_one_or_none()
    if not follow:
        raise HTTPException(status_code=404, detail="Not following this user")

    await db.delete(follow)
    await db.commit()
    return {"status": "unfollowed"}


@router.get("/users/me/following", response_model=list[PublicProfileResponse])
async def get_following(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[PublicProfileResponse]:
    stmt = (
        select(User)
        .join(UserFollow, UserFollow.followed_id == User.id)
        .where(UserFollow.follower_id == current_user.id)
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    users = result.scalars().all()
    return [
        PublicProfileResponse(
            id=str(u.id), username=u.username, display_name=u.display_name,
            avatar_url=u.avatar_url, bio=u.bio, is_verified=u.is_verified or False,
        )
        for u in users
    ]


@router.get("/users/me/followers", response_model=list[PublicProfileResponse])
async def get_followers(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[PublicProfileResponse]:
    stmt = (
        select(User)
        .join(UserFollow, UserFollow.follower_id == User.id)
        .where(UserFollow.followed_id == current_user.id)
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    users = result.scalars().all()
    return [
        PublicProfileResponse(
            id=str(u.id), username=u.username, display_name=u.display_name,
            avatar_url=u.avatar_url, bio=u.bio, is_verified=u.is_verified or False,
        )
        for u in users
    ]


# ---------------------------------------------------------------------------
# Unread counts
# ---------------------------------------------------------------------------

@router.get("/users/me/unread")
async def get_my_unread(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return {group_id: unread_count} for all groups the user is a member of."""
    from app.core.redis import get_redis

    # Get all group IDs for current user
    stmt = select(GroupMembership.group_id).where(
        GroupMembership.user_id == current_user.id
    )
    result = await db.execute(stmt)
    group_ids = [row[0] for row in result.fetchall()]

    if not group_ids:
        return {}

    redis = await get_redis()
    counts = await get_unread_counts(group_ids, current_user.id, redis)
    return counts
