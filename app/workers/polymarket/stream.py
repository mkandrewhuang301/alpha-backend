"""
Polymarket CLOB WebSocket Stream

Subscribes to the Polymarket CLOB WebSocket for real-time price/orderbook updates
AND market lifecycle events for the ERC-1155 token IDs collected by the ingestion worker.

WebSocket endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market
Auth: None — public read-only feed
Subscribe: {"type": "market", "assets_ids": [...], "custom_feature_enabled": true}

Message types handled:
    book              — full orderbook snapshot (bids/asks arrays) → Redis HSET
    price_change      — incremental orderbook update (price_changes array) → Redis HSET
    best_bid_ask      — best bid/ask update (custom_feature_enabled) → Redis HSET
    last_trade_price  — last traded price (settlement signal) → Redis HSET + resolution check
    tick_size_change  — tick size update → Redis HSET
    market_resolved   — market resolved event (custom_feature_enabled) → direct Postgres UPDATE
    new_market        — new market created (custom_feature_enabled) → logged, picked up by reconciliation

Lifecycle / resolution (two paths):
    Path A — WS market_resolved event (preferred, zero Gamma API calls):
        market_resolved msg → _ws_resolutions dict → flusher fires _apply_ws_resolution task
        → Postgres UPDATE (status=resolved, result=winning_outcome)

    Path B — last_trade_price settlement signal (fallback, uses Gamma API):
        last_trade_price >= 0.99 or <= 0.01 → _resolution_candidates set
        → flusher fires _check_and_update_market_resolution task
        → Gamma API confirm → Postgres UPDATE

Architecture:
    1. WS receive loop: parse JSON, put into asyncio.Queue (non-blocking)
    2. _redis_flusher: drains queue in 100ms/500-item batches → Redis HSET pipeline
    3. ZADD events_trending_24h_polymarket per flush cycle (volume rollup)
    4. Resolution candidates → asyncio.create_task(_check_and_update_market_resolution)
    5. WS resolutions → asyncio.create_task(_apply_ws_resolution)

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

# TTL for ticker keys in Redis (48 hours).
# Active markets refresh this on every price update — they never actually expire.
# Resolved / archived markets stop receiving WS events and self-prune after 48h.
TICKER_KEY_TTL = 48 * 3600

# Caps concurrent Gamma API + Supabase round-trips for resolution checks.
# Matches Kalshi's _lifecycle_semaphore(3) pattern.
_lifecycle_semaphore = asyncio.Semaphore(3)

# Token-ID → event ext_id mapping for ZSET trending aggregation.
_token_to_event: dict[str, str] = {}

# Resolution candidates detected by the receive loop (Path B — Gamma API confirm).
# The flusher drains this set and fires asyncio tasks.
_resolution_candidates: set[str] = set()

# WS-driven resolutions (Path A — direct, no Gamma API roundtrip).
# token_id → winning_outcome string.
_ws_resolutions: dict[str, str] = {}


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
    """
    Parse a 'book' orderbook snapshot message.

    bids/asks are arrays of {"price": "0.48", "size": "30"} dicts,
    ordered best-first by the CLOB API.
    """
    asset_id = msg.get("asset_id") or msg.get("assetId", "")
    if not asset_id:
        return None

    bids = msg.get("bids") or []
    asks = msg.get("asks") or []

    # Each entry is a dict: {"price": "0.48", "size": "30"}
    best_bid = str(bids[0].get("price", "0")) if bids else "0"
    best_bid_size = str(bids[0].get("size", "0")) if bids else "0"
    best_ask = str(asks[0].get("price", "0")) if asks else "0"
    best_ask_size = str(asks[0].get("size", "0")) if asks else "0"

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


def _handle_price_change_message(msg: dict) -> list[PolymarketTickerUpdate]:
    """
    Parse a 'price_change' incremental update message.

    A single price_change message carries a top-level `price_changes` array
    that may contain updates for multiple asset IDs simultaneously. Each entry
    includes `best_bid` and `best_ask` from the exchange — use these directly
    rather than recomputing from accumulated book state.

    Returns a list (may be empty) of PolymarketTickerUpdate.
    """
    price_changes = msg.get("price_changes") or []
    ts = str(int(time.time()))
    updates: list[PolymarketTickerUpdate] = []

    for change in price_changes:
        asset_id = str(change.get("asset_id") or "")
        if not asset_id:
            continue

        best_bid = str(change.get("best_bid") or "0")
        best_ask = str(change.get("best_ask") or "0")
        price = str(change.get("price") or "0")
        size = str(change.get("size") or "0")
        side = str(change.get("side") or "").upper()

        # Update in-memory state with exchange-reported best bid/ask
        state = _orderbook_state.get(asset_id, {})
        if side == "BUY":
            state["bid"] = best_bid
            state["bid_size"] = size
        elif side == "SELL":
            state["ask"] = best_ask
            state["ask_size"] = size
        # Always persist best bid/ask regardless of side (exchange provides them)
        if best_bid and best_bid != "0":
            state["bid"] = best_bid
        if best_ask and best_ask != "0":
            state["ask"] = best_ask
        _orderbook_state[asset_id] = state

        updates.append(PolymarketTickerUpdate(
            token_id=asset_id,
            event_ext_id=_token_to_event.get(asset_id, ""),
            redis_key=_make_key(EXCHANGE, asset_id),
            price=_get_mid_price(best_bid or None, best_ask or None),
            bid=state.get("bid", "0"),
            bid_size=state.get("bid_size", "0"),
            ask=state.get("ask", "0"),
            ask_size=state.get("ask_size", "0"),
            volume_24h="0",
            ts=ts,
        ))

    return updates


def _handle_best_bid_ask_message(msg: dict) -> Optional[PolymarketTickerUpdate]:
    """
    Parse a 'best_bid_ask' event (requires custom_feature_enabled: true).

    Provides authoritative best bid/ask directly from the exchange — more
    reliable than computing from incremental price_change events.
    """
    asset_id = msg.get("asset_id") or msg.get("assetId", "")
    if not asset_id:
        return None

    best_bid = str(msg.get("best_bid") or "0")
    best_ask = str(msg.get("best_ask") or "0")

    # Update in-memory state — authoritative source
    state = _orderbook_state.get(asset_id, {})
    state["bid"] = best_bid
    state["ask"] = best_ask
    _orderbook_state[asset_id] = state

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
    flusher can fire a Gamma API + DB update (Path B fallback — Path A via
    market_resolved WS event is preferred).
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
    The API sends `new_tick_size` (not `tick_size`) in the message body.
    """
    asset_id = msg.get("asset_id") or msg.get("assetId", "")
    if not asset_id:
        return None

    # API field is new_tick_size
    tick_size = str(msg.get("new_tick_size") or msg.get("tick_size") or "")
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


def _handle_market_resolved_message(msg: dict) -> None:
    """
    Handle a 'market_resolved' WS event (requires custom_feature_enabled: true).

    The WS event provides the winning_asset_id and winning_outcome directly —
    no Gamma API roundtrip needed. Queue for direct Postgres UPDATE in flusher
    Phase 3 (Path A resolution, faster than the last_trade_price → Gamma path).
    """
    winning_asset_id = str(msg.get("winning_asset_id") or "")
    winning_outcome = str(msg.get("winning_outcome") or "")

    if winning_asset_id and winning_outcome:
        _ws_resolutions[winning_asset_id] = winning_outcome
        logger.info(
            "[polymarket.stream] WS market_resolved: token=%s...  outcome=%s",
            winning_asset_id[:20], winning_outcome,
        )
    else:
        # Fallback: add all asset IDs to the Gamma-confirm path
        for aid in (msg.get("assets_ids") or []):
            _resolution_candidates.add(str(aid))
        logger.info(
            "[polymarket.stream] WS market_resolved (no winner info): "
            "%d assets queued for Gamma confirm",
            len(msg.get("assets_ids") or []),
        )


def _handle_new_market_message(msg: dict) -> None:
    """
    Handle a 'new_market' WS event (requires custom_feature_enabled: true).

    We can't subscribe to the new tokens without reconnecting.
    The Polymarket state reconciliation cron (every 2min in DEV,
    every 2min in prod) will pick up new markets on its next run.
    """
    pass


def _parse_and_enqueue(msg: dict) -> None:
    """Parse a WS message and enqueue resulting TickerUpdate(s) (non-blocking)."""
    event_type = msg.get("event_type", "")
    updates: list[PolymarketTickerUpdate] = []

    if event_type == "book":
        u = _handle_book_message(msg)
        if u:
            updates.append(u)
    elif event_type == "price_change":
        # Returns a list — may contain multiple assets
        updates.extend(_handle_price_change_message(msg))
    elif event_type == "best_bid_ask":
        u = _handle_best_bid_ask_message(msg)
        if u:
            updates.append(u)
    elif event_type == "last_trade_price":
        u = _handle_last_trade_price_message(msg)
        if u:
            updates.append(u)
    elif event_type == "tick_size_change":
        u = _handle_tick_size_change_message(msg)
        if u:
            updates.append(u)
    elif event_type == "market_resolved":
        _handle_market_resolved_message(msg)
        return  # No ticker update to enqueue
    elif event_type == "new_market":
        _handle_new_market_message(msg)
        return  # No ticker update to enqueue
    # Unknown types silently ignored

    if not updates:
        return

    queue = _get_ticker_queue()
    for u in updates:
        try:
            queue.put_nowait(u)
        except asyncio.QueueFull:
            logger.warning(
                "[polymarket.stream] Ticker queue full, dropping update for token %s",
                u.token_id,
            )


# ---------------------------------------------------------------------------
# Market resolution — Path A: direct WS-driven (no Gamma API)
# ---------------------------------------------------------------------------

async def _apply_ws_resolution(token_id: str, winning_outcome: str) -> None:
    """
    Apply a market_resolved event received directly from the WebSocket.

    Skips the Gamma API roundtrip — uses winning_outcome provided by the WS event.
    Guarded by _lifecycle_semaphore(3) to cap concurrent Supabase connections.
    """
    async with _lifecycle_semaphore:
        try:
            from sqlalchemy import select
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

            if current_status in ("resolved", "canceled"):
                return

            async with async_session_factory() as session:
                await session.execute(
                    update(Market)
                    .where(Market.ext_id == condition_id, Market.exchange == EXCHANGE)
                    .values(status="resolved", result=winning_outcome)
                )
                await session.commit()

            logger.info(
                "[polymarket.stream] WS resolution applied: %s → resolved (%s)",
                condition_id, winning_outcome,
            )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "[polymarket.stream] WS resolution apply failed for token %s: %s",
                token_id, exc,
            )


# ---------------------------------------------------------------------------
# Market resolution — Path B: Gamma API confirm (last_trade_price fallback)
# ---------------------------------------------------------------------------

async def _check_and_update_market_resolution(token_id: str) -> None:
    """
    Verify and persist market resolution for a token that hit settlement price.

    Fallback path (Path B) — used when last_trade_price reaches settlement levels
    but no market_resolved WS event has arrived. Queries Gamma API to confirm.

    1. Look up the market's conditionId from the DB via the token's execution_asset_id.
    2. Query the Gamma API for current market status.
    3. If resolved/closed, update markets.status and markets.result in Postgres.

    Guarded by _lifecycle_semaphore(3) to cap concurrent Supabase connections
    (mirrors Kalshi's _lifecycle_semaphore pattern).
    """
    async with _lifecycle_semaphore:
        try:
            from sqlalchemy import select
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

            # Query Gamma API
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

            # Determine new status and result
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

            # Write to Postgres
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
                "[polymarket.stream] Gamma-confirmed resolution: %s → %s (%s)",
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
    Phase 3: Fire WS-driven resolution tasks (_ws_resolutions dict) — Path A,
            no Gamma API roundtrip, outcome known from WS event.
    Phase 4: Fire Gamma-confirm resolution tasks (_resolution_candidates) — Path B
            fallback for last_trade_price settlement signals.
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
            # NOTE: volume_24h is NOT written here — it is owned by the
            # reconciliation cron (populated from Gamma API REST, every 2min).
            # The CLOB WebSocket never transmits volume; writing "0" here would
            # overwrite the cron-set value on every price update.
            pipe = redis_conn.pipeline(transaction=False)
            for u in batch:
                mapping = {
                    "price": u.price,
                    "bid": u.bid,
                    "bid_size": u.bid_size,
                    "ask": u.ask,
                    "ask_size": u.ask_size,
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
                # Refresh TTL on every write so active markets never expire.
                # Resolved/archived markets stop receiving updates and self-prune after 48h.
                pipe.expire(u.redis_key, TICKER_KEY_TTL)
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

            # Phase 3: fire WS-driven resolution tasks (Path A — outcome known)
            if _ws_resolutions:
                ws_res = dict(_ws_resolutions)
                _ws_resolutions.clear()
                for tid, outcome in ws_res.items():
                    asyncio.create_task(_apply_ws_resolution(tid, outcome))
                logger.debug(
                    "[polymarket.stream] Scheduled %d WS-driven resolution tasks",
                    len(ws_res),
                )

            # Phase 4: fire Gamma-confirm resolution checks (Path B fallback)
            if _resolution_candidates:
                candidates = list(_resolution_candidates)
                _resolution_candidates.clear()
                for token_id in candidates:
                    asyncio.create_task(
                        _check_and_update_market_resolution(token_id)
                    )
                logger.debug(
                    "[polymarket.stream] Scheduled %d Gamma resolution checks",
                    len(candidates),
                )

            unique_tokens = {u.token_id for u in batch}
            # Sample one update per unique token for logging
            sample_by_token = {u.token_id: u for u in batch}
            price_summary = ", ".join(
                f"{tid[:12]}…=price:{u.price} bid:{u.bid} ask:{u.ask}"
                for tid, u in list(sample_by_token.items())[:5]
            )
            logger.info(
                "[polymarket.stream] Flushed %d updates | %d tokens | %s%s",
                len(batch),
                len(unique_tokens),
                price_summary,
                f" +{len(unique_tokens) - 5} more" if len(unique_tokens) > 5 else "",
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

    Subscribes to the given ERC-1155 token IDs with custom_feature_enabled: true to get:
        - Real-time price/orderbook updates (book, price_change)
        - Authoritative best bid/ask events (best_bid_ask)
        - Last trade price events with resolution detection (last_trade_price)
        - Tick size changes (tick_size_change)
        - Direct market resolution events (market_resolved) — Path A
        - New market notifications (new_market) — logged, ingested by reconciliation

    All messages flow through the micro-batching queue → Redis flusher.
    Resolution: Path A (WS market_resolved) or Path B (last_trade_price → Gamma API).

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
        "(custom_feature_enabled: book, price_change, best_bid_ask, "
        "last_trade_price, tick_size_change, market_resolved, new_market)",
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
                        "custom_feature_enabled": True,
                    })
                    await ws.send(subscribe_msg)
                    logger.info(
                        "[polymarket.stream] Subscribed to %d tokens "
                        "(all event types, custom_feature_enabled=true)",
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
