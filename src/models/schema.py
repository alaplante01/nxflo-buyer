"""SQLAlchemy models for persisting buying agent state."""

from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, Integer, String, Text, JSON
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from src.config import settings


class Base(AsyncAttrs, DeclarativeBase):
    pass


class OperationRecord(Base):
    """Persisted operation for crash recovery and history."""

    __tablename__ = "operations"

    id = Column(String, primary_key=True)
    operation_type = Column(String, nullable=False)
    seller_name = Column(String, nullable=False)
    seller_url = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")
    task_id = Column(String, nullable=True)
    context_id = Column(String, nullable=True)
    media_buy_id = Column(String, nullable=True)
    buyer_ref = Column(String, nullable=True, index=True)
    request_data = Column(JSON, default=dict)
    response_data = Column(JSON, default=dict)
    error = Column(Text, nullable=True)
    poll_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
    updated_at = Column(DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))
    # Phase 2 fields
    application_context = Column(JSON, default=dict)
    webhook_config = Column(JSON, nullable=True)
    input_required_message = Column(Text, nullable=True)
    input_required_data = Column(JSON, nullable=True)


class SellerRecord(Base):
    """Cached seller agent info from discovery."""

    __tablename__ = "sellers"

    url = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    agent_type = Column(String, default="sales")
    tools = Column(JSON, default=list)
    status = Column(String, default="unknown")
    source = Column(String, default="config")
    last_probed = Column(DateTime, nullable=True)
    # Capabilities from get_adcp_capabilities
    adcp_versions = Column(JSON, default=list)
    supported_protocols = Column(JSON, default=list)
    capabilities = Column(JSON, nullable=True)
    portfolio = Column(JSON, nullable=True)
    extensions_supported = Column(JSON, default=list)


class SellerCapabilityRecord(Base):
    """Cached get_adcp_capabilities response for a seller agent."""

    __tablename__ = "seller_capabilities"

    url = Column(String, primary_key=True)
    adcp_versions = Column(JSON, default=list)
    supported_protocols = Column(JSON, default=list)
    media_buy_features = Column(JSON, nullable=True)
    media_buy_execution = Column(JSON, nullable=True)
    media_buy_portfolio = Column(JSON, nullable=True)
    extensions_supported = Column(JSON, default=list)
    raw_response = Column(JSON, default=dict)
    last_fetched = Column(DateTime, nullable=True)


class WebhookEventRecord(Base):
    """Processed webhook events for idempotency."""

    __tablename__ = "webhook_events"

    event_id = Column(String, primary_key=True)
    task_id = Column(String, nullable=False, index=True)
    operation_id = Column(String, nullable=True, index=True)
    status = Column(String, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    processed_at = Column(DateTime, default=lambda: datetime.now(UTC))
    raw_payload = Column(JSON, default=dict)


# Engine and session factory
engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    """Create all tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
