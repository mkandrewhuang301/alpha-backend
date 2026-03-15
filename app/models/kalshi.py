"""
Pydantic models for Kalshi REST API v2 response shapes.

Usage:
    - Workers parse raw Kalshi API dicts into these models before writing to DB.
    - Use model_config extra='ignore' so undocumented Kalshi fields never cause crashes.
    - Prices from Kalshi are in cents (0–100). Normalize to 0.0–1.0 before storing in DB.
    - Kalshi status strings are mapped to our market_status enum via KALSHI_STATUS_MAP.

Kalshi API docs: https://api.elections.kalshi.com/trade-api/v2/docs
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Status normalization
# ---------------------------------------------------------------------------

KALSHI_STATUS_MAP: dict[str, str] = {
    "open": "active",
    "closed": "closed",
    "settled": "resolved",
    "paused": "suspended",
    "finalized": "resolved",
    "unopened": "unopened",
    # SDK Market model uses these statuses:
    "initialized": "unopened",
    "inactive": "unopened",
    "active": "active",
    "determined": "resolved",
    "disputed": "in_dispute",
    "amended": "resolved",
}


def map_kalshi_status(raw: str | None) -> str:
    if not raw:
        return "active"
    return KALSHI_STATUS_MAP.get(raw.lower(), "active")


# ---------------------------------------------------------------------------
# Settlement Source (shared across series/events)
# ---------------------------------------------------------------------------

class KalshiSettlementSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = None
    url: Optional[str] = None


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------

class KalshiSeries(BaseModel):
    """
    Returned by GET /trade-api/v2/series and GET /trade-api/v2/series/{series_ticker}.

    Fields match the SDK's Series model. image_url and description are NOT
    top-level API fields — they may live inside product_metadata.
    """
    model_config = ConfigDict(extra="ignore")

    ticker: str
    title: str
    category: Optional[str] = None
    tags: Optional[List[str]] = Field(default_factory=list)
    frequency: Optional[str] = None
    settlement_sources: Optional[List[KalshiSettlementSource]] = Field(default_factory=list)
    contract_url: Optional[str] = None
    contract_terms_url: Optional[str] = None
    product_metadata: Optional[Dict[str, Any]] = None
    fee_type: Optional[str] = None
    fee_multiplier: Optional[float] = None
    additional_prohibitions: Optional[List[str]] = Field(default_factory=list)
    volume: Optional[int] = None
    volume_fp: Optional[str] = None
    last_updated_ts: Optional[datetime] = None

    @property
    def image_url(self) -> Optional[str]:
        """Extract image_url from product_metadata if present."""
        metadata = self.product_metadata or {}
        value = metadata.get("image_url")
        return value if isinstance(value, str) else None

    @property
    def description(self) -> Optional[str]:
        """Extract description from product_metadata if present."""
        metadata = self.product_metadata or {}
        value = metadata.get("description")
        return value if isinstance(value, str) else None


class KalshiSeriesListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    series: List[KalshiSeries] = Field(default_factory=list)
    cursor: Optional[str] = None


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

class KalshiEvent(BaseModel):
    """
    Returned by GET /trade-api/v2/events and GET /trade-api/v2/events/{event_ticker}.
    """
    model_config = ConfigDict(extra="ignore")

    event_ticker: str
    series_ticker: Optional[str] = None
    sub_title: Optional[str] = None
    title: str
    collateral_return_type: Optional[str] = None
    mutually_exclusive: Optional[bool] = False
    category: Optional[str] = None
    status: Optional[str] = None
    strike_date: Optional[Any] = None
    strike_period: Optional[str] = None
    close_time: Optional[datetime] = None
    expected_expiration_time: Optional[datetime] = None
    available_on_brokers: Optional[bool] = None
    product_metadata: Optional[Dict[str, Any]] = None
    last_updated_ts: Optional[datetime] = None
    settlement_sources: Optional[List[KalshiSettlementSource]] = Field(default_factory=list)
    markets: Optional[List["KalshiMarket"]] = Field(default_factory=list)

    @property
    def normalized_status(self) -> str:
        return map_kalshi_status(self.status)


class KalshiEventListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    events: List[KalshiEvent] = Field(default_factory=list)
    cursor: Optional[str] = None


# ---------------------------------------------------------------------------
# Event Metadata (from GET /events/{ticker}/metadata)
# ---------------------------------------------------------------------------

class KalshiMarketMetadata(BaseModel):
    """Per-market display metadata from the event metadata endpoint."""
    model_config = ConfigDict(extra="ignore")

    market_ticker: str
    image_url: Optional[str] = None
    color_code: Optional[str] = None


class KalshiEventMetadata(BaseModel):
    """Response from GET /trade-api/v2/events/{event_ticker}/metadata."""
    model_config = ConfigDict(extra="ignore")

    image_url: Optional[str] = None
    featured_image_url: Optional[str] = None
    market_details: Optional[List[KalshiMarketMetadata]] = Field(default_factory=list)
    settlement_sources: Optional[List[KalshiSettlementSource]] = Field(default_factory=list)
    competition: Optional[str] = None
    competition_scope: Optional[str] = None


# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------

class KalshiMarket(BaseModel):
    """
    Returned by GET /trade-api/v2/markets and GET /trade-api/v2/markets/{ticker}.

    Price fields (yes_bid, yes_ask, no_bid, no_ask, last_price, previous_price)
    are in CENTS (0–100). Normalize to 0.0–1.0 (divide by 100) before storing in DB.
    """
    model_config = ConfigDict(extra="ignore")

    ticker: str
    event_ticker: Optional[str] = None
    series_ticker: Optional[str] = None
    market_type: Optional[str] = "binary"     # "binary", "scalar"
    title: str
    subtitle: Optional[str] = None
    yes_sub_title: Optional[str] = None
    no_sub_title: Optional[str] = None
    open_time: Optional[datetime] = None
    close_time: Optional[datetime] = None
    expiration_time: Optional[datetime] = None
    latest_expiration_time: Optional[datetime] = None
    expected_expiration_time: Optional[datetime] = None
    settlement_timer_seconds: Optional[int] = None
    status: Optional[str] = None
    response_price_units: Optional[str] = None  # "usd_cent"

    # Prices in cents (0–100)
    yes_bid: Optional[int] = None
    yes_ask: Optional[int] = None
    no_bid: Optional[int] = None
    no_ask: Optional[int] = None
    last_price: Optional[int] = None
    previous_yes_bid: Optional[int] = None
    previous_yes_ask: Optional[int] = None
    previous_price: Optional[int] = None

    # Dollar string prices (new SDK format)
    yes_bid_dollars: Optional[str] = None
    yes_ask_dollars: Optional[str] = None
    no_bid_dollars: Optional[str] = None
    no_ask_dollars: Optional[str] = None
    last_price_dollars: Optional[str] = None

    volume: Optional[int] = None
    volume_24h: Optional[int] = None
    liquidity: Optional[int] = None
    open_interest: Optional[int] = None
    open_interest_fp: Optional[str] = None
    notional_value: Optional[int] = None
    risk_limit_cents: Optional[int] = None
    fractional_trading_enabled: Optional[bool] = None

    can_close_early: Optional[bool] = None
    early_close_condition: Optional[str] = None
    expiration_value: Optional[str] = None
    category: Optional[str] = None
    strike_type: Optional[str] = None
    floor_strike: Optional[Any] = None
    cap_strike: Optional[Any] = None
    functional_strike: Optional[str] = None
    custom_strike: Optional[Dict[str, Any]] = None
    rules_primary: Optional[str] = None
    rules_secondary: Optional[str] = None
    result: Optional[str] = None               # Resolved value: "yes", "no", or named outcome

    settlement_value: Optional[int] = None
    settlement_value_dollars: Optional[str] = None
    settlement_ts: Optional[datetime] = None

    price_level_structure: Optional[str] = None
    is_provisional: Optional[bool] = None

    @property
    def normalized_status(self) -> str:
        return map_kalshi_status(self.status)

    @property
    def normalized_market_type(self) -> str:
        """Map Kalshi market_type to our DB enum."""
        type_map = {"binary": "binary", "categorical": "categorical", "scalar": "scalar"}
        return type_map.get(self.market_type or "binary", "binary")

    def yes_price_normalized(self) -> Optional[Decimal]:
        """Yes ask price normalized to 0.0–1.0."""
        if self.yes_ask is not None:
            return Decimal(self.yes_ask) / Decimal(100)
        return None

    def no_price_normalized(self) -> Optional[Decimal]:
        """No ask price normalized to 0.0–1.0."""
        if self.no_ask is not None:
            return Decimal(self.no_ask) / Decimal(100)
        return None


class KalshiMarketListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    markets: List[KalshiMarket] = Field(default_factory=list)
    cursor: Optional[str] = None


# Forward reference resolution
KalshiEvent.model_rebuild()


# ---------------------------------------------------------------------------
# WebSocket message shapes (for future real-time streaming)
# ---------------------------------------------------------------------------

class KalshiWSSubscribeMessage(BaseModel):
    """Outbound subscription message sent to Kalshi WebSocket."""
    id: int
    cmd: str = "subscribe"
    params: dict[str, Any]


class KalshiWSOrderbookDelta(BaseModel):
    """
    Inbound orderbook delta from Kalshi WebSocket.
    Used for real-time price caching (not yet implemented).
    """
    model_config = ConfigDict(extra="ignore")

    market_ticker: str
    type: str                              # "orderbook_delta" | "orderbook_snapshot"
    seq: Optional[int] = None
    yes: Optional[List[List[int]]] = None  # [[price_cents, quantity], ...]
    no: Optional[List[List[int]]] = None
