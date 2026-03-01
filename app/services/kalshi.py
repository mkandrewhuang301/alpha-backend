"""
Kalshi API service — thin wrapper around kalshi-python-async SDK.

The SDK handles RSA-PSS authentication, retries, and pagination internally.
This module initialises the SDK client once and exposes async helper functions
matching the signatures that workers and routes already call.
"""

import logging
from typing import Any

from kalshi_python_async import Configuration, KalshiClient

from app.core.config import KALSHI_API_KEY, KALSHI_PRIVATE_KEY, KALSHI_BASE_API_URL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SDK client singleton
# ---------------------------------------------------------------------------

_client: KalshiClient | None = None


def _get_client() -> KalshiClient:
    global _client
    if _client is None:
        config = Configuration(host=KALSHI_BASE_API_URL)
        config.api_key_id = KALSHI_API_KEY
        config.private_key_pem = KALSHI_PRIVATE_KEY
        _client = KalshiClient(config)
    return _client


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------

async def get_series_list(
    category: str = None,
    tags: str = None,
    include_product_metadata: bool = False,
    include_volume: bool = False,
) -> dict[str, Any]:
    """Fetch all Kalshi series. Returns dict with 'series' key."""
    try:
        client = _get_client()
        resp = await client.series_api.get_series()
        return {"series": [s.to_dict() if hasattr(s, "to_dict") else s for s in (resp.series or [])]}
    except Exception as exc:
        logger.error("[kalshi] Failed to fetch series: %s", exc)
        return {"error": True, "status_code": 500, "detail": str(exc)}


async def get_series(series_ticker: str, include_volume: bool = False) -> dict[str, Any]:
    """Fetch a single series by ticker."""
    try:
        client = _get_client()
        resp = await client.series_api.get_series_by_ticker(series_ticker)
        data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return data
    except Exception as exc:
        logger.error("[kalshi] Failed to fetch series %s: %s", series_ticker, exc)
        return {"error": True, "status_code": 500, "detail": str(exc)}


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

async def get_events(
    limit: int = 200,
    cursor: str = None,
    with_nested_markets: bool = False,
    with_milestones: bool = False,
    status: str = None,
    series_ticker: str = None,
) -> dict[str, Any]:
    """Fetch paginated events. Returns dict with 'events' and 'cursor' keys."""
    try:
        client = _get_client()
        kwargs: dict[str, Any] = {"limit": limit}
        if cursor:
            kwargs["cursor"] = cursor
        if with_nested_markets:
            kwargs["with_nested_markets"] = True
        if with_milestones:
            kwargs["with_milestones"] = True
        if status:
            kwargs["status"] = status
        if series_ticker:
            kwargs["series_ticker"] = series_ticker

        resp = await client.events_api.get_events(**kwargs)
        events = [e.to_dict() if hasattr(e, "to_dict") else e for e in (resp.events or [])]
        return {"events": events, "cursor": getattr(resp, "cursor", None)}
    except Exception as exc:
        logger.error("[kalshi] Failed to fetch events: %s", exc)
        return {"error": True, "status_code": 500, "detail": str(exc)}


async def get_event(event_ticker: str, with_nested_markets: bool = False) -> dict[str, Any]:
    """Fetch a single event by ticker."""
    try:
        client = _get_client()
        kwargs: dict[str, Any] = {}
        if with_nested_markets:
            kwargs["with_nested_markets"] = True
        resp = await client.events_api.get_event(event_ticker, **kwargs)
        data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return data
    except Exception as exc:
        logger.error("[kalshi] Failed to fetch event %s: %s", event_ticker, exc)
        return {"error": True, "status_code": 500, "detail": str(exc)}


# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------

async def get_markets(
    status: str = None,
    series_ticker: str = None,
    event_ticker: str = None,
    limit: int = None,
    cursor: str = None,
    min_updated_ts: int = None,
) -> dict[str, Any]:
    """Fetch paginated markets. Returns dict with 'markets' and 'cursor' keys."""
    try:
        client = _get_client()
        kwargs: dict[str, Any] = {}
        if status:
            kwargs["status"] = status
        if series_ticker:
            kwargs["series_ticker"] = series_ticker
        if event_ticker:
            kwargs["event_ticker"] = event_ticker
        if limit:
            kwargs["limit"] = limit
        if cursor:
            kwargs["cursor"] = cursor
        if min_updated_ts is not None:
            kwargs["min_close_ts"] = min_updated_ts

        resp = await client.markets_api.get_markets(**kwargs)
        markets = [m.to_dict() if hasattr(m, "to_dict") else m for m in (resp.markets or [])]
        return {"markets": markets, "cursor": getattr(resp, "cursor", None)}
    except Exception as exc:
        logger.error("[kalshi] Failed to fetch markets: %s", exc)
        return {"error": True, "status_code": 500, "detail": str(exc)}


async def get_market(ticker: str) -> dict[str, Any]:
    """Fetch a single market by ticker."""
    try:
        client = _get_client()
        resp = await client
        data = resp.to_dict() if hasattr(resp, "to_dict") else resp
        return data
    except Exception as exc:
        logger.error("[kalshi] Failed to fetch market %s: %s", ticker, exc)
        return {"error": True, "status_code": 500, "detail": str(exc)}
