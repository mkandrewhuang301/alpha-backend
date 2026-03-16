# Alpha Backend — CLAUDE.md

> **IMPORTANT FOR CODING AGENTS:** You are expected to keep this file updated as the codebase evolves. When you add new services, workers, routes, models, or architectural patterns, update the relevant section here. This is a living document — it should always reflect the actual state of the codebase, not just the initial design.

---

## gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

If gstack skills aren't working, run `cd ~/.claude/skills/gstack && ./setup` to build the binary and register skills.

### Available skills
- `/browse` — browse the web with a headless Chromium browser
- `/plan-ceo-review` — review a plan from a CEO/product perspective
- `/plan-eng-review` — review a plan from an engineering perspective
- `/review` — code review
- `/ship` — ship code (review + merge)
- `/qa` — QA testing with browser automation
- `/qa-only` — QA testing only (no code changes)
- `/setup-browser-cookies` — set up browser cookies for authenticated browsing
- `/retro` — run a retrospective
- `/gstack-upgrade` — upgrade gstack to the latest version

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
Polymarket CLOB WS ──► book/price_change ──► Redis HSET ticker:polymarket:{token_id}
                                           + ZADD events_trending_24h_polymarket
Polymarket CLOB WS ──► last_trade_price >= 0.99|<= 0.01 ──► _resolution_candidates set
                   ──► Phase 3 flusher ──► _check_and_update_market_resolution()
                   ──► Gamma API confirm ──► PostgreSQL UPDATE (status=resolved, result=outcome)
Kalshi WS ──► DISABLED (ws_task = None in main.py) ──► no streaming in current build
arq cron  ──► polymarket_state_reconciliation (every 2min, DEV + prod) ──► Gamma API batch → PostgreSQL UPDATE
arq cron  ──► kalshi_full_sync (15m, prod only) ──► PostgreSQL (full series+events+markets+outcomes backfill)
arq cron  ──► kalshi_state_reconciliation (1m, prod only) ──► PostgreSQL (delta via min_updated_ts filter)
arq cron  ──► aggregate_event_volumes (5m, prod only) ──► PostgreSQL (events.volume_24h from market metadata)
arq cron  ──► aggregate_ohlcv (1m, prod only) ──► PostgreSQL market_ticks (tick snapshots from Redis)
POST /api/v1/dev/sync/{exchange} ──► run_{exchange}_dev_sync() ──► PostgreSQL (dev mode manual trigger)
Kalshi REST ──► historical candlesticks (on-demand per API request)
PostgreSQL + Redis ──► FastAPI routes ──► iOS app
```

**Kalshi WebSocket status:** Currently **disabled** (`ws_task = None` in `main.py`, import commented out). Kalshi price data is updated only via arq crons. This is intentional — Polymarket is the primary real-time exchange in the current build.

**Polymarket WebSocket strategy (`workers/polymarket/stream.py`):**
```
Both DEV_MODE and production use the same micro-batching flusher:
  WS receive loop → _parse_and_enqueue(msg) → asyncio.Queue(maxsize=100_000) (non-blocking, drops if full)
  _redis_flusher() → drain in 100ms/500-item batches → pipeline HSET ticker:polymarket:{token_id}
                   → Phase 2: ZADD events_trending_24h_polymarket (volume from tick data)
                   → Phase 3: drain _ws_resolutions dict → asyncio.create_task(_apply_ws_resolution())
                   → Phase 4: drain _resolution_candidates → asyncio.create_task(_check_and_update_market_resolution())

Subscribe message (custom_feature_enabled=true required for market_resolved/new_market/best_bid_ask):
  {"type": "market", "assets_ids": [token_id, ...], "custom_feature_enabled": true}

Message types handled:
  book            → _handle_book_message() — bids/asks are {"price": "0.48", "size": "30"} dicts (not tuples)
  price_change    → _handle_price_change_message() — top-level price_changes array (multi-asset per msg)
                    each entry has asset_id, best_bid, best_ask directly from exchange
  best_bid_ask    → _handle_best_bid_ask_message() — authoritative best bid/ask (custom_feature_enabled)
  last_trade_price → _handle_last_trade_price_message() — last traded price, resolution detection
  tick_size_change → _handle_tick_size_change_message() — new_tick_size field (not tick_size)
  market_resolved  → _handle_market_resolved_message() — direct resolution (Path A, no Gamma roundtrip)
  new_market       → _handle_new_market_message() — logged, picked up by reconciliation cron

Resolution paths (two-tier):
  Path A (WS market_resolved event — preferred, no Gamma API):
    winning_asset_id + winning_outcome from WS → _ws_resolutions dict
    → _apply_ws_resolution(token_id, outcome) → DB lookup → Postgres UPDATE
  Path B (last_trade_price fallback — Gamma API confirm):
    last_trade_price >= 0.99 or <= 0.01 → _resolution_candidates set
    → _check_and_update_market_resolution(token_id) → Gamma API → Postgres UPDATE
  Both paths: Semaphore(3) — caps concurrent Supabase connections
```

**Kalshi WebSocket strategy (`workers/kalshi/stream.py`) — for when it's re-enabled:**
```
DEV_MODE=True:
  Subscribe: {"channels": [...], "market_tickers": [...DEV_TARGET_MARKETS]} (filtered to explicit series)
  WS receive loop → _enqueue_ticker() → asyncio.Queue(maxsize=100_000)
  _redis_flusher() → drain in 100ms/500-item batches → pipeline HSET

DEV_MODE=False (production):
  Subscribe: global firehose (no market_tickers filter — all 5000+ markets)
  WS receive loop → _enqueue_ticker() → asyncio.Queue(maxsize=100_000)
  _redis_flusher() → drain in 100ms/500-item batches → pipeline HSET
  Phase 2: asyncio.create_task(_flush_candles()) → batch candle EVAL (fire-and-forget)

Lifecycle events (both modes):
  market_lifecycle_v2 → asyncio.create_task(_handle_market_lifecycle(msg))
  _lifecycle_semaphore = asyncio.Semaphore(3) — caps concurrent Supabase connections
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
- **Real-time**: Polymarket CLOB WebSocket (`websockets` library) for live price + resolution detection; Kalshi WebSocket currently disabled
- **Polymarket REST**: `httpx` async client — Gamma API (public, no auth) for series/events/markets/tags/sports
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
│   │                                # NOTE: Kalshi WS disabled (ws_task = None). Polymarket WS active.
│   ├── api/
│   │   └── routes/
│   │       ├── markets.py           # Market endpoints — queries DB
│   │       ├── series.py            # Series endpoints — queries DB
│   │       ├── events.py            # Event endpoints — queries DB
│   │       ├── v1/                  # Versioned endpoints (DB + Redis live data)
│   │       │   ├── events.py        # /api/v1/{exchange}/events — live prices merged
│   │       │   ├── candlesticks.py  # /api/v1/kalshi/events/{ticker}/candlesticks — hybrid OHLCV
│   │       │   └── dev.py           # /api/v1/dev/sync/{exchange} — manual sync (DEV_MODE only, returns 403 in prod)
│   │       ├── categories.py        # /categories/platform-tags — PlatformTag taxonomy (exchange, type, parent_ids, slug)
│   │       ├── sports.py            # Injuries, lineups, stats, weather, refs (planned)
│   │       ├── traders.py           # Sharp trader leaderboard, profiles (planned)
│   │       ├── users.py             # Auth, positions, portfolio (planned)
│   │       ├── groups.py            # Group betting social layer (planned)
│   │       └── notifications.py     # Notification history and preferences (planned)
│   ├── models/
│   │   ├── db.py                    # SQLAlchemy ORM models (one per DB table)
│   │   │                            # Key models: Series, Event, Market, MarketOutcome,
│   │   │                            #   PlatformTag (unified, parent_ids ARRAY, GIN indexed),
│   │   │                            #   SportsMetadata (exchange + sport PK, tag_ids ARRAY, GIN indexed)
│   │   ├── kalshi.py                # Pydantic models for Kalshi API responses
│   │   └── polymarket.py            # Pydantic models for Polymarket Gamma API responses
│   │                                # Key models: PolymarketSeries, PolymarketEvent, PolymarketMarket,
│   │                                #   PolymarketToken, PolymarketCategory
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
│   │   │   ├── ingest.py            # Full sync + delta sync + OHLCV aggregation (prod arq crons)
│   │   │   └── stream.py            # WebSocket firehose (DISABLED — ws_task=None in main.py)
│   │   ├── polymarket/
│   │   │   ├── ingest.py            # Gamma API sync: series/events/markets/outcomes/tags/sports
│   │   │   │                        # run_polymarket_dev_sync() — startup sync (DEV_MODE)
│   │   │   │                        # run_polymarket_state_reconciliation() — arq cron (DEV + prod, every 2min)
│   │   │   │                        # _fetch_tag_children() — exponential backoff on 429 (5s→10s→20s→40s→60s→60s, 6 attempts, capped at 60s)
│   │   │   │                        # _process_series() — caps at 4 events per series in DEV_MODE
│   │   │   └── stream.py            # CLOB WS: book/price_change → Redis HSET, ZADD trending ZSET
│   │   │                            # Resolution: last_trade_price >= 0.99|<= 0.01 → Gamma confirm → Postgres UPDATE
│   │   ├── taxonomy.py              # upsert_platform_tag(parent_ids, force_hide, platform_metadata), slugify()
│   │   ├── injury_monitor.py        # Polls injury reports every 5 min (planned)
│   │   ├── lineup_monitor.py        # Watches lineup releases (planned)
│   │   ├── market_monitor.py        # Watches line movement and volume spikes (planned)
│   │   └── whale_monitor.py         # Polymarket large position detection (planned)
│   └── core/
│       ├── config.py                # Loads all env variables (including DEV_MODE)
│       ├── dev_config.py            # DEV_MODE sandbox constants:
│       │                            #   DEV_EXPLICIT_SERIES (Kalshi series dict, currently all commented out)
│       │                            #   DEV_TARGET_SERIES, DEV_TARGET_MARKETS (populated at startup)
│       │                            #   POLYMARKET_DEV_TOKEN_IDS, POLYMARKET_DEV_EVENT_IDS
│       ├── database.py              # SQLAlchemy engine + raw asyncpg pool
│       ├── redis.py                 # Redis async connection (arq queue + tick cache)
│       └── arq_worker.py            # arq WorkerSettings + cron job registration
│                                    # DEV_MODE: only polymarket_state_reconciliation (every 2min)
│                                    # PROD: full suite (kalshi_full_sync, kalshi_state_reconciliation,
│                                    #   aggregate_ohlcv, aggregate_event_volumes, polymarket_state_reconciliation)
├── migrations/
│   └── versions/
│       └── 0001_complete_schema.py  # Single idempotent migration — safe on fresh or existing DB
│                                    # Idempotency guards: _table_exists(), _col_exists(), _index_exists()
│                                    # Handles: platform_tags (parent_ids ARRAY, force_hide, platform_metadata),
│                                    #   sports_metadata table, drops old polymarket_tags/polymarket_sports_metadata
├── tests/
├── .env                             # Real keys — NEVER commit
├── .env.example                     # Template — commit this
├── .gitignore
├── requirements.txt
├── Procfile                         # web: uvicorn + worker: arq (both needed in DEV_MODE and prod)
└── README.md
```

---

## Database Schema Overview

Three-level market hierarchy normalised across exchanges:

```
series (exchange, ext_id)
  └── events (exchange, ext_id, series_ids ARRAY[UUID])
        └── markets (exchange, ext_id, event_id)
              └── market_outcomes (market_id, execution_asset_id)
```

Key design decisions:
- `ext_id` is the native exchange identifier (Kalshi ticker, Polymarket conditionId/event id/series id)
- `exchange` enum (`kalshi` | `polymarket`) on every row enables cross-exchange queries
- `platform_metadata JSONB` absorbs exchange-specific fields without schema changes
- All tables use soft deletes (`is_deleted`) — never hard-delete market data
- `market_outcomes.execution_asset_id` = Kalshi `"yes"/"no"` or Polymarket ERC-1155 clobTokenId
- **Array taxonomy**: `series.categories`, `series.tags`, `events.categories`, `events.tags` are all `ARRAY(text)` slug strings with GIN indexes — O(1) `@>` containment filtering
- **series_ids**: `events.series_ids` is `ARRAY(UUID)` — supports Polymarket many-to-many events↔series. Filter via `array_position(series_ids, $uuid)` in SQLAlchemy.
- **PlatformTag** (`platform_tags` table): unified tag taxonomy across exchanges. Fields: `exchange`, `type` (`category`|`tag`), `slug`, `label`, `image_url`, `is_carousel`, `force_show`, `force_hide`, `platform_metadata JSONB`, **`parent_ids ARRAY(text)`** (GIN indexed). Slug matches ARRAY values in events/series. `parent_ids` enables multi-parent tag hierarchies (e.g. a tag belonging to multiple sport categories).
- **SportsMetadata** (`sports_metadata` table): `exchange` + `sport` as composite PK, `tag_ids ARRAY(text)` (GIN indexed), `resolution_url`, `series_slug`. Replaces old exchange-specific `polymarket_sports_metadata` table.
- **Taxonomy slugify rule**: `slugify(str)` normalizes any string to URL-safe slug (lowercase, hyphens, no special chars). All ARRAY taxonomy values use slugified strings.
- **Alembic**: Single idempotent migration `0001_complete_schema.py` — run `alembic upgrade head` to apply on any DB state.

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
- Typed representations of Polymarket Gamma API responses
- Prices from Polymarket are already normalized to 0.0–1.0 float
- Key models: `PolymarketCategory` (id + label, `label_str` property), `PolymarketSeries` (`category_labels` property), `PolymarketEvent`, `PolymarketMarket` (conditionId as ext_id), `PolymarketToken`
- `_parse_json_field()` handles Gamma API returning `outcomes` and `clobTokenIds` as either JSON strings or native lists
- Zipping: `zip(outcomes, token_ids)` → one `MarketOutcome` row per `(outcome_label, token_id)` pair
- Wallet hashes stored as-is in `tracked_entities.external_identifier`

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
- **Startup behavior (DEV_MODE=True)**: Kalshi sync is disabled entirely. Polymarket dev sync runs as a background `asyncio.create_task` — server starts immediately, sync + WS subscribe happen in background. arq worker process still must be started separately for state reconciliation cron.
- **Startup behavior (DEV_MODE=False)**: No sync on startup — arq handles scheduling. Polymarket WS starts immediately using token IDs loaded from DB. Kalshi WS is currently disabled (`ws_task = None`).
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
- **Kalshi**: All Kalshi sync and WebSocket are disabled. `DEV_EXPLICIT_SERIES` dict is defined but all entries are commented out. `DEV_TARGET_SERIES` and `DEV_TARGET_MARKETS` remain empty.
- **Polymarket startup**: `run_polymarket_dev_sync()` runs as a background `asyncio.create_task` — server starts immediately while sync runs in background. Fetches 200 active series from Gamma API, selects up to 20 diverse series across (category × format_type) buckets (max 2 per bucket), caps at 4 events per series. Falls back to DB token IDs if sync yields 0. After sync completes, builds token→event map and starts Polymarket CLOB WS.
- **Dev config**: `app/core/dev_config.py` defines `DEV_REDIS_FLUSH_INTERVAL` (0.5s), mutable `POLYMARKET_DEV_TOKEN_IDS` + `POLYMARKET_DEV_EVENT_IDS` (populated at startup). No hardcoded slugs — series selected dynamically.
- **Polymarket DEV diversity logic**: `_infer_series_format_type()` classifies each series as `binary` / `categorical` / `scalar` using negRisk flag, slug/title keywords, and category hints. `_select_diverse_series()` groups into (category, format) buckets and round-robins to maximize variety.
- **arq crons**: Only `run_polymarket_state_reconciliation` (every 2 minutes). All Kalshi crons disabled. **arq worker process is still required in DEV_MODE** — run `arq app.core.arq_worker.WorkerSettings` alongside uvicorn.
- **Dev endpoint**: `POST /api/v1/dev/sync/{exchange}` — returns 403 in prod

**When DEV_MODE=False (production):**
- Polymarket WS: loads all token IDs from DB immediately, subscribes to full production set
- All arq crons enabled (full Kalshi suite + Polymarket reconciliation every 2min)
- `POST /api/v1/dev/sync/{exchange}` returns HTTP 403

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
9. ✅ Polymarket data ingestion — CLOB WS + Gamma API sync working; GIN-indexed array taxonomy (categories/tags/series_ids); PlatformTag table (unified, parent_ids ARRAY); SportsMetadata table; state reconciliation cron (every 2min, DEV + prod); resolution detection via CLOB WS last_trade_price signals
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
