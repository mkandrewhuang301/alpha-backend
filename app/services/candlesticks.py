"""
Candlestick service — hybrid aggregation strategy.

Historical candles: fetched from Kalshi REST API via SDK.
Live candle: computed from WebSocket ticks aggregated in Redis.

The merge logic ensures the frontend gets a seamless, real-time chart array.
"""

import logging
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import redis.asyncio as aioredis

from app.core.market_cache import _normalize_cents

logger = logging.getLogger(__name__)

# Redis key pattern for live candles
CANDLE_KEY_PREFIX = "candle:kalshi"
CANDLE_TTL_SECONDS = 300  # 5 minutes


def _candle_key(market_ticker: str, minute_ts: int) -> str:
    """Build Redis key for a live candle: candle:kalshi:{ticker}:{minute_ts}."""
    return f"{CANDLE_KEY_PREFIX}:{market_ticker}:{minute_ts}"


def _current_minute_ts() -> int:
    """Return the current Unix timestamp truncated to the start of the minute."""
    return int(time.time()) // 60 * 60


def _normalize_price(cents_value: int | float | str | None) -> str:
    """Normalize Kalshi cents (0-100) to $1.00-base float string (0.00-1.00)."""
    return _normalize_cents(cents_value)


# ---------------------------------------------------------------------------
# Live candle aggregation (called from WebSocket worker)
# ---------------------------------------------------------------------------

# Lua script for atomic OHLCV update
_LUA_UPSERT_CANDLE = """
local key = KEYS[1]
local price = tonumber(ARGV[1])
local volume = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])

local exists = redis.call("EXISTS", key)

if exists == 0 then
    redis.call("HSET", key, "open", price, "high", price, "low", price, "close", price, "volume", volume)
    redis.call("EXPIRE", key, ttl)
else
    local cur_high = tonumber(redis.call("HGET", key, "high"))
    local cur_low = tonumber(redis.call("HGET", key, "low"))

    redis.call("HSET", key, "close", price)

    if price > cur_high then
        redis.call("HSET", key, "high", price)
    end
    if price < cur_low then
        redis.call("HSET", key, "low", price)
    end

    redis.call("HINCRBY", key, "volume", volume)
end

return 1
"""


async def update_live_candle(
    redis_conn: aioredis.Redis,
    market_ticker: str,
    last_price_cents: int,
    volume: int,
) -> None:
    """
    Update the live candle for the current minute in Redis.

    Called by the WebSocket ticker handler on each price tick.
    Prices are stored normalized to 0.00-1.00 scale (multiplied by 10000
    for integer Lua math, then divided back on read).
    """
    minute_ts = _current_minute_ts()
    key = _candle_key(market_ticker, minute_ts)

    # Store prices as integer basis points (0-10000) for Lua integer math
    price_bp = int(last_price_cents * 100)  # cents (0-100) → basis points (0-10000)

    await redis_conn.eval(
        _LUA_UPSERT_CANDLE,
        1,
        key,
        str(price_bp),
        str(volume),
        str(CANDLE_TTL_SECONDS),
    )


async def get_live_candle(
    redis_conn: aioredis.Redis,
    market_ticker: str,
) -> Optional[dict]:
    """
    Fetch the live (current minute) candle from Redis.

    Returns dict with open/high/low/close as normalized floats (0.0-1.0)
    and volume as int, or None if no live candle exists.
    """
    minute_ts = _current_minute_ts()
    key = _candle_key(market_ticker, minute_ts)

    raw = await redis_conn.hgetall(key)
    if not raw:
        return None

    def _bp_to_float(bp_str: str) -> float:
        """Convert basis points string to normalized 0.0-1.0 float."""
        return int(bp_str) / 10000.0

    return {
        "timestamp": minute_ts,
        "open": _bp_to_float(raw["open"]),
        "high": _bp_to_float(raw["high"]),
        "low": _bp_to_float(raw["low"]),
        "close": _bp_to_float(raw["close"]),
        "volume": int(raw.get("volume", 0)),
    }


# ---------------------------------------------------------------------------
# Historical candle fetcher (Kalshi REST API)
# ---------------------------------------------------------------------------

async def fetch_historical_candlesticks(
    event_ticker: str,
    series_ticker: str,
    start_ts: int,
    end_ts: int,
    period_interval: int = 1,
) -> list[dict]:
    """
    Fetch historical candlesticks from Kalshi REST API via SDK.

    Returns list of dicts with timestamp, open, high, low, close, volume —
    all prices normalized to 0.0-1.0.
    """
    from app.services.kalshi import get_market_candlesticks

    try:
        resp = await get_market_candlesticks(
            event_ticker=event_ticker,
            series_ticker=series_ticker,
            start_ts=start_ts,
            end_ts=end_ts,
            period_interval=period_interval,
        )

        candles: list[dict] = []
        if resp and hasattr(resp, "candlesticks") and resp.candlesticks:
            for c in resp.candlesticks:
                candles.append({
                    "timestamp": c.end_period_ts if hasattr(c, "end_period_ts") else c.get("end_period_ts", 0),
                    "open": (c.price.open if hasattr(c, "price") else c.get("open", 0)) / 100.0,
                    "high": (c.price.high if hasattr(c, "price") else c.get("high", 0)) / 100.0,
                    "low": (c.price.low if hasattr(c, "price") else c.get("low", 0)) / 100.0,
                    "close": (c.price.close if hasattr(c, "price") else c.get("close", 0)) / 100.0,
                    "volume": c.volume if hasattr(c, "volume") else c.get("volume", 0),
                })

        return candles

    except Exception as exc:
        logger.error("[candlesticks] Failed to fetch historical candles for %s: %s", event_ticker, exc)
        return []


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

async def get_merged_candlesticks(
    redis_conn: aioredis.Redis,
    event_ticker: str,
    series_ticker: str,
    market_tickers: list[str],
    start_ts: int,
    end_ts: int,
) -> list[dict]:
    """
    Merge historical Kalshi candles with live Redis candle for each market.

    Steps:
    1. Fetch historical candles from Kalshi REST API.
    2. Fetch live candle from Redis for the current minute.
    3. If the current minute exists in historical data, overwrite HLCV with Redis data.
       If not, append the Redis candle.
    4. Return unified array sorted by timestamp.
    """
    # Step 1: Historical from Kalshi
    historical = await fetch_historical_candlesticks(
        event_ticker=event_ticker,
        series_ticker=series_ticker,
        start_ts=start_ts,
        end_ts=end_ts,
    )

    # Step 2: Live candles from Redis for each market
    live_candles: list[dict] = []
    for ticker in market_tickers:
        candle = await get_live_candle(redis_conn, ticker)
        if candle:
            live_candles.append(candle)

    if not live_candles:
        return historical

    # Use the first market's live candle for the merged view
    # (event-level candlesticks aggregate across markets; for single-market
    # endpoints this list will have exactly one entry)
    live = live_candles[0]
    current_minute = live["timestamp"]

    # Step 3: Merge
    merged = list(historical)
    found = False
    for i, candle in enumerate(merged):
        if candle["timestamp"] == current_minute:
            # Overwrite with more up-to-date Redis data
            merged[i]["high"] = max(candle["high"], live["high"])
            merged[i]["low"] = min(candle["low"], live["low"])
            merged[i]["close"] = live["close"]
            merged[i]["volume"] = max(candle["volume"], live["volume"])
            found = True
            break

    if not found:
        merged.append(live)

    # Step 4: Sort by timestamp
    merged.sort(key=lambda c: c["timestamp"])

    return merged
