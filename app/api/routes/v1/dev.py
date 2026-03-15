"""
Development-only endpoints — only available when DEV_MODE=True.

POST /api/v1/dev/sync/{exchange}
    Manually trigger a restricted sync for a specific exchange (DEV_TARGET_SERIES only).
    Use this instead of the automatic arq cron when testing the frontend.
    Call it whenever you need fresh DB data during UI development.
"""

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel

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
