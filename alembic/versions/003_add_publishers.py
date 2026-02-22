"""Add publishers table for publisher acquisition pipeline.

Revision ID: 003
Revises: 002
Create Date: 2026-02-19
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "publishers",
        sa.Column("id", sa.String(), primary_key=True),          # pub_<token>
        sa.Column("site_id", sa.String(), nullable=False),        # site_<token> — used in data-site-id
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("domain", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_publishers_site_id", "publishers", ["site_id"], unique=True)
    op.create_index("ix_publishers_domain", "publishers", ["domain"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_publishers_domain", table_name="publishers")
    op.drop_index("ix_publishers_site_id", table_name="publishers")
    op.drop_table("publishers")
