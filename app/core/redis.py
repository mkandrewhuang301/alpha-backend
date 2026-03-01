"""
Redis connection for arq job queue and ticker price cache.

Usage:
    from app.core.redis import get_redis, close_redis

Tick data is stored as HSET: market:{ticker} → {best_bid, best_ask, last_price, volume, ts}
"""

import redis.asyncio as aioredis

from app.core.config import REDIS_URL

_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Return the shared Redis connection, creating it on first call."""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            REDIS_URL,
            decode_responses=True,
        )
    return _redis


async def close_redis() -> None:
    """Close the Redis connection. Called during app shutdown."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
