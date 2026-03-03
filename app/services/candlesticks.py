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


async def batch_update_live_candles(
    redis_conn: aioredis.Redis,
    updates: list[tuple[str, int, int]],
) -> None:
    """
    Batch-update live candles for multiple markets via a single Redis pipeline.

    Each item in `updates` is (market_ticker, last_price_cents, volume).
    All EVAL commands are sent in one network round-trip via pipeline.execute().
    """
    if not updates:
        return

    minute_ts = _current_minute_ts()
    pipe = redis_conn.pipeline(transaction=False)

    for market_ticker, last_price_cents, volume in updates:
        key = _candle_key(market_ticker, minute_ts)
        price_bp = int(last_price_cents * 100)
        pipe.eval(
            _LUA_UPSERT_CANDLE,
            1,
            key,
            str(price_bp),
            str(volume),
            str(CANDLE_TTL_SECONDS),
        )

    await pipe.execute()


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
    series_ticker: str,
    market_ticker: str,
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
            series_ticker=series_ticker,
            market_ticker=market_ticker,
            start_ts=start_ts,
            end_ts=end_ts,
            period_interval=period_interval,
        )

        candles: list[dict] = []
        if resp and hasattr(resp, "candlesticks") and resp.candlesticks:
            for c in resp.candlesticks:
                # Prefer actual trade prices (PriceDistribution.open/high/low/close).
                # Kalshi sets these to None when no trades occurred in the period.
                # Fall back to bid/ask midpoint when trade prices are unavailable.
                price = c.price
                if price.open is not None:
                    open_p = price.open / 100.0
                    high_p = price.high / 100.0
                    low_p = price.low / 100.0
                    close_p = price.close / 100.0
                elif c.yes_bid and c.yes_ask:
                    # Midpoint of yes bid/ask (cents → 0.0-1.0)
                    open_p = (c.yes_bid.open + c.yes_ask.open) / 2 / 100.0
                    high_p = (c.yes_bid.high + c.yes_ask.high) / 2 / 100.0
                    low_p = (c.yes_bid.low + c.yes_ask.low) / 2 / 100.0
                    close_p = (c.yes_bid.close + c.yes_ask.close) / 2 / 100.0
                else:
                    continue  # skip if no price data at all

                candles.append({
                    "timestamp": c.end_period_ts,
                    "open": open_p,
                    "high": high_p,
                    "low": low_p,
                    "close": close_p,
                    "volume": c.volume or 0,
                })

        return candles

    except Exception as exc:
        logger.error("[candlesticks] Failed to fetch historical candles for %s/%s: %s", series_ticker, market_ticker, exc)
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
    1. Fetch historical candles from Kalshi REST API (uses first market_ticker).
    2. Fetch live candle from Redis for the current minute.
    3. If the current minute exists in historical data, overwrite HLCV with Redis data.
       If not, append the Redis candle.
    4. Return unified array sorted by timestamp.
    """
    # Step 1: Historical from Kalshi (use first market_ticker if available)
    historical: list[dict] = []
    if market_tickers:
        historical = await fetch_historical_candlesticks(
            series_ticker=series_ticker,
            market_ticker=market_tickers[0],
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
