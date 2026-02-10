"""SQLAlchemy models for persisting buying agent state."""

from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Text, JSON
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


# Engine and session factory
engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    """Create all tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
