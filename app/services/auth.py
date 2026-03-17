"""
Supabase Auth JWT verification and FastAPI user dependency.

Flow:
  1. iOS client sends Authorization: Bearer <supabase-jwt>
  2. get_current_user dep extracts and verifies the JWT
  3. Looks up the user in our DB by supabase_uid (= JWT sub claim)
  4. Returns the User ORM object

JWT verification:
  - Primary: RS256 via JWKS endpoint (cached by PyJWT's PyJWKClient)
  - Fallback: HS256 via SUPABASE_JWT_SECRET if set (for older Supabase projects)

JWKS keys are cached automatically by PyJWKClient; re-fetched on key miss.
"""

import logging
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import SUPABASE_JWT_SECRET, SUPABASE_URL
from app.core.database import get_db
from app.models.db import User

logger = logging.getLogger(__name__)

_jwks_client: Optional[jwt.PyJWKClient] = None


def _get_jwks_client() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        jwks_uri = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json"
        _jwks_client = jwt.PyJWKClient(jwks_uri, cache_keys=True)
    return _jwks_client


security = HTTPBearer()

_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or expired token",
    headers={"WWW-Authenticate": "Bearer"},
)


def _decode_token(token: str) -> dict:
    """
    Attempt to decode a Supabase JWT.
    1. Try RS256 via JWKS if SUPABASE_URL is set.
    2. Fall back to HS256 via SUPABASE_JWT_SECRET if set.
    Raises jwt.exceptions.PyJWTError on failure.
    """
    last_exc: Optional[Exception] = None

    # --- RS256 via JWKS ---
    if SUPABASE_URL:
        try:
            client = _get_jwks_client()
            signing_key = client.get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "ES256"],
                audience="authenticated",
                options={"verify_exp": True},
            )
        except Exception as exc:
            last_exc = exc
            logger.debug("RS256 JWT verification failed: %s", exc)

    # --- HS256 fallback ---
    if SUPABASE_JWT_SECRET:
        try:
            return jwt.decode(
                token,
                SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                audience="authenticated",
                options={"verify_exp": True},
            )
        except Exception as exc:
            last_exc = exc
            logger.debug("HS256 JWT verification failed: %s", exc)

    raise last_exc or jwt.exceptions.PyJWTError("No verification method configured")


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    FastAPI dependency — verifies Supabase JWT and returns the User ORM object.

    Raises HTTP 401 if JWT is missing, invalid, or expired.
    Raises HTTP 404 if supabase_uid is not in our users table.
    """
    token = credentials.credentials
    try:
        payload = _decode_token(token)
    except Exception:
        raise _CREDENTIALS_EXCEPTION

    supabase_uid: Optional[str] = payload.get("sub")
    if not supabase_uid:
        raise _CREDENTIALS_EXCEPTION

    stmt = select(User).where(User.supabase_uid == supabase_uid)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found. Register first.",
        )
    return user
