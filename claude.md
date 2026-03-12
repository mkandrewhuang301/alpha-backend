# Alpha Backend — CLAUDE.md

> **IMPORTANT FOR CODING AGENTS:** You are expected to keep this file updated as the codebase evolves. When you add new services, workers, routes, models, or architectural patterns, update the relevant section here. This is a living document — it should always reflect the actual state of the codebase, not just the initial design.

---

## What Is Alpha

Alpha is a consumer iOS prediction market intelligence app for mainstream sports bettors. Think Action Network meets eToro meets Bloomberg Terminal — but for regular people who just want to feel informed enough to trade confidently on Kalshi and Polymarket.

The target user is NOT a quant trader. NOT a crypto native. It's the 25 year old who bets on DraftKings every Sunday, just heard prediction markets exist, and has no idea why lines move or what smart money is doing.

Tagline: **Trade what you know.**

---

## What This Backend Does

This backend is the engine powering the Alpha iOS app. It has two distinct jobs:

**1. API Server** — responds to requests from the iOS app. User opens app, app asks for today's markets, sharp trader leaderboard, user positions, etc. Server responds with clean, typed JSON.

**2. Background Workers** — runs continuously regardless of user activity. Syncs market data from Kalshi and Polymarket into the database. Monitors for unusual activity. When something material happens — injury report drops, lineup released, sharp money moves — workers generate AI explanations and fire push notifications to relevant users immediately.

The workers are more important than the API server for Alpha's core value proposition.

---

## Core Features This Backend Powers

### 1. Market Data Aggregation (Primary Foundation)
- Continuous background sync of all Kalshi series, events, markets, and outcomes into PostgreSQL
- Normalized data model that abstracts over exchange-specific formats (Kalshi ticker, Polymarket conditionId)
- Three-level market hierarchy: **Series → Events → Markets → Outcomes**
  - **Series**: Top-level category (e.g., "NBA", "US Elections")
  - **Event**: A specific resolution scope (e.g., "NBA Champion 2025")
  - **Market**: A specific tradeable contract (e.g., "Will the Lakers win?")
  - **Outcome**: Each tradeable side (yes/no for binary, or named outcomes for categorical)
- Routes serve data from the database — never directly from exchange APIs

### 2. Initial Research Layer
- Head-to-head stats, win rates, home/away splits, cover analysis
- Weather data for outdoor games
- NBA referee assignments and foul rate analytics
- NFL practice reports (Wednesday/Thursday/Friday cadence)
- News from sources such as Twitter for market sentiment
- News from relevant sources for economics, government, culture, climate, finance
- Starting lineup releases (especially NBA — drops 30 min before tip)
- AI synthesis: takes raw stats and generates 3–6 sentence plain English summary per market

### 3. Sharp Trader Intelligence
- Verified trader profiles from Kalshi and Polymarket public on-chain data
- Win rate, ROI, sport specialties, recent positions per trader
- Leaderboard of top performers by category with full track record history from Polymarket wallet history
- Whale watching — large position detection on Polymarket on-chain data
- Cross-platform intelligence: Polymarket sharp money as signal for Kalshi markets

### 4. Position Protection
- Monitors all user open positions continuously
- Detects material changes: injury news, lineup scratches, sharp money spikes, weather changes
- Generates plain English explanation of what changed and what it means for the specific position
- Calculates real-time P&L impact
- Fires targeted push notification only to users with affected positions, or users that follow market
- Two-tier notification honesty:
  - When cause is known (injury, lineup): state it confidently with source
  - When cause is unknown (silent sharp money): flag unusual activity without fabricating reason

### 5. Group Betting Social Layer (save for later)
- Group creation and membership management
- Trade copy: member puts in trade, can share to group, other members can view and copy. Private trades are hidden
- Simultaneous individual trade execution — each user trades their OWN Kalshi account
- NEVER pool funds (CPO regulatory issue) — always individual accounts, social coordination only
- Shared P&L tracking per group position

### 6. In-App Execution
- Connects to Kalshi via user's own API key / OAuth
- Executes trades on user's individual Kalshi account
- Never touches user funds — Kalshi holds everything
- Alpha triggers the API call, Kalshi does the rest

---

## Architecture

```
iOS App (React Native)
      ↕ HTTPS/JSON
FastAPI Backend (web process)
      ↕
┌───────────────────────────────────────┐
│ PostgreSQL (Supabase)                  │
│  ├── series, events, markets, outcomes │
│  ├── market_ticks (OHLCV candles)     │
│  ├── users, accounts, tracked_entities│
│  └── user_orders, public_trades       │
└───────────────────────────────────────┘
      ↕ (workers populate DB continuously)
┌─────────────────────────────────────┐
│ Redis                                          │
│  ├── arq job queue (worker process)           │
│  ├── ticker:{exchange}:{asset_id} HSET (ticks)│
│  └── candle:kalshi:{ticker}:{min_ts} HSET     │
└────────────────────────────────────────────────┘
      ↕
┌─────────────────────────────────────┐
│ External APIs / Data Sources         │
│ - Kalshi REST (via kalshi-python-async SDK) │
│ - Kalshi WebSocket firehose          │
│ - Polymarket REST + on-chain (MATIC) │
│ - Sportradar / Rotowire (sports)     │
│ - OpenAI GPT-4o (AI synthesis)       │
│ - OpenWeather (weather)              │
│ - Firebase (push notifications)      │
└─────────────────────────────────────┘
```

**Data flow for market data:**
```
Kalshi WS ──► ticker channel ──► Redis HSET ticker:{exchange}:{asset_id} + candle:{ticker}:{min} (live OHLCV)
Kalshi WS ──► market_lifecycle_v2 ──► PostgreSQL UPDATE only (status/result; semaphore(3) for connection safety)
arq cron  ──► kalshi_full_sync (15m, prod only) ──► PostgreSQL (full series+events+markets+outcomes backfill)
arq cron  ──► state_reconciliation (1m, prod only) ──► PostgreSQL (delta via min_updated_ts filter)
arq cron  ──► aggregate_event_volumes (5m, prod only) ──► PostgreSQL (events.volume_24h from market metadata)
arq cron  ──► aggregate_ohlcv (1m, prod only) ──► PostgreSQL market_ticks (tick snapshots from Redis)
POST /api/v1/dev/sync/{exchange} ──► run_{exchange}_dev_sync() ──► PostgreSQL (dev mode manual trigger)
Kalshi REST ──► historical candlesticks (on-demand per API request)
PostgreSQL + Redis ──► FastAPI routes ──► iOS app
```

**DEV_MODE vs Production WebSocket strategy:**
```
DEV_MODE=False (production):
  Subscribe: global firehose (no market_tickers filter)
  WS receive loop → _enqueue_ticker() → asyncio.Queue(maxsize=100_000) (non-blocking, drops if full)
  _redis_flusher() → drain in 100ms/500-item batches → pipeline HSET
  Phase 2: asyncio.create_task(_flush_candles()) → batch candle EVAL (fire-and-forget, dedup per market)

DEV_MODE=True (Railway Pro — no ops/sec constraint):
  Subscribe: {"channels": [...], "market_tickers": [...DEV_TARGET_MARKETS]} (filtered to explicit 11 series)
  WS receive loop → _enqueue_ticker() → asyncio.Queue(maxsize=100_000) (same as production)
  _redis_flusher() → drain in 100ms/500-item batches → pipeline HSET (same as production)
  Scope is limited via WS filter (DEV_TARGET_MARKETS), not via debouncing

Lifecycle events (both modes):
  market_lifecycle_v2 → asyncio.create_task(_handle_market_lifecycle(msg))
  _lifecycle_semaphore = asyncio.Semaphore(3) — caps concurrent Supabase connections (free-tier limit)
  Only UPDATEs (status, result) — never INSERTs (lifecycle msgs lack event_id, which is NOT NULL)
```

**Hybrid OHLCV Candlestick Strategy:**
- **Historical candles**: Fetched from Kalshi REST API on-demand (pre-computed, no wasted compute)
- **Live candle**: Aggregated in Redis from WebSocket ticks using atomic Lua script (key: `candle:kalshi:{ticker}:{minute_ts}`, 5-min TTL)
- **Merged endpoint**: `GET /api/v1/kalshi/events/{event_ticker}/candlesticks` merges both sources — overwrites or appends the live candle to the historical array
- All prices normalized to $1.00 base (0.0-1.0) before returning to frontend

**Write separation rule:** Tick-level price data goes to Redis (high frequency, ephemeral). Metadata goes to PostgreSQL (durable). OHLCV candles use a hybrid: historical from Kalshi REST, live from Redis. Routes read from PostgreSQL + Redis.

The database is the single source of truth for all market data served to the frontend. Routes never call external APIs directly for market data — they query the DB, which workers keep up to date.

---

## Tech Stack

- **Framework**: FastAPI (Python 3.11+)
- **Server**: Uvicorn
- **Database**: Supabase PostgreSQL (via SQLAlchemy async + asyncpg)
- **ORM**: SQLAlchemy 2.0 (async with `AsyncSession`) — routes only
- **Raw DB**: asyncpg pool — workers use `executemany()` for high-perf batch upserts
- **Cache / Queue**: Redis (via `redis[hiredis]`) — tick data HSET cache + arq job queue
- **Background Jobs**: arq (Redis-backed async worker, runs as separate process)
- **Kalshi SDK**: `kalshi-python-async` — handles RSA auth, pagination internally
- **Real-time**: WebSocket firehose (`websockets` library) to Kalshi for ticker + lifecycle events
- **Push Notifications**: Firebase Cloud Messaging + APNs
- **AI**: OpenAI API (GPT-4o)
- **Sports Data**: Rotowire (MVP) → Sportradar (post traction)
- **Weather**: OpenWeather API
- **Hosting**: Railway

---

## Folder Structure

```
alpha-backend/
├── app/
│   ├── main.py                      # FastAPI entry point; lifespan manages WS + pools
│   ├── api/
│   │   └── routes/
│   │       ├── markets.py           # Market endpoints — queries DB
│   │       ├── series.py            # Series endpoints — queries DB
│   │       ├── events.py            # Event endpoints — queries DB
│   │       ├── v1/                  # Versioned endpoints (DB + Redis live data)
│   │       │   ├── events.py        # /api/v1/{exchange}/events — live prices merged
│   │       │   ├── candlesticks.py  # /api/v1/kalshi/events/{ticker}/candlesticks — hybrid OHLCV
│   │       │   └── dev.py           # /api/v1/dev/sync/{exchange} — manual sync (DEV_MODE only, returns 403 in prod)
│   │       ├── categories.py        # /categories — legacy Category table + /platform-tags — PlatformTag UI taxonomy
│   │       ├── sports.py            # Injuries, lineups, stats, weather, refs (planned)
│   │       ├── traders.py           # Sharp trader leaderboard, profiles (planned)
│   │       ├── users.py             # Auth, positions, portfolio (planned)
│   │       ├── groups.py            # Group betting social layer (planned)
│   │       └── notifications.py     # Notification history and preferences (planned)
│   ├── models/
│   │   ├── db.py                    # SQLAlchemy ORM models (one per DB table)
│   │   ├── kalshi.py                # Pydantic models for Kalshi API responses
│   │   └── polymarket.py            # Pydantic models for Polymarket API responses
│   ├── services/
│   │   ├── kalshi.py                # Kalshi SDK wrapper (REST API + candlesticks)
│   │   ├── events.py                # Event query logic + Redis price enrichment
│   │   ├── candlesticks.py          # Hybrid candle aggregation (historical + live merge)
│   │   ├── polymarket.py            # Polymarket on-chain data wrapper (planned)
│   │   ├── sportradar.py            # Sports data wrapper (planned)
│   │   ├── openai.py                # AI synthesis service (planned)
│   │   ├── firebase.py              # Push notification service (planned)
│   │   └── weather.py               # Weather API wrapper (planned)
│   ├── workers/
│   │   ├── kalshi/
│   │   │   ├── ingest.py            # Syncs series/events/markets/outcomes + OHLCV aggregation
│   │   │   └── stream.py            # WebSocket firehose: ticker → Redis + live candles, lifecycle → Postgres
│   │   ├── polymarket/
│   │   │   ├── ingest.py            # Gamma API sync: series/events/markets/outcomes; array taxonomy categories/tags
│   │   │   └── stream.py            # CLOB WS: book/price_change → Redis HSET, ZADD trending ZSET
│   │   ├── taxonomy.py              # upsert_platform_tag(), slugify() — shared taxonomy helpers
│   │   ├── injury_monitor.py        # Polls injury reports every 5 min
│   │   ├── lineup_monitor.py        # Watches lineup releases
│   │   ├── market_monitor.py        # Watches line movement and volume spikes
│   │   └── whale_monitor.py         # Polymarket large position detection
│   └── core/
│       ├── config.py                # Loads all env variables (including DEV_MODE)
│       ├── dev_config.py            # DEV_MODE sandbox constants: DEV_TARGET_SERIES, DEV_TARGET_MARKETS
│       ├── database.py              # SQLAlchemy engine + raw asyncpg pool
│       ├── redis.py                 # Redis async connection (arq queue + tick cache)
│       └── arq_worker.py            # arq WorkerSettings + cron job registration (crons disabled in DEV_MODE)
├── tests/
├── .env                             # Real keys — NEVER commit
├── .env.example                     # Template — commit this
├── .gitignore
├── requirements.txt
├── Procfile
└── README.md
```

---

## Database Schema Overview

Three-level market hierarchy normalised across exchanges:

```
series (exchange, ext_id)
  └── events (exchange, ext_id, series_id)
        └── markets (exchange, ext_id, event_id)
              └── market_outcomes (market_id, execution_asset_id)
```

Key design decisions:
- `ext_id` is the native exchange identifier (Kalshi ticker, Polymarket slug/conditionId)
- `exchange` enum (`kalshi` | `polymarket`) on every row enables cross-exchange queries
- `platform_metadata JSONB` absorbs exchange-specific fields (Kalshi CTF IDs, Neg Risk flags, etc.) without schema changes
- All tables use soft deletes (`is_deleted`) — never hard-delete market data
- `market_outcomes.execution_asset_id` = Kalshi `"yes"/"no"` or Polymarket ERC-1155 token ID
- **Array taxonomy**: `series.categories`, `series.tags`, `events.categories`, `events.tags` are all `ARRAY(text)` slug strings with GIN indexes — O(1) `@>` containment filtering
- **series_ids**: `events.series_ids` is `ARRAY(UUID)` replacing old `series_id` FK — supports Polymarket many-to-many events↔series. Filter via `array_position(series_ids, $uuid)` in SQLAlchemy.
- **PlatformTag**: `platform_tags` table (exchange, type=category|tag, slug, label, image_url, is_carousel, force_show) — UI metadata for the category/tag taxonomy displayed in the frontend. Slug matches ARRAY values in events/series.
- **Taxonomy slugify rule**: `slugify(str)` normalizes any string to URL-safe slug (lowercase, hyphens, no special chars). All ARRAY taxonomy values use slugified strings.

Frontend display columns (added to avoid JSONB parsing on the FE):
- `series.settlement_sources` (JSONB), `series.contract_url`, `series.additional_prohibitions` (JSONB)
- `events.sub_title`, `events.featured_image_url`, `events.settlement_sources` (JSONB), `events.competition`, `events.competition_scope`
- `markets.yes_sub_title`, `markets.no_sub_title`, `markets.image_url`, `markets.color_code`, `markets.open_interest`, `markets.volume`

Full SQL schema is in `/schema.sql`. ORM models live in `app/models/db.py`.

---

## Models Directory (`app/models/`)

### `core/market_cache.py` — Redis Key Schema
- Key format: `ticker:{exchange}:{execution_asset_id}` — e.g. `ticker:kalshi:KXHIGHNY-26MAR04-B52.5-yes`
- For Kalshi: `execution_asset_id` = `{market_ticker}-{side}` (side is `yes` or `no`)
- For Polymarket: `execution_asset_id` = ERC-1155 token ID (globally unique, no composite needed)
- Each key is a Redis HSET with fields: `price`, `bid`, `bid_size`, `ask`, `ask_size`, `volume_24h`, `ts`
- Live candle keys: `candle:kalshi:{market_ticker}:{minute_ts}` with 5-minute TTL
- **Important**: prefix is `ticker:` not `market:` — searching `market:*` finds no keys

### `db.py` — SQLAlchemy ORM
- One class per database table
- Uses PostgreSQL-specific types: `UUID`, `JSONB`, `ENUM` (via `sqlalchemy.dialects.postgresql`)
- ENUMs use `create_type=False` — types are created in PostgreSQL directly, not by SQLAlchemy
- All models inherit from `Base` (declarative base in `app/core/database.py`)
- Timestamp fields: `created_at` with `server_default=func.now()`, `updated_at` with `onupdate=func.now()`

### `kalshi.py` — Pydantic API Models
- Typed representations of every response shape from the Kalshi REST API and WebSocket
- Use `model_config = ConfigDict(extra='ignore')` to safely handle undocumented fields Kalshi may return
- Prices from Kalshi are in **cents (0–100)**. Normalize to `Decimal` / float (0.0–1.0) before storing in DB
- Status strings from Kalshi (`"open"`, `"closed"`, `"settled"`) are mapped to our `market_status` enum via `KALSHI_STATUS_MAP`
- All Optional fields should default to `None`, not raise `ValidationError`

### `polymarket.py` — Pydantic API Models
- Typed representations of Polymarket REST/on-chain responses
- Prices from Polymarket are already normalized to 0.0–1.0 float
- Wallet hashes are stored as-is in `tracked_entities.external_identifier`

**Rule:** When adding a new exchange or data source, add a new `<exchange>.py` model file. Never put exchange-specific types in `db.py`.

---

## Backend Best Practices

### Frontend Communication
- **All routes return Pydantic-typed response models.** Never return raw DB objects or raw API dicts.
- Define response schemas inline in route files for simple endpoints, or in `app/api/schemas/` for complex shared schemas.
- Use consistent field naming: `snake_case` throughout (FastAPI serialises to camelCase for the FE if configured).
- Pagination: use cursor-based pagination matching the Kalshi convention (`cursor` query param, return `cursor` in response).
- Error responses: always return `{"detail": "human readable message"}` with appropriate HTTP status codes. Never leak internal error messages or stack traces to the FE.
- Null vs missing: prefer `Optional[T]` fields in response models over omitting fields — the FE can rely on a stable schema.

### External API Calls
- All external API calls happen in `app/services/`. Routes and workers import from services — never use `httpx` directly in route handlers or workers.
- Services should be thin wrappers: they handle auth, retries, and response normalisation. They do NOT contain business logic.
- Kalshi REST: use `kalshi-python-async` SDK. Auth (RSA-PSS signing) is handled internally by the SDK. Singleton client in `services/kalshi.py::_get_client()`.
- Kalshi WebSocket: separate implementation in `workers/kalshi/stream.py` using `websockets` library with manual RSA auth headers on connect.
- Handle API errors explicitly: check status codes, log errors with context, return typed error responses. Never let an unhandled HTTP exception propagate to the route.
- Rate limits: respect exchange rate limits. Add exponential backoff for retryable errors (5xx, 429).

### Data Modeling and Typing
- Every piece of data that flows through the system must be typed:
  - External API response → **Pydantic model** (`app/models/<exchange>.py`)
  - Database row → **SQLAlchemy ORM model** (`app/models/db.py`)
  - API response to FE → **Pydantic response model** (inline in route or `app/api/schemas/`)
- Upserts: use `sqlalchemy.dialects.postgresql.insert().on_conflict_do_update()` for all ingestion. Never check-then-insert — race conditions.
- Use `JSONB` (`platform_metadata`) for exchange-specific fields that don't belong in typed columns. Query JSONB with SQLAlchemy's `column['key'].as_string()` operators.
- Soft deletes only: set `is_deleted = True`, never `DELETE FROM`. Market history is valuable.
- Numeric precision: monetary values use `NUMERIC(24, 8)` in DB and `Decimal` in Python. Prices use `NUMERIC(10, 6)`. Never use `float` for money.

### Worker Conventions
- Workers are async functions, scheduled via **arq** cron jobs in `app/core/arq_worker.py`.
- arq runs as a **separate process** (`arq app.core.arq_worker.WorkerSettings`) — not inside the FastAPI event loop.
- Each arq task receives a `ctx` dict containing `asyncpg_pool` and `redis` (initialized in `on_startup`).
- For SQLAlchemy-based upserts, workers create their own `AsyncSession` from `async_session_factory()`.
- Workers log at `INFO` level for normal operation, `WARNING` for skipped items, `ERROR` for failures.
- Workers must not crash the process on errors — wrap the body in `try/except Exception` and log.
- Ingestion order matters: always sync `series → events → markets → outcomes` in sequence. Markets reference events; events reference series.
- **Startup behavior**: DEV_MODE=True runs `run_kalshi_dev_sync()` synchronously (fast, ~30s) before server starts. Polymarket dev sync runs as a background task (Gamma API is slow, ~10s/event) so the server starts immediately and Polymarket WS subscribes after sync completes. DEV_MODE=False (production) does NOT run any sync on startup — arq handles full sync on its 15min schedule. The Kalshi WS starts immediately; Polymarket WS starts after loading token IDs from DB.
- **Write separation:** tick-level price data → Redis HSET (`ticker:{exchange}:{asset_id}`). Metadata → PostgreSQL. Live candles → Redis HSET (`candle:kalshi:{ticker}:{min_ts}`, 5-min TTL).
- **Batch upserts via UNNEST**: ingest.py builds column arrays and uses `conn.execute(INSERT ... SELECT unnest($1::uuid[]), ...)` for bulk upserts in a single round-trip. Falls back to row-by-row on error to isolate bad rows.
- **MVE filtering**: tickers starting with `KXMVE` are multivariate event (parlay/combo) markets — skipped at ingest to avoid DB bloat. Check `_is_mve(ticker)` in `ingest.py`.
- **Event volume**: `events.volume_24h` is not set directly from Kalshi — it's aggregated by `aggregate_event_volumes` arq cron from `markets.platform_metadata->>'volume_24h'` summed per event. Runs every 5 minutes in production.

### Route Conventions
- Routes are thin: validate input, call a service function, return a typed response. No business logic in route handlers.
- V1 routes live in `app/api/routes/v1/`. Basic routes live in `app/api/routes/`.
- Use `Depends(get_db)` for all routes that need DB access.
- Routes query the DB (via SQLAlchemy) — they do NOT call external APIs directly for market data.
- If a resource doesn't exist in the DB, return `404` with a clear message. Do not fall back to calling the exchange API.
- Use `select()` with explicit column loading. Avoid `session.execute(text(...))` raw SQL unless absolutely necessary.
- Filter by `is_deleted = False` on every query.

### Service Layer Pattern
- **API service wrappers** (`services/kalshi.py`): Thin wrappers around external SDKs/APIs. Handle auth, retries, error logging. No business logic.
- **Domain services** (`services/events.py`, `services/candlesticks.py`): Handle the heavy lifting — DB queries, Redis enrichment, data merging. Called by route handlers.
- Routes import from services and delegate all query/merge logic. Routes only handle request validation and response serialization.
- When adding new endpoints, create or extend a service file for the domain logic, then write a thin route handler that calls it.

### Candlestick Caching Pattern
- **Live candle aggregation**: The WebSocket flusher calls `batch_update_live_candles(redis, [(ticker, price_cents, volume), ...])` from `services/candlesticks.py`. This runs an atomic Lua script per market via a single Redis pipeline, maintaining current-minute OHLCV at key `candle:kalshi:{ticker}:{minute_ts}` with 5-min TTL.
- **Historical candles**: Fetched on-demand from Kalshi REST API via `services/kalshi.py::get_market_candlesticks(series_ticker, market_ticker, start_ts, end_ts, period_interval)`. Note: takes `series_ticker` + `market_ticker`, not `event_ticker`.
- **Merge**: `services/candlesticks.py::get_merged_candlesticks()` combines both — overwrites the current minute if it exists in historical data, or appends if not.
- Prices stored in Redis as integer basis points (0–10000) for Lua integer math; normalized to 0.0–1.0 floats on read.
- **Kalshi 5000-candle limit**: `get_market_candlesticks` returns HTTP 400 if the requested range contains more than 5000 candles (~83 hours at 1-min interval). The service returns `[]` gracefully on error.
- **None price fallback**: Kalshi `price.open/high/low/close` fields are `None` when no actual trades occurred in a period. Fall back to `(yes_bid.open + yes_ask.open) / 2` midpoint. Never skip periods — fall through to the bid/ask midpoint check.
- **market_ticker is required for live candle**: The candlestick endpoint accepts optional `market_ticker` query param. Without it, `market_tickers=[]` and the live Redis candle is not fetched — only historical data is returned.

### DEV_MODE — Railway Pro Sandbox

**When DEV_MODE=True:**
- **Scope**: Explicit curated series list — 11 tickers across 4 categories defined in `app/core/dev_config.py::DEV_EXPLICIT_SERIES`. Each series tests a specific UI/data edge case.
- **Dev config**: `app/core/dev_config.py` defines `DEV_EXPLICIT_SERIES` (dict of category → tickers), `DEV_EXPLICIT_SERIES_FLAT` (flat list), `DEV_REDIS_FLUSH_INTERVAL` (0.5s), and mutable lists `DEV_TARGET_SERIES` + `DEV_TARGET_MARKETS` (populated dynamically at startup)
- **Startup flow**: `run_kalshi_dev_sync()` runs before WS connects → fetches all series from Kalshi, filters to `DEV_EXPLICIT_SERIES_FLAT` → upserts selected series → fetches `status="open"` events only → fetches event metadata (images, colors, settlement sources) → builds `DEV_TARGET_MARKETS` list → WS subscribes to those markets only
- **Event metadata**: During dev sync, `get_event_metadata()` is called per event to fetch display data (image_url, featured_image_url, market-level image_url/color_code, settlement_sources, competition info)
- **WebSocket**: Same production micro-batching flusher (`_enqueue_ticker` + asyncio.Queue + 100ms drain) — scope limited by filtered WS subscription, not debouncing
- **Subscribe message**: `{"id": 1, "cmd": "subscribe", "params": {"channels": ["ticker", "market_lifecycle_v2"], "market_tickers": [...DEV_TARGET_MARKETS]}}`
- **arq crons**: All cron jobs disabled (`WorkerSettings.cron_jobs = []`) — manually trigger via `POST /api/v1/dev/sync-kalshi`
- **Dev endpoint**: `app/api/routes/v1/dev.py::POST /api/v1/dev/sync-kalshi` — calls `run_kalshi_dev_sync()`, returns `{"status": "ok", "target_markets_count": N}`, returns 403 if DEV_MODE=False

**Explicit DEV series (11 tickers across 4 categories):**
- Politics: `KXNEXTIRANLEADER`, `KXDHSFUND`, `KXLOSEREELECTIONGOV`
- Economics: `KXFEDDECISION`, `KXINXY`, `KXISMPMI`
- Tech/Corporate: `KXIPO`, `KXGAMEAWARDS`
- Climate/Mentions: `KXNYTHEAD`, `KXHMONTH`, `KXWARMING`

**When DEV_MODE=False (production):**
- Global firehose subscription (no `market_tickers` filter)
- Production micro-batching: asyncio.Queue + 100ms/500-item flusher
- All arq crons enabled
- `POST /api/v1/dev/sync-kalshi` returns HTTP 403

---

## Key Design Principles

**Services are reusable, routes are specific.**
- `services/kalshi.py` knows HOW to talk to Kalshi. Nothing else.
- `services/events.py` knows HOW to query and enrich event data. Routes call it.
- `services/candlesticks.py` knows HOW to merge historical + live candles.
- `routes/v1/events.py` knows WHAT URLs exist and maps service output to response shapes.
- `workers/kalshi/ingest.py` knows HOW to keep the DB in sync with Kalshi.
- `main.py` just assembles everything together.

**The database is the source of truth.**
Routes serve from PostgreSQL. Workers keep PostgreSQL in sync. External APIs are inputs to the workers, not inputs to the routes.

**Workers are the real product.**
The notification pipeline — detecting the event, understanding what it means, finding affected users, generating the explanation, firing the push — is the core value proposition. Build this reliably.

**Never touch user funds.**
Alpha triggers API calls on user-owned accounts. Kalshi and Polymarket hold all funds. This is non-negotiable for regulatory cleanliness.

**Never pool funds for group betting.**
Each group member trades their own individual Kalshi account simultaneously. Social coordination layer only.

**Honesty in notifications.**
Only state the reason for a market move when the source is verifiable (injury report, lineup, weather). When cause is unknown, flag unusual activity honestly without fabricating an explanation. One wrong explanation destroys trust.

**Two-tier notification system:**
- Confident: "LeBron just listed doubtful. That's why your position dropped 56%."
- Honest uncertainty: "Unusual movement on this market. Line dropped 20%. No public news yet. Could be insider info."

---

## Regulatory Notes

- Never act as money transmitter — never hold user funds
- Never act as investment advisor — surface information, never recommend
- Never pool funds for group betting — individual accounts only
- Research layer and notifications are financial media, not financial advice

---

## Environment Variables Required

```
# Kalshi
KALSHI_API_KEY_ID=
KALSHI_PRIVATE_KEY=
KALSHI_BASE_API_URL=https://api.elections.kalshi.com/trade-api/v2
KALSHI_WS_URL=wss://api.elections.kalshi.com/trade-api/ws/v2

# Polymarket
POLYMARKET_API_KEY=

# Sports / Weather
SPORTRADAR_API_KEY=
ROTOWIRE_API_KEY=
OPENWEATHER_API_KEY=

# AI
OPENAI_API_KEY=

# Database (PostgreSQL via Supabase)
DATABASE_URL=postgresql+asyncpg://postgres:[password]@db.[ref].supabase.co:5432/postgres

# Redis (arq job queue + tick data cache)
REDIS_URL=redis://localhost:6379

# Firebase
FIREBASE_CREDENTIALS_PATH=

# App
ENVIRONMENT=development
DEV_MODE=false  # Set to true for local/free-tier development (restricts ingestion + WS to 3 series)
```

---

## Build Priority Order

1. ✅ Kalshi API connection — RSA auth, GET endpoints working
2. ✅ Market data ingestion — series/events/markets syncing into PostgreSQL via background worker
3. ✅ Routes serve from DB — all market endpoints query PostgreSQL
3b. ✅ Hybrid OHLCV candlestick endpoint — Kalshi historical + Redis live candle merge
3c. ✅ DEV_MODE sandbox — restricted 3-series sync, memory-debounced WebSocket, manual sync endpoint
4. Supabase user model — basic auth, user table
5. Sports data pipeline — Rotowire injury feed flowing
6. Background worker — injury detection running every 5 minutes
7. OpenAI synthesis — plain English explanations generating
8. Firebase notifications — push firing on real injury events
9. ✅ Polymarket data ingestion — CLOB WS + Gamma API sync working; array taxonomy (categories/tags/series_ids) on events+series; PlatformTags table created
10. Group betting social layer — simultaneous individual trade coordination
11. Full API ready for iOS consumption

**Ship in this order. Do not skip ahead.**

---

## Keeping This File Updated

When you (the coding agent) make significant changes to the codebase, update the relevant sections of this file:

- **New file added** → update the Folder Structure section
- **New env variable needed** → add to Environment Variables Required
- **New external API integrated** → add to Tech Stack and Architecture
- **Schema change** → update Database Schema Overview
- **New worker added** → update workers section in Folder Structure
- **Build milestone completed** → mark with ✅ in Build Priority Order
- **New design pattern established** → add to Backend Best Practices

This file is read by coding agents at the start of every session. It must always reflect reality.

**Build accordingly.**
