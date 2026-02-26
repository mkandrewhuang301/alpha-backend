# Alpha Backend — CLAUDE.md

## What Is Alpha

Alpha is a consumer iOS prediction market intelligence app for mainstream sports bettors. Think Action Network meets eToro meets Bloomberg Terminal — but for regular people who just want to feel informed enough to trade confidently on Kalshi and Polymarket.

The target user is NOT a quant trader. NOT a crypto native. It's the 25 year old who bets on DraftKings every Sunday, just heard prediction markets exist, and has no idea why lines move or what smart money is doing.

Tagline: **Trade what you know.**

---

## What This Backend Does

This backend is the engine powering the Alpha iOS app. It has two distinct jobs:

**1. API Server** — responds to requests from the iOS app. User opens app, app asks for today's games, sharp trader leaderboard, user positions, etc. Server responds with clean formatted data.

**2. Background Workers** — runs continuously regardless of user activity. Polls sports data APIs every few minutes. Monitors Kalshi and Polymarket for unusual activity. When something material happens — injury report drops, lineup released, sharp money moves — workers generate AI explanations and fire push notifications to relevant users immediately.

The workers are more important than the API server for Alpha's core value proposition.

---

## Core Features This Backend Powers

### 1. Initial Research Layer
- Head to head stats, win rates, home/away splits, cover analysis
- Weather data for outdoor games
- NBA referee assignments and foul rate analytics
- NFL practice reports (Wednesday/Thursday/Friday cadence)
- News from sources such as twitter for market sentiment
- News from relevant sources for economics, govnernment, culture,climate, finance
- Starting lineup releases (especially NBA — drops 30 min before tip)
- AI synthesis: takes raw stats and generates 3 - 6 sentence plain English summary per market

### 2. Sharp Trader Intelligence
- Verified trader profiles from Kalshi and Polymarket public onchain data
- Win rate, ROI, sport specialties, recent positions per trader
- Leaderboard of top performers by category with full track record history from polymarket wallet history
- Whale watching — large position detection on Polymarket on-chain data
- Cross-platform intelligence: Polymarket sharp money as signal for Kalshi markets

### 3. Position Protection
- Monitors all user open positions continuously
- Detects material changes: injury news, lineup scratches, sharp money spikes, weather changes
- Generates plain English explanation of what changed and what it means for the specific position
- Calculates real-time P&L impact
- Fires targeted push notification only to users with affected positions, or users that follow market
- Two-tier notification honesty:
  - When cause is known (injury, lineup): state it confidently with source
  - When cause is unknown (silent sharp money): flag unusual activity without fabricating reason

### 4. Group Betting Social Layer (save for later)
- Group creation and membership management
- Trade copy: member puts in trade, can share to group, if shared other members can view and copy. private trades are hidden
- Simultaneous individual trade execution — each user trades their OWN Kalshi account
- NEVER pool funds (CPO regulatory issue) — always individual accounts, social coordination only
- Shared P&L tracking per group position

### 5. In-App Execution
- Connects to Kalshi via user's own API key / OAuth
- Executes trades on user's individual Kalshi account
- Never touches user funds — Kalshi holds everything
- Alpha triggers the API call, Kalshi does the rest

---

## Architecture

```
iOS App (React Native)
      ↕ HTTP
FastAPI Backend (this repo)
      ↕
┌─────────────────────────────────────┐
│ External APIs                        │
│ - Kalshi API (markets, execution)    │
│ - Polymarket API (on-chain data)     │
│ - Sportradar / Rotowire (sports)     │
│ - OpenAI GPT-4o (AI synthesis)       │
│ - OpenWeather (weather)              │
│ - Firebase (push notifications)      │
└─────────────────────────────────────┘
      ↕
┌─────────────────┐
│ Supabase         │
│ PostgreSQL       │
│ Redis (cache)    │
└─────────────────┘
```

---

## Tech Stack

- **Framework**: FastAPI (Python)
- **Server**: Uvicorn
- **Database**: Supabase (PostgreSQL + Redis for real-time caching)
- **HTTP Client**: httpx (async)
- **Background Jobs**: APScheduler
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
│   ├── main.py                    # FastAPI entry point, registers all routers
│   ├── api/
│   │   └── routes/
│   │       ├── markets.py         # Kalshi market data endpoints
│   │       ├── sports.py          # Injuries, lineups, stats, weather, refs
│   │       ├── traders.py         # Sharp trader leaderboard, profiles
│   │       ├── users.py           # Auth, positions, portfolio
│   │       ├── groups.py          # Group betting social layer
│   │       └── notifications.py   # Notification history and preferences
│   ├── services/
│   │   ├── kalshi.py              # Kalshi API wrapper
│   │   ├── polymarket.py          # Polymarket on-chain data wrapper
│   │   ├── sportradar.py          # Sports data wrapper
│   │   ├── openai.py              # AI synthesis service
│   │   ├── firebase.py            # Push notification service
│   │   └── weather.py             # Weather API wrapper
│   ├── workers/
│   │   ├── injury_monitor.py      # Polls injury reports every 5 min
│   │   ├── lineup_monitor.py      # Watches lineup releases
│   │   ├── market_monitor.py      # Watches line movement and volume spikes
│   │   └── whale_monitor.py       # Polymarket large position detection
│   └── core/
│       ├── config.py              # Loads all env variables
│       ├── database.py            # Supabase connection
│       └── scheduler.py           # Starts all background workers
├── tests/
├── .env                           # Real keys — NEVER commit
├── .env.example                   # Template — commit this
├── .gitignore
├── requirements.txt
├── Dockerfile
└── README.md
```

---

## Key Design Principles

**Services are reusable, routes are specific.**
- `services/kalshi.py` knows HOW to talk to Kalshi. Nothing else.
- `routes/markets.py` knows WHAT URLs exist. Delegates to services.
- `main.py` just assembles everything together.

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
- Honest uncertainty: "Unusual movement on this market. mentions market line dropped 20%.  No public news yet. Could be insider info."

---

## Regulatory Notes

- Never act as money transmitter — never hold user funds
- Never act as investment advisor — surface information, never recommend
- Never pool funds for group betting — individual accounts only
- Research layer and notifications are financial media, not financial advice
---

## Environment Variables Required

```
KALSHI_API_KEY=
KALSHI_BASE_URL=https://trading-api.kalshi.com/trade-api/v2
POLYMARKET_API_KEY=
SPORTRADAR_API_KEY=
ROTOWIRE_API_KEY=
OPENAI_API_KEY=
OPENWEATHER_API_KEY=
SUPABASE_URL=
SUPABASE_KEY=
FIREBASE_CREDENTIALS_PATH=
ENVIRONMENT=development
```

---

## Build Priority Order

1. Kalshi API connection — pull live market data, one working endpoint
2. Supabase connected — user model, basic auth
3. Sports data pipeline — Rotowire injury feed flowing
4. Background worker — injury detection running every 5 minutes
5. OpenAI synthesis — plain English explanations generating
6. Firebase notifications — push firing on real injury events
7. Polymarket whale watching — sharp trader scoring
8. Group betting social layer — simultaneous individual trade coordination
9. Full API ready for iOS consumption

**Ship in this order. Do not skip ahead.**

-
**Build accordingly.**