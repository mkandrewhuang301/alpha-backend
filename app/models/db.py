"""
SQLAlchemy ORM models mirroring the PostgreSQL schema.

All ENUM types (exchange_type, market_status, etc.) are created automatically by
SQLAlchemy's metadata.create_all() if they don't already exist in PostgreSQL.

Three-level market hierarchy:
    Series → Events → Markets → MarketOutcomes

Classification tables:
    PlatformTags — UI metadata table for categories + tags (both exchanges)

Supporting tables:
    Users → Accounts, TrackedEntities
    PublicTrades, UserOrders
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import relationship

from app.core.database import Base

# ---------------------------------------------------------------------------
# PostgreSQL ENUM types (auto-created if not present)
# ---------------------------------------------------------------------------

exchange_enum = Enum(
    "kalshi", "polymarket",
    name="exchange_type",
)

platform_enum = Enum(
    "native_kalshi", "native_polymarket", "alpha_aggregator",
    name="platform_type",
)

trade_side_enum = Enum(
    "no", "yes", "other",
    name="trade_side",
)

order_action_enum = Enum(
    "buy", "sell",
    name="order_action",
)

market_type_enum = Enum(
    "binary", "categorical", "scalar",
    name="market_type",
)

order_status_enum = Enum(
    "pending_submission", "live", "partial", "matched_pending",
    "filled", "canceled", "expired", "rejected", "failed",
    name="order_status",
)

market_status_enum = Enum(
    "unopened", "active", "suspended", "closed",
    "in_dispute", "resolved", "canceled",
    name="market_status",
)

platform_tag_type_enum = Enum(
    "category", "tag",
    name="platform_tag_type",
)


# ---------------------------------------------------------------------------
# 1a. PlatformTags — UI metadata table for frontend display
# ---------------------------------------------------------------------------

class PlatformTag(Base):
    """
    UI metadata table for categories and tags — serves frontend display state.

    Stores human-readable labels, images, and display flags for categories/tags
    that appear in the `categories` and `tags` ARRAY(text) columns on Series/Event.

    The `slug` field is the critical link — it matches the slug strings stored in
    Series.categories, Series.tags, Event.categories, and Event.tags.

    Kalshi:     type='category', ext_id=slug,    parent_ids=[]                (top-level)
                type='tag',      ext_id=slug,    parent_ids=["<cat-slug>"]    (tag under category)
    Polymarket: type='category', ext_id=cat_id, parent_ids=[]                (top-level)
                type='tag',      ext_id=tag_id, parent_ids=["<parent-tag-id>", ...] (multi-parent)

    parent_ids is GIN-indexed for fast child lookups:
        WHERE parent_ids @> ARRAY['<parent-slug-or-id>']

    platform_metadata stores exchange-specific overflow fields:
        Polymarket tags: {"polymarket_id": "12345", ...}
    """
    __tablename__ = "platform_tags"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    exchange = Column(exchange_enum, nullable=False)
    type = Column(platform_tag_type_enum, nullable=False)
    ext_id = Column(String, nullable=False)              # Polymarket id or Kalshi slug
    parent_ids = Column(ARRAY(String), default=list, nullable=True)  # Parent slugs/IDs (multi-parent support)
    slug = Column(String, nullable=False)                # Critical link to Event/Series ARRAY columns
    label = Column(String, nullable=False)               # UI display text
    image_url = Column(Text, nullable=True)
    is_carousel = Column(Boolean, default=False, nullable=False)
    force_show = Column(Boolean, default=False, nullable=False)
    force_hide = Column(Boolean, default=False, nullable=False)
    platform_metadata = Column(JSONB, default=dict, nullable=True)   # Exchange-specific overflow
    is_deleted = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(
        DateTime(timezone=True),
        server_default=text("NOW()"),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("exchange", "slug", "type", name="uq_platform_tags_exchange_slug_type"),
        Index("idx_platform_tags_slug", "slug"),
        Index("idx_platform_tags_exchange_type", "exchange", "type"),
        Index("idx_platform_tags_parent_ids_gin", "parent_ids", postgresql_using="gin"),
    )


# ---------------------------------------------------------------------------
# 2. Core Identity and Authentication
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False)
    magic_did = Column(Text, unique=True, nullable=False)
    eoa_address = Column(Text, unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=lambda: datetime.now(timezone.utc))

    accounts = relationship("Account", back_populates="user")
    orders = relationship("UserOrder", back_populates="user")


class Account(Base):
    __tablename__ = "accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    exchange = Column(exchange_enum, nullable=False)
    ext_account_id = Column(Text, nullable=False)
    safe_address = Column(Text, unique=True, nullable=True)  # Polymarket only — Gnosis Safe address on Polygon
    encrypted_api_key = Column(Text)
    encrypted_api_secret = Column(Text)
    encrypted_passphrase = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    __table_args__ = (
        UniqueConstraint("user_id", "exchange", "ext_account_id", name="uq_accounts_user_exchange_extid"),
    )

    user = relationship("User", back_populates="accounts")
    orders = relationship("UserOrder", back_populates="account")


class TrackedEntity(Base):
    """
    Represents a public trader wallet or Kalshi pseudonym being monitored.
    May or may not be linked to an Alpha user (user_id is nullable).
    """
    __tablename__ = "tracked_entities"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), unique=True, nullable=True)
    exchange = Column(exchange_enum, nullable=False)
    external_identifier = Column(Text, nullable=False)
    alias = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    __table_args__ = (
        UniqueConstraint("exchange", "external_identifier", name="uq_tracked_entities_exchange_extid"),
    )


# ---------------------------------------------------------------------------
# 3. Unified Market Hierarchy Engine
# ---------------------------------------------------------------------------

class Series(Base):
    """
    Top-level market category.
    Kalshi:     ext_id = series_ticker (e.g. "KXINAUGURATE"); no slug concept
    Polymarket: ext_id = Gamma series id (stable UUID/int);
                slug stored in platform_metadata["slug"] for UI sharing/linking

    categories and tags are GIN-indexed ARRAY(text) columns — slug strings that
    match PlatformTag.slug for fast lookup and filtering without JOIN overhead.
    """
    __tablename__ = "series"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    exchange = Column(exchange_enum, nullable=False)
    ext_id = Column(Text, nullable=False)           # Kalshi ticker | Polymarket series id
    title = Column(Text, nullable=False)
    description = Column(Text)

    # GIN-indexed ARRAY(text) taxonomy columns — slug strings for fast filtering
    # Kalshi:     categories = [slugified_category_string]  (single-element)
    # Polymarket: categories = [slug1, slug2, ...]          (multi-element from API)
    categories = Column(ARRAY(String), default=list, nullable=True)
    tags = Column(ARRAY(String), default=list, nullable=True)

    image_url = Column(Text)
    frequency = Column(Text)                        # Kalshi: "daily", "weekly", "2y", etc.

    # Settlement & contract info (from Kalshi Series API)
    settlement_sources = Column(JSONB, default=list)    # [{name, url}, ...]
    contract_url = Column(Text)                         # Link to original contract filing
    additional_prohibitions = Column(JSONB, default=list)  # ["No trading if...", ...]

    # Exchange-specific overflow: Polymarket slug/ticker, Kalshi contract URLs, etc.
    platform_metadata = Column(JSONB, default=dict)

    # Fee structure
    fee_type = Column(String(50))
    fee_multiplier = Column(Numeric(10, 4))

    # Aggregated volume metrics
    volume_24h = Column(Numeric(24, 8), default=0)
    total_volume = Column(Numeric(24, 8), default=0)

    is_deleted = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("exchange", "ext_id", name="uq_series_exchange_extid"),
        Index("idx_series_categories_gin", "categories", postgresql_using="gin"),
        Index("idx_series_tags_gin", "tags", postgresql_using="gin"),
    )


class Event(Base):
    """
    A specific resolution scope within a series.
    Kalshi:     ext_id = event_ticker (e.g. "KXINAUGURATE-25JAN20"); no slug concept
    Polymarket: ext_id = Gamma event id (stable string);
                slug stored in platform_metadata["slug"] for UI sharing/linking

    series_ids: ARRAY(UUID) linking to Series.id — supports Polymarket's many-to-many
                relationship between events and series.

    categories/tags: GIN-indexed ARRAY(text) slug strings — cascaded from parent Series
                     during ingest. Enables fast filtering without JOIN overhead.
    """
    __tablename__ = "events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    exchange = Column(exchange_enum, nullable=False)
    ext_id = Column(Text, nullable=False)           # Kalshi event_ticker | Polymarket event id
    title = Column(Text, nullable=False)
    description = Column(Text)                       # Maps to Kalshi sub_title
    sub_title = Column(Text)                         # Short descriptive title

    # GIN-indexed ARRAY(text) taxonomy columns — slug strings for fast filtering
    categories = Column(ARRAY(String), default=list, nullable=True)
    tags = Column(ARRAY(String), default=list, nullable=True)

    # ARRAY(UUID) links to Series rows — supports Polymarket multi-series events
    # Kalshi: single-element array [series.id]
    # Polymarket: multi-element array for events spanning multiple series
    series_ids = Column(ARRAY(UUID(as_uuid=True)), default=list, nullable=True)

    status = Column(market_status_enum, default="active")
    mutually_exclusive = Column(Boolean, default=False)
    close_time = Column(DateTime(timezone=True))
    expected_expiration_time = Column(DateTime(timezone=True))
    volume_24h = Column(Numeric(24, 8), default=0)

    # Display metadata
    event_image_url = Column(Text)
    image_url = Column(Text)
    featured_image_url = Column(Text)
    settlement_sources = Column(JSONB, default=list)
    competition = Column(String(100))
    competition_scope = Column(String(100))

    # Aggregated metrics
    total_volume = Column(Numeric(24, 8), default=0)
    open_interest = Column(Numeric(24, 8), default=0)

    platform_metadata = Column(JSONB, default=dict)
    is_deleted = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("exchange", "ext_id", name="uq_events_exchange_extid"),
        Index(
            "idx_active_events",
            "exchange", "close_time",
            postgresql_where=text("status = 'active'"),
        ),
        Index("idx_events_metadata_gin", "platform_metadata", postgresql_using="gin"),
        Index("idx_events_categories_gin", "categories", postgresql_using="gin"),
        Index("idx_events_tags_gin", "tags", postgresql_using="gin"),
        Index("idx_events_series_ids_gin", "series_ids", postgresql_using="gin"),
    )

    markets = relationship("Market", back_populates="event")


class Market(Base):
    """
    A single tradeable contract within an event.
    Kalshi:     ext_id = specific ticker (e.g. "KXINAUGURATE-25JAN20-B0.5"); no slug concept
    Polymarket: ext_id = conditionId (stable hex string);
                market_slug stored in platform_metadata["market_slug"] for UI sharing/linking
    """
    __tablename__ = "markets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id", ondelete="RESTRICT"), nullable=False)
    exchange = Column(exchange_enum, nullable=False)
    ext_id = Column(Text, nullable=False)           # Kalshi ticker | Polymarket conditionId
    title = Column(Text, nullable=False)
    subtitle = Column(Text)
    yes_sub_title = Column(Text)
    no_sub_title = Column(Text)
    type = Column(market_type_enum, default="binary")
    status = Column(market_status_enum, default="unopened")
    open_time = Column(DateTime(timezone=True))
    close_time = Column(DateTime(timezone=True))
    resolve_time = Column(DateTime(timezone=True))

    result = Column(Text)
    rules_primary = Column(Text)
    rules_secondary = Column(Text)

    image_url = Column(Text)
    color_code = Column(String(10))

    fractional_trading_enabled = Column(Boolean, default=False)
    response_price_units = Column(String(50))
    strike_type = Column(String(50))

    open_interest = Column(Numeric(24, 8))
    volume = Column(Numeric(24, 8))
    volume_24h = Column(Numeric(24, 8), default=0)
    total_volume = Column(Numeric(24, 8), default=0)
    liquidity = Column(Numeric(24, 8), default=0)

    platform_metadata = Column(JSONB, default=dict)

    is_deleted = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), server_default=text("NOW()"), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("exchange", "ext_id", name="uq_markets_exchange_extid"),
        Index("idx_markets_metadata_gin", "platform_metadata", postgresql_using="gin"),
        Index(
            "idx_active_markets_by_event",
            "event_id", "type",
            postgresql_where=text("status IN ('unopened', 'active')"),
        ),
    )

    event = relationship("Event", back_populates="markets")
    outcomes = relationship("MarketOutcome", back_populates="market")
    public_trades = relationship("PublicTrade", back_populates="market")
    user_orders = relationship("UserOrder", back_populates="market")


class MarketOutcome(Base):
    """
    A single tradeable side within a market.
    Binary markets: two rows (yes / no).
    Categorical markets: one row per named outcome.
    """
    __tablename__ = "market_outcomes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    market_id = Column(UUID(as_uuid=True), ForeignKey("markets.id", ondelete="RESTRICT"), nullable=False)
    execution_asset_id = Column(Text, nullable=False)  # Kalshi: "yes"/"no" | Polymarket: ERC-1155 token ID
    title = Column(Text, nullable=False)
    side = Column(trade_side_enum, nullable=False)
    is_winner = Column(Boolean, default=None)
    platform_metadata = Column(JSONB, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))

    __table_args__ = (
        UniqueConstraint("market_id", "execution_asset_id", name="uq_market_outcomes_market_asset"),
        Index("idx_outcomes_market_lookup", "market_id"),
    )

    market = relationship("Market", back_populates="outcomes")
    public_trades = relationship("PublicTrade", back_populates="outcome")
    user_orders = relationship("UserOrder", back_populates="outcome")


# ---------------------------------------------------------------------------
# 3b. Sports Metadata (exchange-generic, GIN-indexed tag_ids array)
# ---------------------------------------------------------------------------

class SportsMetadata(Base):
    """
    Sports metadata across exchanges: sport identifiers, resolution oracles, linked tag IDs.

    Polymarket: populated from GET /sports. The tags field is a comma-separated
    string of tag IDs — stored as GIN-indexed ARRAY(text) for fast reverse lookups:
        WHERE tag_ids @> ARRAY['<tag_id>']

    Uses a composite PK (exchange, sport) so the same sport can exist per exchange.
    """
    __tablename__ = "sports_metadata"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    exchange = Column(exchange_enum, nullable=False)
    sport = Column(Text, nullable=False)              # Sport identifier (e.g. "basketball")
    series_slug = Column(Text, nullable=True)          # Active series slug
    resolution_url = Column(Text, nullable=True)       # Resolution oracle URI
    tag_ids = Column(ARRAY(Text), default=list, nullable=True)   # ARRAY of associated tag IDs
    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(
        DateTime(timezone=True),
        server_default=text("NOW()"),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("exchange", "sport", name="uq_sports_metadata_exchange_sport"),
        Index("idx_sports_metadata_tag_ids_gin", "tag_ids", postgresql_using="gin"),
    )


# ---------------------------------------------------------------------------
# 4. Trading Execution and Activity Logging
# ---------------------------------------------------------------------------

class PublicTrade(Base):
    """
    Anonymized public trade activity for whale watching and sharp money detection.
    """
    __tablename__ = "public_trades"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    exchange = Column(exchange_enum, nullable=False)
    market_id = Column(UUID(as_uuid=True), ForeignKey("markets.id"), nullable=False)
    outcome_id = Column(UUID(as_uuid=True), ForeignKey("market_outcomes.id"), nullable=False)
    external_identifier = Column(Text, nullable=False)
    action = Column(order_action_enum, nullable=False)
    price = Column(Numeric(10, 6), nullable=False)
    size = Column(Numeric(24, 8), nullable=False)
    total_value_usd = Column(Numeric(24, 8), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("idx_public_trades_recent", "market_id", text("timestamp DESC")),
        Index(
            "idx_public_trades_whale",
            "external_identifier",
            postgresql_where=(Column("size") > 10000),
        ),
        Index("idx_public_trades_brin_ts", "timestamp", postgresql_using="brin"),
    )

    market = relationship("Market", back_populates="public_trades")
    outcome = relationship("MarketOutcome", back_populates="public_trades")


class UserOrder(Base):
    """
    A trade order placed by an Alpha user on Kalshi or Polymarket via Alpha.
    """
    __tablename__ = "user_orders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    market_id = Column(UUID(as_uuid=True), ForeignKey("markets.id"), nullable=False)
    outcome_id = Column(UUID(as_uuid=True), ForeignKey("market_outcomes.id"), nullable=False)
    exchange = Column(exchange_enum, nullable=False)
    platform = Column(platform_enum, nullable=False, default="alpha_aggregator")

    client_order_id = Column(Text, unique=True)
    exchange_order_id = Column(Text)
    execution_receipt = Column(Text)

    action = Column(order_action_enum, nullable=False)
    type = Column(String(20), nullable=False)

    time_in_force = Column(String(20), default="gtc")
    expiration_time = Column(DateTime(timezone=True))

    requested_price = Column(Numeric(10, 6), nullable=False)
    requested_size = Column(Numeric(24, 8), nullable=False)

    filled_price = Column(Numeric(10, 6), default=0)
    filled_size = Column(Numeric(24, 8), default=0)

    maker_fee_usd = Column(Numeric(14, 6), default=0)
    taker_fee_usd = Column(Numeric(14, 6), default=0)
    settlement_profit_fee = Column(Numeric(14, 6), default=0)

    status = Column(order_status_enum, nullable=False, default="pending_submission")
    error_message = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at = Column(
        DateTime(timezone=True),
        server_default=text("NOW()"),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        Index("idx_user_orders_portfolio", "user_id", "status", text("created_at DESC")),
    )

    user = relationship("User", back_populates="orders")
    account = relationship("Account", back_populates="orders")
    market = relationship("Market", back_populates="user_orders")
    outcome = relationship("MarketOutcome", back_populates="user_orders")
