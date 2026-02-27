"""
SQLAlchemy ORM models mirroring the PostgreSQL schema.

All ENUM types (exchange_type, market_status, etc.) are defined in PostgreSQL directly.
SQLAlchemy references them with create_type=False to avoid re-creating them on startup.

Three-level market hierarchy:
    Series → Events → Markets → MarketOutcomes

Supporting tables:
    Users → Accounts, TrackedEntities
    PublicTrades, UserOrders
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Enum,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from sqlalchemy import DateTime

from app.core.database import Base

# ---------------------------------------------------------------------------
# PostgreSQL ENUM type references (already created in DB — do not recreate)
# ---------------------------------------------------------------------------

exchange_enum = Enum(
    "kalshi", "polymarket",
    name="exchange_type", create_type=False,
)

platform_enum = Enum(
    "native_kalshi", "native_polymarket", "alpha_aggregator",
    name="platform_type", create_type=False,
)

trade_side_enum = Enum(
    "no", "yes", "other",
    name="trade_side", create_type=False,
)

order_action_enum = Enum(
    "buy", "sell",
    name="order_action", create_type=False,
)

market_type_enum = Enum(
    "binary", "categorical", "scalar",
    name="market_type", create_type=False,
)

order_status_enum = Enum(
    "pending_submission", "live", "partial", "matched_pending",
    "filled", "canceled", "expired", "rejected", "failed",
    name="order_status", create_type=False,
)

market_status_enum = Enum(
    "unopened", "active", "suspended", "closed",
    "in_dispute", "resolved", "canceled",
    name="market_status", create_type=False,
)


# ---------------------------------------------------------------------------
# 2. Core Identity and Authentication
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    accounts = relationship("Account", back_populates="user")
    orders = relationship("UserOrder", back_populates="user")


class Account(Base):
    __tablename__ = "accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    exchange = Column(exchange_enum, nullable=False)
    ext_account_id = Column(Text, nullable=False)
    encrypted_api_key = Column(Text)
    encrypted_api_secret = Column(Text)
    encrypted_passphrase = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

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
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("exchange", "external_identifier", name="uq_tracked_entities_exchange_extid"),
    )


# ---------------------------------------------------------------------------
# 3. Unified Market Hierarchy Engine
# ---------------------------------------------------------------------------

class Series(Base):
    """
    Top-level market category.
    Kalshi: series_ticker (e.g., "KXINAUGURATE")
    Polymarket: slug or UUID
    """
    __tablename__ = "series"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    exchange = Column(exchange_enum, nullable=False)
    ext_id = Column(Text, nullable=False)           # Kalshi series_ticker | Polymarket slug
    title = Column(Text, nullable=False)
    description = Column(Text)
    category = Column(Text)
    tags = Column(JSONB, default=list)              # Array of tag strings
    image_url = Column(Text)
    frequency = Column(Text)                        # Kalshi: "daily", "weekly", "2y", etc.
    is_deleted = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("exchange", "ext_id", name="uq_series_exchange_extid"),
    )

    events = relationship("Event", back_populates="series")


class Event(Base):
    """
    A specific resolution scope within a series.
    Kalshi: event_ticker (e.g., "KXINAUGURATE-25JAN20")
    Polymarket: slug
    """
    __tablename__ = "events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    series_id = Column(UUID(as_uuid=True), ForeignKey("series.id", ondelete="SET NULL"), nullable=True)
    exchange = Column(exchange_enum, nullable=False)
    ext_id = Column(Text, nullable=False)           # Kalshi event_ticker | Polymarket slug
    title = Column(Text, nullable=False)
    description = Column(Text)
    category = Column(Text)
    status = Column(market_status_enum, default="active")
    mutually_exclusive = Column(Boolean, default=False)
    close_time = Column(DateTime(timezone=True))
    expected_expiration_time = Column(DateTime(timezone=True))
    platform_metadata = Column(JSONB, default=dict)  # Neg Risk flags, MVP grouping rules, etc.
    is_deleted = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("exchange", "ext_id", name="uq_events_exchange_extid"),
    )

    series = relationship("Series", back_populates="events")
    markets = relationship("Market", back_populates="event")


class Market(Base):
    """
    A single tradeable contract within an event.
    Kalshi: specific ticker (e.g., "KXINAUGURATE-25JAN20-B0.5")
    Polymarket: conditionId or UUID
    """
    __tablename__ = "markets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id", ondelete="RESTRICT"), nullable=False)
    exchange = Column(exchange_enum, nullable=False)
    ext_id = Column(Text, nullable=False)           # Kalshi ticker | Polymarket conditionId
    title = Column(Text, nullable=False)
    subtitle = Column(Text)
    type = Column(market_type_enum, default="binary")
    status = Column(market_status_enum, default="unopened")
    open_time = Column(DateTime(timezone=True))
    close_time = Column(DateTime(timezone=True))
    resolve_time = Column(DateTime(timezone=True))

    # Resolution fields
    result = Column(Text)                           # Final resolved value ("yes", "no", named outcome)
    rules_primary = Column(Text)
    rules_secondary = Column(Text)

    # Exchange-specific overflow: CTF condition IDs, Kalshi custom strikes, Neg Risk config, etc.
    platform_metadata = Column(JSONB, default=dict)

    is_deleted = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("exchange", "ext_id", name="uq_markets_exchange_extid"),
        Index("idx_markets_metadata_gin", "platform_metadata", postgresql_using="gin"),
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
    title = Column(Text, nullable=False)               # Human-readable: "Yes", "No", "Kamala Harris"
    side = Column(trade_side_enum, nullable=False)
    is_winner = Column(Boolean, default=None)
    platform_metadata = Column(JSONB, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("market_id", "execution_asset_id", name="uq_market_outcomes_market_asset"),
    )

    market = relationship("Market", back_populates="outcomes")
    public_trades = relationship("PublicTrade", back_populates="outcome")
    user_orders = relationship("UserOrder", back_populates="outcome")


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
    external_identifier = Column(Text, nullable=False)  # Anonymized wallet or account hash
    action = Column(order_action_enum, nullable=False)
    price = Column(Numeric(10, 6), nullable=False)       # Normalized 0.0–1.0 (not cents)
    size = Column(Numeric(24, 8), nullable=False)        # Shares/tokens traded
    total_value_usd = Column(Numeric(24, 8), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("idx_public_trades_feed", "market_id", "timestamp"),
        Index(
            "idx_public_trades_whale",
            "external_identifier",
            postgresql_where=(Column("size") > 10000),
        ),
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

    client_order_id = Column(Text, unique=True)          # App-generated UUID for deduplication
    exchange_order_id = Column(Text)                     # Exchange-assigned order ID
    execution_receipt = Column(Text)                     # Polygon txn hash or Kalshi receipt

    action = Column(order_action_enum, nullable=False)
    type = Column(String(20), nullable=False)            # "limit", "market", "fok", "ioc"

    requested_price = Column(Numeric(10, 6), nullable=False)
    requested_size = Column(Numeric(24, 8), nullable=False)

    filled_price = Column(Numeric(10, 6), default=0)
    filled_size = Column(Numeric(24, 8), default=0)

    maker_fee_usd = Column(Numeric(14, 6), default=0)
    taker_fee_usd = Column(Numeric(14, 6), default=0)
    settlement_profit_fee = Column(Numeric(14, 6), default=0)

    status = Column(order_status_enum, nullable=False, default="pending_submission")
    error_message = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_user_orders_lookup", "user_id", "status", "created_at"),
    )

    user = relationship("User", back_populates="orders")
    account = relationship("Account", back_populates="orders")
    market = relationship("Market", back_populates="user_orders")
    outcome = relationship("MarketOutcome", back_populates="user_orders")
