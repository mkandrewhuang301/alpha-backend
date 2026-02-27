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
    events.py           # /events endpoints
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

### Markets

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/markets` | List markets |
| GET | `/markets/{ticker}` | Get a specific market by ticker |

**`GET /markets` Query Parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `status` | string | Filter by market status |
| `series_ticker` | string | Filter by series ticker |
| `event_ticker` | string | Filter by event ticker |
| `limit` | int | Max number of results |
| `cursor` | string | Pagination cursor from previous response |

**`GET /markets/{ticker}` Path Parameters:**
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `ticker` | string | ✅ | Market ticker |

### Series

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/series` | List all series |
| GET | `/series/{series_ticker}` | Get a specific series by ticker |

**`GET /series` Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `category` | string | — | Filter by category (e.g. Sports, Politics) |
| `tags` | string | — | Filter by tags |
| `include_product_metadata` | bool | `false` | Include product metadata in response |
| `include_volume` | bool | `false` | Include total volume traded across all events in each series |

**`GET /series/{series_ticker}` Path & Query Parameters:**
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `series_ticker` | string | ✅ | — | The ticker of the series to retrieve |
| `include_volume` | bool | — | `false` | Include total volume traded across all events |

### Events

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/events` | List events |
| GET | `/events/{event_ticker}` | Get a specific event by ticker |

**`GET /events` Query Parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | `200` | Results per page (1–200) |
| `cursor` | string | — | Pagination cursor from previous response |
| `with_nested_markets` | bool | `false` | Include nested market objects in each event |
| `with_milestones` | bool | `false` | Include related milestones alongside events |
| `status` | string | — | Filter by status: `open`, `closed`, or `settled` |
| `series_ticker` | string | — | Filter by series ticker |

**`GET /events/{event_ticker}` Path & Query Parameters:**
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `event_ticker` | string | ✅ | — | Event ticker |
| `with_nested_markets` | bool | — | `false` | Include nested market objects within the event |

### Example Calls

```bash
# All open NBA markets
GET /markets?series_ticker=KXNBA&status=open

# Paginate markets
GET /markets?limit=10&cursor=abc123

# Specific market by ticker
GET /markets/KXNBA3PT-26FEB26MIAPHI-MIANPOWELL24-1

# All sports series
GET /series?category=Sports

# Series with volume info
GET /series?category=Sports&include_volume=true

# Specific series
GET /series/KXNBA

# List open events for a series with nested markets
GET /events?series_ticker=KXNBA&status=open&with_nested_markets=true

# Paginate events (use cursor from previous response)
GET /events?limit=50&cursor=abc123

# Specific event with its markets
GET /events/KXNBA-26FEB26MIAPHI?with_nested_markets=true
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

- [x] Add `/events` endpoint to browse events within a series
- [x] Add `/series/{ticker}` and `/events/{ticker}` individual lookup endpoints
- [x] Add pagination support (cursor-based) to markets and events list endpoints
- [ ] Integrate OpenAI to analyze and summarize market data
- [ ] Add user-facing endpoints for placing/managing trades
- [ ] Add caching layer to reduce Kalshi API calls
- [ ] Deploy to cloud (Railway / Render / AWS)
