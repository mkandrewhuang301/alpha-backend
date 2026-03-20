"""
FastAPI dependency for Privy JWT authentication.

Usage in routes:
    @router.get("/some/protected/route")
    async def handler(eoa_address: str = Depends(get_authenticated_eoa), db: AsyncSession = Depends(get_db)):
        ...

iOS sends:  Authorization: Bearer <privy_access_token>
Backend:    validates JWT signature → looks up user by privy_did → returns eoa_address
"""

from fastapi import Depends, HTTPException, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.db import User
from app.services.privy import _decode_token


async def get_authenticated_eoa(
    authorization: str = Header(..., description="Bearer <privy_access_token>"),
    db: AsyncSession = Depends(get_db),
) -> str:
    """
    Validate Privy access token and return the authenticated user's EOA address.

    Raises 401 if token is missing, invalid, expired, or user not registered.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization header must be 'Bearer <token>'")

    token = authorization[7:]
    try:
        payload = _decode_token(token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    privy_did = payload.get("sub", "")
    if not privy_did:
        raise HTTPException(status_code=401, detail="Invalid token: missing sub claim")

    result = await db.execute(select(User).where(User.privy_did == privy_did))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not registered — call POST /users/register first")

    return user.eoa_address
