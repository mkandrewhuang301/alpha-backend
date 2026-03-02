"""
Kalshi WebSocket Firehose

Persistent async WebSocket client that subscribes to:
    - market_lifecycle_v2: market status changes, new markets, settlements → upsert to Postgres
    - ticker: real-time price/volume updates → HSET to Redis

Runs as a long-lived asyncio task started in FastAPI lifespan.
Reconnects with exponential backoff on disconnect.
"""

import asyncio
import base64
import json
import logging
import time
import uuid
from datetime import datetime, timezone

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.core.config import KALSHI_API_KEY, KALSHI_PRIVATE_KEY, KALSHI_WS_URL
from app.core.database import async_session_factory
from app.core.market_cache import MarketCacheManager, _normalize_cents
from app.core.redis import get_redis
from app.models.db import Market
from app.models.kalshi import map_kalshi_status
from app.services.candlesticks import update_live_candle

logger = logging.getLogger(__name__)

MAX_BACKOFF = 60
INITIAL_BACKOFF = 1


def _ws_auth_headers() -> dict[str, str]:
    """Generate RSA-PSS auth headers for the WebSocket handshake."""
    timestamp_ms = str(int(time.time() * 1000))
    path = "/trade-api/ws/v2"
    message = timestamp_ms + "GET" + path

    private_key = serialization.load_pem_private_key(
        KALSHI_PRIVATE_KEY.encode(), password=None
    )
    signature = private_key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = base64.b64encode(signature).decode()

    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
    }


async def _handle_ticker(msg: dict, cache: MarketCacheManager, redis_conn) -> None:
    """Write per-outcome ticker updates to Redis via MarketCacheManager + live candle."""
    try:
        data = msg.get("msg", {})
        ticker = data.get("market_ticker")
        if not ticker:
            return

        ts = str(int(time.time()))
        last_price = data.get("last_price", 0) or 0
        volume_raw = data.get("volume", 0) or 0
        volume = str(volume_raw)

        # Yes side
        await cache.update_ticker("kalshi", f"{ticker}-yes", {
            "price": _normalize_cents(last_price),
            "bid": _normalize_cents(data.get("yes_bid", 0)),
            "bid_size": str(data.get("yes_bid_size", 0)),
            "ask": _normalize_cents(data.get("yes_ask", 0)),
            "ask_size": str(data.get("yes_ask_size", 0)),
            "volume_24h": volume,
            "ts": ts,
        })

        # No side
        await cache.update_ticker("kalshi", f"{ticker}-no", {
            "price": _normalize_cents(100 - int(last_price)),
            "bid": _normalize_cents(data.get("no_bid", 0)),
            "bid_size": str(data.get("no_bid_size", 0)),
            "ask": _normalize_cents(data.get("no_ask", 0)),
            "ask_size": str(data.get("no_ask_size", 0)),
            "volume_24h": volume,
            "ts": ts,
        })

        # Live candle aggregation: update current-minute OHLCV in Redis
        await update_live_candle(
            redis_conn=redis_conn,
            market_ticker=ticker,
            last_price_cents=int(last_price),
            volume=int(volume_raw),
        )
    except Exception as exc:
        logger.error("[kalshi.stream] Error handling ticker: %s", exc)


async def _handle_market_lifecycle(msg: dict) -> None:
    """Handle market lifecycle event — upsert status/result changes to Postgres."""
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        data = msg.get("msg", {})
        ticker = data.get("market_ticker")
        if not ticker:
            return

        status = map_kalshi_status(data.get("status"))
        result = data.get("result")

        async with async_session_factory() as session:
            stmt = (
                pg_insert(Market)
                .values(
                    id=uuid.uuid4(),
                    event_id=None,  # lifecycle events may not include event_id
                    exchange="kalshi",
                    ext_id=ticker,
                    title=data.get("title", ticker),
                    status=status,
                    result=result,
                    is_deleted=False,
                )
                .on_conflict_do_update(
                    constraint="uq_markets_exchange_extid",
                    set_={
                        "status": status,
                        "result": result,
                        "updated_at": datetime.now(timezone.utc),
                    },
                )
            )
            await session.execute(stmt)
            await session.commit()

        logger.info("[kalshi.stream] Market lifecycle: %s → %s", ticker, status)
    except Exception as exc:
        logger.error("[kalshi.stream] Error handling lifecycle: %s", exc)


async def _dispatch_message(raw: str, cache: MarketCacheManager, redis_conn) -> None:
    """Route an incoming WebSocket message to the appropriate handler."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[kalshi.stream] Non-JSON message: %s", raw[:200])
        return

    msg_type = msg.get("type")
    if msg_type == "ticker":
        await _handle_ticker(msg, cache, redis_conn)
    elif msg_type == "market_lifecycle_v2":
        await _handle_market_lifecycle(msg)


async def run_kalshi_ws() -> None:
    """
    Main WebSocket loop with exponential backoff reconnection.
    Intended to be run as an asyncio.create_task() from FastAPI lifespan.
    """
    backoff = INITIAL_BACKOFF

    # Create cache manager once for the lifetime of the WS connection
    redis = await get_redis()
    cache = MarketCacheManager(redis)
    redis_conn = redis  # same connection for live candle aggregation

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

                # Subscribe to channels
                subscribe_msg = json.dumps({
                    "id": 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["ticker", "market_lifecycle_v2"],
                    },
                })
                await ws.send(subscribe_msg)
                logger.info("[kalshi.stream] Subscribed to ticker + market_lifecycle_v2")

                async for raw_msg in ws:
                    await _dispatch_message(raw_msg, cache, redis_conn)

        except asyncio.CancelledError:
            logger.info("[kalshi.stream] Task cancelled, shutting down.")
            return
        except Exception as exc:
            logger.error("[kalshi.stream] Connection error: %s — reconnecting in %ds", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
