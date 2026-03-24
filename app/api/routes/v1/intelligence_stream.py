"""
Real-time intelligence feed via Server-Sent Events (SSE).

GET /api/v1/intelligence/stream

Subscribes to Redis Pub/Sub channel `intel:feed:global` and streams
new intelligence items as SSE events to the iOS app. The client filters
by interest tags client-side.

SSE is simpler than WebSocket for unidirectional data, works through
CDNs, and auto-reconnects natively in EventSource clients.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.core.config import INTELLIGENCE_ENABLED
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

router = APIRouter()

SSE_CHANNEL = "intel:feed:global"
SSE_HEARTBEAT_INTERVAL = 30  # seconds


async def _sse_generator(request: Request):
    """
    Async generator that yields SSE events from Redis Pub/Sub.
    Sends heartbeat comments every 30s to keep connection alive.
    """
    redis = await get_redis()
    pubsub = redis.pubsub()

    try:
        await pubsub.subscribe(SSE_CHANNEL)

        while True:
            # Check if client disconnected
            if await request.is_disconnected():
                break

            try:
                # Wait for message with timeout for heartbeat
                message = await asyncio.wait_for(
                    _get_message(pubsub),
                    timeout=SSE_HEARTBEAT_INTERVAL,
                )

                if message and message["type"] == "message":
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")

                    # Validate it's valid JSON before sending
                    try:
                        json.loads(data)
                        yield f"data: {data}\n\n"
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("[sse] Invalid JSON in pub/sub message, skipping")

            except asyncio.TimeoutError:
                # Send SSE comment as heartbeat (keeps connection alive through proxies)
                yield ": heartbeat\n\n"

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("[sse] SSE stream error: %s", e)
    finally:
        await pubsub.unsubscribe(SSE_CHANNEL)
        await pubsub.close()


async def _get_message(pubsub):
    """Poll for pub/sub message with async sleep between checks."""
    while True:
        message = await pubsub.get_message(ignore_subscribe_messages=True)
        if message:
            return message
        await asyncio.sleep(0.1)


@router.get("/intelligence/stream")
async def intelligence_stream(request: Request):
    """
    SSE endpoint for real-time intelligence feed.

    Events are JSON objects with:
        id, title, summary, source_domain, impact_level, matched_tags

    Connect via EventSource:
        const es = new EventSource('/api/v1/intelligence/stream');
        es.onmessage = (e) => { const item = JSON.parse(e.data); ... };
    """
    if not INTELLIGENCE_ENABLED:
        return StreamingResponse(
            iter(["data: {\"error\": \"Intelligence engine disabled\"}\n\n"]),
            media_type="text/event-stream",
            status_code=503,
        )

    return StreamingResponse(
        _sse_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )
