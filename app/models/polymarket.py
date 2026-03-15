"""
Pydantic models for Polymarket REST API and on-chain data response shapes.

Usage:
    - Workers parse raw Polymarket API dicts into these models before writing to DB.
    - Use model_config extra='ignore' so undocumented fields never cause crashes.
    - Prices from Polymarket are already normalized to 0.0–1.0 float.
    - Wallet addresses (Polygon) are stored as-is in tracked_entities.external_identifier.

Polymarket CLOB API: https://docs.polymarket.com
Gamma API (metadata): https://gamma-api.polymarket.com

NOTE: Polymarket ingestion is not yet implemented. These models are placeholders
for when Polymarket sync is built out.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Status normalization
# ---------------------------------------------------------------------------

POLYMARKET_STATUS_MAP: dict[str, str] = {
    "open": "active",
    "closed": "closed",
    "resolved": "resolved",
    "paused": "suspended",
    "cancelled": "canceled",
}


def map_polymarket_status(raw: str | None) -> str:
    if not raw:
        return "active"
    return POLYMARKET_STATUS_MAP.get(raw.lower(), "active")


# ---------------------------------------------------------------------------
# Series (Gamma API /series endpoint)
# ---------------------------------------------------------------------------

class PolymarketCategory(BaseModel):
    """A category label from the Gamma /series categories array."""
    model_config = ConfigDict(extra="ignore")

    id: Optional[Any] = None
    label: Optional[str] = None

    @property
    def label_str(self) -> str:
        """Return label string, falling back to str(id) if label is missing."""
        return self.label or str(self.id or "")


class PolymarketSeries(BaseModel):
    """
    A Polymarket series from the Gamma API /series endpoint.
    Maps to our series table.

    Identifier strategy:
        ext_id          = series.id      (stable internal ID for background polling)
        platform_metadata["slug"] = series.slug (URL-friendly for FE sharing/linking)
        platform_metadata["ticker"] = series.ticker (short machine-readable key)
    """
    model_config = ConfigDict(extra="ignore")

    id: str                                         # Used as ext_id in series table
    slug: Optional[str] = None                      # URL-friendly — stored in platform_metadata
    ticker: Optional[str] = None                    # Short key — stored in platform_metadata
    title: str
    description: Optional[str] = None
    categories: Optional[List[PolymarketCategory]] = Field(default_factory=list)
    tags: Optional[List[str]] = Field(default_factory=list)
    image: Optional[str] = None                     # Featured image URL
    icon: Optional[str] = None                      # Small icon URL
    active: Optional[bool] = None
    closed: Optional[bool] = None
    archived: Optional[bool] = None
    featured: Optional[bool] = None
    recurrence: Optional[str] = None               # "weekly", "daily", "monthly" → frequency
    volume24hr: Optional[Decimal] = None
    volume: Optional[Decimal] = None
    liquidity: Optional[Decimal] = None
    start_date: Optional[str] = Field(default=None, alias="startDate")
    events: Optional[List[Any]] = Field(default_factory=list)

    @property
    def category_labels(self) -> List[str]:
        """Extract category labels as a plain string list for DB storage."""
        return [c.label_str for c in (self.categories or []) if c.label_str]

    @property
    def image_url(self) -> Optional[str]:
        return self.image or self.icon


# ---------------------------------------------------------------------------
# Markets (Gamma API metadata layer)
# ---------------------------------------------------------------------------

class PolymarketToken(BaseModel):
    """A single tradeable outcome token (ERC-1155)."""
    model_config = ConfigDict(extra="ignore")

    token_id: str                          # ERC-1155 token ID — used as execution_asset_id in DB
    outcome: str                           # Human-readable: "Yes", "No", "Kamala Harris"
    price: Optional[Decimal] = None        # Current mid price 0.0–1.0
    winner: Optional[bool] = None


class PolymarketMarket(BaseModel):
    """
    A single market from the Gamma API.
    Maps to our markets + market_outcomes tables.
    """
    model_config = ConfigDict(extra="ignore")

    condition_id: str                      # Primary identifier — stored as ext_id in markets table
    question_id: Optional[str] = None
    question: str                          # Market title
    description: Optional[str] = None
    market_slug: Optional[str] = None
    status: Optional[str] = None
    active: Optional[bool] = None
    closed: Optional[bool] = None
    archived: Optional[bool] = None
    neg_risk: Optional[bool] = False
    neg_risk_market_id: Optional[str] = None
    end_date_iso: Optional[datetime] = None
    game_start_time: Optional[datetime] = None
    tokens: Optional[List[PolymarketToken]] = Field(default_factory=list)
    rewards: Optional[dict[str, Any]] = None
    volume: Optional[Decimal] = None
    liquidity: Optional[Decimal] = None
    platform_metadata: Optional[dict[str, Any]] = Field(default_factory=dict)

    @property
    def normalized_status(self) -> str:
        return map_polymarket_status(self.status)


class PolymarketMarketListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    data: List[PolymarketMarket] = Field(default_factory=list)
    next_cursor: Optional[str] = None
    limit: Optional[int] = None
    count: Optional[int] = None


# ---------------------------------------------------------------------------
# Events / Groups (Gamma API)
# ---------------------------------------------------------------------------

class PolymarketEvent(BaseModel):
    """
    A Polymarket event (group of related markets).
    Maps to our events table.
    """
    model_config = ConfigDict(extra="ignore")

    id: str                                # Used as ext_id in events table
    slug: Optional[str] = None
    title: str
    description: Optional[str] = None
    category: Optional[str] = None
    image: Optional[str] = None
    active: Optional[bool] = None
    closed: Optional[bool] = None
    archived: Optional[bool] = None
    neg_risk: Optional[bool] = False
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    markets: Optional[List[PolymarketMarket]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# On-chain trade data (CLOB API / subgraph)
# ---------------------------------------------------------------------------

class PolymarketTrade(BaseModel):
    """
    A public trade from the Polymarket CLOB or on-chain subgraph.
    Maps to public_trades table.
    """
    model_config = ConfigDict(extra="ignore")

    transaction_hash: str
    condition_id: str                      # Maps to markets.ext_id
    token_id: str                          # Maps to market_outcomes.execution_asset_id
    maker_address: Optional[str] = None
    taker_address: Optional[str] = None
    side: Optional[str] = None             # "buy" | "sell"
    price: Decimal                         # Normalized 0.0–1.0
    size: Decimal                          # Shares traded
    usd_value: Optional[Decimal] = None
    timestamp: datetime

    @property
    def total_value_usd(self) -> Decimal:
        if self.usd_value is not None:
            return self.usd_value
        return self.price * self.size


# ---------------------------------------------------------------------------
# Tag Taxonomy (Gamma API /tags and /tags/{id}/related-tags/tags)
# ---------------------------------------------------------------------------

class PolymarketTagAPI(BaseModel):
    """A single tag from the Polymarket Gamma /tags endpoint."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: Optional[Any] = None
    label: Optional[str] = None
    slug: Optional[str] = None
    is_carousel: Optional[bool] = Field(default=False, alias="isCarousel")
    force_show: Optional[bool] = Field(default=False, alias="forceShow")
    force_hide: Optional[bool] = Field(default=False, alias="forceHide")

    @property
    def id_str(self) -> str:
        return str(self.id) if self.id is not None else ""


class PolymarketSportAPI(BaseModel):
    """Sports metadata entry from the Polymarket Gamma /sports endpoint."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    sport: Optional[str] = None
    series: Optional[str] = None                    # Active series slug
    resolution_oracle_uri: Optional[str] = Field(default=None, alias="resolutionOracleURI")
    tags: Optional[str] = None                      # Comma-separated tag ID string e.g. "100381, 100382"

    @property
    def tag_ids_list(self) -> List[str]:
        """Split comma-separated tags string into a clean list of tag ID strings."""
        if not self.tags:
            return []
        return [t.strip() for t in self.tags.split(",") if t.strip()]


# ---------------------------------------------------------------------------
# WebSocket / CLOB streaming (for future real-time price caching)
# ---------------------------------------------------------------------------

class PolymarketWSOrderbookSnapshot(BaseModel):
    """
    Inbound orderbook snapshot from Polymarket CLOB WebSocket.
    Used for real-time price caching (not yet implemented).
    """
    model_config = ConfigDict(extra="ignore")

    asset_id: str                          # ERC-1155 token ID
    bids: Optional[List[List[str]]] = None  # [[price, size], ...]
    asks: Optional[List[List[str]]] = None
    timestamp: Optional[str] = None
