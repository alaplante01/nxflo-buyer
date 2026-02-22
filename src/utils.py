"""Shared utilities for the Nexflo Buyer service."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return current UTC time as a naive datetime (no tzinfo).

    asyncpg rejects tz-aware datetimes for TIMESTAMP WITHOUT TIME ZONE columns.
    """
    return datetime.now(UTC).replace(tzinfo=None)
