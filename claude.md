# Alpha Backend — CLAUDE.md

> **Keep this file updated** as the codebase evolves. It should always reflect reality.

---

## gstack

Use `/browse` for all web browsing. Never use `mcp__claude-in-chrome__*` tools.
Fix: `cd ~/.claude/skills/gstack && ./setup`

---

## What Is Alpha

Prediction market intelligence app for mainstream sports bettors (iOS). Action Network meets Bloomberg Terminal for regular people trading on Kalshi and Polymarket. **Trade what you know.**

Two backend jobs:
1. **API Server** — serves typed JSON to the iOS app (markets, leaderboards, positions)
2. **Background Workers** — continuously syncs exchange data, detects material events (injuries, sharp money), generates AI explanations, fires push notifications. **Workers are the real product.**

---

## Architecture

```
iOS App (React Native) ↔ HTTPS/JSON ↔ FastAPI
  ↕ PostgreSQL (Supabase) — series/events/markets/outcomes, users, social
  ↕ Redis — arq queue, ticker:{exchange}:{asset_id} HSET, candle HSET
  ↕ External: Kalshi REST/WS, Polymarket CLOB WS + Gamma API, OpenAI, Firebase, Rotowire, OpenWeather
```

**Data flow:** Workers sync exchanges → PostgreSQL. Routes read PostgreSQL + Redis → iOS app. Routes never call external APIs for market data.

**Write separation:** Tick prices → Redis HSET. Metadata → PostgreSQL. Live candles → Redis (5-min TTL). Historical candles → Kalshi REST on-demand.

**Kalshi WS:** Disabled (`ws_task = None` in main.py). Data updated via arq crons only.

**Polymarket WS (`workers/polymarket/stream.py`):**
- Micro-batch flusher: WS → Queue(100K) → 100ms/500-item batches → Redis pipeline HSET
- Subscribe with `custom_feature_enabled=true` for market_resolved/new_market/best_bid_ask
- Messages: book, price_change, best_bid_ask, last_trade_price, tick_size_change, market_resolved, new_market
- Resolution Path A: WS market_resolved → direct DB update (no Gamma roundtrip)
- Resolution Path B: last_trade_price ≥0.99/≤0.01 → Gamma API confirm → DB update
- Both paths: Semaphore(3) caps concurrent DB connections

---

## Tech Stack

FastAPI + Uvicorn | SQLAlchemy 2.0 async (routes) + asyncpg pool (workers) | Supabase PostgreSQL | Redis + arq | `kalshi-python-async` SDK | Polymarket CLOB WS (`websockets`) + Gamma API (`httpx`) | OpenAI GPT-4o | Firebase FCM | Rotowire → Sportradar | OpenWeather | Railway

---

## Folder Structure

```
app/
├── main.py                    # Lifespan: pools, Polymarket WS. Kalshi WS disabled.
├── api/routes/
│   ├── markets.py, series.py, events.py, categories.py  # DB queries
│   ├── sports.py, traders.py, notifications.py          # Planned
│   └── v1/
│       ├── events.py          # /api/v1/{exchange}/events — live prices merged
│       ├── candlesticks.py    # Hybrid OHLCV (Kalshi historical + Redis live)
│       ├── dev.py             # Manual sync (DEV_MODE only, 403 in prod)
│       ├── users.py           # Auth, profiles, follows, unread
│       ├── groups.py          # Group CRUD, membership, invite links, discovery
│       ├── messages.py        # Group messages — send/list/delete
│       ├── search.py          # Semantic search (pgvector + optional RAG)
│       ├── feed.py            # Personalized intelligence feed
│       └── intelligence_stream.py  # SSE real-time feed via Redis Pub/Sub
├── models/
│   ├── db.py                  # SQLAlchemy ORM (all tables). ENUMs: create_type=False
│   ├── kalshi.py              # Pydantic — Kalshi API. Prices in cents (0-100), normalize to 0.0-1.0
│   └── polymarket.py          # Pydantic — Gamma API. Prices already 0.0-1.0
├── services/
│   ├── kalshi.py              # SDK wrapper (REST + candlesticks)
│   ├── events.py              # Event queries + Redis enrichment
│   ├── candlesticks.py        # Historical + live merge
│   ├── auth.py                # Supabase JWT (JWKS RS256 + HS256 fallback) + get_current_user
│   ├── groups.py              # Group/membership/message logic + unread fan-out
│   ├── search.py              # Semantic search across markets/events/intelligence
│   ├── feed.py                # Ranked feed: exposure + credibility + recency
│   ├── notifications.py       # Firebase FCM push + intelligence alert dispatch
│   ├── nlp_client.py          # Singleton async OpenAI client
│   ├── nlp_entities.py        # Tiered NER: local keyword match → GPT-4o fallback
│   ├── nlp_embeddings.py      # text-embedding-3-large (768 dims)
│   └── nlp_rag.py             # Optional RAG synthesis for search
├── workers/
│   ├── kalshi/ingest.py       # Full sync + delta + OHLCV aggregation (prod crons)
│   ├── kalshi/stream.py       # WS firehose (DISABLED)
│   ├── polymarket/ingest.py   # Gamma sync, dev_sync, state_reconciliation (2min cron)
│   ├── polymarket/stream.py   # CLOB WS → Redis + resolution detection
│   ├── taxonomy.py            # upsert_platform_tag(), slugify()
│   ├── group_alerts.py        # price_alerts (5m) + resolution_alerts (2m)
│   └── intelligence/
│       ├── base.py            # content_hash, redis dedup, bulk_insert_intelligence
│       ├── news.py            # NewsAPI ingestion (configurable 2-5min cron)
│       ├── sports.py          # Sportradar ingestion (2min) + fast-path notifications
│       ├── nlp_worker.py      # Tiered NER + embeddings + unified matching engine
│       ├── credibility.py     # Hourly source accuracy evaluation
│       ├── embedding_backfill.py  # Backfill market/event embeddings (10min)
│       ├── notification_dispatch.py  # FCM push for high-impact alerts
│       ├── group_integration.py  # System messages + article intelligence
│       └── stream.py          # Abstract IntelligenceStream base (ready for future feeds)
├── core/
│   ├── config.py, dev_config.py, database.py, redis.py
│   ├── arq_worker.py          # DEV: polymarket reconciliation only. PROD: full suite
│   └── limiter.py             # Rate limiting
migrations/versions/
├── 0001_complete_schema.py    # Core schema (idempotent)
├── 0002_social_layer.py       # Social tables + user profile columns
└── 0003_intelligence_layer.py # Intelligence: pgvector, embeddings(768), mapping, credibility
```

---

## Database Schema

```
series (exchange, ext_id) → events (series_ids ARRAY[UUID]) → markets (event_id) → market_outcomes (execution_asset_id)
```

Key patterns:
- `ext_id` = native exchange ID. `exchange` enum on every row. `platform_metadata JSONB` for exchange-specific fields.
- Soft deletes only (`is_deleted`). Never hard-delete.
- **Array taxonomy**: categories/tags are `ARRAY(text)` slugs with GIN indexes. Filter via `@>`.
- **PlatformTag**: unified taxonomy — `parent_ids ARRAY(text)` (GIN), slug/label/force_hide/platform_metadata JSONB
- **SportsMetadata**: `(exchange, sport)` composite PK, `tag_ids ARRAY(text)` GIN
- **Social tables**: user_follows, groups (access_type, invite_code, member_count), group_memberships (role enum, last_read_at), group_messages (type: text|image|trade|market|article|profile|system, metadata JSONB, reply_to_id)
- **Intelligence tables**: external_intelligence (source_domain enum, embedding VECTOR(768), nlp_status, content_hash dedup), intelligence_market_mapping (event_id required, market_id optional, confidence_score from unified matching, price_at_publish snapshot, matched_tags), source_credibility (per-source accuracy tracking)
- **Embeddings**: VECTOR(768) on external_intelligence, events, markets — HNSW indexes for cosine similarity
- **Matching engine**: Unified confidence = (tag_score × 0.5) + (cosine_similarity × 0.5). Tags-only: ×0.7 cap. Embedding-only: ×0.6 cap. Threshold: 0.3
- Numeric precision: money = `NUMERIC(24,8)` + `Decimal`. Prices = `NUMERIC(10,6)`. Never `float` for money.

**Redis keys:**
- `ticker:{exchange}:{execution_asset_id}` — HSET: price, bid, bid_size, ask, ask_size, volume_24h, ts
- `candle:kalshi:{ticker}:{minute_ts}` — live OHLCV, 5-min TTL, basis points (0-10000)
- `intel:feed:global` — Pub/Sub channel for real-time SSE feed
- `intel_tags_cache` — cached PlatformTag slugs for NER (5-min TTL)
- Prefix is `ticker:` not `market:`

---

## Conventions

**Routes:** Thin — validate, call service, return typed Pydantic response. Use `Depends(get_db)`. Filter `is_deleted=False`. Return 404 for missing resources. V1 routes in `app/api/routes/v1/`.

**Services:** API wrappers (auth, retries, no logic) vs domain services (queries, enrichment, merging). External calls only in `app/services/`.

**Workers:** Async via arq crons (separate process). Receive `ctx` with `asyncpg_pool` + `redis`. Wrap in try/except. Sync order: series → events → markets → outcomes. Batch upserts via UNNEST. MVE tickers (`KXMVE*`) skipped.

**Models:** One exchange = one Pydantic file. Never put exchange types in `db.py`. Use `ConfigDict(extra='ignore')`. Kalshi prices: cents → normalize. Polymarket prices: already normalized.

**Upserts:** Always `insert().on_conflict_do_update()`. Never check-then-insert.

**Candlesticks:** Historical from Kalshi REST (5000 limit). Live from Redis Lua script. Merged in `services/candlesticks.py`. None prices → bid/ask midpoint fallback.

---

## DEV_MODE

**DEV_MODE=True:** Kalshi disabled. Polymarket dev sync as background task (200 series → 20 diverse picks, 4 events/series). arq: only polymarket reconciliation (2min). Dev endpoint: `POST /api/v1/dev/sync/{exchange}`.

**DEV_MODE=False:** Full arq suite. Polymarket WS loads all DB tokens. Dev endpoint returns 403.

---

## Design Principles

1. **DB is source of truth.** Routes read PostgreSQL. Workers keep it synced.
2. **Never touch user funds.** Alpha triggers API calls on user-owned exchange accounts.
3. **Never pool funds for groups.** Individual accounts, social coordination only.
4. **Honest notifications.** Known cause → state confidently. Unknown → flag uncertainty. Never fabricate.
5. **Regulatory:** Not a money transmitter, not an investment advisor, not pooling funds. Financial media only.

---

## Environment Variables

See `.env.example` for full list. Key ones: `DATABASE_URL`, `REDIS_URL`, `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY`, `POLYMARKET_API_KEY`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `OPENAI_API_KEY`, `DEV_MODE`, `ENVIRONMENT`, `INTELLIGENCE_ENABLED`, `NEWSAPI_KEY`, `SPORTRADAR_API_KEY`, `SPORTRADAR_TIER`, `FCM_ENABLED`, `FIREBASE_CREDENTIALS_JSON`, `NEWSAPI_POLL_INTERVAL_SECONDS`.

---

## Build Priority

1. ✅ Kalshi API + market ingestion + DB routes + OHLCV candlesticks + DEV_MODE sandbox
2. ✅ Supabase auth + social layer (profiles, follows, groups, chat, alerts, migration 0002)
3. ✅ Polymarket ingestion (CLOB WS, Gamma sync, taxonomy, resolution detection)
4. ✅ Group betting social layer (squads, channels, invite links, 7 message types, alerts)
5. ✅ Intelligence engine: news/sports ingestion, tiered NER, unified matching (tags+embeddings), SSE real-time feed, credibility tracking, push notifications
6. Full API ready for iOS

**Ship in order. Do not skip ahead.**

---

## Keeping Updated

New file → update Folder Structure. New env var → update Environment Variables. Schema change → update Database Schema. Milestone done → mark ✅ in Build Priority.
