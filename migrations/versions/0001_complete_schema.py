"""complete_schema

Single authoritative migration for the Alpha backend schema.
Idempotent — safe to run against both a fresh database and an existing one
that was created by earlier ad-hoc migrations or init_db().

Revision ID: 0001_complete_schema
Revises:
Create Date: 2026-03-15

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "0001_complete_schema"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _table_exists(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _col_exists(inspector, table: str, col: str) -> bool:
    return any(c["name"] == col for c in inspector.get_columns(table))


def _index_exists(inspector, table: str, index_name: str) -> bool:
    return any(i["name"] == index_name for i in inspector.get_indexes(table))


def _constraint_exists(bind, constraint_name: str, table: str) -> bool:
    row = bind.execute(
        text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE constraint_name = :name AND table_name = :tbl"
        ),
        {"name": constraint_name, "tbl": table},
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    # ------------------------------------------------------------------ ENUMs
    for ddl in [
        "DO $$ BEGIN CREATE TYPE exchange_type AS ENUM ('kalshi','polymarket'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        "DO $$ BEGIN CREATE TYPE platform_type AS ENUM ('native_kalshi','native_polymarket','alpha_aggregator'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        "DO $$ BEGIN CREATE TYPE trade_side AS ENUM ('no','yes','other'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        "DO $$ BEGIN CREATE TYPE order_action AS ENUM ('buy','sell'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        "DO $$ BEGIN CREATE TYPE market_type AS ENUM ('binary','categorical','scalar'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        "DO $$ BEGIN CREATE TYPE order_status AS ENUM ('pending_submission','live','partial','matched_pending','filled','canceled','expired','rejected','failed'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        "DO $$ BEGIN CREATE TYPE market_status AS ENUM ('unopened','active','suspended','closed','in_dispute','resolved','canceled'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        "DO $$ BEGIN CREATE TYPE platform_tag_type AS ENUM ('category','tag'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
    ]:
        op.execute(ddl)

    # Re-inspect after potential schema changes
    insp = inspect(bind)

    # --------------------------------------------------------- platform_tags
    if not _table_exists(insp, "platform_tags"):
        op.create_table(
            "platform_tags",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("exchange", postgresql.ENUM("kalshi", "polymarket",
                      name="exchange_type", create_type=False), nullable=False),
            sa.Column("type", postgresql.ENUM("category", "tag",
                      name="platform_tag_type", create_type=False), nullable=False),
            sa.Column("ext_id", sa.String(), nullable=False),
            sa.Column("parent_ids", postgresql.ARRAY(sa.Text()), nullable=True),
            sa.Column("slug", sa.String(), nullable=False),
            sa.Column("label", sa.String(), nullable=False),
            sa.Column("image_url", sa.Text(), nullable=True),
            sa.Column("is_carousel", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("force_show", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("force_hide", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("platform_metadata", postgresql.JSONB(), nullable=True),
            sa.Column("is_deleted", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.UniqueConstraint("exchange", "slug", "type",
                                name="uq_platform_tags_exchange_slug_type"),
        )
        op.create_index("idx_platform_tags_slug", "platform_tags", ["slug"])
        op.create_index("idx_platform_tags_exchange_type", "platform_tags", ["exchange", "type"])
        op.create_index("idx_platform_tags_parent_ids_gin", "platform_tags",
                        ["parent_ids"], postgresql_using="gin")
    else:
        # Table exists — apply delta changes only
        cols = {c["name"] for c in insp.get_columns("platform_tags")}
        idxs = {i["name"] for i in insp.get_indexes("platform_tags")}

        # Drop old single-parent column
        if "parent_id" in cols:
            op.drop_column("platform_tags", "parent_id")

        # Add multi-parent array column
        if "parent_ids" not in cols:
            op.add_column("platform_tags",
                          sa.Column("parent_ids", postgresql.ARRAY(sa.Text()), nullable=True))
        if "idx_platform_tags_parent_ids_gin" not in idxs:
            op.create_index("idx_platform_tags_parent_ids_gin", "platform_tags",
                            ["parent_ids"], postgresql_using="gin")

        # Add force_hide
        if "force_hide" not in cols:
            op.add_column("platform_tags",
                          sa.Column("force_hide", sa.Boolean(),
                                    server_default="false", nullable=False))

        # Add platform_metadata
        if "platform_metadata" not in cols:
            op.add_column("platform_tags",
                          sa.Column("platform_metadata", postgresql.JSONB(), nullable=True))

        # Ensure base indexes exist
        if "idx_platform_tags_slug" not in idxs:
            op.create_index("idx_platform_tags_slug", "platform_tags", ["slug"])
        if "idx_platform_tags_exchange_type" not in idxs:
            op.create_index("idx_platform_tags_exchange_type",
                            "platform_tags", ["exchange", "type"])

    # ---------------------------------------------------------------- series
    if not _table_exists(insp, "series"):
        op.create_table(
            "series",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("exchange", postgresql.ENUM("kalshi", "polymarket",
                      name="exchange_type", create_type=False), nullable=False),
            sa.Column("ext_id", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("description", sa.Text()),
            sa.Column("categories", postgresql.ARRAY(sa.String()), nullable=True),
            sa.Column("tags", postgresql.ARRAY(sa.String()), nullable=True),
            sa.Column("image_url", sa.Text()),
            sa.Column("frequency", sa.Text()),
            sa.Column("settlement_sources", postgresql.JSONB(), server_default="[]"),
            sa.Column("contract_url", sa.Text()),
            sa.Column("additional_prohibitions", postgresql.JSONB(), server_default="[]"),
            sa.Column("platform_metadata", postgresql.JSONB(), server_default="{}"),
            sa.Column("fee_type", sa.String(50)),
            sa.Column("fee_multiplier", sa.Numeric(10, 4)),
            sa.Column("volume_24h", sa.Numeric(24, 8), server_default="0"),
            sa.Column("total_volume", sa.Numeric(24, 8), server_default="0"),
            sa.Column("is_deleted", sa.Boolean(), server_default="false"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.UniqueConstraint("exchange", "ext_id", name="uq_series_exchange_extid"),
        )
        op.create_index("idx_series_categories_gin", "series",
                        ["categories"], postgresql_using="gin")
        op.create_index("idx_series_tags_gin", "series", ["tags"], postgresql_using="gin")
    else:
        # Table exists — add ARRAY columns if missing (upgrade from pre-Polymarket schema)
        cols = {c["name"] for c in insp.get_columns("series")}
        idxs = {i["name"] for i in insp.get_indexes("series")}
        if "categories" not in cols:
            op.add_column("series", sa.Column("categories", postgresql.ARRAY(sa.String()), nullable=True))
        if "tags" not in cols:
            op.add_column("series", sa.Column("tags", postgresql.ARRAY(sa.String()), nullable=True))
        if "idx_series_categories_gin" not in idxs:
            op.create_index("idx_series_categories_gin", "series", ["categories"], postgresql_using="gin")
        if "idx_series_tags_gin" not in idxs:
            op.create_index("idx_series_tags_gin", "series", ["tags"], postgresql_using="gin")

    # ---------------------------------------------------------------- events
    if not _table_exists(insp, "events"):
        op.create_table(
            "events",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("exchange", postgresql.ENUM("kalshi", "polymarket",
                      name="exchange_type", create_type=False), nullable=False),
            sa.Column("ext_id", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("description", sa.Text()),
            sa.Column("sub_title", sa.Text()),
            sa.Column("categories", postgresql.ARRAY(sa.String()), nullable=True),
            sa.Column("tags", postgresql.ARRAY(sa.String()), nullable=True),
            sa.Column("series_ids", postgresql.ARRAY(postgresql.UUID()), nullable=True),
            sa.Column("status", postgresql.ENUM(
                "unopened", "active", "suspended", "closed", "in_dispute", "resolved", "canceled",
                name="market_status", create_type=False), server_default="active"),
            sa.Column("mutually_exclusive", sa.Boolean(), server_default="false"),
            sa.Column("close_time", sa.DateTime(timezone=True)),
            sa.Column("expected_expiration_time", sa.DateTime(timezone=True)),
            sa.Column("volume_24h", sa.Numeric(24, 8), server_default="0"),
            sa.Column("event_image_url", sa.Text()),
            sa.Column("image_url", sa.Text()),
            sa.Column("featured_image_url", sa.Text()),
            sa.Column("settlement_sources", postgresql.JSONB(), server_default="[]"),
            sa.Column("competition", sa.String(100)),
            sa.Column("competition_scope", sa.String(100)),
            sa.Column("total_volume", sa.Numeric(24, 8), server_default="0"),
            sa.Column("open_interest", sa.Numeric(24, 8), server_default="0"),
            sa.Column("platform_metadata", postgresql.JSONB(), server_default="{}"),
            sa.Column("is_deleted", sa.Boolean(), server_default="false"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.UniqueConstraint("exchange", "ext_id", name="uq_events_exchange_extid"),
        )
        op.create_index("idx_events_categories_gin", "events",
                        ["categories"], postgresql_using="gin")
        op.create_index("idx_events_tags_gin", "events", ["tags"], postgresql_using="gin")
        op.create_index("idx_events_series_ids_gin", "events",
                        ["series_ids"], postgresql_using="gin")
        op.create_index("idx_events_metadata_gin", "events",
                        ["platform_metadata"], postgresql_using="gin")
        op.create_index(
            "idx_active_events", "events", ["exchange", "close_time"],
            postgresql_where=sa.text("status = 'active'"),
        )
    else:
        # Table exists — add ARRAY columns if missing (upgrade from pre-Polymarket schema)
        cols = {c["name"] for c in insp.get_columns("events")}
        idxs = {i["name"] for i in insp.get_indexes("events")}
        if "categories" not in cols:
            op.add_column("events", sa.Column("categories", postgresql.ARRAY(sa.String()), nullable=True))
        if "tags" not in cols:
            op.add_column("events", sa.Column("tags", postgresql.ARRAY(sa.String()), nullable=True))
        if "series_ids" not in cols:
            op.add_column("events", sa.Column("series_ids", postgresql.ARRAY(postgresql.UUID()), nullable=True))
        if "idx_events_categories_gin" not in idxs:
            op.create_index("idx_events_categories_gin", "events", ["categories"], postgresql_using="gin")
        if "idx_events_tags_gin" not in idxs:
            op.create_index("idx_events_tags_gin", "events", ["tags"], postgresql_using="gin")
        if "idx_events_series_ids_gin" not in idxs:
            op.create_index("idx_events_series_ids_gin", "events", ["series_ids"], postgresql_using="gin")

    # --------------------------------------------------------------- markets
    if not _table_exists(insp, "markets"):
        op.create_table(
            "markets",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("event_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("events.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("exchange", postgresql.ENUM("kalshi", "polymarket",
                      name="exchange_type", create_type=False), nullable=False),
            sa.Column("ext_id", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("subtitle", sa.Text()),
            sa.Column("yes_sub_title", sa.Text()),
            sa.Column("no_sub_title", sa.Text()),
            sa.Column("type", postgresql.ENUM("binary", "categorical", "scalar",
                      name="market_type", create_type=False), server_default="binary"),
            sa.Column("status", postgresql.ENUM(
                "unopened", "active", "suspended", "closed", "in_dispute", "resolved", "canceled",
                name="market_status", create_type=False), server_default="unopened"),
            sa.Column("open_time", sa.DateTime(timezone=True)),
            sa.Column("close_time", sa.DateTime(timezone=True)),
            sa.Column("resolve_time", sa.DateTime(timezone=True)),
            sa.Column("result", sa.Text()),
            sa.Column("rules_primary", sa.Text()),
            sa.Column("rules_secondary", sa.Text()),
            sa.Column("image_url", sa.Text()),
            sa.Column("color_code", sa.String(10)),
            sa.Column("fractional_trading_enabled", sa.Boolean(), server_default="false"),
            sa.Column("response_price_units", sa.String(50)),
            sa.Column("strike_type", sa.String(50)),
            sa.Column("open_interest", sa.Numeric(24, 8)),
            sa.Column("volume", sa.Numeric(24, 8)),
            sa.Column("volume_24h", sa.Numeric(24, 8), server_default="0"),
            sa.Column("total_volume", sa.Numeric(24, 8), server_default="0"),
            sa.Column("liquidity", sa.Numeric(24, 8), server_default="0"),
            sa.Column("platform_metadata", postgresql.JSONB(), server_default="{}"),
            sa.Column("is_deleted", sa.Boolean(), server_default="false"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.UniqueConstraint("exchange", "ext_id", name="uq_markets_exchange_extid"),
        )
        op.create_index("idx_markets_metadata_gin", "markets",
                        ["platform_metadata"], postgresql_using="gin")
        op.create_index(
            "idx_active_markets_by_event", "markets", ["event_id", "type"],
            postgresql_where=sa.text("status IN ('unopened', 'active')"),
        )

    # -------------------------------------------------------- market_outcomes
    if not _table_exists(insp, "market_outcomes"):
        op.create_table(
            "market_outcomes",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("market_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("markets.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("execution_asset_id", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("side", postgresql.ENUM("no", "yes", "other",
                      name="trade_side", create_type=False), nullable=False),
            sa.Column("is_winner", sa.Boolean()),
            sa.Column("platform_metadata", postgresql.JSONB(), server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.UniqueConstraint("market_id", "execution_asset_id",
                                name="uq_market_outcomes_market_asset"),
        )
        op.create_index("idx_outcomes_market_lookup", "market_outcomes", ["market_id"])

    # ----------------------------------------------------------------- users
    if not _table_exists(insp, "users"):
        op.create_table(
            "users",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("email", sa.String(255), unique=True, nullable=False),
            sa.Column("password_hash", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        )

    # -------------------------------------------------------------- accounts
    if not _table_exists(insp, "accounts"):
        op.create_table(
            "accounts",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("exchange", postgresql.ENUM("kalshi", "polymarket",
                      name="exchange_type", create_type=False), nullable=False),
            sa.Column("ext_account_id", sa.Text(), nullable=False),
            sa.Column("encrypted_api_key", sa.Text()),
            sa.Column("encrypted_api_secret", sa.Text()),
            sa.Column("encrypted_passphrase", sa.Text()),
            sa.Column("is_active", sa.Boolean(), server_default="true"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.UniqueConstraint("user_id", "exchange", "ext_account_id",
                                name="uq_accounts_user_exchange_extid"),
        )

    # -------------------------------------------------------- tracked_entities
    if not _table_exists(insp, "tracked_entities"):
        op.create_table(
            "tracked_entities",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="SET NULL"), unique=True, nullable=True),
            sa.Column("exchange", postgresql.ENUM("kalshi", "polymarket",
                      name="exchange_type", create_type=False), nullable=False),
            sa.Column("external_identifier", sa.Text(), nullable=False),
            sa.Column("alias", sa.Text()),
            sa.Column("is_active", sa.Boolean(), server_default="true"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.UniqueConstraint("exchange", "external_identifier",
                                name="uq_tracked_entities_exchange_extid"),
        )

    # ---------------------------------------------------------- public_trades
    if not _table_exists(insp, "public_trades"):
        op.create_table(
            "public_trades",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("exchange", postgresql.ENUM("kalshi", "polymarket",
                      name="exchange_type", create_type=False), nullable=False),
            sa.Column("market_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("markets.id"), nullable=False),
            sa.Column("outcome_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("market_outcomes.id"), nullable=False),
            sa.Column("external_identifier", sa.Text(), nullable=False),
            sa.Column("action", postgresql.ENUM("buy", "sell",
                      name="order_action", create_type=False), nullable=False),
            sa.Column("price", sa.Numeric(10, 6), nullable=False),
            sa.Column("size", sa.Numeric(24, 8), nullable=False),
            sa.Column("total_value_usd", sa.Numeric(24, 8), nullable=False),
            sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("idx_public_trades_recent", "public_trades",
                        ["market_id", sa.text("timestamp DESC")])
        op.create_index("idx_public_trades_brin_ts", "public_trades",
                        ["timestamp"], postgresql_using="brin")

    # ----------------------------------------------------------- user_orders
    if not _table_exists(insp, "user_orders"):
        op.create_table(
            "user_orders",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("users.id"), nullable=False),
            sa.Column("account_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("accounts.id"), nullable=False),
            sa.Column("market_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("markets.id"), nullable=False),
            sa.Column("outcome_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("market_outcomes.id"), nullable=False),
            sa.Column("exchange", postgresql.ENUM("kalshi", "polymarket",
                      name="exchange_type", create_type=False), nullable=False),
            sa.Column("platform", postgresql.ENUM(
                "native_kalshi", "native_polymarket", "alpha_aggregator",
                name="platform_type", create_type=False), nullable=False,
                server_default="alpha_aggregator"),
            sa.Column("client_order_id", sa.Text(), unique=True),
            sa.Column("exchange_order_id", sa.Text()),
            sa.Column("execution_receipt", sa.Text()),
            sa.Column("action", postgresql.ENUM("buy", "sell",
                      name="order_action", create_type=False), nullable=False),
            sa.Column("type", sa.String(20), nullable=False),
            sa.Column("time_in_force", sa.String(20), server_default="gtc"),
            sa.Column("expiration_time", sa.DateTime(timezone=True)),
            sa.Column("requested_price", sa.Numeric(10, 6), nullable=False),
            sa.Column("requested_size", sa.Numeric(24, 8), nullable=False),
            sa.Column("filled_price", sa.Numeric(10, 6), server_default="0"),
            sa.Column("filled_size", sa.Numeric(24, 8), server_default="0"),
            sa.Column("maker_fee_usd", sa.Numeric(14, 6), server_default="0"),
            sa.Column("taker_fee_usd", sa.Numeric(14, 6), server_default="0"),
            sa.Column("settlement_profit_fee", sa.Numeric(14, 6), server_default="0"),
            sa.Column("status", postgresql.ENUM(
                "pending_submission", "live", "partial", "matched_pending",
                "filled", "canceled", "expired", "rejected", "failed",
                name="order_status", create_type=False), nullable=False,
                server_default="pending_submission"),
            sa.Column("error_message", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"),
                      nullable=False),
        )
        op.create_index("idx_user_orders_portfolio", "user_orders",
                        ["user_id", "status", sa.text("created_at DESC")])

    # -------------------------------------------------------- sports_metadata  (NEW)
    if not _table_exists(insp, "sports_metadata"):
        op.create_table(
            "sports_metadata",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("exchange", postgresql.ENUM("kalshi", "polymarket",
                      name="exchange_type", create_type=False), nullable=False),
            sa.Column("sport", sa.Text(), nullable=False),
            sa.Column("series_slug", sa.Text(), nullable=True),
            sa.Column("resolution_url", sa.Text(), nullable=True),
            sa.Column("tag_ids", postgresql.ARRAY(sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.UniqueConstraint("exchange", "sport", name="uq_sports_metadata_exchange_sport"),
        )
        op.create_index("idx_sports_metadata_tag_ids_gin", "sports_metadata",
                        ["tag_ids"], postgresql_using="gin")

    # ---- Drop Polymarket-specific tables if they exist from old init_db runs ----
    for stale_table in ("polymarket_tags", "polymarket_sports_metadata"):
        if _table_exists(insp, stale_table):
            op.drop_table(stale_table)

    # ---- Drop legacy category/tag tables if they somehow still exist ----
    for legacy in ("categories", "tags"):
        if _table_exists(insp, legacy):
            op.execute(f"DROP TABLE IF EXISTS {legacy} CASCADE")


# ---------------------------------------------------------------------------
# downgrade — drops only the tables/columns added in this migration
# ---------------------------------------------------------------------------

def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    # Drop sports_metadata
    if _table_exists(insp, "sports_metadata"):
        op.drop_table("sports_metadata")

    # Revert platform_tags delta (only if the table exists)
    if _table_exists(insp, "platform_tags"):
        cols = {c["name"] for c in insp.get_columns("platform_tags")}
        if "platform_metadata" in cols:
            op.drop_column("platform_tags", "platform_metadata")
        if "force_hide" in cols:
            op.drop_column("platform_tags", "force_hide")
        if "parent_ids" in cols:
            op.drop_index("idx_platform_tags_parent_ids_gin", table_name="platform_tags")
            op.drop_column("platform_tags", "parent_ids")
        # Restore original single-parent column
        if "parent_id" not in cols:
            op.add_column("platform_tags",
                          sa.Column("parent_id", sa.String(), nullable=True))
