"""
Development-only endpoints — only available when DEV_MODE=True.

POST /api/v1/dev/sync/{exchange}
    Manually trigger a restricted sync for a specific exchange (DEV_TARGET_SERIES only).
    Use this instead of the automatic arq cron when testing the frontend.
    Call it whenever you need fresh DB data during UI development.

POST /api/v1/dev/seed-user
    Create a test user directly (for integration tests that use HS256 dev JWTs).
"""

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel
from typing import Optional

from app.core.config import DEV_MODE

router = APIRouter()


class DevSyncResponse(BaseModel):
    status: str
    message: str
    exchange: str
    target_markets_count: int


@router.post(
    "/dev/sync/{exchange}",
    response_model=DevSyncResponse,
    summary="Manually trigger restricted sync for a specific exchange (DEV_MODE only)",
)
async def manual_dev_sync(
    exchange: str = Path(
        ...,
        description="Exchange to sync: 'kalshi' or 'polymarket'",
        pattern="^(kalshi|polymarket)$",
    ),
) -> DevSyncResponse:
    """
    Trigger a restricted sync for the specified exchange.

    - kalshi:      syncs DEV_TARGET_SERIES from Kalshi, updates DEV_TARGET_MARKETS
    - polymarket:  fetches active series dynamically, selects diverse subset, updates POLYMARKET_DEV_TOKEN_IDS

    Only available when DEV_MODE=True. Returns 403 in production.
    """
    if not DEV_MODE:
        raise HTTPException(
            status_code=403,
            detail="This endpoint is only available when DEV_MODE=True.",
        )

    try:
        if exchange == "kalshi":
            from app.workers.kalshi.ingest import run_kalshi_dev_sync
            import app.core.dev_config as dev_cfg

            await run_kalshi_dev_sync()

            return DevSyncResponse(
                status="ok",
                message="Kalshi DEV sync complete. DB updated with latest data from DEV_TARGET_SERIES.",
                exchange="kalshi",
                target_markets_count=len(dev_cfg.DEV_TARGET_MARKETS),
            )

        elif exchange == "polymarket":
            from app.workers.polymarket.ingest import run_polymarket_dev_sync
            import app.core.dev_config as dev_cfg

            token_ids = await run_polymarket_dev_sync()

            return DevSyncResponse(
                status="ok",
                message="Polymarket DEV sync complete. DB updated with latest data from curated slugs.",
                exchange="polymarket",
                target_markets_count=len(token_ids),
            )

        else:
            raise HTTPException(status_code=400, detail=f"Unknown exchange: {exchange}")

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DEV sync failed for {exchange}: {exc}")


class DevSeedUserRequest(BaseModel):
    privy_did: str
    eoa_address: str
    email: str
    username: Optional[str] = None
    display_name: Optional[str] = None


class DevSeedUserResponse(BaseModel):
    id: str
    privy_did: str
    eoa_address: str
    email: str
    username: Optional[str] = None
    display_name: Optional[str] = None
    created: bool


@router.post(
    "/dev/seed-user",
    response_model=DevSeedUserResponse,
    status_code=201,
    summary="Create a test user directly (DEV_MODE only)",
)
async def seed_test_user(body: DevSeedUserRequest):
    """Create or return a test user for integration tests. DEV_MODE only."""
    if not DEV_MODE:
        raise HTTPException(status_code=403, detail="DEV_MODE only.")

    from sqlalchemy import select
    from app.core.database import async_session_factory
    from app.models.db import User

    async with async_session_factory() as db:
        # Check if user already exists by privy_did or eoa_address
        stmt = select(User).where(
            (User.privy_did == body.privy_did) | (User.eoa_address == body.eoa_address)
        )
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            return DevSeedUserResponse(
                id=str(existing.id),
                privy_did=existing.privy_did,
                eoa_address=existing.eoa_address,
                email=existing.email,
                username=existing.username,
                display_name=existing.display_name,
                created=False,
            )

        user = User(
            privy_did=body.privy_did,
            eoa_address=body.eoa_address,
            email=body.email,
            username=body.username,
            display_name=body.display_name,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        return DevSeedUserResponse(
            id=str(user.id),
            privy_did=user.privy_did,
            eoa_address=user.eoa_address,
            email=user.email,
            username=user.username,
            display_name=user.display_name,
            created=True,
        )
