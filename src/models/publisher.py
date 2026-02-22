"""Publisher model for the publisher acquisition pipeline."""

from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, String

from src.models.schema import Base


class PublisherRecord(Base):
    """A publisher who has installed the Nexflo Prebid wrapper."""

    __tablename__ = "publishers"

    id = Column(String, primary_key=True)          # "pub_<token>"
    site_id = Column(String, nullable=False, index=True, unique=True)  # "site_<token>"
    name = Column(String, nullable=False)
    domain = Column(String, nullable=False, index=True, unique=True)
    email = Column(String, nullable=True)
    status = Column(String, nullable=False, default="active")
    created_at = Column(DateTime, default=lambda: datetime.now(UTC))
