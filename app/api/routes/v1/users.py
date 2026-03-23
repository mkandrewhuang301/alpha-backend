"""
User authentication and registration routes.

Auth (Wallet — Privy):
  POST /api/v1/users/register/wallet     Verify Privy tokens, create User + Account
  GET  /api/v1/users/clob/signing-message  EIP-712 ClobAuth payload for iOS signing
  POST /api/v1/users/clob/credentials      Derive + store CLOB API credentials
  GET  /api/v1/users/clob/credentials      Check stored CLOB api_key

Auth (Email — Supabase):
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
from app.models.db import Account, Group, GroupMembership, User, UserFollow
from app.services.auth import get_current_user
from app.services.groups import get_unread_counts
from app.services.privy import verify_privy_tokens
from app.services.polymarket import (
    get_clob_signing_message,
    derive_clob_credentials,
    encrypt_credential,
    decrypt_credential,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,50}$")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

# -- Wallet auth (Privy) --

class WalletRegisterRequest(BaseModel):
    access_token: str    # Short-lived JWT from privy.user.getAccessToken()
    identity_token: str  # Longer-lived JWT from privy.user.getIdentityToken()


class WalletRegisterResponse(BaseModel):
    user_id: UUID
    eoa_address: str     # Privy embedded EOA wallet address
    wallet_address: str  # Same as eoa_address — used directly for Polymarket
    email: str
    is_new_user: bool


class ClobSigningMessageResponse(BaseModel):
    address: str      # EOA address (checksum)
    timestamp: str    # Unix seconds — iOS must use this exact value when signing
    nonce: int        # Always 0 for first-time credential derivation
    message: str      # Static attestation string
    chain_id: int     # 137 (Polygon)


class ClobCredentialsRequest(BaseModel):
    eoa_address: str
    signature: str    # EIP-712 ClobAuth signature from iOS signing
    timestamp: str    # Must match the timestamp returned by GET /users/clob/signing-message
    nonce: int = 0


class ClobCredentialsResponse(BaseModel):
    api_key: str
    has_credentials: bool


# -- Email auth (Supabase) --

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


# -- Profile --

class UserProfileResponse(BaseModel):
    id: str
    email: str
    username: Optional[str]
    display_name: Optional[str]
    avatar_url: Optional[str]
    bio: Optional[str]
    is_verified: bool
    eoa_address: Optional[str] = None


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
# Wallet auth endpoints (Privy)
# ---------------------------------------------------------------------------

@router.post("/users/register/wallet", response_model=WalletRegisterResponse)
async def register_wallet(
    body: WalletRegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Register or log in a user via Privy tokens (wallet auth).

    Called by iOS after Privy login + createEthereumWallet(). Idempotent —
    safe to call on every login. Returns same response for new and returning users.

    iOS must call createEthereumWallet() before this endpoint — the EOA address
    must be present in the identity token's linked_accounts.
    """
    # 1. Verify Privy tokens → extract privy_did, email, eoa_address
    try:
        user_info = await verify_privy_tokens(body.access_token, body.identity_token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    eoa_address = user_info.eoa_address
    email = user_info.email
    privy_did = user_info.privy_did

    # 2. Check if user already exists (by wallet address)
    result = await db.execute(
        select(User).where(User.eoa_address == eoa_address)
    )
    existing_user = result.scalar_one_or_none()

    if existing_user:
        # Backfill privy_did if missing (users registered before this column existed)
        if not existing_user.privy_did:
            existing_user.privy_did = privy_did
            await db.commit()
        logger.info("Returning user login: eoa=%s", eoa_address)
        return WalletRegisterResponse(
            user_id=existing_user.id,
            eoa_address=eoa_address,
            wallet_address=eoa_address,
            email=existing_user.email,
            is_new_user=False,
        )

    # 3. New user — create User row
    new_user = User(
        email=email,
        eoa_address=eoa_address,
        privy_did=privy_did,
    )
    db.add(new_user)
    await db.flush()

    # 4. Create Polymarket Account row — EOA is the trading address directly
    account = Account(
        user_id=new_user.id,
        exchange="polymarket",
        ext_account_id=eoa_address,
    )
    db.add(account)
    await db.commit()

    logger.info("New user registered: eoa=%s privy_did=%s", eoa_address, privy_did)

    return WalletRegisterResponse(
        user_id=new_user.id,
        eoa_address=eoa_address,
        wallet_address=eoa_address,
        email=email,
        is_new_user=True,
    )


@router.get("/users/clob/signing-message", response_model=ClobSigningMessageResponse)
async def get_clob_signing_message_endpoint(eoa_address: str):
    """
    Return the EIP-712 ClobAuth payload iOS needs to sign to derive CLOB API credentials.
    """
    msg = get_clob_signing_message(eoa_address)
    return ClobSigningMessageResponse(
        address=msg.address,
        timestamp=msg.timestamp,
        nonce=msg.nonce,
        message=msg.message,
        chain_id=137,
    )


@router.post("/users/clob/credentials", response_model=ClobCredentialsResponse)
async def store_clob_credentials(
    body: ClobCredentialsRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Derive Polymarket CLOB API credentials from iOS signature and store them encrypted.
    """
    try:
        creds = await derive_clob_credentials(
            eoa_address=body.eoa_address,
            signature=body.signature,
            timestamp=body.timestamp,
            nonce=body.nonce,
        )
    except Exception as e:
        logger.error("CLOB credential derivation failed for eoa=%s: %s", body.eoa_address, e)
        raise HTTPException(status_code=502, detail=f"CLOB API error: {e}")

    account_result = await db.execute(
        select(Account).join(User).where(
            User.eoa_address == body.eoa_address,
            Account.exchange == "polymarket",
        )
    )
    account = account_result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Polymarket account not found — register first")

    account.encrypted_api_key = encrypt_credential(creds.api_key)
    account.encrypted_api_secret = encrypt_credential(creds.api_secret)
    account.encrypted_passphrase = encrypt_credential(creds.passphrase)
    await db.commit()

    logger.info("CLOB credentials stored for eoa=%s api_key=%s", body.eoa_address, creds.api_key)

    return ClobCredentialsResponse(api_key=creds.api_key, has_credentials=True)


@router.get("/users/clob/credentials", response_model=ClobCredentialsResponse)
async def get_clob_credentials(
    eoa_address: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Check whether CLOB credentials exist for a user and return the (unencrypted) api_key.
    Does NOT return secret or passphrase — those stay server-side.
    """
    account_result = await db.execute(
        select(Account).join(User).where(
            User.eoa_address == eoa_address,
            Account.exchange == "polymarket",
        )
    )
    account = account_result.scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=404, detail="Polymarket account not found")

    encrypted_key = account.encrypted_api_key
    if not encrypted_key:
        raise HTTPException(status_code=404, detail="No CLOB credentials stored — call POST /users/clob/credentials first")

    try:
        api_key = decrypt_credential(encrypted_key)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to decrypt stored credentials")

    return ClobCredentialsResponse(api_key=api_key, has_credentials=True)


# ---------------------------------------------------------------------------
# Email auth endpoints (Supabase)
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

    session = data.get("session") or data
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
        eoa_address=current_user.eoa_address,
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
        eoa_address=current_user.eoa_address,
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

    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

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
