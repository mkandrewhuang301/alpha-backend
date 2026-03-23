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
from app.api.routes.v1 import positions as v1_positions
from app.api.routes.v1 import orders as v1_orders
from app.api.routes.v1 import approvals as v1_approvals
from app.api.routes.v1 import groups as v1_groups
from app.api.routes.v1 import messages as v1_messages
from app.api.routes.v1 import positions as v1_positions
from app.api.routes.v1 import orders as v1_orders
from app.api.routes.v1 import approvals as v1_approvals
from app.api.routes.v1 import groups as v1_groups
from app.api.routes.v1 import messages as v1_messages
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
        # Skip Polymarket re-sync — load token IDs directly from DB for faster startup.
        from app.workers.polymarket.ingest import load_polymarket_token_ids_from_db, build_token_event_map as build_pm_map
        polymarket_token_ids = await load_polymarket_token_ids_from_db()
        logger.info(
            "DEV_MODE — loaded %d Polymarket token IDs from DB (skipping re-sync).",
            len(polymarket_token_ids),
        )
        token_event_map = await build_pm_map()
        set_token_event_map(token_event_map)
        logger.info("Polymarket token→event map populated (%d entries).", len(token_event_map))
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

    polymarket_ws_task = asyncio.create_task(run_polymarket_ws(polymarket_token_ids))
    logger.info(
        "Polymarket CLOB WebSocket started (%d tokens).", len(polymarket_token_ids)
    )

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

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.core.limiter import limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(series.router, prefix="/series", tags=["series"])
app.include_router(events.router, prefix="/events", tags=["events"])
app.include_router(markets.router, prefix="/markets", tags=["markets"])
app.include_router(categories.router, prefix="/categories", tags=["categories"])
app.include_router(v1_events.router, prefix="/api/v1", tags=["v1-events"])
app.include_router(v1_candlesticks.router, prefix="/api/v1", tags=["v1-candlesticks"])
app.include_router(v1_dev.router, prefix="/api/v1", tags=["v1-dev"])
app.include_router(v1_users.router, prefix="/api/v1", tags=["v1-users"])
app.include_router(v1_positions.router, prefix="/api/v1", tags=["v1-positions"])
app.include_router(v1_orders.router, prefix="/api/v1", tags=["v1-orders"])
app.include_router(v1_approvals.router, prefix="/api/v1", tags=["v1-approvals"])
app.include_router(v1_groups.router, prefix="/api/v1", tags=["v1-groups"])
app.include_router(v1_messages.router, prefix="/api/v1", tags=["v1-messages"])
app.include_router(v1_positions.router, prefix="/api/v1", tags=["v1-positions"])
app.include_router(v1_orders.router, prefix="/api/v1", tags=["v1-orders"])
app.include_router(v1_approvals.router, prefix="/api/v1", tags=["v1-approvals"])
app.include_router(v1_users.router, prefix="/api/v1", tags=["v1-users"])
app.include_router(v1_groups.router, prefix="/api/v1", tags=["v1-groups"])
app.include_router(v1_messages.router, prefix="/api/v1", tags=["v1-messages"])
