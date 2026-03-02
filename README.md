# Alpha Backend

FastAPI backend powering the Alpha iOS prediction market intelligence app. Integrates with [Kalshi](https://kalshi.com) (and eventually Polymarket) for real-time market data, charting, and trading.

## What It Does

- Continuously syncs market data from Kalshi into PostgreSQL via background workers
- Serves normalized market data to the iOS app via REST API
- Provides real-time price updates via WebSocket firehose → Redis cache
- Delivers hybrid OHLCV candlestick data (Kalshi historical + Redis live) for charting

## Project Structure

```
app/
  main.py                     # Entry point, registers all routers, lifespan management
  core/
    config.py                 # Loads env vars (API keys, base URL)
    database.py               # SQLAlchemy engine + raw asyncpg pool
    redis.py                  # Redis async connection (arq queue + tick cache)
    market_cache.py           # Redis cache manager for real-time ticker data
    arq_worker.py             # arq WorkerSettings + cron job registration
  api/routes/
    series.py                 # /series endpoints (DB queries)
    events.py                 # /events endpoints (DB queries)
    markets.py                # /markets endpoints (DB queries)
    v1/
      events.py               # /api/v1/{exchange}/events — DB + live Redis prices
      candlesticks.py         # /api/v1/kalshi/events/{ticker}/candlesticks — hybrid OHLCV
  services/
    kalshi.py                 # Kalshi SDK wrapper (REST API + candlesticks)
    events.py                 # Event query logic + Redis enrichment
    candlesticks.py           # Hybrid candle aggregation (historical + live merge)
  workers/
    kalshi/
      ingest.py               # Full sync + delta sync + OHLCV aggregation
      stream.py               # WebSocket firehose: ticker → Redis + live candles, lifecycle → Postgres
  models/
    db.py                     # SQLAlchemy ORM models (all tables)
    kalshi.py                 # Pydantic models for Kalshi API responses
    polymarket.py             # Pydantic models for Polymarket (planned)
```

## Architecture

```
iOS App (React Native)
      ↕ HTTPS/JSON
FastAPI Backend (web process)
      ↕
PostgreSQL (Supabase) ←→ Redis (tick cache + live candles + arq queue)
      ↕
Workers: Kalshi REST sync (arq cron) + WebSocket firehose (asyncio task)
```

**Data flow:**
- Kalshi WS → ticker channel → Redis HSET (real-time prices) + Redis candle (live OHLCV)
- Kalshi WS → market_lifecycle_v2 → PostgreSQL (status/result changes)
- arq cron → kalshi_full_sync (15m) → PostgreSQL (full backfill)
- arq cron → state_reconciliation (1m) → PostgreSQL (delta sync)
- Kalshi REST → historical candlesticks (on-demand per API request)
- PostgreSQL + Redis → FastAPI routes → iOS app

## Current Endpoints

### Basic Endpoints (DB queries only)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/series` | List all series |
| GET | `/series/{ticker}` | Get series by ticker |
| GET | `/events` | List events |
| GET | `/events/{event_ticker}` | Get event by ticker |
| GET | `/markets` | List markets |
| GET | `/markets/{ticker}` | Get market by ticker with outcomes |

### V1 Endpoints (DB + live Redis prices)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/{exchange}/categories` | Distinct active event categories |
| GET | `/api/v1/{exchange}/events` | Event feed with sorting, pagination, live prices |
| GET | `/api/v1/{exchange}/events/{event_ext_id}` | Full event detail with markets/outcomes/ticker data |
| GET | `/api/v1/kalshi/events/{event_ticker}/candlesticks` | Hybrid OHLCV candlesticks for charting |

### Candlestick Endpoint Details

`GET /api/v1/kalshi/events/{event_ticker}/candlesticks`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `event_ticker` | path | yes | Kalshi event ticker |
| `series_ticker` | query | yes | Parent series ticker |
| `start_ts` | query | yes | Start Unix timestamp |
| `end_ts` | query | no | End Unix timestamp (defaults to now) |
| `market_ticker` | query | no | Specific market ticker for live candle |

Returns 1-minute OHLCV candles with prices normalized to 0.0-1.0 ($1.00 base). The most recent candle includes sub-second live data from the WebSocket feed.

## Hybrid Candlestick Strategy

Historical candles are fetched from Kalshi's REST API (pre-computed, no wasted compute). The live current-minute candle is aggregated in Redis from WebSocket ticks using an atomic Lua script. The endpoint merges both sources, providing a seamless real-time charting experience.

## Setup

1. Clone the repo
2. Create a `.env` file (see `.env.example`)
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Run the server:
   ```bash
   python3 -m uvicorn app.main:app --reload
   ```
5. Run the arq worker (separate process):
   ```bash
   arq app.core.arq_worker.WorkerSettings
   ```
6. Open `http://localhost:8000/docs` for interactive API explorer

## Near Term Goals

- [x] Kalshi API connection — RSA auth, GET endpoints working
- [x] Market data ingestion — series/events/markets syncing into PostgreSQL
- [x] Routes serve from DB — all market endpoints query PostgreSQL
- [x] Real-time price cache via WebSocket → Redis
- [x] Hybrid OHLCV candlestick endpoint (historical + live)
- [ ] Supabase user model — basic auth, user table
- [ ] Sports data pipeline — injury feed
- [ ] OpenAI synthesis — plain English explanations
- [ ] Firebase push notifications
- [ ] Polymarket data ingestion
