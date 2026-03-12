"""
Development sandbox configuration.

When DEV_MODE=True the backend restricts ingestion and WebSocket
subscriptions to an explicit curated set of Kalshi series tickers.

Series are organized by category to test different market behaviors:
- Politics: cascading deadlines, threshold UIs, early settlement logic
- Economics: categorical grouping, scalar ranges, directional floor logic
- Tech/Corporate: async external settlement, media-heavy categorical markets
- Climate/Mentions: cumulative tallying, long-duration epochs, DB boundary logic

DEV_TARGET_SERIES and DEV_TARGET_MARKETS are populated dynamically
at startup by run_kalshi_dev_sync(). The WebSocket worker reads
DEV_TARGET_MARKETS to build a filtered subscription.

Now running on Railway Pro — no ops/sec constraint. Flush interval
is reduced to 0.5s for responsive live data updates.
"""

from typing import Dict, List

# Explicit series tickers to track in DEV_MODE, organized by category.
# These are fixed tickers chosen to test specific UI/data edge cases.
DEV_EXPLICIT_SERIES: Dict[str, List[str]] = {
    "Politics": [
        "KXNEXTIRANLEADER",    # Tests early settlement logic
        "KXDHSFUND",            # Tests cascading rolling deadlines within an event
        "KXLOSEREELECTIONGOV", # Tests quantitative integer threshold UIs
    ],
    "Economics": [
        "KXFEDDECISION",        # Tests standard categorical grouping
        "KXINXY",               # Tests scalar price ranges and ladder arrays
        "KXISMPMI",             # Tests directional "At least X" floor logic
    ],
    "Tech/Corporate": [
        "KXIPO",                # Tests unpredictable asynchronous external settlement sources
        "KXGAMEAWARDS",         # Tests heavy categorical media and image ingestion
    ],
    "Climate/Mentions": [
        "KXNYTHEAD",            # Tests cumulative directional tallying arrays
        "KXHMONTH",             # Tests long-duration monthly polling epochs
        "KXWARMING",            # Tests PostgreSQL boundary logic for decadal 2050 timestamps
    ],
}

# Flat list of all explicit series tickers (for convenience)
DEV_EXPLICIT_SERIES_FLAT: List[str] = [
    ticker
    for tickers in DEV_EXPLICIT_SERIES.values()
    for ticker in tickers
]

# Redis flush interval in seconds for dev mode.
# Railway Pro removes the free-tier 100 ops/sec constraint — 0.5s for
# responsive live data without overwhelming the event loop.
DEV_REDIS_FLUSH_INTERVAL: float = 0.5

# Populated dynamically at startup by run_kalshi_dev_sync().
# Mutable lists — workers write to them via .clear() + .extend().
DEV_TARGET_SERIES: List[str] = []
DEV_TARGET_MARKETS: List[str] = []

# ---------------------------------------------------------------------------
# Polymarket DEV_MODE targets
# ---------------------------------------------------------------------------
# Curated Polymarket SERIES slugs to sync in DEV_MODE.
# These are fetched from the Gamma API /series?slug={slug} endpoint.
# Each series contains multiple events (recurring polls / milestones).
#
# Chosen to cover multi-event recurring series across Politics, Economics, and Crypto:
#   Politics:  US government / elections series (ongoing, multi-event)
#   Economics: Federal Reserve rate decisions (recurring monthly events)
#   Crypto:    Bitcoin / Ethereum price series (high-volume, always-active)
#
# Verified against live Gamma API (GET https://gamma-api.polymarket.com/series?active=true).
# Chosen to cover different market types and categories:
#   Politics:    Trump approval ratings (binary, recurring)
#   Economics:   US annual inflation + ECB interest rates (recurring monthly)
#   Crypto:      Solana monthly price (multi-strike, scalar ranges)
#   Culture:     Billboard #1 song (categorical, multiple-choice)
POLYMARKET_DEV_SERIES_SLUGS: List[str] = [
    # Politics — binary approval rating (2–4 events, clean binary market)
    "trump-approval-positve",
    "trump-negative-approval",
    # Economics — recurring monthly data series
    "us-annual-inflation",
    "unemployment",
    # Crypto — price range markets
    "solana-hit-price-monthly",
]

# Populated dynamically at startup by run_polymarket_dev_sync().
# Contains the ERC-1155 token IDs collected from the Gamma API ingest.
# The CLOB WebSocket stream uses these to subscribe to live price feeds.
POLYMARKET_DEV_TOKEN_IDS: List[str] = []

# Polymarket event ext_ids (Gamma API event IDs) — used for trending ZSET seeding.
POLYMARKET_DEV_EVENT_IDS: List[str] = []
