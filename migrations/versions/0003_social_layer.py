"""social_layer

Adds social coordination layer: user profiles, follows, groups, memberships,
and group messages. Idempotent — safe to run against any DB state.

Revision ID: 0003_social_layer
Revises: 0002_magic_auth_wallet
Create Date: 2026-03-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql

revision: str = "0003_social_layer"
down_revision: Union[str, Sequence[str], None] = "0002_magic_auth_wallet"
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


def _index_exists_pg(bind, index_name: str) -> bool:
    """Live check via pg_indexes — reflects current transaction state."""
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

    # ------------------------------------------------------------------ ENUMs
    for ddl in [
        "DO $$ BEGIN CREATE TYPE group_access_type AS ENUM ('private', 'public'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        "DO $$ BEGIN CREATE TYPE group_member_role AS ENUM ('owner', 'admin', 'member', 'follower'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
        "DO $$ BEGIN CREATE TYPE message_type AS ENUM ('text', 'image', 'trade', 'market', 'article', 'profile', 'system'); EXCEPTION WHEN duplicate_object THEN NULL; END $$;",
    ]:
        op.execute(ddl)

    # ---------------------------------------------------------- users (delta)
    if _table_exists(insp, "users"):
        cols = {c["name"] for c in insp.get_columns("users")}

        if "supabase_uid" not in cols:
            op.add_column("users", sa.Column("supabase_uid", sa.Text(), nullable=True))
        if "username" not in cols:
            op.add_column("users", sa.Column("username", sa.String(50), nullable=True))
        if "display_name" not in cols:
            op.add_column("users", sa.Column("display_name", sa.String(100), nullable=True))
        if "avatar_url" not in cols:
            op.add_column("users", sa.Column("avatar_url", sa.Text(), nullable=True))
        if "bio" not in cols:
            op.add_column("users", sa.Column("bio", sa.Text(), nullable=True))
        if "is_verified" not in cols:
            op.add_column("users", sa.Column(
                "is_verified", sa.Boolean(), server_default="false", nullable=False
            ))

        # Make password_hash nullable if it still exists (0002 may have already dropped it)
        if _col_exists(insp, "users", "password_hash"):
            op.execute(text(
                "ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL"
            ))

        # Unique constraints (safe to re-run: guarded by _constraint_exists)
        if not _constraint_exists(bind, "uq_users_supabase_uid", "users"):
            op.create_unique_constraint("uq_users_supabase_uid", "users", ["supabase_uid"])
        if not _constraint_exists(bind, "uq_users_username", "users"):
            op.create_unique_constraint("uq_users_username", "users", ["username"])

        # Indexes
        if not _index_exists_pg(bind, "idx_users_supabase_uid"):
            op.create_index("idx_users_supabase_uid", "users", ["supabase_uid"])
        if not _index_exists_pg(bind, "idx_users_username"):
            op.create_index("idx_users_username", "users", ["username"])

        # pg_trgm index for username prefix search
        op.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        if not _index_exists_pg(bind, "idx_users_username_trgm"):
            op.execute(text(
                "CREATE INDEX idx_users_username_trgm ON users "
                "USING gin (username gin_trgm_ops)"
            ))

    # --------------------------------------------------------------- user_follows
    if not _table_exists(insp, "user_follows"):
        op.create_table(
            "user_follows",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("follower_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("followed_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.UniqueConstraint("follower_id", "followed_id", name="uq_user_follows_pair"),
        )
        op.create_index("idx_user_follows_follower", "user_follows", ["follower_id"])
        op.create_index("idx_user_follows_followed", "user_follows", ["followed_id"])

    # ------------------------------------------------------------------ groups
    if not _table_exists(insp, "groups"):
        op.create_table(
            "groups",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("slug", sa.String(100), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("avatar_url", sa.Text(), nullable=True),
            sa.Column("access_type", postgresql.ENUM(
                "private", "public", name="group_access_type", create_type=False),
                nullable=False, server_default="private"),
            sa.Column("owner_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("invite_code", sa.String(32), nullable=True),
            sa.Column("max_members", sa.Integer(), nullable=True),
            sa.Column("member_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("is_deleted", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.UniqueConstraint("slug", name="uq_groups_slug"),
        )
        op.create_index("idx_groups_slug", "groups", ["slug"], unique=True)
        # Partial unique index on invite_code where not null
        op.execute(text(
            "CREATE UNIQUE INDEX idx_groups_invite_code ON groups (invite_code) "
            "WHERE invite_code IS NOT NULL"
        ))
        # Partial index for public group discovery
        op.execute(text(
            "CREATE INDEX idx_groups_public_discovery ON groups (access_type, created_at) "
            "WHERE access_type = 'public' AND is_deleted = false"
        ))

    # --------------------------------------------------------- group_memberships
    if not _table_exists(insp, "group_memberships"):
        op.create_table(
            "group_memberships",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("group_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("role", postgresql.ENUM(
                "owner", "admin", "member", "follower",
                name="group_member_role", create_type=False),
                nullable=False, server_default="member"),
            sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.UniqueConstraint("group_id", "user_id", name="uq_group_memberships_pair"),
        )
        op.create_index("idx_memberships_user", "group_memberships", ["user_id"])
        op.create_index("idx_memberships_group", "group_memberships", ["group_id"])
        op.create_index("idx_memberships_group_role", "group_memberships", ["group_id", "role"])

    # ----------------------------------------------------------- group_messages
    if not _table_exists(insp, "group_messages"):
        op.create_table(
            "group_messages",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("group_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
            sa.Column("sender_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("type", postgresql.ENUM(
                "text", "image", "trade", "market", "article", "profile", "system",
                name="message_type", create_type=False), nullable=False),
            sa.Column("content", sa.Text(), nullable=True),
            sa.Column("metadata", postgresql.JSONB(), server_default="{}"),
            sa.Column("reply_to_id", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("group_messages.id", ondelete="SET NULL"), nullable=True),
            sa.Column("is_deleted", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        )
        op.create_index(
            "idx_messages_group_created", "group_messages",
            ["group_id", sa.text("created_at DESC")],
        )
        op.create_index("idx_messages_sender", "group_messages", ["sender_id"])
        op.create_index("idx_messages_metadata_gin", "group_messages",
                        ["metadata"], postgresql_using="gin")
        # Partial index for alert worker market_id lookups
        op.execute(text(
            "CREATE INDEX idx_messages_market_ref ON group_messages "
            "((metadata->>'market_id')) WHERE type IN ('market', 'system')"
        ))


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    for tbl in ["group_messages", "group_memberships", "groups", "user_follows"]:
        if _table_exists(insp, tbl):
            op.drop_table(tbl)

    if _table_exists(insp, "users"):
        cols = {c["name"] for c in insp.get_columns("users")}
        for col in ["supabase_uid", "username", "display_name", "avatar_url", "bio", "is_verified"]:
            if col in cols:
                op.drop_column("users", col)
        # Restore NOT NULL constraint if password_hash exists
        if _col_exists(insp, "users", "password_hash"):
            op.execute(text("ALTER TABLE users ALTER COLUMN password_hash SET NOT NULL"))

    for ddl in [
        "DROP TYPE IF EXISTS message_type",
        "DROP TYPE IF EXISTS group_member_role",
        "DROP TYPE IF EXISTS group_access_type",
    ]:
        op.execute(ddl)
