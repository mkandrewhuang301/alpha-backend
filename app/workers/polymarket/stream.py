"""
Polymarket CLOB WebSocket Stream

Subscribes to the Polymarket CLOB WebSocket for real-time price/orderbook updates
for the ERC-1155 token IDs collected by the ingestion worker.

WebSocket endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market
Auth: None — public read-only feed
Subscribe: {"type": "market", "assets_ids": ["0x...", ...]}

Message types handled:
    book        — full orderbook snapshot (bids/asks arrays)
    price_change — incremental price update for a specific side

Architecture (matching Kalshi stream pattern):
    1. WS receive loop: parse JSON, put into asyncio.Queue (non-blocking)
    2. _redis_flusher: drains queue in 100ms/500-item batches → Redis HSET pipeline
    3. ZADD events_trending_24h_polymarket per flush cycle (volume rollup)

Redis key schema:
    ticker:polymarket:{token_id}
    → HSET fields: price, bid, bid_size, ask, ask_size, volume_24h, ts

Runs as a long-lived asyncio task started in FastAPI lifespan.
Reconnects with exponential backoff on disconnect.
"""

import asyncio
import json
import logging
import time
from decimal import Decimal
from typing import NamedTuple, Optional

import websockets

from app.core.market_cache import _make_key
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
EXCHANGE = "polymarket"

MAX_BACKOFF = 60
INITIAL_BACKOFF = 1

# Micro-batching parameters — same as Kalshi stream
FLUSH_INTERVAL = 0.1    # 100ms
BATCH_SIZE = 500
QUEUE_MAX_SIZE = 100_000

# Token-ID → event ext_id mapping for ZSET trending aggregation.
# Populated at startup by injecting from dev_config or a full ingest lookup.
_token_to_event: dict[str, str] = {}


def set_token_event_map(mapping: dict[str, str]) -> None:
    """Set the token_id → event_ext_id mapping used for ZSET trending ZADD calls.

    Called once at startup after the ingest worker builds the mapping.
    """
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
# In-memory state for bid/ask tracking per token
# (WS delivers bid and ask separately; we merge for Redis HSET)
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

    # Best bid: highest price, best ask: lowest price
    best_bid = bids[0][0] if bids else "0"
    best_bid_size = bids[0][1] if bids else "0"
    best_ask = asks[0][0] if asks else "0"
    best_ask_size = asks[0][1] if asks else "0"

    # Update in-memory orderbook state
    _orderbook_state[asset_id] = {
        "bid": best_bid,
        "bid_size": best_bid_size,
        "ask": best_ask,
        "ask_size": best_ask_size,
    }

    price = _get_mid_price(best_bid, best_ask)
    event_ext_id = _token_to_event.get(asset_id, "")

    return PolymarketTickerUpdate(
        token_id=asset_id,
        event_ext_id=event_ext_id,
        redis_key=_make_key(EXCHANGE, asset_id),
        price=price,
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
    price = _get_mid_price(best_bid or None, best_ask or None)
    event_ext_id = _token_to_event.get(asset_id, "")

    return PolymarketTickerUpdate(
        token_id=asset_id,
        event_ext_id=event_ext_id,
        redis_key=_make_key(EXCHANGE, asset_id),
        price=price,
        bid=best_bid,
        bid_size=state.get("bid_size", "0"),
        ask=best_ask,
        ask_size=state.get("ask_size", "0"),
        volume_24h="0",
        ts=str(int(time.time())),
    )


def _parse_and_enqueue(msg: dict) -> None:
    """Parse a WS message and enqueue the TickerUpdate (non-blocking)."""
    event_type = msg.get("event_type", "")
    update: Optional[PolymarketTickerUpdate] = None

    if event_type == "book":
        update = _handle_book_message(msg)
    elif event_type == "price_change":
        update = _handle_price_change_message(msg)
    # last_trade_price and tick_size_change are intentionally ignored

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
# Redis flusher — drains queue in micro-batches
# ---------------------------------------------------------------------------

async def _redis_flusher(redis_conn) -> None:
    """
    Background task: drain the ticker queue and flush to Redis via pipeline.

    Phase 1: HSET for all token ticker data in one pipeline.execute().
    Phase 2: ZADD events_trending_24h_polymarket for volume rollup.

    Mirrors the Kalshi stream flusher for consistency.
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

            # Phase 1: HSET all ticker data in one pipeline
            pipe = redis_conn.pipeline(transaction=False)
            for update in batch:
                pipe.hset(update.redis_key, mapping={
                    "price": update.price,
                    "bid": update.bid,
                    "bid_size": update.bid_size,
                    "ask": update.ask,
                    "ask_size": update.ask_size,
                    "volume_24h": update.volume_24h,
                    "ts": update.ts,
                })
            await pipe.execute()

            # Phase 2: ZADD trending leaderboard per event
            # For Polymarket, volume is not always in the WS messages.
            # We track the event association for when volume data arrives
            # (e.g., from periodic Gamma API polling).
            # Here we do a lightweight presence update using score=0 if no volume.
            event_scores: dict[str, float] = {}
            for update in batch:
                if update.event_ext_id:
                    # Only update if we have real volume data
                    vol = float(update.volume_24h) if update.volume_24h != "0" else 0.0
                    if vol > 0:
                        event_scores[update.event_ext_id] = event_scores.get(
                            update.event_ext_id, 0.0
                        ) + vol

            if event_scores:
                pipe2 = redis_conn.pipeline(transaction=False)
                for evt, vol in event_scores.items():
                    pipe2.zadd("events_trending_24h_polymarket", {evt: vol})
                await pipe2.execute()

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

    Subscribes to the given ERC-1155 token IDs for real-time price updates.
    Runs as a long-lived asyncio task from FastAPI lifespan.

    Args:
        token_ids: List of clobTokenIds to subscribe to (from ingest worker).
    """
    if not token_ids:
        logger.warning(
            "[polymarket.stream] No token IDs provided — WebSocket will not subscribe to any markets. "
            "Run polymarket dev/full sync first to populate token IDs."
        )

    backoff = INITIAL_BACKOFF
    redis_conn = await get_redis()

    flusher_task = asyncio.create_task(_redis_flusher(redis_conn))
    logger.info(
        "[polymarket.stream] Starting CLOB WebSocket for %d tokens (micro-batching flusher active)",
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
                    backoff = INITIAL_BACKOFF  # reset on successful connect

                    # Subscribe to market channel for our token IDs
                    subscribe_msg = json.dumps({
                        "type": "market",
                        "assets_ids": token_ids,
                    })
                    await ws.send(subscribe_msg)
                    logger.info(
                        "[polymarket.stream] Subscribed to %d tokens on market channel",
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
