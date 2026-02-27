"""
Alpha Backend — FastAPI entry point.

Startup sequence:
    1. Register APScheduler jobs
    2. Run initial full Kalshi sync (populates DB before serving requests)
    3. Start scheduler (repeating syncs every 15 min)

Shutdown:
    4. Stop scheduler cleanly
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import events, markets, series
from app.core.scheduler import register_jobs, scheduler
from app.workers.kalshi_ingest import run_kalshi_full_sync

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

    # Register recurring background jobs
    register_jobs()

    # Run an immediate full Kalshi sync on startup so the DB is populated
    # before the app starts serving requests.
    logger.info("Running initial Kalshi sync...")
    await run_kalshi_full_sync()

    # Start the scheduler (non-blocking — runs jobs in the background event loop)
    scheduler.start()
    logger.info("Scheduler started with %d jobs.", len(scheduler.get_jobs()))

    yield

    # Graceful shutdown
    logger.info("Shutting down scheduler...")
    scheduler.shutdown(wait=False)


app = FastAPI(
    title="Alpha Backend",
    description="Prediction market intelligence API powering the Alpha iOS app.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(series.router, prefix="/series", tags=["series"])
app.include_router(events.router, prefix="/events", tags=["events"])
app.include_router(markets.router, prefix="/markets", tags=["markets"])
