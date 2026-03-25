"""magic_auth_wallet

Replace password_hash with eoa_address on users table (Magic auth — no password needed).
Add safe_address to accounts table for Polymarket wallet.

Revision ID: 0002_magic_auth_wallet
Revises: 0001_complete_schema
Create Date: 2026-03-15

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0002_magic_auth_wallet"
down_revision: Union[str, Sequence[str], None] = "0001_complete_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    insp = inspect(op.get_bind())

    # -- users: drop password_hash, add eoa_address
    if _column_exists(insp, "users", "password_hash"):
        op.drop_column("users", "password_hash")

    if not _column_exists(insp, "users", "eoa_address"):
        op.add_column("users", sa.Column("eoa_address", sa.Text(), nullable=True))
        op.create_unique_constraint("uq_users_eoa_address", "users", ["eoa_address"])

    # -- users: add privy_did
    if not _column_exists(insp, "users", "privy_did"):
        op.add_column("users", sa.Column("privy_did", sa.Text(), nullable=True))
        op.create_unique_constraint("uq_users_privy_did", "users", ["privy_did"])

    # -- accounts: add safe_address
    if not _column_exists(insp, "accounts", "safe_address"):
        op.add_column("accounts", sa.Column("safe_address", sa.Text(), nullable=True))
        op.create_unique_constraint("uq_accounts_safe_address", "accounts", ["safe_address"])


def downgrade() -> None:
    insp = inspect(op.get_bind())

    # -- accounts: remove safe_address
    if _column_exists(insp, "accounts", "safe_address"):
        op.drop_constraint("uq_accounts_safe_address", "accounts", type_="unique")
        op.drop_column("accounts", "safe_address")

    # -- users: remove privy_did
    if _column_exists(insp, "users", "privy_did"):
        op.drop_constraint("uq_users_privy_did", "users", type_="unique")
        op.drop_column("users", "privy_did")

    # -- users: remove eoa_address, restore password_hash
    if _column_exists(insp, "users", "eoa_address"):
        op.drop_constraint("uq_users_eoa_address", "users", type_="unique")
        op.drop_column("users", "eoa_address")

    if not _column_exists(insp, "users", "password_hash"):
        op.add_column("users", sa.Column("password_hash", sa.Text(), nullable=True))
