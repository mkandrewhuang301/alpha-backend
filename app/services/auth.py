"""
Privy Auth JWT verification and FastAPI user dependency.

Flow:
  1. iOS client sends Authorization: Bearer <privy-access-token>
  2. get_current_user dep extracts and verifies the JWT via Privy's ES256 key
  3. Looks up the user in our DB by privy_did (= JWT sub claim)
  4. Returns the User ORM object

DEV_MODE fallback:
  If PRIVY_VERIFICATION_KEY is not set, accepts HS256 JWTs signed with
  DEV_JWT_SECRET (for local testing only). Never use in production.
"""

import logging
import os
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DEV_MODE, PRIVY_APP_ID, PRIVY_VERIFICATION_KEY
from app.core.database import get_db
from app.models.db import User

logger = logging.getLogger(__name__)

# For local testing when PRIVY_VERIFICATION_KEY is not configured
DEV_JWT_SECRET = os.getenv("DEV_JWT_SECRET", "test-jwt-secret-for-local-dev-only")

security = HTTPBearer()

_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or expired token",
    headers={"WWW-Authenticate": "Bearer"},
)


def _decode_token(token: str) -> dict:
    """
    Decode and verify an access token.

    Primary: ES256 via PRIVY_VERIFICATION_KEY (production).
    Fallback: HS256 via DEV_JWT_SECRET (DEV_MODE only, when PRIVY_VERIFICATION_KEY unset).
    """
    last_exc: Optional[Exception] = None

    # --- ES256 via Privy verification key ---
    if PRIVY_VERIFICATION_KEY:
        try:
            return jwt.decode(
                token,
                PRIVY_VERIFICATION_KEY,
                algorithms=["ES256"],
                issuer="privy.io",
                audience=PRIVY_APP_ID,
                options={"verify_exp": True},
            )
        except Exception as exc:
            last_exc = exc
            logger.debug("ES256 JWT verification failed: %s", exc)

    # --- HS256 dev fallback (local testing only) ---
    if DEV_MODE and DEV_JWT_SECRET:
        try:
            return jwt.decode(
                token,
                DEV_JWT_SECRET,
                algorithms=["HS256"],
                options={"verify_exp": True, "verify_aud": False},
            )
        except Exception as exc:
            last_exc = exc
            logger.debug("HS256 dev JWT verification failed: %s", exc)

    raise last_exc or jwt.exceptions.PyJWTError("No verification method configured")


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    FastAPI dependency — verifies access token and returns the User ORM object.

    Raises HTTP 401 if JWT is missing, invalid, or expired.
    Raises HTTP 404 if privy_did is not in our users table.
    """
    token = credentials.credentials
    try:
        payload = _decode_token(token)
    except Exception:
        raise _CREDENTIALS_EXCEPTION

    privy_did: Optional[str] = payload.get("sub")
    if not privy_did:
        raise _CREDENTIALS_EXCEPTION

    stmt = select(User).where(User.privy_did == privy_did)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found. Register first.",
        )
    return user
