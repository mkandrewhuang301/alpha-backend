"""
APScheduler setup for Alpha background workers.

All jobs use AsyncIOScheduler so they run in the same event loop as FastAPI.
Workers are registered here and started/stopped via the FastAPI lifespan in main.py.

Adding a new worker:
    1. Import the worker's entrypoint function
    2. Call scheduler.add_job(...) with an appropriate interval
    3. Document the job in this file
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.workers.kalshi_ingest import run_kalshi_full_sync

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


def register_jobs() -> None:
    """
    Register all background jobs with the scheduler.
    Called once during app startup before scheduler.start().
    """

    # Kalshi market data sync — series, events, markets, outcomes
    # Runs every 15 minutes to keep the DB current with Kalshi's market list.
    # A full sync takes ~30s on average; 15min interval gives enough headroom.
    scheduler.add_job(
        run_kalshi_full_sync,
        trigger=IntervalTrigger(minutes=15),
        id="kalshi_full_sync",
        name="Kalshi Full Market Sync",
        replace_existing=True,
        misfire_grace_time=120,   # Allow up to 2 min late start before skipping
        max_instances=1,          # Never run two syncs concurrently
    )

    logger.info("[scheduler] Registered %d jobs", len(scheduler.get_jobs()))
