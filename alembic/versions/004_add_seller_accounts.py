"""Add seller_accounts table for AdCP 3.2 accounts (list_accounts / sync_accounts).

Revision ID: 004
Revises: 003
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "seller_accounts",
        sa.Column("seller_url", sa.String(), primary_key=True),
        sa.Column("account_key", sa.String(), primary_key=True),
        sa.Column("account_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("brand", sa.JSON(), nullable=True),
        sa.Column("operator", sa.String(), nullable=True),
        sa.Column("sandbox", sa.Integer(), server_default="0"),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("billing", sa.String(), nullable=True),
        sa.Column("account_mode", sa.String(), nullable=True),
        sa.Column("raw", sa.JSON(), server_default="{}"),
        sa.Column("updated_at", sa.DateTime()),
    )


def downgrade() -> None:
    op.drop_table("seller_accounts")
