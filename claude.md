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
│ Redis                                │
│  ├── arq job queue (worker process) │
│  └── market:{ticker} HSET (ticks)   │
└─────────────────────────────────────┘
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
Kalshi WS ──► ticker channel ──► Redis HSET (real-time prices)
Kalshi WS ──► market_lifecycle_v2 ──► PostgreSQL (status/result changes)
arq cron  ──► kalshi_full_sync (15m) ──► PostgreSQL (full backfill)
arq cron  ──► state_reconciliation (1m) ──► PostgreSQL (delta sync)
arq cron  ──► aggregate_ohlcv (1m) ──► Redis ticks → PostgreSQL market_ticks
PostgreSQL ──► FastAPI routes ──► iOS app
```

**Write separation rule:** Tick-level price data goes to Redis (high frequency, ephemeral). Metadata and OHLCV candles go to PostgreSQL (durable). Routes read from PostgreSQL.

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
│   │       ├── sports.py            # Injuries, lineups, stats, weather, refs
│   │       ├── traders.py           # Sharp trader leaderboard, profiles
│   │       ├── users.py             # Auth, positions, portfolio
│   │       ├── groups.py            # Group betting social layer
│   │       └── notifications.py     # Notification history and preferences
│   ├── models/
│   │   ├── db.py                    # SQLAlchemy ORM models (one per DB table)
│   │   ├── kalshi.py                # Pydantic models for Kalshi API responses
│   │   └── polymarket.py            # Pydantic models for Polymarket API responses
│   ├── services/
│   │   ├── kalshi.py                # Kalshi SDK wrapper (kalshi-python-async)
│   │   ├── polymarket.py            # Polymarket on-chain data wrapper
│   │   ├── sportradar.py            # Sports data wrapper
│   │   ├── openai.py                # AI synthesis service
│   │   ├── firebase.py              # Push notification service
│   │   └── weather.py               # Weather API wrapper
│   ├── workers/
│   │   ├── kalshi/
│   │   │   ├── ingest.py            # Syncs series/events/markets/outcomes + OHLCV aggregation
│   │   │   └── stream.py            # WebSocket firehose: ticker → Redis, lifecycle → Postgres
│   │   ├── injury_monitor.py        # Polls injury reports every 5 min
│   │   ├── lineup_monitor.py        # Watches lineup releases
│   │   ├── market_monitor.py        # Watches line movement and volume spikes
│   │   └── whale_monitor.py         # Polymarket large position detection
│   └── core/
│       ├── config.py                # Loads all env variables
│       ├── database.py              # SQLAlchemy engine + raw asyncpg pool
│       ├── redis.py                 # Redis async connection (arq queue + tick cache)
│       └── arq_worker.py            # arq WorkerSettings + cron job registration
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

Full SQL schema is in `/schema.sql`. ORM models live in `app/models/db.py`.

---

## Models Directory (`app/models/`)

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
- On FastAPI startup: run a full sync immediately before serving requests. The WebSocket firehose also starts as a background task in the web process.
- **Write separation:** tick-level price data → Redis HSET. Metadata/OHLCV → PostgreSQL.

### Route Conventions
- Routes are thin: validate input, call a query or service, return a typed response. No business logic.
- Use `Depends(get_db)` for all routes that need DB access.
- Routes query the DB (via SQLAlchemy) — they do NOT call external APIs directly for market data.
- If a resource doesn't exist in the DB, return `404` with a clear message. Do not fall back to calling the exchange API.
- Use `select()` with explicit column loading. Avoid `session.execute(text(...))` raw SQL unless absolutely necessary.
- Filter by `is_deleted = False` on every query.

---

## Key Design Principles

**Services are reusable, routes are specific.**
- `services/kalshi.py` knows HOW to talk to Kalshi. Nothing else.
- `routes/markets.py` knows WHAT URLs exist and maps DB data to response shapes.
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
```

---

## Build Priority Order

1. ✅ Kalshi API connection — RSA auth, GET endpoints working
2. ✅ Market data ingestion — series/events/markets syncing into PostgreSQL via background worker
3. ✅ Routes serve from DB — all market endpoints query PostgreSQL
4. Supabase user model — basic auth, user table
5. Sports data pipeline — Rotowire injury feed flowing
6. Background worker — injury detection running every 5 minutes
7. OpenAI synthesis — plain English explanations generating
8. Firebase notifications — push firing on real injury events
9. Polymarket data ingestion — whale watching, sharp trader scoring
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
