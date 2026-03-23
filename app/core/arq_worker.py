"""
arq worker configuration — replaces APScheduler.

Run as a separate process:
    arq app.core.arq_worker.WorkerSettings

Cron jobs (DEV_MODE):
    - polymarket_state_reconciliation: every 2 minutes

Cron jobs (production):
    - kalshi_full_sync: every 15 minutes (full backfill)
    - kalshi_state_reconciliation: every 1 minute (delta sync)
    - aggregate_ohlcv: every 1 minute (Redis ticks → 1m candles in Postgres)
    - aggregate_event_volumes: every 5 minutes
    - polymarket_state_reconciliation: every 2 minutes
    - check_price_alerts: every 5 minutes (group alert worker)
    - check_resolution_alerts: every 2 minutes (group alert worker)

Cron jobs (production + INTELLIGENCE_ENABLED):
    - ingest_news: every 5 minutes (NewsAPI polling)
    - ingest_sports: every 2 minutes (Sportradar polling)
    - backfill_market_embeddings: every 10 minutes
    - backfill_event_embeddings: every 10 minutes (offset 5)
    - evaluate_credibility: hourly
    - inject_intelligence_system_messages: every 5 minutes

On-demand tasks (always registered):
    - process_intelligence_nlp_batch: batch NER + embedding
    - send_intelligence_notification: single FCM push
    - process_article_intelligence: article NLP from groups
    - dispatch_intelligence_alerts: alert dispatch to exposed users
"""

import logging

from arq import cron
from arq.connections import RedisSettings

from app.core.config import REDIS_URL, DEV_MODE, INTELLIGENCE_ENABLED

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


async def run_aggregate_event_volumes(ctx: dict) -> None:
    """Sum market volumes per event and write to events.volume_24h."""
    from app.workers.kalshi.ingest import aggregate_event_volumes
    await aggregate_event_volumes(ctx)


async def run_polymarket_state_reconciliation(ctx: dict) -> None:
    """Periodic delta sync: check Polymarket market status/resolution changes."""
    from app.workers.polymarket.ingest import run_polymarket_state_reconciliation as _sync
    await _sync()


async def run_check_price_alerts(ctx: dict) -> None:
    """Detect markets that moved >= 10% since shared in groups; insert system messages."""
    from app.workers.group_alerts import check_price_alerts
    await check_price_alerts(ctx)


async def run_check_resolution_alerts(ctx: dict) -> None:
    """Update resolved trade messages and insert resolution system messages."""
    from app.workers.group_alerts import check_resolution_alerts
    await check_resolution_alerts(ctx)


# ---------------------------------------------------------------------------
# Intelligence engine workers (gated by INTELLIGENCE_ENABLED)
# ---------------------------------------------------------------------------

async def run_ingest_news(ctx: dict) -> None:
    """Poll NewsAPI for articles matching prediction market topics."""
    from app.workers.intelligence.news import ingest_news
    await ingest_news(ctx)


async def run_ingest_sports(ctx: dict) -> None:
    """Poll Sportradar for injuries/lineups."""
    from app.workers.intelligence.sports import ingest_sports_data
    await ingest_sports_data(ctx)


async def run_backfill_market_embeddings(ctx: dict) -> None:
    """Backfill embedding column on markets with NULL embedding."""
    from app.workers.intelligence.embedding_backfill import backfill_market_embeddings
    await backfill_market_embeddings(ctx)


async def run_backfill_event_embeddings(ctx: dict) -> None:
    """Backfill embedding column on events with NULL embedding."""
    from app.workers.intelligence.embedding_backfill import backfill_event_embeddings
    await backfill_event_embeddings(ctx)


async def run_evaluate_credibility(ctx: dict) -> None:
    """Evaluate source credibility by comparing sentiment with price movements."""
    from app.workers.intelligence.credibility import evaluate_source_credibility
    await evaluate_source_credibility(ctx)


async def run_inject_intelligence_system_messages(ctx: dict) -> None:
    """Inject high-impact intelligence as system messages into relevant groups."""
    from app.workers.intelligence.group_integration import inject_intelligence_system_messages
    await inject_intelligence_system_messages(ctx)


async def run_process_intelligence_nlp_batch(ctx: dict, intelligence_ids: list[str]) -> None:
    """Batch NER + embedding + mapping for up to 10 intelligence articles."""
    from app.workers.intelligence.nlp_worker import process_intelligence_nlp_batch
    await process_intelligence_nlp_batch(ctx, intelligence_ids)


async def run_send_intelligence_notification(ctx: dict, user_id: str, payload: dict) -> None:
    """Send a single FCM push notification for an intelligence alert."""
    from app.workers.intelligence.notification_dispatch import send_intelligence_notification
    await send_intelligence_notification(ctx, user_id, payload)


async def run_process_article_intelligence(ctx: dict, message_id: str, group_id: str) -> None:
    """Process an article shared in a group through the NLP pipeline."""
    from app.workers.intelligence.group_integration import process_article_intelligence
    await process_article_intelligence(ctx, message_id, group_id)


async def run_dispatch_intelligence_alerts(
    ctx: dict, intelligence_id: str, impact_level: str,
    event_ids: list[str], market_ids: list[str],
) -> None:
    """Dispatch intelligence alerts to users with financial exposure."""
    from app.services.notifications import dispatch_intelligence_alerts
    await dispatch_intelligence_alerts(
        intelligence_id, impact_level, event_ids, market_ids,
        ctx["asyncpg_pool"], ctx["redis"],
    )


class WorkerSettings:
    functions = [
        run_kalshi_full_sync,
        run_kalshi_state_reconciliation,
        run_aggregate_ohlcv,
        run_aggregate_event_volumes,
        run_polymarket_state_reconciliation,
        run_check_price_alerts,
        run_check_resolution_alerts,
        # Intelligence engine (on-demand tasks — always registered, gated at call site)
        run_process_intelligence_nlp_batch,
        run_send_intelligence_notification,
        run_process_article_intelligence,
        run_dispatch_intelligence_alerts,
    ]
    # DEV_MODE: only Polymarket reconciliation (Kalshi sync/streaming disabled in dev).
    # PROD: full suite of Kalshi + Polymarket crons + group alert workers.
    # PROD+INTEL: add intelligence ingestion, backfill, credibility, and group intel crons.
    _every_2_min = {0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40, 42, 44, 46, 48, 50, 52, 54, 56, 58}
    _every_5_min = {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}
    _every_10_min = {0, 10, 20, 30, 40, 50}
    _every_10_min_offset5 = {5, 15, 25, 35, 45, 55}

    # Intelligence crons (only in PROD with INTELLIGENCE_ENABLED)
    _intel_crons = [
        cron(run_ingest_news, minute=_every_5_min),
        cron(run_ingest_sports, minute=_every_2_min),
        cron(run_backfill_market_embeddings, minute=_every_10_min),
        cron(run_backfill_event_embeddings, minute=_every_10_min_offset5),
        cron(run_evaluate_credibility, minute={0}),  # hourly
        cron(run_inject_intelligence_system_messages, minute=_every_5_min),
    ] if (not DEV_MODE and INTELLIGENCE_ENABLED) else []

    cron_jobs = [
        cron(run_polymarket_state_reconciliation, minute=_every_2_min),
    ] if DEV_MODE else [
        cron(run_kalshi_full_sync, minute={0, 15, 30, 45}),
        cron(run_kalshi_state_reconciliation, minute=set(range(60)), unique=True),
        cron(run_aggregate_ohlcv, minute=set(range(60)), unique=True),
        cron(run_aggregate_event_volumes, minute=_every_5_min),
        cron(run_polymarket_state_reconciliation, minute=_every_2_min),
        cron(run_check_price_alerts, minute=_every_5_min),
        cron(run_check_resolution_alerts, minute=_every_2_min),
        *_intel_crons,
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = _parse_redis_settings()
    max_jobs = 10
    job_timeout = 300
