"""
Kalshi API service — thin wrapper around kalshi-python-async SDK.

The SDK handles RSA-PSS authentication, retries, and pagination internally.
This module initialises the SDK client once and exposes async helper functions
matching the signatures that workers and routes already call.
"""

import json
import logging

from kalshi_python_async import Configuration, KalshiClient
from kalshi_python_async.api.market_api import MarketApi
from kalshi_python_async.api.events_api import EventsApi
from kalshi_python_async.models import (
    GetSeriesResponse,
    GetEventsResponse,
    GetEventResponse,
    GetMarketsResponse,
    GetMarketResponse,
)

from app.core.config import KALSHI_API_KEY, KALSHI_PRIVATE_KEY, KALSHI_BASE_API_URL
from app.models.kalshi import KalshiSeriesListResponse

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


def _get_market_api() -> MarketApi:
    return MarketApi(_get_client())

def _get_events_api() -> EventsApi:
    return EventsApi(_get_client())

# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------

async def get_series_list(
    category: str | None = None,
    tags: str | None = None,
    include_product_metadata: bool = False,
    include_volume: bool = False,
    min_updated_ts: int | None = None,
) -> KalshiSeriesListResponse:
    """Fetch all Kalshi series.

    Bypasses the SDK's own deserialization because the SDK's Series model
    rejects tags=null from the API. We use our KalshiSeries model instead,
    which handles Optional[List[str]] correctly.
    """
    try:
        market_api = _get_market_api()
        # Use the SDK's serialization for params + auth, but get raw response
        _param = market_api._get_series_list_serialize(
            category=category,
            tags=tags,
            include_product_metadata=include_product_metadata,
            include_volume=include_volume,
            min_updated_ts=min_updated_ts,
            _request_auth=None,
            _content_type=None,
            _headers=None,
            _host_index=0,
        )
        response_data = await market_api.api_client.call_api(*_param)
        await response_data.read()
        raw = json.loads(response_data.data)
        return KalshiSeriesListResponse(**raw)
    except Exception as exc:
        logger.error("[kalshi] Failed to fetch series: %s", exc)
        raise


async def get_series(
    series_ticker: str,
    include_volume: bool = False,
) -> GetSeriesResponse:
    """Fetch a single series by ticker."""
    try:
        market_api = _get_market_api()
        resp: GetSeriesResponse = await market_api.get_series(
            series_ticker=series_ticker,
            include_volume=include_volume,
        )
        return resp
    except Exception as exc:
        logger.error("[kalshi] Failed to fetch series %s: %s", series_ticker, exc)
        raise


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

async def get_events(
    limit: int = 200,
    cursor: str | None = None,
    with_nested_markets: bool = False,
    with_milestones: bool = False,
    status: str | None = None,
    series_ticker: str | None = None,
) -> GetEventsResponse:
    """Fetch paginated events."""
    try:
        events_api = _get_events_api()
        resp: GetEventsResponse = await events_api.get_events(
            limit=limit,
            cursor=cursor,
            with_nested_markets=with_nested_markets,
            with_milestones=with_milestones,
            status=status,
            series_ticker=series_ticker,
        )
        return resp
    except Exception as exc:
        logger.error("[kalshi] Failed to fetch events: %s", exc)
        raise


async def get_event(
    event_ticker: str,
    with_nested_markets: bool = False,
) -> GetEventResponse:
    """Fetch a single event by ticker."""
    try:
        events_api = _get_events_api()
        resp: GetEventResponse = await events_api.get_event(
            event_ticker=event_ticker,
            with_nested_markets=with_nested_markets,
        )
        return resp
    except Exception as exc:
        logger.error("[kalshi] Failed to fetch event %s: %s", event_ticker, exc)
        raise


# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------

async def get_markets(
    status: str | None = None,
    series_ticker: str | None = None,
    event_ticker: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    min_close_ts: int | None = None,
    min_updated_ts: int | None = None,
) -> GetMarketsResponse:
    """Fetch paginated markets."""
    try:
        market_api = _get_market_api()
        resp: GetMarketsResponse = await market_api.get_markets(
            status=status,
            series_ticker=series_ticker,
            event_ticker=event_ticker,
            limit=limit,
            cursor=cursor,
            min_close_ts=min_close_ts,
            min_updated_ts=min_updated_ts,
        )
        return resp
    except Exception as exc:
        logger.error("[kalshi] Failed to fetch markets: %s", exc)
        raise


async def get_market(ticker: str) -> GetMarketResponse:
    """Fetch a single market by ticker."""
    try:
        market_api = _get_market_api()
        resp: GetMarketResponse = await market_api.get_market(ticker=ticker)
        return resp
    except Exception as exc:
        logger.error("[kalshi] Failed to fetch market %s: %s", ticker, exc)
        raise
