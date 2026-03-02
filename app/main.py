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

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import events, markets, series
from app.core.database import init_db, init_asyncpg_pool, close_asyncpg_pool
from app.core.redis import get_redis, close_redis
from app.workers.kalshi.ingest import run_kalshi_full_sync

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

    # Run an immediate full Kalshi sync on startup so the DB is populated
    # before the app starts serving requests.
    logger.info("Running initial Kalshi sync...")
    await run_kalshi_full_sync()

    yield

    # Graceful shutdown
    logger.info("Shutting down...")

    await close_redis()
    await close_asyncpg_pool()
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
