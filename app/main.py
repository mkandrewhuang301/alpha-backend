"""
Alpha Backend — FastAPI entry point.

Startup sequence:
    1. Initialize asyncpg pool + Redis connection
    2. Run initial full Kalshi sync (populates DB before serving requests)
    3. Start WebSocket firehose as background task

Shutdown:
    4. Cancel WebSocket task
    5. Close Redis + asyncpg pool

arq worker runs as a SEPARATE process (see Procfile).
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import categories, events, markets, series
from app.api.routes.v1 import events as v1_events
from app.api.routes.v1 import candlesticks as v1_candlesticks
from app.api.routes.v1 import dev as v1_dev
from app.api.routes.v1 import users as v1_users
from app.core.config import DEV_MODE
from app.core.database import init_db, init_asyncpg_pool, close_asyncpg_pool
from app.core.redis import get_redis, close_redis
# from app.workers.kalshi.stream import run_kalshi_ws  # Kalshi streaming disabled
from app.workers.polymarket.stream import run_polymarket_ws, set_token_event_map

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage the application lifecycle.
    All startup logic runs before yield; shutdown logic runs after.
    """
    logger.info("Starting Alpha backend...")

    # Create tables and enum types if they don't exist
    await init_db()
    logger.info("Database schema verified.")

    # Initialize connection pools
    await init_asyncpg_pool()
    await get_redis()
    logger.info("Connection pools ready (asyncpg + Redis).")

    if DEV_MODE:
        # Kalshi DEV sync commented out — only Polymarket data is active.
        # logger.info("DEV_MODE=True — running restricted Kalshi sync (curated series)...")
        # from app.workers.kalshi.ingest import run_kalshi_dev_sync
        # await run_kalshi_dev_sync()
        # logger.info("DEV_MODE Kalshi sync complete.")

        # Polymarket sync is slow (Gamma API ~10s/event, 50+ events total).
        # Run it as a background task so the server starts accepting requests
        # immediately. The Polymarket WS starts after the sync completes.
        from app.workers.polymarket.ingest import run_polymarket_dev_sync, build_token_event_map as build_pm_map
        async def _polymarket_startup():
            token_ids = await run_polymarket_dev_sync()
            logger.info("DEV_MODE Polymarket sync complete: %d token IDs.", len(token_ids))
            tm = await build_pm_map()
            set_token_event_map(tm)
            logger.info("Polymarket token→event map populated (%d entries).", len(tm))
            pm_ws = asyncio.create_task(run_polymarket_ws(token_ids))
            logger.info("Polymarket CLOB WebSocket started (%d tokens).", len(token_ids))
            await pm_ws
        asyncio.create_task(_polymarket_startup())
        polymarket_token_ids: list = []
    else:
        logger.info("PROD MODE — full Kalshi sync skipped on startup (handled by arq cron).")
        # For production, load existing Polymarket token IDs from the DB
        # so the WS can subscribe immediately without waiting for arq full sync.
        from app.workers.polymarket.ingest import load_polymarket_token_ids_from_db, build_token_event_map as build_pm_map
        polymarket_token_ids = await load_polymarket_token_ids_from_db()
        logger.info(
            "PROD MODE — loaded %d Polymarket token IDs from DB for WS subscription.",
            len(polymarket_token_ids),
        )

        # Build token_id → event_ext_id mapping for Polymarket ZSET trending
        token_event_map = await build_pm_map()
        set_token_event_map(token_event_map)
        logger.info("Polymarket token→event map populated (%d entries).", len(token_event_map))

    # Kalshi WebSocket firehose disabled — only Polymarket streaming active.
    # ws_task = asyncio.create_task(run_kalshi_ws())
    # logger.info("Kalshi WebSocket firehose started (mode=%s).", "dev" if DEV_MODE else "prod")
    ws_task = None  # Kalshi WS disabled

    if not DEV_MODE:
        polymarket_ws_task = asyncio.create_task(run_polymarket_ws(polymarket_token_ids))
        logger.info(
            "Polymarket CLOB WebSocket started (%d tokens).", len(polymarket_token_ids)
        )
    else:
        # Polymarket WS is started inside _polymarket_startup() background task
        polymarket_ws_task = None

    yield

    # Graceful shutdown
    logger.info("Shutting down...")

    # ws_task is None (Kalshi WS disabled) — skip cancel
    if ws_task is not None:
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
    if polymarket_ws_task is not None:
        polymarket_ws_task.cancel()
        try:
            await polymarket_ws_task
        except asyncio.CancelledError:
            pass

    # Close connection pools to release Supabase connections cleanly.
    # Without this, connections leak and accumulate across restarts.
    await close_asyncpg_pool()
    await close_redis()
    logger.info("Shutdown complete.")


app = FastAPI(
    title="Alpha Backend",
    description="Prediction market intelligence API powering the Alpha iOS app.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(series.router, prefix="/series", tags=["series"])
app.include_router(events.router, prefix="/events", tags=["events"])
app.include_router(markets.router, prefix="/markets", tags=["markets"])
app.include_router(categories.router, prefix="/categories", tags=["categories"])
app.include_router(v1_events.router, prefix="/api/v1", tags=["v1-events"])
app.include_router(v1_candlesticks.router, prefix="/api/v1", tags=["v1-candlesticks"])
app.include_router(v1_dev.router, prefix="/api/v1", tags=["v1-dev"])
app.include_router(v1_users.router, prefix="/api/v1", tags=["v1-users"])
