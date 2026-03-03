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

from app.api.routes import events, markets, series
from app.api.routes.v1 import events as v1_events
from app.api.routes.v1 import candlesticks as v1_candlesticks
from app.api.routes.v1 import dev as v1_dev
from app.core.config import DEV_MODE
from app.core.database import init_db, init_asyncpg_pool, close_asyncpg_pool
from app.core.redis import get_redis, close_redis
from app.workers.kalshi.stream import run_kalshi_ws

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
        # DEV_MODE: sync only the 3 sandbox series before starting WS.
        # This populates DEV_TARGET_MARKETS so the WS subscription is correct.
        logger.info("DEV_MODE=True — running restricted Kalshi sync (3 series)...")
        from app.workers.kalshi.ingest import run_kalshi_dev_sync
        await run_kalshi_dev_sync()
        logger.info("DEV_MODE sync complete.")
    else:
        logger.info("PROD MODE — full Kalshi sync skipped on startup (handled by arq cron).")

    # Start WebSocket firehose as a background task
    ws_task = asyncio.create_task(run_kalshi_ws())
    logger.info("Kalshi WebSocket firehose started (mode=%s).", "dev" if DEV_MODE else "prod")

    yield

    # Graceful shutdown
    logger.info("Shutting down...")

    ws_task.cancel()
    try:
        await ws_task
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

app.include_router(series.router, prefix="/series", tags=["series"])
app.include_router(events.router, prefix="/events", tags=["events"])
app.include_router(markets.router, prefix="/markets", tags=["markets"])
app.include_router(v1_events.router, prefix="/api/v1", tags=["v1-events"])
app.include_router(v1_candlesticks.router, prefix="/api/v1", tags=["v1-candlesticks"])
app.include_router(v1_dev.router, prefix="/api/v1", tags=["v1-dev"])
