"""
Polymarket price data — historical fetch + live buffer stitching.

Read-through cache strategy:
    1. Historical prices from CLOB REST API, cached in Redis (60s TTL).
    2. Live buffer from WS flusher (RPUSH ticks, 5-min TTL).
    3. Stitch: historical base + live ticks with ts > last historical ts.
"""

import json
import logging
import time

import httpx
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

CLOB_PRICES_HISTORY_URL = "https://clob.polymarket.com/prices-history"

VALID_INTERVALS = {"live", "1d", "1w", "1m", "max"}
LIVE_WINDOW = 3600  # 1 hour in seconds
HISTORY_CACHE_TTL = 60  # seconds
LIVE_BUFFER_KEY_PREFIX = "live_buffer:poly:"
HISTORY_KEY_PREFIX = "history:poly:"

# The CLOB API requires a 'fidelity' param (minutes between data points).
# Each interval has a minimum fidelity — using these gives maximum resolution.
INTERVAL_FIDELITY: dict[str, int] = {
    "1d": 1,
    "1w": 5,
    "1m": 30,
    "max": 60,
}


class PriceDataUnavailable(Exception):
    """Raised when historical price data cannot be fetched from the CLOB API."""
    pass


async def fetch_historical_prices(
    redis_conn: aioredis.Redis,
    asset_id: str,
    interval: str = "1d",
) -> list[dict]:
    """
    Fetch historical {t, p} prices from Polymarket CLOB API with read-through cache.

    Returns a list of {"t": int, "p": float} dicts sorted by timestamp ascending.
    Raises PriceDataUnavailable if the CLOB API is unreachable or returns an error.
    """
    cache_key = f"{HISTORY_KEY_PREFIX}{asset_id}:{interval}"

    # Check cache first
    cached = await redis_conn.get(cache_key)
    if cached:
        return json.loads(cached)

    # Fetch from CLOB API
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                CLOB_PRICES_HISTORY_URL,
                params={
                    "market": asset_id,
                    "interval": interval,
                    "fidelity": INTERVAL_FIDELITY.get(interval, 1),
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        logger.warning("CLOB prices-history timeout for asset %s interval %s", asset_id, interval)
        raise PriceDataUnavailable(f"Polymarket API timed out for asset {asset_id}")
    except httpx.HTTPStatusError as e:
        logger.warning(
            "CLOB prices-history HTTP %s for asset %s interval %s: %s",
            e.response.status_code, asset_id, interval, e.response.text[:200],
        )
        raise PriceDataUnavailable(
            f"Polymarket API returned {e.response.status_code} for asset {asset_id}"
        )
    except httpx.RequestError as e:
        logger.warning("CLOB prices-history request error for asset %s: %s", asset_id, e)
        raise PriceDataUnavailable(f"Could not reach Polymarket API: {e}")

    # The API returns {"history": [{"t": 1234, "p": 0.55}, ...]}
    history = data.get("history") or []

    # Normalize: ensure t is int and p is float (0.0–1.0)
    normalized: list[dict] = []
    for point in history:
        normalized.append({
            "t": int(point["t"]),
            "p": round(float(point["p"]), 6),
        })

    # Cache for 60s
    await redis_conn.set(cache_key, json.dumps(normalized), ex=HISTORY_CACHE_TTL)

    return normalized


async def get_live_buffer(
    redis_conn: aioredis.Redis,
    asset_id: str,
) -> list[dict]:
    """
    Read the full live buffer list from Redis.

    Returns a list of {"t": int, "p": float} dicts sorted by insertion order (ascending).
    """
    key = f"{LIVE_BUFFER_KEY_PREFIX}{asset_id}"
    raw_items = await redis_conn.lrange(key, 0, -1)

    ticks: list[dict] = []
    for raw in raw_items:
        try:
            tick = json.loads(raw)
            ticks.append({"t": int(tick["t"]), "p": round(float(tick["p"]), 6)})
        except (json.JSONDecodeError, KeyError, ValueError):
            continue

    return ticks


async def get_stitched_chart(
    redis_conn: aioredis.Redis,
    asset_id: str,
    interval: str = "1d",
) -> list[dict]:
    """
    Return a unified, gapless {t, p} array: historical base + live buffer tail.

    The live buffer is filtered to only include ticks with t strictly greater
    than the last historical timestamp, preventing overlapping data.

    "live" interval: last hour of 1-min CLOB data + sub-minute WS ticks.
    """
    # "live" uses the 1d CLOB data (1-min fidelity) trimmed to the last hour
    api_interval = "1d" if interval == "live" else interval

    historical = await fetch_historical_prices(redis_conn, asset_id, api_interval)

    if interval == "live":
        cutoff = int(time.time()) - LIVE_WINDOW
        historical = [pt for pt in historical if pt["t"] >= cutoff]

    live = await get_live_buffer(redis_conn, asset_id)

    if not historical:
        if interval == "live":
            # For live, return all buffer ticks within the hour window
            cutoff = int(time.time()) - LIVE_WINDOW
            return [tick for tick in live if tick["t"] >= cutoff]
        return live

    last_historical_ts = historical[-1]["t"]

    # Filter live buffer to only ticks after the last historical point
    live_tail = [tick for tick in live if tick["t"] > last_historical_ts]

    return historical + live_tail
