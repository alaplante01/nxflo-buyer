"""Initial schema — operations, sellers, capabilities, webhook events.

Revision ID: 001
Revises:
Create Date: 2025-01-01 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("operation_type", sa.String(), nullable=False),
        sa.Column("seller_name", sa.String(), nullable=False),
        sa.Column("seller_url", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("context_id", sa.String(), nullable=True),
        sa.Column("media_buy_id", sa.String(), nullable=True),
        sa.Column("buyer_ref", sa.String(), nullable=True, index=True),
        sa.Column("request_data", sa.JSON(), server_default="{}"),
        sa.Column("response_data", sa.JSON(), server_default="{}"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("poll_count", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
        sa.Column("application_context", sa.JSON(), server_default="{}"),
        sa.Column("webhook_config", sa.JSON(), nullable=True),
        sa.Column("input_required_message", sa.Text(), nullable=True),
        sa.Column("input_required_data", sa.JSON(), nullable=True),
    )

    op.create_table(
        "sellers",
        sa.Column("url", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("agent_type", sa.String(), server_default="sales"),
        sa.Column("tools", sa.JSON(), server_default="[]"),
        sa.Column("status", sa.String(), server_default="unknown"),
        sa.Column("source", sa.String(), server_default="config"),
        sa.Column("last_probed", sa.DateTime(), nullable=True),
        sa.Column("adcp_versions", sa.JSON(), server_default="[]"),
        sa.Column("supported_protocols", sa.JSON(), server_default="[]"),
        sa.Column("capabilities", sa.JSON(), nullable=True),
        sa.Column("portfolio", sa.JSON(), nullable=True),
        sa.Column("extensions_supported", sa.JSON(), server_default="[]"),
    )

    op.create_table(
        "seller_capabilities",
        sa.Column("url", sa.String(), primary_key=True),
        sa.Column("adcp_versions", sa.JSON(), server_default="[]"),
        sa.Column("supported_protocols", sa.JSON(), server_default="[]"),
        sa.Column("media_buy_features", sa.JSON(), nullable=True),
        sa.Column("media_buy_execution", sa.JSON(), nullable=True),
        sa.Column("media_buy_portfolio", sa.JSON(), nullable=True),
        sa.Column("extensions_supported", sa.JSON(), server_default="[]"),
        sa.Column("raw_response", sa.JSON(), server_default="{}"),
        sa.Column("last_fetched", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "webhook_events",
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), nullable=False, index=True),
        sa.Column("operation_id", sa.String(), nullable=True, index=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime()),
        sa.Column("raw_payload", sa.JSON(), server_default="{}"),
    )


def downgrade() -> None:
    op.drop_table("webhook_events")
    op.drop_table("seller_capabilities")
    op.drop_table("sellers")
    op.drop_table("operations")
