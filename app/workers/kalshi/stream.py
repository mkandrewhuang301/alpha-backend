"""
Kalshi WebSocket Firehose

Persistent async WebSocket client that subscribes to:
    - ticker: real-time price/volume updates → batched HSET to Redis via pipeline
    - market_lifecycle_v2: market status changes, new markets, settlements → upsert to Postgres

Architecture — two modes controlled by DEV_MODE env var:

PRODUCTION MODE (DEV_MODE=False):
    Micro-batching to prevent event loop starvation:
    1. WebSocket receive loop: parse JSON, put ticker data into asyncio.Queue (non-blocking)
    2. Background flusher task: drain queue in 100ms or 500-item batches, flush via Redis pipeline
    3. market_lifecycle_v2 messages: dispatched as individual asyncio.Tasks (low-frequency)

DEVELOPMENT MODE (DEV_MODE=True):
    Scope reduction with production-grade micro-batching (Railway Pro — no ops/sec limit):
    1. Subscribe only to DEV_TARGET_MARKETS (explicit 11 series → scoped markets)
    2. WebSocket receive loop: enqueue ticker updates into asyncio.Queue (same as prod)
    3. Background flusher: drains queue in 100ms or 500-item batches (same as prod)
    The filtered WS subscription limits scope; micro-batching handles throughput.

Runs as a long-lived asyncio task started in FastAPI lifespan.
Reconnects with exponential backoff on disconnect.
"""

import asyncio
import base64
import json
import logging
import time
from decimal import Decimal
from datetime import datetime, timezone
from typing import NamedTuple

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.core.config import KALSHI_API_KEY, KALSHI_PRIVATE_KEY, KALSHI_WS_URL, DEV_MODE
from app.core.database import async_session_factory
from app.core.market_cache import _make_key, _normalize_cents
from app.core.redis import get_redis
from app.models.db import Market
from app.models.kalshi import map_kalshi_status
from app.services.candlesticks import batch_update_live_candles

logger = logging.getLogger(__name__)

MAX_BACKOFF = 60
INITIAL_BACKOFF = 1

# Production micro-batching parameters
FLUSH_INTERVAL = 0.1   # 100ms
BATCH_SIZE = 500        # max items per flush
QUEUE_MAX_SIZE = 100_000



# ---------------------------------------------------------------------------
# Ticker update tuple (pre-parsed, ready for Redis writes)
# ---------------------------------------------------------------------------

class TickerUpdate(NamedTuple):
    market_ticker: str
    event_ticker: str  # parent event for ZSET trending aggregation
    yes_key: str
    no_key: str
    yes_data: dict
    no_data: dict
    last_price_cents: int
    volume: int
    volume_24h_fp: Decimal  # fixed-point 24h volume from Kalshi _fp field


# ---------------------------------------------------------------------------
# Shared asyncio.Queue — production mode only
# ---------------------------------------------------------------------------

_ticker_queue: asyncio.Queue | None = None


def _get_ticker_queue() -> asyncio.Queue:
    global _ticker_queue
    if _ticker_queue is None:
        _ticker_queue = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
    return _ticker_queue


# ---------------------------------------------------------------------------
# RSA-PSS auth headers for WebSocket handshake
# ---------------------------------------------------------------------------

def _ws_auth_headers() -> dict[str, str]:
    """Generate RSA-PSS auth headers for the WebSocket handshake.

    Uses PSS padding with MGF1(SHA-256) and DIGEST_LENGTH salt — matches
    the SDK's KalshiAuth.create_auth_headers() implementation exactly.
    IMPORTANT: Must use PSS, NOT PKCS1v15. PKCS1v15 returns HTTP 401.
    """
    timestamp_ms = str(int(time.time() * 1000))
    path = "/trade-api/ws/v2"
    message = (timestamp_ms + "GET" + path).encode("utf-8")

    private_key = serialization.load_pem_private_key(
        KALSHI_PRIVATE_KEY.encode(), password=None
    )
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    sig_b64 = base64.b64encode(signature).decode("utf-8")

    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
    }


# ---------------------------------------------------------------------------
# Ticker parser (shared between prod and dev)
# ---------------------------------------------------------------------------

def _parse_ticker(msg: dict) -> TickerUpdate | None:
    """
    Parse a ticker WebSocket message into a TickerUpdate.

    Returns None if the message has no market_ticker.
    Intentionally synchronous and non-blocking.

    Extracts both legacy integer fields and new fixed-point _fp string variants
    (volume_24h_fp, volume_fp) introduced in Kalshi's March 2026 migration.
    """
    data = msg.get("msg", {})
    ticker = data.get("market_ticker")
    if not ticker:
        return None

    event_ticker = data.get("event_ticker", "")

    ts = str(int(time.time()))
    last_price = data.get("last_price", 0) or 0
    volume_raw = data.get("volume", 0) or 0

    # Prefer fixed-point _fp strings when available, fall back to legacy ints
    volume_24h_fp_str = data.get("volume_24h_fp") or data.get("volume_fp")
    volume_24h_fp = Decimal(volume_24h_fp_str) if volume_24h_fp_str else Decimal(volume_raw)

    volume_str = str(volume_24h_fp)

    yes_data = {
        "price": _normalize_cents(last_price),
        "bid": _normalize_cents(data.get("yes_bid", 0)),
        "bid_size": str(data.get("yes_bid_size", 0)),
        "ask": _normalize_cents(data.get("yes_ask", 0)),
        "ask_size": str(data.get("yes_ask_size", 0)),
        "volume_24h": volume_str,
        "ts": ts,
    }

    no_data = {
        "price": _normalize_cents(100 - int(last_price)),
        "bid": _normalize_cents(data.get("no_bid", 0)),
        "bid_size": str(data.get("no_bid_size", 0)),
        "ask": _normalize_cents(data.get("no_ask", 0)),
        "ask_size": str(data.get("no_ask_size", 0)),
        "volume_24h": volume_str,
        "ts": ts,
    }

    return TickerUpdate(
        market_ticker=ticker,
        event_ticker=event_ticker,
        yes_key=_make_key("kalshi", f"{ticker}-yes"),
        no_key=_make_key("kalshi", f"{ticker}-no"),
        yes_data=yes_data,
        no_data=no_data,
        last_price_cents=int(last_price),
        volume=int(volume_raw),
        volume_24h_fp=volume_24h_fp,
    )


# ---------------------------------------------------------------------------
# PRODUCTION MODE: Non-blocking ticker enqueue + background flusher
# ---------------------------------------------------------------------------

def _enqueue_ticker(msg: dict) -> None:
    """
    Parse a ticker message and enqueue for batch Redis write (production mode).

    Synchronous and non-blocking — returns in microseconds.
    The WS receive loop is never stalled.
    """
    update = _parse_ticker(msg)
    if update is None:
        return
    try:
        _get_ticker_queue().put_nowait(update)
    except asyncio.QueueFull:
        logger.warning("[kalshi.stream] Ticker queue full, dropping update for %s", update.market_ticker)


async def _flush_candles(redis_conn, updates: list[TickerUpdate]) -> None:
    """
    Fire-and-forget: batch-update live candles via a single Redis pipeline.
    All EVAL commands sent in one round-trip.
    """
    try:
        await batch_update_live_candles(
            redis_conn,
            [(u.market_ticker, u.last_price_cents, u.volume) for u in updates],
        )
    except Exception as exc:
        logger.error("[kalshi.stream] Candle batch update error: %s", exc)


async def _redis_flusher(redis_conn) -> None:
    """
    Production background task: drain the ticker queue and flush to Redis via pipeline.

    Two-phase per cycle:
      1. FAST — batch HSET for all yes/no ticker data via a single pipeline.execute().
      2. BACKGROUND — candle Lua eval updates fired as a non-blocking asyncio.Task.

    Reduces Redis round-trips from O(ticks/sec) to ~10/sec.
    """
    queue = _get_ticker_queue()

    while True:
        try:
            batch: list[TickerUpdate] = []

            try:
                first = await asyncio.wait_for(queue.get(), timeout=FLUSH_INTERVAL)
                batch.append(first)
            except asyncio.TimeoutError:
                await asyncio.sleep(0)
                continue

            while len(batch) < BATCH_SIZE:
                try:
                    batch.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            # Phase 1: fast pipeline HSET for all yes/no ticker data
            pipe = redis_conn.pipeline(transaction=False)
            for update in batch:
                pipe.hset(update.yes_key, mapping=update.yes_data)
                pipe.hset(update.no_key, mapping=update.no_data)
            await pipe.execute()

            # Phase 2: fire candle updates as a background task (non-blocking)
            seen: dict[str, TickerUpdate] = {}
            for update in batch:
                seen[update.market_ticker] = update  # last write per market wins
            asyncio.create_task(_flush_candles(redis_conn, list(seen.values())))

            # Phase 3: ZADD trending events leaderboard (aggregate volume per event)
            event_volumes: dict[str, float] = {}
            for update in batch:
                if update.event_ticker:
                    event_volumes[update.event_ticker] = event_volumes.get(
                        update.event_ticker, 0
                    ) + float(update.volume_24h_fp)
            if event_volumes:
                pipe2 = redis_conn.pipeline(transaction=False)
                for evt, vol in event_volumes.items():
                    pipe2.zadd("events_trending_24h", {evt: vol})
                await pipe2.execute()

            logger.debug(
                "[kalshi.stream] Flushed %d ticker updates (%d unique markets)",
                len(batch),
                len(seen),
            )

        except asyncio.CancelledError:
            logger.info("[kalshi.stream] Flusher task cancelled.")
            return
        except Exception as exc:
            logger.error("[kalshi.stream] Flusher error: %s", exc)
            await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# Market lifecycle handler (Postgres UPDATE only)
# ---------------------------------------------------------------------------

# Semaphore limits concurrent DB connections from lifecycle events.
# On Supabase free-tier the pooler has a hard client limit — without this,
# a burst of lifecycle events on WS connect saturates the connection pool.
_lifecycle_semaphore: asyncio.Semaphore | None = None


def _get_lifecycle_semaphore() -> asyncio.Semaphore:
    global _lifecycle_semaphore
    if _lifecycle_semaphore is None:
        # Allow 3 concurrent lifecycle DB writes — safe for free-tier Supabase
        _lifecycle_semaphore = asyncio.Semaphore(3)
    return _lifecycle_semaphore


async def _handle_market_lifecycle(msg: dict) -> None:
    """Handle market lifecycle event — UPDATE status/result for existing markets in Postgres.

    We intentionally skip INSERT here because lifecycle events don't include event_id,
    and the markets table has event_id NOT NULL. Full market creation happens via the
    ingest worker (run_kalshi_dev_sync or run_kalshi_full_sync). This handler only
    patches status + result for markets that already exist in the DB.
    """
    async with _get_lifecycle_semaphore():
        try:
            from sqlalchemy import update as sa_update

            data = msg.get("msg", {})
            ticker = data.get("market_ticker")
            if not ticker:
                return

            status = map_kalshi_status(data.get("status"))
            result = data.get("result")

            update_fields: dict = {"updated_at": datetime.now(timezone.utc)}
            if status:
                update_fields["status"] = status
            if result is not None:
                update_fields["result"] = result

            async with async_session_factory() as session:
                stmt = (
                    sa_update(Market)
                    .where(Market.ext_id == ticker, Market.exchange == "kalshi")
                    .values(**update_fields)
                )
                result_proxy = await session.execute(stmt)
                await session.commit()

                if result_proxy.rowcount > 0:
                    logger.info("[kalshi.stream] Market lifecycle: %s → %s", ticker, status)
                else:
                    logger.debug(
                        "[kalshi.stream] Market lifecycle: %s not in DB yet (will be synced by ingest worker)",
                        ticker,
                    )
        except Exception as exc:
            logger.error("[kalshi.stream] Error handling lifecycle: %s", exc)


# ---------------------------------------------------------------------------
# Main WebSocket loop
# ---------------------------------------------------------------------------

async def run_kalshi_ws() -> None:
    """
    Main WebSocket loop with exponential backoff reconnection.
    Intended to be run as an asyncio.create_task() from FastAPI lifespan.

    Selects the appropriate flusher and subscription strategy based on DEV_MODE:
    - DEV_MODE=False (production): global firehose + asyncio.Queue micro-batching
    - DEV_MODE=True  (dev):        filtered subscription (DEV_TARGET_MARKETS) + 1.5s debounce
    """
    backoff = INITIAL_BACKOFF

    redis_conn = await get_redis()

    # Both modes use the production micro-batching flusher.
    # DEV_MODE only differs in WS subscription scope (filtered to DEV_TARGET_MARKETS).
    if DEV_MODE:
        flusher_coro = _redis_flusher(redis_conn)
        logger.info("[kalshi.stream] DEV_MODE: using production micro-batching flusher (filtered subscription)")
    else:
        flusher_coro = _redis_flusher(redis_conn)
        logger.info("[kalshi.stream] PROD MODE: using micro-batching flusher (100ms interval)")

    flusher_task = asyncio.create_task(flusher_coro)

    try:
        while True:
            try:
                headers = _ws_auth_headers()
                logger.info("[kalshi.stream] Connecting to %s ...", KALSHI_WS_URL)

                async with websockets.connect(
                    KALSHI_WS_URL,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    logger.info("[kalshi.stream] Connected.")
                    backoff = INITIAL_BACKOFF  # reset on successful connect

                    # Build subscribe params based on mode
                    if DEV_MODE:
                        from app.core.dev_config import DEV_TARGET_MARKETS
                        subscribe_params: dict = {
                            "channels": ["ticker", "market_lifecycle_v2"],
                        }
                        if DEV_TARGET_MARKETS:
                            subscribe_params["market_tickers"] = list(DEV_TARGET_MARKETS)
                            logger.info(
                                "[kalshi.stream] DEV: Subscribing to %d markets across 3 series",
                                len(DEV_TARGET_MARKETS),
                            )
                        else:
                            logger.warning(
                                "[kalshi.stream] DEV: DEV_TARGET_MARKETS is empty — "
                                "subscribing without market filter (run dev sync first)"
                            )
                    else:
                        subscribe_params = {
                            "channels": ["ticker", "market_lifecycle_v2"],
                        }

                    subscribe_msg = json.dumps({
                        "id": 1,
                        "cmd": "subscribe",
                        "params": subscribe_params,
                    })
                    await ws.send(subscribe_msg)
                    logger.info("[kalshi.stream] Subscribed (mode=%s)", "dev" if DEV_MODE else "prod")

                    async for raw_msg in ws:
                        try:
                            msg = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            logger.warning("[kalshi.stream] Non-JSON message: %s", raw_msg[:200])
                            continue

                        msg_type = msg.get("type")

                        if msg_type == "ticker":
                            _enqueue_ticker(msg)
                        elif msg_type == "market_lifecycle_v2":
                            asyncio.create_task(_handle_market_lifecycle(msg))
                        else:
                            logger.debug("[kalshi.stream] Unhandled message type: %s", msg_type)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "[kalshi.stream] Connection error: %s — reconnecting in %ds",
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

    except asyncio.CancelledError:
        logger.info("[kalshi.stream] WS task cancelled, shutting down.")
        flusher_task.cancel()
        try:
            await flusher_task
        except asyncio.CancelledError:
            pass
        return
