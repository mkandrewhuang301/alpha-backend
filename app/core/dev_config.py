"""
Development sandbox configuration.

When DEV_MODE=True the backend restricts ingestion and WebSocket
subscriptions to a small subset of markets. This keeps Redis Cloud
free-tier ops/sec consumption safely under the 100 ops/sec hard limit.

Three highly active series are tracked across different categories:
    - KXHIGHNY     : Daily High Temp in NYC (Weather)
    - KXCPI        : US CPI Inflation (Economics)
    - KXNBAPLAYOFF : NBA Playoff Qualifiers (Sports)

DEV_TARGET_MARKETS is populated dynamically at startup by run_kalshi_dev_sync(). 
The WebSocket worker reads this list to build a filtered subscription.
"""

from typing import List

# Series tickers to track in DEV_MODE
DEV_TARGET_SERIES: List[str] = [
    "KXHIGHNY",      # Daily High Temp in NYC — Weather
    "KXCPI",         # US CPI Inflation — Economics
    "KXNBAPLAYOFF",  # NBA Playoff Qualifiers — Sports
]

# Populated dynamically at startup by run_kalshi_dev_sync().
# Mutable list — workers write to it via .clear() + .extend().
DEV_TARGET_MARKETS: List[str] = []
