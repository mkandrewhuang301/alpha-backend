"""add_series_platform_metadata_and_category_jsonb

Revision ID: c1042e1ddcd2
Revises: 96703fb6b631
Create Date: 2026-03-11 11:40:08.388866

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c1042e1ddcd2'
down_revision: Union[str, Sequence[str], None] = '96703fb6b631'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Change series.category from TEXT to JSONB (now stores list of labels)
    op.alter_column('series', 'category',
               existing_type=sa.TEXT(),
               type_=postgresql.JSONB(astext_type=sa.Text()),
               existing_nullable=True,
               postgresql_using="to_jsonb(ARRAY[category])")

    # Add platform_metadata to series (stores slug/ticker for Polymarket FE linking)
    op.add_column('series',
        sa.Column('platform_metadata', postgresql.JSONB(astext_type=sa.Text()),
                  nullable=True, server_default='{}'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('series', 'platform_metadata')
    op.alter_column('series', 'category',
               existing_type=postgresql.JSONB(astext_type=sa.Text()),
               type_=sa.TEXT(),
               existing_nullable=True)
