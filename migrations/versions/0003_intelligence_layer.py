"""intelligence_layer

Adds intelligence synchronization engine: pgvector extension, external_intelligence
table with VECTOR(1536) embeddings, intelligence-market mapping, source credibility
tracking, and VECTOR columns on events/markets for semantic search.

Idempotent — safe to run against any DB state.

Revision ID: 0003_intelligence_layer
Revises: 0002_social_layer
Create Date: 2026-03-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql

revision: str = "0003_intelligence_layer"
down_revision: Union[str, Sequence[str], None] = "0002_social_layer"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Helpers (same pattern as 0002)
# ---------------------------------------------------------------------------

def _table_exists(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _col_exists(inspector, table: str, col: str) -> bool:
    return any(c["name"] == col for c in inspector.get_columns(table))


def _constraint_exists(bind, constraint_name: str, table: str) -> bool:
    row = bind.execute(
        text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE constraint_name = :name AND table_name = :tbl"
        ),
        {"name": constraint_name, "tbl": table},
    ).fetchone()
    return row is not None


def _index_exists_pg(bind, index_name: str) -> bool:
    row = bind.execute(
        text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
        {"name": index_name},
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    # --------------------------------------------------------- extensions
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --------------------------------------------------------- ENUMs
    for ddl in [
        "DO $$ BEGIN CREATE TYPE source_domain_type AS ENUM ('news', 'social', 'sports', 'crypto', 'weather'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        "DO $$ BEGIN CREATE TYPE impact_level_type AS ENUM ('high', 'medium', 'low'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        "DO $$ BEGIN CREATE TYPE nlp_status_type AS ENUM ('pending', 'partial', 'complete', 'failed'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
    ]:
        op.execute(ddl)

    # ------------------------------------------------- external_intelligence
    if not _table_exists(insp, "external_intelligence"):
        op.create_table(
            "external_intelligence",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("source_domain", postgresql.ENUM(
                "news", "social", "sports", "crypto", "weather",
                name="source_domain_type", create_type=False), nullable=False),
            sa.Column("source_name", sa.String(255), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("raw_text", sa.Text(), nullable=False),
            sa.Column("url", sa.Text(), nullable=True),
            sa.Column("author", sa.String(255), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("metadata", postgresql.JSONB(), server_default="{}"),
            sa.Column("impact_level", postgresql.ENUM(
                "high", "medium", "low",
                name="impact_level_type", create_type=False),
                nullable=False, server_default="low"),
            sa.Column("nlp_status", postgresql.ENUM(
                "pending", "partial", "complete", "failed",
                name="nlp_status_type", create_type=False),
                nullable=False, server_default="pending"),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("is_deleted", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.UniqueConstraint("content_hash", name="uq_external_intelligence_content_hash"),
        )
        # Vector column added separately (requires pgvector extension)
        op.execute(text(
            "ALTER TABLE external_intelligence ADD COLUMN embedding vector(1536)"
        ))
        # Indexes
        op.create_index(
            "idx_intel_source_published",
            "external_intelligence",
            ["source_domain", sa.text("published_at DESC")],
        )
        op.create_index("idx_intel_impact_level", "external_intelligence", ["impact_level"])
        op.create_index("idx_intel_nlp_status", "external_intelligence", ["nlp_status"])
        op.create_index(
            "idx_intel_metadata_gin", "external_intelligence",
            ["metadata"], postgresql_using="gin",
        )
        # HNSW index for vector similarity search (partial: only non-deleted with embeddings)
        op.execute(text(
            "CREATE INDEX idx_intel_embedding_hnsw ON external_intelligence "
            "USING hnsw (embedding vector_cosine_ops) "
            "WHERE is_deleted = false AND embedding IS NOT NULL"
        ))

    # ----------------------------------------- intelligence_market_mapping
    if not _table_exists(insp, "intelligence_market_mapping"):
        op.create_table(
            "intelligence_market_mapping",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("intelligence_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("external_intelligence.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("event_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("events.id", ondelete="CASCADE"),
                      nullable=True),
            sa.Column("market_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("markets.id", ondelete="CASCADE"),
                      nullable=True),
            sa.Column("confidence_score", sa.Numeric(5, 3), nullable=False),
            sa.Column("sentiment_polarity", sa.Numeric(5, 3), server_default="0.000"),
            sa.Column("matched_tags", postgresql.ARRAY(sa.Text()), server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.CheckConstraint(
                "event_id IS NOT NULL OR market_id IS NOT NULL",
                name="ck_mapping_has_target",
            ),
            sa.UniqueConstraint(
                "intelligence_id", "event_id", "market_id",
                name="uq_intel_mapping_triple",
            ),
        )
        op.create_index("idx_mapping_intelligence", "intelligence_market_mapping", ["intelligence_id"])
        op.create_index("idx_mapping_event", "intelligence_market_mapping", ["event_id"])
        op.create_index("idx_mapping_market", "intelligence_market_mapping", ["market_id"])

    # ------------------------------------------------- source_credibility
    if not _table_exists(insp, "source_credibility"):
        op.create_table(
            "source_credibility",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("source_domain", postgresql.ENUM(
                "news", "social", "sports", "crypto", "weather",
                name="source_domain_type", create_type=False), nullable=False),
            sa.Column("source_name", sa.String(255), nullable=False),
            sa.Column("credibility_score", sa.Numeric(5, 3), server_default="0.500"),
            sa.Column("total_predictions", sa.Integer(), server_default="0"),
            sa.Column("correct_predictions", sa.Integer(), server_default="0"),
            sa.Column("avg_lead_time_minutes", sa.Numeric(10, 2), nullable=True),
            sa.Column("domain_scores", postgresql.JSONB(), server_default="{}"),
            sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.UniqueConstraint("source_domain", "source_name", name="uq_source_credibility_domain_name"),
        )

    # ---------------------------------------- ALTER events: add embedding
    if _table_exists(insp, "events") and not _col_exists(insp, "events", "embedding"):
        op.execute(text("ALTER TABLE events ADD COLUMN embedding vector(1536)"))
        op.execute(text(
            "CREATE INDEX idx_events_embedding_hnsw ON events "
            "USING hnsw (embedding vector_cosine_ops) "
            "WHERE embedding IS NOT NULL"
        ))

    # ---------------------------------------- ALTER markets: add embedding
    if _table_exists(insp, "markets") and not _col_exists(insp, "markets", "embedding"):
        op.execute(text("ALTER TABLE markets ADD COLUMN embedding vector(1536)"))
        op.execute(text(
            "CREATE INDEX idx_markets_embedding_hnsw ON markets "
            "USING hnsw (embedding vector_cosine_ops) "
            "WHERE embedding IS NOT NULL"
        ))

    # ---------------------------------------- Seed default source credibility rows
    for domain, name in [
        ("news", "newsapi"),
        ("sports", "sportradar"),
        ("sports", "rotowire"),
    ]:
        op.execute(text(
            "INSERT INTO source_credibility (id, source_domain, source_name) "
            "VALUES (gen_random_uuid(), :domain, :name) "
            "ON CONFLICT (source_domain, source_name) DO NOTHING"
        ).bindparams(domain=domain, name=name))

    # ---- intelligence_market_mapping: event_id NOT NULL ----
    # Original schema had event_id nullable (either event OR market required).
    # Redesigned: event_id is always required for FE correlation; market_id is
    # an optional future field for market-specific intelligence.
    bind.execute(text(
        "DELETE FROM intelligence_market_mapping WHERE event_id IS NULL"
    ))
    # Drop old check constraint if present, then make event_id NOT NULL
    if _constraint_exists(bind, "ck_mapping_has_target", "intelligence_market_mapping"):
        op.drop_constraint("ck_mapping_has_target", "intelligence_market_mapping")
    # Check if event_id is still nullable before altering
    col_info = next(
        (c for c in inspect(bind).get_columns("intelligence_market_mapping")
         if c["name"] == "event_id"),
        None,
    )
    if col_info and col_info.get("nullable", True):
        op.alter_column("intelligence_market_mapping", "event_id", nullable=False)

    # ---- Fix idx_messages_market_ref partial index from migration 0002 ----
    # The original index only covered type IN ('market', 'system') but the
    # resolution alert worker queries type IN ('market', 'trade'). Rebuild
    # the index to include 'trade' so resolution lookups use the index.
    if _index_exists_pg(bind, "idx_messages_market_ref"):
        op.execute("DROP INDEX idx_messages_market_ref")
    op.execute(
        "CREATE INDEX idx_messages_market_ref ON group_messages "
        "((metadata->>'market_id')) WHERE type IN ('market', 'system', 'trade')"
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    # Drop new tables
    for tbl in ["intelligence_market_mapping", "external_intelligence", "source_credibility"]:
        if _table_exists(insp, tbl):
            op.drop_table(tbl)

    # Remove embedding columns from events/markets
    if _table_exists(insp, "events") and _col_exists(insp, "events", "embedding"):
        op.execute(text("DROP INDEX IF EXISTS idx_events_embedding_hnsw"))
        op.drop_column("events", "embedding")

    if _table_exists(insp, "markets") and _col_exists(insp, "markets", "embedding"):
        op.execute(text("DROP INDEX IF EXISTS idx_markets_embedding_hnsw"))
        op.drop_column("markets", "embedding")

    # Drop enums
    for ddl in [
        "DROP TYPE IF EXISTS nlp_status_type",
        "DROP TYPE IF EXISTS impact_level_type",
        "DROP TYPE IF EXISTS source_domain_type",
    ]:
        op.execute(ddl)

    # Drop pgvector extension (careful — only if no other tables use it)
    op.execute("DROP EXTENSION IF EXISTS vector")
