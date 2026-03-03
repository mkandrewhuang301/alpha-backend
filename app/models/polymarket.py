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
