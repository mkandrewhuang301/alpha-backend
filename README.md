# Alpha Backend

FastAPI backend that integrates with the [Kalshi](https://kalshi.com) prediction markets API.

## What It Does

Exposes a REST API that proxies and organizes Kalshi market data. Kalshi is a regulated prediction market platform where users trade yes/no contracts on real-world events (sports, politics, crypto, etc.).

## Project Structure

```
app/
  main.py               # Entry point, registers all routers
  core/
    config.py           # Loads env vars (API keys, base URL)
  api/routes/
    markets.py          # /markets endpoints
    series.py           # /series endpoints
  services/
    kalshi.py           # All Kalshi API calls + RSA auth logic
```

## How Kalshi Data Is Organized

```
Category  (Sports, Politics, Crypto...)
  └── Series    (recurring template — e.g. "NBA Points Props")
        └── Event     (specific instance — e.g. "Duke vs UNC Feb 26")
              └── Market    (specific yes/no bet — e.g. "Will Flagg score 20+?")
```

## Current Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/markets` | List markets. Supports `status`, `series_ticker`, `event_ticker`, `limit` filters |
| GET | `/markets/{ticker}` | Get a specific market by its ticker |
| GET | `/series` | List all series. Supports `category` filter (e.g. Sports, Politics) |

### Example Calls

```bash
# All open NBA markets
GET /markets?series_ticker=KXNBA&status=open

# Limit results
GET /markets?limit=10

# All sports series
GET /series?category=Sports

# Specific market
GET /markets/KXNBA3PT-26FEB26MIAPHI-MIANPOWELL24-1
```

## Auth

Kalshi uses RSA key-based authentication. Each request is signed with a private key and includes:
- `KALSHI-ACCESS-KEY` — your API key ID
- `KALSHI-ACCESS-TIMESTAMP` — current timestamp in ms
- `KALSHI-ACCESS-SIGNATURE` — base64-encoded RSA signature of `timestamp + method + path`

## Setup

1. Clone the repo
2. Create a `.env` file (see `.env.example`)
3. Install dependencies:
   ```bash
   pip install fastapi httpx uvicorn python-dotenv cryptography
   ```
4. Run the server:
   ```bash
   python -m uvicorn app.main:app --reload
   ```
5. Open `http://localhost:8000/docs` for interactive API explorer

## Near Term Goals

- [ ] Add `/events` endpoint to browse events within a series
- [ ] Add pagination support (cursor-based) to all list endpoints
- [ ] Integrate OpenAI to analyze and summarize market data
- [ ] Add user-facing endpoints for placing/managing trades
- [ ] Add caching layer to reduce Kalshi API calls
- [ ] Deploy to cloud (Railway / Render / AWS)
