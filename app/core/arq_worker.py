"""
arq worker configuration — replaces APScheduler.

Run as a separate process:
    arq app.core.arq_worker.WorkerSettings

Cron jobs:
    - kalshi_full_sync: every 15 minutes (full backfill)
    - kalshi_state_reconciliation: every 1 minute (delta sync)
    - aggregate_ohlcv: every 1 minute (Redis ticks → 1m candles in Postgres)
"""

import logging

from arq import cron
from arq.connections import RedisSettings

from app.core.config import REDIS_URL

logger = logging.getLogger(__name__)


def _parse_redis_settings() -> RedisSettings:
    """Convert REDIS_URL string to arq RedisSettings."""
    # REDIS_URL format: redis://[:password@]host:port[/db]
    from urllib.parse import urlparse
    parsed = urlparse(REDIS_URL)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        password=parsed.password,
        database=int(parsed.path.lstrip("/") or 0),
    )


async def startup(ctx: dict) -> None:
    """Called once when the arq worker process starts."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("[arq] Worker starting up...")

    from app.core.database import init_asyncpg_pool
    from app.core.redis import get_redis

    ctx["asyncpg_pool"] = await init_asyncpg_pool()
    ctx["redis"] = await get_redis()
    logger.info("[arq] Worker ready.")


async def shutdown(ctx: dict) -> None:
    """Called once when the arq worker process shuts down."""
    logger.info("[arq] Worker shutting down...")

    from app.core.database import close_asyncpg_pool
    from app.core.redis import close_redis

    await close_asyncpg_pool()
    await close_redis()


async def run_kalshi_full_sync(ctx: dict) -> None:
    """Full Kalshi sync: series → events → markets → outcomes."""
    from app.workers.kalshi.ingest import run_kalshi_full_sync as _sync
    await _sync()


async def run_kalshi_state_reconciliation(ctx: dict) -> None:
    """Delta sync: only markets updated since last run."""
    from app.workers.kalshi.ingest import kalshi_state_reconciliation
    await kalshi_state_reconciliation(ctx)


async def run_aggregate_ohlcv(ctx: dict) -> None:
    """Read latest tick prices from Redis, compute 1m OHLCV, write to Postgres."""
    from app.workers.kalshi.ingest import aggregate_ohlcv
    await aggregate_ohlcv(ctx)


class WorkerSettings:
    functions = [
        run_kalshi_full_sync,
        run_kalshi_state_reconciliation,
        run_aggregate_ohlcv,
    ]
    cron_jobs = [
        cron(run_kalshi_full_sync, minute={0, 15, 30, 45}),
        cron(run_kalshi_state_reconciliation, minute=set(range(60)), unique=True),
        cron(run_aggregate_ohlcv, minute=set(range(60)), unique=True),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _parse_redis_settings()
    max_jobs = 10
    job_timeout = 300
