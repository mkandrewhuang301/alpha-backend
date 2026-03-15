"""
Polymarket CLOB WebSocket Stream

Subscribes to the Polymarket CLOB WebSocket for real-time price/orderbook updates
AND market lifecycle events for the ERC-1155 token IDs collected by the ingestion worker.

WebSocket endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market
Auth: None — public read-only feed
Subscribe: {"type": "market", "assets_ids": ["0x...", ...]}

Message types handled:
    book            — full orderbook snapshot (bids/asks arrays) → Redis HSET
    price_change    — incremental orderbook update → Redis HSET
    last_trade_price — last traded price (settlement signal) → Redis HSET + resolution check
    tick_size_change — tick size update → Redis HSET

Lifecycle / resolution:
    When last_trade_price hits 0.99+ or 0.01-, it signals a market near resolution.
    The flusher fires a background _check_and_update_market_resolution task that:
        1. Queries the Gamma API for the market's current status
        2. If resolved/closed, updates markets.status + markets.result in Postgres
    This mirrors Kalshi's market_lifecycle_v2 handler pattern.

Architecture:
    1. WS receive loop: parse JSON, put into asyncio.Queue (non-blocking)
    2. _redis_flusher: drains queue in 100ms/500-item batches → Redis HSET pipeline
    3. ZADD events_trending_24h_polymarket per flush cycle (volume rollup)
    4. Resolution candidates → asyncio.create_task(_check_and_update_market_resolution)

Redis key schema:
    ticker:polymarket:{token_id}
    → HSET fields: price, bid, bid_size, ask, ask_size, volume_24h, ts,
                   last_trade_price, last_trade_side, tick_size

Runs as a long-lived asyncio task started in FastAPI lifespan.
Reconnects with exponential backoff on disconnect.
"""

import asyncio
import json
import logging
import time
from decimal import Decimal
from typing import NamedTuple, Optional

import httpx
import websockets
from sqlalchemy import update

from app.core.database import async_session_factory
from app.core.market_cache import _make_key
from app.core.redis import get_redis
from app.models.db import Market
from app.models.polymarket import map_polymarket_status

logger = logging.getLogger(__name__)

CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
EXCHANGE = "polymarket"

MAX_BACKOFF = 60
INITIAL_BACKOFF = 1

# Micro-batching parameters — same as Kalshi stream
FLUSH_INTERVAL = 0.1    # 100ms
BATCH_SIZE = 500
QUEUE_MAX_SIZE = 100_000

# Caps concurrent Gamma API + Supabase round-trips for resolution checks.
# Matches Kalshi's _lifecycle_semaphore(3) pattern.
_lifecycle_semaphore = asyncio.Semaphore(3)

# Token-ID → event ext_id mapping for ZSET trending aggregation.
_token_to_event: dict[str, str] = {}

# Resolution candidates detected by the receive loop.
# The flusher drains this set and fires asyncio tasks.
_resolution_candidates: set[str] = set()


def set_token_event_map(mapping: dict[str, str]) -> None:
    """Inject token_id → event_ext_id mapping used for ZSET trending ZADD calls."""
    _token_to_event.clear()
    _token_to_event.update(mapping)


# ---------------------------------------------------------------------------
# Ticker update tuple
# ---------------------------------------------------------------------------

class PolymarketTickerUpdate(NamedTuple):
    token_id: str                        # ERC-1155 token ID (execution_asset_id)
    event_ext_id: str                    # Parent event ext_id for ZSET trending
    redis_key: str                       # ticker:polymarket:{token_id}
    price: str                           # Mid-price 0.0–1.0 as string
    bid: str
    bid_size: str
    ask: str
    ask_size: str
    volume_24h: str                      # 24h volume (updated when available)
    ts: str
    last_trade_price: str = ""           # From last_trade_price events
    last_trade_side: str = ""            # "BUY" or "SELL" from last_trade_price events
    tick_size: str = ""                  # From tick_size_change events


# ---------------------------------------------------------------------------
# Shared asyncio.Queue
# ---------------------------------------------------------------------------

_ticker_queue: asyncio.Queue | None = None


def _get_ticker_queue() -> asyncio.Queue:
    global _ticker_queue
    if _ticker_queue is None:
        _ticker_queue = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
    return _ticker_queue


# ---------------------------------------------------------------------------
# In-memory orderbook state per token
# ---------------------------------------------------------------------------

_orderbook_state: dict[str, dict] = {}


def _get_mid_price(bid: Optional[str], ask: Optional[str]) -> str:
    """Calculate mid-price from best bid and ask. Returns '0' if not available."""
    try:
        if bid and ask:
            mid = (Decimal(bid) + Decimal(ask)) / 2
            return str(mid.quantize(Decimal("0.000001")))
        if bid:
            return bid
        if ask:
            return ask
        return "0"
    except Exception:
        return "0"


# ---------------------------------------------------------------------------
# Message parsers
# ---------------------------------------------------------------------------

def _handle_book_message(msg: dict) -> Optional[PolymarketTickerUpdate]:
    """Parse a 'book' orderbook snapshot message."""
    asset_id = msg.get("asset_id") or msg.get("assetId", "")
    if not asset_id:
        return None

    bids = msg.get("bids") or []
    asks = msg.get("asks") or []

    best_bid = bids[0][0] if bids else "0"
    best_bid_size = bids[0][1] if bids else "0"
    best_ask = asks[0][0] if asks else "0"
    best_ask_size = asks[0][1] if asks else "0"

    _orderbook_state[asset_id] = {
        "bid": best_bid,
        "bid_size": best_bid_size,
        "ask": best_ask,
        "ask_size": best_ask_size,
    }

    return PolymarketTickerUpdate(
        token_id=asset_id,
        event_ext_id=_token_to_event.get(asset_id, ""),
        redis_key=_make_key(EXCHANGE, asset_id),
        price=_get_mid_price(best_bid, best_ask),
        bid=best_bid,
        bid_size=best_bid_size,
        ask=best_ask,
        ask_size=best_ask_size,
        volume_24h="0",
        ts=str(int(time.time())),
    )


def _handle_price_change_message(msg: dict) -> Optional[PolymarketTickerUpdate]:
    """Parse a 'price_change' incremental update message."""
    asset_id = msg.get("asset_id") or msg.get("assetId", "")
    if not asset_id:
        return None

    changes = msg.get("changes") or []
    state = _orderbook_state.get(asset_id, {})

    for change in changes:
        price_val = str(change.get("price", "0"))
        size_val = str(change.get("size", "0"))
        side = str(change.get("side", "")).upper()

        if side == "BUY":
            state["bid"] = price_val
            state["bid_size"] = size_val
        elif side == "SELL":
            state["ask"] = price_val
            state["ask_size"] = size_val

    _orderbook_state[asset_id] = state

    best_bid = state.get("bid", "0")
    best_ask = state.get("ask", "0")

    return PolymarketTickerUpdate(
        token_id=asset_id,
        event_ext_id=_token_to_event.get(asset_id, ""),
        redis_key=_make_key(EXCHANGE, asset_id),
        price=_get_mid_price(best_bid or None, best_ask or None),
        bid=best_bid,
        bid_size=state.get("bid_size", "0"),
        ask=best_ask,
        ask_size=state.get("ask_size", "0"),
        volume_24h="0",
        ts=str(int(time.time())),
    )


def _handle_last_trade_price_message(msg: dict) -> Optional[PolymarketTickerUpdate]:
    """
    Parse a 'last_trade_price' message.

    Caches last trade price/side in Redis. If the price indicates settlement
    (>= 0.99 or <= 0.01), marks the token as a resolution candidate so the
    flusher can fire a Gamma API + DB update.
    """
    asset_id = msg.get("asset_id") or msg.get("assetId", "")
    if not asset_id:
        return None

    last_price = str(msg.get("price") or "0")
    last_side = str(msg.get("side") or "")
    state = _orderbook_state.get(asset_id, {})

    # Mark as resolution candidate when price is at/near settlement
    try:
        if float(last_price) >= 0.99 or float(last_price) <= 0.01:
            _resolution_candidates.add(asset_id)
    except ValueError:
        pass

    return PolymarketTickerUpdate(
        token_id=asset_id,
        event_ext_id=_token_to_event.get(asset_id, ""),
        redis_key=_make_key(EXCHANGE, asset_id),
        price=last_price,                        # Use last traded price as current price
        bid=state.get("bid", "0"),
        bid_size=state.get("bid_size", "0"),
        ask=state.get("ask", "0"),
        ask_size=state.get("ask_size", "0"),
        volume_24h="0",
        ts=str(int(time.time())),
        last_trade_price=last_price,
        last_trade_side=last_side,
    )


def _handle_tick_size_change_message(msg: dict) -> Optional[PolymarketTickerUpdate]:
    """
    Parse a 'tick_size_change' message.

    Stores the new tick size as a Redis HSET field on the token key.
    """
    asset_id = msg.get("asset_id") or msg.get("assetId", "")
    if not asset_id:
        return None

    tick_size = str(msg.get("tick_size") or msg.get("tickSize") or "")
    if not tick_size:
        return None

    state = _orderbook_state.get(asset_id, {})

    return PolymarketTickerUpdate(
        token_id=asset_id,
        event_ext_id=_token_to_event.get(asset_id, ""),
        redis_key=_make_key(EXCHANGE, asset_id),
        price=_get_mid_price(state.get("bid") or None, state.get("ask") or None),
        bid=state.get("bid", "0"),
        bid_size=state.get("bid_size", "0"),
        ask=state.get("ask", "0"),
        ask_size=state.get("ask_size", "0"),
        volume_24h="0",
        ts=str(int(time.time())),
        tick_size=tick_size,
    )


def _parse_and_enqueue(msg: dict) -> None:
    """Parse a WS message and enqueue the TickerUpdate (non-blocking)."""
    event_type = msg.get("event_type", "")
    update: Optional[PolymarketTickerUpdate] = None

    if event_type == "book":
        update = _handle_book_message(msg)
    elif event_type == "price_change":
        update = _handle_price_change_message(msg)
    elif event_type == "last_trade_price":
        update = _handle_last_trade_price_message(msg)
    elif event_type == "tick_size_change":
        update = _handle_tick_size_change_message(msg)
    # Unknown types silently ignored

    if update is None:
        return

    try:
        _get_ticker_queue().put_nowait(update)
    except asyncio.QueueFull:
        logger.warning(
            "[polymarket.stream] Ticker queue full, dropping update for token %s",
            update.token_id,
        )


# ---------------------------------------------------------------------------
# Market resolution check (Gamma API → Postgres)
# ---------------------------------------------------------------------------

async def _check_and_update_market_resolution(token_id: str) -> None:
    """
    Verify and persist market resolution for a token that hit settlement price.

    1. Look up the market's conditionId from the DB via the token's execution_asset_id.
    2. Query the Gamma API for current market status.
    3. If resolved/closed, update markets.status and markets.result in Postgres.

    Guarded by _lifecycle_semaphore(3) to cap concurrent Supabase connections
    (mirrors Kalshi's _lifecycle_semaphore pattern).
    """
    async with _lifecycle_semaphore:
        try:
            # Step 1: find the conditionId (market.ext_id) for this token
            from sqlalchemy import select, text as sa_text
            from app.models.db import MarketOutcome

            async with async_session_factory() as session:
                result = await session.execute(
                    select(Market.ext_id, Market.status)
                    .join(MarketOutcome, MarketOutcome.market_id == Market.id)
                    .where(
                        MarketOutcome.execution_asset_id == token_id,
                        Market.exchange == EXCHANGE,
                        Market.is_deleted == False,
                    )
                )
                row = result.first()

            if not row:
                return
            condition_id, current_status = row

            # Skip markets already terminal
            if current_status in ("resolved", "canceled"):
                return

            # Step 2: query Gamma API
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{GAMMA_API_BASE}/markets",
                    params={"conditionId": condition_id},
                )
                if resp.status_code != 200:
                    return
                markets_data = resp.json()
                if not isinstance(markets_data, list) or not markets_data:
                    return
                mkt = markets_data[0]

            # Step 3: determine new status and result
            active = mkt.get("active", True)
            closed = mkt.get("closed", False)
            archived = mkt.get("archived", False)

            tokens = mkt.get("tokens") or []
            winning_outcome = next(
                (t.get("outcome") for t in tokens if t.get("winner")),
                None,
            )

            if winning_outcome:
                new_status = "resolved"
            elif closed or archived:
                new_status = "closed"
            elif not active:
                new_status = "closed"
            else:
                return  # Still active — nothing to update

            if new_status == current_status:
                return

            # Step 4: write to Postgres
            async with async_session_factory() as session:
                await session.execute(
                    update(Market)
                    .where(
                        Market.ext_id == condition_id,
                        Market.exchange == EXCHANGE,
                    )
                    .values(status=new_status, result=winning_outcome)
                )
                await session.commit()

            logger.info(
                "[polymarket.stream] Market %s resolved: status=%s result=%s",
                condition_id, new_status, winning_outcome,
            )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "[polymarket.stream] Resolution check failed for token %s: %s",
                token_id, exc,
            )


# ---------------------------------------------------------------------------
# Redis flusher — drains queue in micro-batches
# ---------------------------------------------------------------------------

async def _redis_flusher(redis_conn) -> None:
    """
    Background task: drain the ticker queue and flush to Redis via pipeline.

    Phase 1: HSET for all token ticker data in one pipeline.execute().
            Includes last_trade_price, last_trade_side, tick_size when present.
    Phase 2: ZADD events_trending_24h_polymarket for volume rollup.
    Phase 3: Fire resolution check tasks for any settlement candidates detected
            by the receive loop (non-blocking, semaphore-guarded).
    """
    queue = _get_ticker_queue()

    while True:
        try:
            batch: list[PolymarketTickerUpdate] = []

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

            # Phase 1: HSET all ticker data
            pipe = redis_conn.pipeline(transaction=False)
            for u in batch:
                mapping = {
                    "price": u.price,
                    "bid": u.bid,
                    "bid_size": u.bid_size,
                    "ask": u.ask,
                    "ask_size": u.ask_size,
                    "volume_24h": u.volume_24h,
                    "ts": u.ts,
                }
                # Include optional lifecycle fields when present
                if u.last_trade_price:
                    mapping["last_trade_price"] = u.last_trade_price
                if u.last_trade_side:
                    mapping["last_trade_side"] = u.last_trade_side
                if u.tick_size:
                    mapping["tick_size"] = u.tick_size

                pipe.hset(u.redis_key, mapping=mapping)
            await pipe.execute()

            # Phase 2: ZADD trending leaderboard per event
            event_scores: dict[str, float] = {}
            for u in batch:
                if u.event_ext_id:
                    vol = float(u.volume_24h) if u.volume_24h != "0" else 0.0
                    if vol > 0:
                        event_scores[u.event_ext_id] = (
                            event_scores.get(u.event_ext_id, 0.0) + vol
                        )

            if event_scores:
                pipe2 = redis_conn.pipeline(transaction=False)
                for evt, vol in event_scores.items():
                    pipe2.zadd("events_trending_24h_polymarket", {evt: vol})
                await pipe2.execute()

            # Phase 3: fire resolution checks for settlement candidates
            if _resolution_candidates:
                candidates = list(_resolution_candidates)
                _resolution_candidates.clear()
                for token_id in candidates:
                    asyncio.create_task(
                        _check_and_update_market_resolution(token_id)
                    )
                logger.debug(
                    "[polymarket.stream] Scheduled resolution checks for %d tokens",
                    len(candidates),
                )

            logger.debug(
                "[polymarket.stream] Flushed %d ticker updates (%d unique tokens)",
                len(batch),
                len({u.token_id for u in batch}),
            )

        except asyncio.CancelledError:
            logger.info("[polymarket.stream] Flusher task cancelled.")
            return
        except Exception as exc:
            logger.error("[polymarket.stream] Flusher error: %s", exc)
            await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# Main WebSocket loop
# ---------------------------------------------------------------------------

async def run_polymarket_ws(token_ids: list[str]) -> None:
    """
    Main Polymarket CLOB WebSocket loop with exponential backoff reconnection.

    Subscribes to the given ERC-1155 token IDs for:
        - Real-time price/orderbook updates (book, price_change)
        - Last trade price events with resolution detection (last_trade_price)
        - Tick size changes (tick_size_change)

    All messages flow through the micro-batching queue → Redis flusher.
    Resolution signals are forwarded to the Gamma API + Postgres lifecycle handler.

    Runs as a long-lived asyncio task from FastAPI lifespan.
    """
    if not token_ids:
        logger.warning(
            "[polymarket.stream] No token IDs provided — WebSocket will not subscribe "
            "to any markets. Run polymarket dev/full sync first."
        )

    backoff = INITIAL_BACKOFF
    redis_conn = await get_redis()

    flusher_task = asyncio.create_task(_redis_flusher(redis_conn))
    logger.info(
        "[polymarket.stream] Starting CLOB WebSocket for %d tokens "
        "(price + lifecycle events, micro-batching flusher active)",
        len(token_ids),
    )

    try:
        while True:
            try:
                logger.info("[polymarket.stream] Connecting to %s ...", CLOB_WS_URL)
                async with websockets.connect(
                    CLOB_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    open_timeout=30,
                ) as ws:
                    logger.info("[polymarket.stream] Connected.")
                    backoff = INITIAL_BACKOFF

                    subscribe_msg = json.dumps({
                        "type": "market",
                        "assets_ids": token_ids,
                    })
                    await ws.send(subscribe_msg)
                    logger.info(
                        "[polymarket.stream] Subscribed to %d tokens "
                        "(book, price_change, last_trade_price, tick_size_change)",
                        len(token_ids),
                    )

                    async for raw_msg in ws:
                        try:
                            msg = json.loads(raw_msg)
                        except json.JSONDecodeError:
                            logger.warning(
                                "[polymarket.stream] Non-JSON message: %s",
                                raw_msg[:200],
                            )
                            continue

                        # CLOB WS sends both single dicts and arrays of events
                        if isinstance(msg, list):
                            for m in msg:
                                _parse_and_enqueue(m)
                        elif isinstance(msg, dict):
                            _parse_and_enqueue(msg)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "[polymarket.stream] Connection error: %s — reconnecting in %ds",
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

    except asyncio.CancelledError:
        logger.info("[polymarket.stream] WS task cancelled, shutting down.")
        flusher_task.cancel()
        try:
            await flusher_task
        except asyncio.CancelledError:
            pass
        return
