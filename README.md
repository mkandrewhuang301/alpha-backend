# Alpha Backend

FastAPI backend powering the Alpha iOS prediction market intelligence app. Integrates with [Kalshi](https://kalshi.com) (and eventually Polymarket) for real-time market data, charting, and trading.

## What It Does

- Continuously syncs market data from Kalshi into PostgreSQL via background workers
- Serves normalized market data to the iOS app via REST API
- Provides real-time price updates via WebSocket firehose → Redis cache
- Delivers hybrid OHLCV candlestick data (Kalshi historical + Redis live) for charting

## Development vs Production Mode

This backend ships with a **DEV_MODE** flag designed for free-tier infrastructure development.

### DEV_MODE=True (Development)

Set `DEV_MODE=true` in `.env` to activate the sandbox environment. When enabled:

- **Restricted ingestion**: Only 3 series are synced instead of the entire Kalshi catalog:
  - `KXHIGHNY` — Daily High Temp in NYC (Weather)
  - `KXCPI` — US CPI Inflation (Economics)
  - `KXNBAPLAYOFF` — NBA Playoff Qualifiers (Sports)
- **Filtered WebSocket**: Kalshi ticker subscription is scoped to open markets from those 3 series only (~30–110 markets), instead of the global firehose (5,000+ markets)
- **Memory debouncing**: Incoming ticker updates overwrite an in-memory dict; a background task flushes to Redis exactly every 1.5 seconds — ≤60 Redis ops/sec regardless of how fast Kalshi sends data
- **Cron disabled**: arq background cron jobs are disabled — use `POST /api/v1/dev/sync-kalshi` to manually trigger a DB refresh
- **Startup sync**: The server runs `run_kalshi_dev_sync()` on startup before accepting WebSocket connections, populating `DEV_TARGET_MARKETS` with open market tickers so the WS subscription is correct from the first connect
- **Open events only**: Dev sync fetches only `status="open"` events from Kalshi, cutting startup time from 3+ minutes to ~10 seconds while still providing all currently tradeable markets

This keeps Redis Cloud free-tier usage safely under the **100 ops/sec** hard limit.

### DEV_MODE=False (Production)

- **Global WebSocket firehose**: Subscribes to all Kalshi markets (5,000+ tickers)
- **Micro-batching**: asyncio.Queue receives all ticks non-blocking; background flusher drains in 100ms or 500-item batches via Redis pipeline (~10 round-trips/sec)
- **arq crons**: Full sync every 15min, delta reconciliation every 1min, OHLCV aggregation every 1min, event volume aggregation every 5min
- **Startup**: No sync on startup — arq handles scheduling. The web process only starts the WebSocket firehose.

## Project Structure

```
app/
  main.py                     # Entry point, registers all routers, lifespan management
  core/
    config.py                 # Loads env vars (API keys, DEV_MODE flag)
    dev_config.py             # DEV_MODE sandbox constants (TARGET_SERIES, TARGET_MARKETS)
    database.py               # SQLAlchemy engine + raw asyncpg pool
    redis.py                  # Redis async connection (arq queue + tick cache)
    market_cache.py           # Redis cache manager for real-time ticker data
    arq_worker.py             # arq WorkerSettings + cron job registration (crons disabled in DEV_MODE)
  api/routes/
    series.py                 # /series endpoints (DB queries)
    events.py                 # /events endpoints (DB queries)
    markets.py                # /markets endpoints (DB queries)
    v1/
      events.py               # /api/v1/{exchange}/events — DB + live Redis prices
      candlesticks.py         # /api/v1/kalshi/events/{ticker}/candlesticks — hybrid OHLCV
      dev.py                  # /api/v1/dev/sync-kalshi — manual sync trigger (DEV_MODE only)
  services/
    kalshi.py                 # Kalshi SDK wrapper (REST API + candlesticks)
    events.py                 # Event query logic + Redis enrichment
    candlesticks.py           # Hybrid candle aggregation (historical + live merge)
  workers/
    kalshi/
      ingest.py               # Full sync + delta sync + OHLCV aggregation + DEV_MODE sync
      stream.py               # WebSocket firehose: ticker → Redis, lifecycle → Postgres
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
Workers: Kalshi REST sync (arq cron or manual) + WebSocket firehose (asyncio task)
```

**Data flow:**
- Kalshi WS → ticker channel → Redis HSET (real-time prices) + Redis candle (live OHLCV)
- Kalshi WS → market_lifecycle_v2 → PostgreSQL (status/result changes only — no INSERTs, uses semaphore to cap Supabase connections at 3 concurrent)
- arq cron → kalshi_full_sync (15m, production only) → PostgreSQL (full backfill)
- arq cron → state_reconciliation (1m, production only) → PostgreSQL (delta sync for recently-changed markets)
- arq cron → aggregate_event_volumes (5m, production only) → PostgreSQL (rolls up `events.volume_24h` from child market metadata)
- arq cron → aggregate_ohlcv (1m, production only) → PostgreSQL (Redis tick snapshots → `market_ticks` table)
- Kalshi REST → historical candlesticks (on-demand per API request)
- PostgreSQL + Redis → FastAPI routes → iOS app

**Redis key schema:**
- Ticker data: `ticker:{exchange}:{market_ticker}-{side}` (e.g. `ticker:kalshi:KXHIGHNY-26MAR04-B52.5-yes`)
- Live candle: `candle:kalshi:{market_ticker}:{minute_ts}` (5-minute TTL, Lua script OHLCV aggregation)
- Note: the prefix is `ticker:` not `market:` — searching `market:*` finds nothing

**WebSocket modes (`workers/kalshi/stream.py`):**
```
DEV_MODE=True:
  Subscribe: filtered to DEV_TARGET_MARKETS (populated by startup sync)
  WS receive loop → _dev_enqueue_ticker() → _latest_ticks[market_id] = update (dict overwrite)
  _dev_redis_flusher() → sleep 1.5s → snapshot+clear dict → pipeline HSET+EVAL → Redis
  ~60–90 ops/sec max (N markets × 2 HSET + N candle EVAL per 1.5s cycle)

DEV_MODE=False (production):
  Subscribe: global firehose (no market_tickers filter — all 5000+ markets)
  WS receive loop → _enqueue_ticker() → asyncio.Queue(maxsize=100_000) (non-blocking, drops if full)
  _redis_flusher() → drain queue in 100ms/500-item batches → pipeline HSET
  candle EVALs fired as asyncio.create_task (fire-and-forget, dedup per market per batch)

Lifecycle events (both modes):
  market_lifecycle_v2 → asyncio.create_task(_handle_market_lifecycle())
  Semaphore(3) limits concurrent DB writes to avoid saturating Supabase free-tier connection pool
  Only UPDATEs existing markets (no INSERT — lifecycle events lack event_id FK)
```

**Ingestion strategy (`workers/kalshi/ingest.py`):**
- Uses `with_nested_markets=True` on the Kalshi events API — markets arrive attached to their parent event, eliminating a separate ~500k market pagination
- Batch upserts via PostgreSQL `UNNEST` arrays for high-throughput bulk inserts (falls back to row-by-row on failure)
- Skips multivariate event (MVE) series — tickers starting with `KXMVE` are parlay markets not useful for Alpha
- State reconciliation uses `min_updated_ts` param to fetch only recently changed markets
- Event `volume_24h` is denormalized: aggregated from `markets.platform_metadata->>'volume_24h'` by arq cron

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

### Development Endpoints (DEV_MODE=True only)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/dev/sync-kalshi` | Manually trigger restricted Kalshi sync (3 series) |

Returns 403 in production. Call this from Postman or your frontend whenever you need fresh DB data during UI development — it replaces the arq cron scheduler.

### Candlestick Endpoint Details

`GET /api/v1/kalshi/events/{event_ticker}/candlesticks`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `event_ticker` | path | yes | Kalshi event ticker (e.g. `KXNBA-26FEB26MIAPHI`) |
| `series_ticker` | query | yes | Parent series ticker (e.g. `KXNBA`) |
| `start_ts` | query | yes | Start Unix timestamp |
| `end_ts` | query | no | End Unix timestamp (defaults to now) |
| `market_ticker` | query | no | Specific market ticker to merge in the live Redis candle |

Returns 1-minute OHLCV candles with prices normalized to 0.0–1.0 ($1.00 base). Omitting `market_ticker` returns historical candles only with no live candle appended. Kalshi caps candlestick requests at 5,000 candles (~83 hours at 1-min interval) — requests exceeding this return an empty array gracefully.

**Response schema:**
```json
{
  "event_ticker": "KXNBA-26FEB26MIAPHI",
  "period_interval": 1,
  "count": 240,
  "candlesticks": [
    { "timestamp": 1740000000, "open": 0.55, "high": 0.58, "low": 0.54, "close": 0.57, "volume": 1200 }
  ]
}
```

## Hybrid Candlestick Strategy

Historical candles are fetched from Kalshi's REST API (pre-computed, no wasted compute). The live current-minute candle is aggregated in Redis from WebSocket ticks using an atomic Lua script. The endpoint merges both sources, providing a seamless real-time charting experience.

When no actual trades have occurred in a period, Kalshi returns `price.open/high/low/close = None`. The service falls back to `(yes_bid + yes_ask) / 2` midpoint in this case.

## WebSocket Authentication

Kalshi WebSocket uses **RSA-PSS** signing with `MGF1(SHA-256)` and `DIGEST_LENGTH` salt. Using PKCS1v15 padding instead returns HTTP 401. See `workers/kalshi/stream.py::_ws_auth_headers()`.

The signed message format is: `{timestamp_ms}GET/trade-api/ws/v2` (no separator). Timestamp is in milliseconds. Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`.

## Setup

1. Clone the repo
2. Create a `.env` file (see `.env.example`):
   ```
   DEV_MODE=true          # Set to true for local development
   KALSHI_API_KEY_ID=...
   KALSHI_PRIVATE_KEY=...
   DATABASE_URL=...
   REDIS_URL=...
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Run the server:
   ```bash
   python3 -m uvicorn app.main:app --reload
   ```
   In DEV_MODE, this automatically runs a restricted sync on startup.

5. (Optional) Run the arq worker — only needed in production:
   ```bash
   arq app.core.arq_worker.WorkerSettings
   ```
   In DEV_MODE, crons are disabled so the arq worker is not needed.

6. Open `http://localhost:8000/docs` for interactive API explorer

## Development Workflow (DEV_MODE=True)

1. Start the server — it auto-syncs 3 series on startup
2. Develop your frontend against the V1 endpoints
3. When you need fresh data, call `POST /api/v1/dev/sync-kalshi`
4. Real-time prices update automatically via the filtered WebSocket (every 1.5s flush)

## Near Term Goals

- [x] Kalshi API connection — RSA auth, GET endpoints working
- [x] Market data ingestion — series/events/markets syncing into PostgreSQL
- [x] Routes serve from DB — all market endpoints query PostgreSQL
- [x] Real-time price cache via WebSocket → Redis
- [x] Hybrid OHLCV candlestick endpoint (historical + live)
- [x] DEV_MODE sandbox — restricted sync + debounced WebSocket for free-tier Redis
- [ ] Supabase user model — basic auth, user table
- [ ] Sports data pipeline — injury feed
- [ ] OpenAI synthesis — plain English explanations
- [ ] Firebase push notifications
- [ ] Polymarket data ingestion
