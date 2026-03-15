# Alpha Backend

FastAPI backend powering the Alpha iOS prediction market intelligence app. Integrates with [Kalshi](https://kalshi.com) and [Polymarket](https://polymarket.com) for real-time market data, charting, and trading.

## What It Does

- Continuously syncs market data from **Polymarket** (Gamma API) and **Kalshi** (REST + arq crons) into PostgreSQL via background workers
- Serves normalized market data to the iOS app via REST API
- Streams real-time Polymarket price updates via CLOB WebSocket → Redis cache
- Maintains a unified tag taxonomy (categories + tags) with GIN-indexed ARRAY columns for O(1) filtering
- Runs periodic state reconciliation to detect market resolution/closure without relying on a dedicated lifecycle channel

> **Note:** Kalshi WebSocket streaming and Kalshi startup sync are currently **disabled**. Kalshi data is updated only via arq crons (full sync every 15min, delta every 1min) in production. Polymarket is the primary real-time data source.

## Development vs Production Mode

Set `DEV_MODE=true` in `.env` to activate the sandbox environment.

### DEV_MODE=True (Development — Railway Pro)

- **Polymarket sync on startup**: `run_polymarket_dev_sync()` runs as a background task. The server starts immediately and accepts requests while sync completes in the background.
- **Curated series**: Only the slugs listed in `POLYMARKET_DEV_SERIES_SLUGS` (dev_config.py) are synced — currently 5 series across Politics, Economics, and Crypto.
- **Event cap**: Max 4 events per series in DEV_MODE to keep the DB small and startup fast.
- **Polymarket CLOB WebSocket**: Starts automatically after sync completes, subscribing to all token IDs from the synced events.
- **arq crons**: Only the Polymarket state reconciliation job runs (every 2 minutes). All Kalshi crons are disabled.
- **No Kalshi sync**: Kalshi ingestion is fully commented out in DEV_MODE.
- **Railway Pro**: No ops/sec constraint. Redis flush interval is 0.5s for responsive live data.

### DEV_MODE=False (Production)

- **No startup sync**: arq handles all sync scheduling. The web process only starts the Polymarket WebSocket.
- **Polymarket WS**: Loads existing token IDs from DB immediately on startup, subscribes to the full production set.
- **Full arq cron suite**: Kalshi full sync (15min), Kalshi state reconciliation (1min), OHLCV aggregation (1min), event volume aggregation (5min), Polymarket state reconciliation (2min).
- **Micro-batching**: asyncio.Queue → 100ms/500-item flusher → Redis pipeline (~10 round-trips/sec).

## Running Locally

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill in .env
cp .env.example .env
# Set DEV_MODE=true for local development

# 3. Apply DB migrations
./venv/bin/python3 -m alembic upgrade head

# 4. Start the FastAPI server
python3 -m uvicorn app.main:app --reload

# 5. Start the arq worker (separate terminal — needed even in DEV_MODE for state reconciliation cron)
arq app.core.arq_worker.WorkerSettings
```

Open `http://localhost:8000/docs` for the interactive API explorer.

## Project Structure

```
app/
  main.py                       # FastAPI entry point; lifespan manages WS + pools
  core/
    config.py                   # Loads all env vars (DEV_MODE, DATABASE_URL, REDIS_URL, etc.)
    dev_config.py               # DEV_MODE sandbox constants: POLYMARKET_DEV_SERIES_SLUGS,
                                #   DEV_EXPLICIT_SERIES (Kalshi), DEV_TARGET_MARKETS, etc.
    database.py                 # SQLAlchemy async engine + raw asyncpg pool
    redis.py                    # Redis async connection (arq queue + tick cache)
    arq_worker.py               # arq WorkerSettings + cron job registration
  api/routes/
    series.py                   # /series — basic DB queries
    events.py                   # /events — basic DB queries
    markets.py                  # /markets — basic DB queries
    categories.py               # /categories/platform-tags — taxonomy UI metadata
    v1/
      events.py                 # /api/v1/{exchange}/events — DB + live Redis prices
      candlesticks.py           # /api/v1/kalshi/events/{ticker}/candlesticks — hybrid OHLCV
      dev.py                    # /api/v1/dev/sync/{exchange} — manual sync (DEV_MODE only)
  services/
    kalshi.py                   # Kalshi SDK wrapper (REST API + candlesticks)
    events.py                   # Event query logic + Redis price enrichment
    candlesticks.py             # Hybrid candle aggregation (historical + live merge)
  workers/
    kalshi/
      ingest.py                 # Full sync + delta sync + OHLCV aggregation
      stream.py                 # WebSocket firehose: ticker → Redis, lifecycle → Postgres
                                #   (currently disabled — ws_task = None in main.py)
    polymarket/
      ingest.py                 # Gamma API sync: series/events/markets/outcomes/tags/sports
                                #   run_polymarket_dev_sync() — DEV_MODE startup sync
                                #   run_polymarket_state_reconciliation() — arq cron delta sync
      stream.py                 # CLOB WS: book/price_change → Redis HSET, ZADD trending ZSET
                                #   Resolution detection: last_trade_price >= 0.99|<= 0.01 →
                                #   Gamma API confirm → Postgres UPDATE
    taxonomy.py                 # upsert_platform_tag() + slugify() — shared taxonomy helpers
  models/
    db.py                       # SQLAlchemy ORM models (all tables)
    kalshi.py                   # Pydantic models for Kalshi API responses
    polymarket.py               # Pydantic models for Polymarket Gamma API responses
migrations/
  versions/
    0001_complete_schema.py     # Single idempotent migration — safe to run on fresh or existing DB
```

## Architecture

```
iOS App (React Native)
      ↕ HTTPS/JSON
FastAPI Backend (web process)
      ↕
PostgreSQL (Supabase) ←→ Redis (tick cache + arq queue)
      ↕
Workers:
  Polymarket CLOB WS (asyncio task) → Redis HSET + ZADD trending
  arq worker (separate process)     → DB sync + state reconciliation
```

**Data flow:**
```
Polymarket CLOB WS → book/price_change → Redis HSET ticker:polymarket:{token_id}
                                        + ZADD events_trending_24h_polymarket
Polymarket CLOB WS → last_trade_price >= 0.99|<= 0.01 → resolution candidate
                   → _check_and_update_market_resolution() → Gamma API confirm → Postgres UPDATE
arq cron → polymarket_state_reconciliation (every 2min) → Gamma API batch → Postgres UPDATE status/result
arq cron → kalshi_full_sync (15min, prod only)          → Postgres (full backfill)
arq cron → kalshi_state_reconciliation (1min, prod only) → Postgres (delta sync)
arq cron → aggregate_ohlcv (1min, prod only)            → Postgres market_ticks
arq cron → aggregate_event_volumes (5min, prod only)    → Postgres events.volume_24h
Kalshi REST → historical candlesticks (on-demand per API request)
PostgreSQL + Redis → FastAPI routes → iOS app
```

**Redis key schema:**
- Ticker data: `ticker:{exchange}:{asset_id}` (e.g. `ticker:polymarket:{token_id}`, `ticker:kalshi:KXHIGHNY-26MAR04-B52.5-yes`)
- Live candle: `candle:kalshi:{market_ticker}:{minute_ts}` (5-min TTL, Lua script OHLCV)
- Trending: `events_trending_24h_polymarket`, `events_trending_24h_kalshi` (ZSET, score = volume_24h)
- **Note:** prefix is `ticker:` not `market:` — `market:*` finds nothing

## Database Schema

Three-level hierarchy across exchanges:
```
series (exchange, ext_id)
  └── events (exchange, ext_id, series_ids ARRAY[UUID])
        └── markets (exchange, ext_id, event_id)
              └── market_outcomes (market_id, execution_asset_id)
```

Key design decisions:
- `ext_id` = native exchange ID (Kalshi ticker, Polymarket conditionId/event id/series id)
- `exchange` enum (`kalshi` | `polymarket`) on every row
- `series_ids ARRAY(UUID)` on events — supports Polymarket many-to-many events↔series
- `categories ARRAY(text)` and `tags ARRAY(text)` on series + events — GIN indexed slug arrays
- `platform_metadata JSONB` absorbs exchange-specific fields without schema changes
- Soft deletes (`is_deleted`) — never hard-delete market data

**Tag taxonomy tables:**
- `platform_tags` — unified across exchanges. `exchange`, `type` (`category`|`tag`), `slug`, `label`, `parent_ids ARRAY(text)` (GIN indexed), `is_carousel`, `force_show`, `force_hide`, `platform_metadata`
- `sports_metadata` — exchange + sport as PK, `tag_ids ARRAY(text)` (GIN indexed), `resolution_url`, `series_slug`

**Alembic:** Single migration `0001_complete_schema.py` with idempotency guards (`_table_exists`, `_col_exists`, `_index_exists`). Safe to run on fresh or existing Supabase DB.

```bash
./venv/bin/python3 -m alembic upgrade head
```

## Current Endpoints

### Basic Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/series` | List all series |
| GET | `/series/{ticker}` | Get series by ticker |
| GET | `/events` | List events |
| GET | `/events/{event_ticker}` | Get event by ticker |
| GET | `/markets` | List markets |
| GET | `/markets/{ticker}` | Get market by ticker with outcomes |
| GET | `/categories/platform-tags/` | List taxonomy tags (filter by exchange, type, parent_id, is_carousel) |
| GET | `/categories/platform-tags/{id}` | Get single platform tag by UUID |

### V1 Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/{exchange}/events` | Event feed with sorting, pagination, live Redis prices |
| GET | `/api/v1/{exchange}/events/{event_ext_id}` | Full event detail with markets/outcomes/live prices |
| GET | `/api/v1/{exchange}/events/trending` | Top events by volume from Redis ZSET |
| GET | `/api/v1/kalshi/events/{event_ticker}/candlesticks` | Hybrid OHLCV candlesticks |

### Development Endpoints (DEV_MODE=True only)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/dev/sync/{exchange}` | Manually trigger sync for `kalshi` or `polymarket` |

Returns 403 in production.

## Polymarket Integration

**Gamma API** (`https://gamma-api.polymarket.com`) — public, no auth required:
- `/series?slug={slug}` — fetch series metadata
- `/events?seriesSlug={slug}` — fetch events for a series
- `/markets?conditionId={id}` — fetch market status for reconciliation
- `/tags` — full tag taxonomy (paginated)
- `/tags/{id}/related-tags/tags` — parent→child tag relationships (rate-limited — uses 3-attempt exponential backoff: 2s → 4s → 8s)
- `/sports` — sports metadata with tag associations

**CLOB WebSocket** (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) — public, no auth:
- Subscribe: `{"type": "market", "assets_ids": [token_id, ...]}`
- Message types: `book` (full snapshot), `price_change` (incremental), `last_trade_price`, `tick_size_change`
- Resolution detection: `last_trade_price >= 0.99 or <= 0.01` → `_resolution_candidates` set → Phase 3 flusher → Gamma API confirm → Postgres UPDATE

**Polymarket identifier strategy:**
- Series: `ext_id` = Gamma series `id`; slug in `platform_metadata["slug"]`
- Event: `ext_id` = Gamma event `id`; slug in `platform_metadata["slug"]`
- Market: `ext_id` = `conditionId` (hex); market slug in `platform_metadata["market_slug"]`; token IDs in `platform_metadata["clob_token_ids"]`
- Outcome: `execution_asset_id` = ERC-1155 clobTokenId (globally unique)

## Polymarket DEV_MODE Series

Defined in `app/core/dev_config.py::POLYMARKET_DEV_SERIES_SLUGS`:
```python
[
    "trump-approval-positive",   # Politics — binary approval rating
    "trump-negative-approval",
    "us-annual-inflation",       # Economics — recurring monthly
    "unemployment",
    "solana-hit-price-monthly",  # Crypto — price range markets
]
```

## Hybrid Candlestick Strategy (Kalshi)

Historical candles are fetched from Kalshi REST API on-demand. The live current-minute candle is aggregated in Redis from WebSocket ticks via an atomic Lua script. The endpoint merges both sources.

- Historical key: Kalshi REST `get_market_candlesticks(series_ticker, market_ticker, start_ts, end_ts, period_interval)`
- Live key: `candle:kalshi:{market_ticker}:{minute_ts}` (prices as basis points 0–10000, normalized to 0.0–1.0 on read)
- Kalshi caps at 5,000 candles per request (~83 hours at 1min interval) — returns `[]` gracefully on overflow
- When Kalshi `price` field is None (no trades), falls back to `(yes_bid + yes_ask) / 2`

## Near Term Goals

- [x] Kalshi API connection — RSA auth, GET endpoints working
- [x] Market data ingestion — series/events/markets syncing into PostgreSQL
- [x] Routes serve from DB — all market endpoints query PostgreSQL
- [x] Real-time price cache via WebSocket → Redis
- [x] Hybrid OHLCV candlestick endpoint (historical + live)
- [x] DEV_MODE sandbox — curated series sync + filtered WebSocket
- [x] Polymarket data ingestion — Gamma API sync, CLOB WS streaming, GIN-indexed tag taxonomy
- [x] Polymarket state reconciliation — periodic delta sync for market resolution/closure
- [ ] Supabase user model — basic auth, user table
- [ ] Sports data pipeline — injury feed
- [ ] OpenAI synthesis — plain English explanations
- [ ] Firebase push notifications
- [ ] Sharp trader leaderboard (Polymarket on-chain wallet history)
