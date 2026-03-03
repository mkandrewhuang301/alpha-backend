"""
Development-only endpoints — only available when DEV_MODE=True.

POST /api/v1/dev/sync-kalshi
    Manually trigger a restricted Kalshi sync (DEV_TARGET_SERIES only).
    Use this instead of the automatic arq cron when testing the frontend.
    Call it whenever you need fresh DB data during UI development.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import DEV_MODE

router = APIRouter()


class DevSyncResponse(BaseModel):
    status: str
    message: str
    target_markets_count: int


@router.post(
    "/dev/sync-kalshi",
    response_model=DevSyncResponse,
    summary="Manually trigger restricted Kalshi sync (DEV_MODE only)",
)
async def manual_dev_sync() -> DevSyncResponse:
    """
    Trigger a full restricted sync of DEV_TARGET_SERIES from Kalshi.

    This is the DEV_MODE replacement for the arq cron scheduler.
    It syncs series → events → markets → outcomes for the 3 sandbox series,
    then updates DEV_TARGET_MARKETS so the WebSocket subscription is current.

    Only available when DEV_MODE=True. Returns 403 in production.
    """
    if not DEV_MODE:
        raise HTTPException(
            status_code=403,
            detail="This endpoint is only available when DEV_MODE=True.",
        )

    try:
        from app.workers.kalshi.ingest import run_kalshi_dev_sync
        import app.core.dev_config as dev_cfg

        await run_kalshi_dev_sync()

        return DevSyncResponse(
            status="ok",
            message="DEV sync complete. DB updated with latest data from DEV_TARGET_SERIES.",
            target_markets_count=len(dev_cfg.DEV_TARGET_MARKETS),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"DEV sync failed: {exc}")
