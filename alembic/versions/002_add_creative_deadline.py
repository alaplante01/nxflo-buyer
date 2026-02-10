"""Add creative_deadline column to operations table.

Revision ID: 002
Revises: 001
Create Date: 2026-02-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("operations", sa.Column("creative_deadline", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("operations", "creative_deadline")
