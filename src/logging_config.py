"""Structured JSON logging with request correlation IDs.

In production (non-TTY), emits JSON lines for structured log aggregation.
In development (TTY), uses human-readable format for convenience.
"""

import contextvars
import logging
import sys
import uuid

from pythonjsonlogger.json import JsonFormatter

# Context var for per-request correlation ID
correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


class CorrelationJsonFormatter(JsonFormatter):
    """JSON formatter that injects correlation_id into every log record."""

    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        log_record["correlation_id"] = correlation_id.get("")
        log_record["service"] = "nxflo-buyer"


def setup_logging(force_json: bool = False) -> None:
    """Configure root logger.

    Uses JSON format in production (non-TTY or force_json=True),
    human-readable format in development (TTY).
    """
    use_json = force_json or not sys.stderr.isatty()

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Remove existing handlers to avoid duplicates
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)

    if use_json:
        formatter = CorrelationJsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
        )
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [%(correlation_id)s] %(message)s",
            defaults={"correlation_id": ""},
        )

    handler.setFormatter(formatter)
    root.addHandler(handler)


def new_correlation_id() -> str:
    """Generate and set a new correlation ID for the current context."""
    cid = uuid.uuid4().hex[:12]
    correlation_id.set(cid)
    return cid
