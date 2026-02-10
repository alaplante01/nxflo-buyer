"""Async operation tracker for AdCP task lifecycle.

Tracks media buy operations through the AdCP state machine:
    submitted -> working -> completed | failed | input-required

Pattern from: adcp/docs/building/implementation/orchestrator-design.mdx
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"  # Local only: not yet sent to seller
    SUBMITTED = "submitted"  # Seller accepted, long-running (hours/days)
    WORKING = "working"  # Seller processing (< 120s)
    INPUT_REQUIRED = "input-required"  # Needs human approval
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    AUTH_REQUIRED = "auth-required"


@dataclass
class TrackedOperation:
    """A tracked AdCP operation with full lifecycle state."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    operation_type: str = ""  # create_media_buy, sync_creatives, etc.
    seller_name: str = ""
    seller_url: str = ""
    status: TaskStatus = TaskStatus.PENDING
    task_id: str | None = None  # Remote task_id from seller
    context_id: str | None = None  # MCP context_id for session continuity
    media_buy_id: str | None = None  # Seller's media buy ID (after completion)
    buyer_ref: str | None = None  # Our idempotency key
    request_data: dict = field(default_factory=dict)
    response_data: dict = field(default_factory=dict)
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    poll_count: int = 0


class OperationTracker:
    """Tracks all in-flight and completed AdCP operations.

    MVP: in-memory dict. Will migrate to SQLite/PostgreSQL.
    """

    def __init__(self):
        self._operations: dict[str, TrackedOperation] = {}

    def create(
        self,
        operation_type: str,
        seller_name: str,
        seller_url: str,
        buyer_ref: str,
        request_data: dict,
    ) -> TrackedOperation:
        """Create a new tracked operation."""
        op = TrackedOperation(
            operation_type=operation_type,
            seller_name=seller_name,
            seller_url=seller_url,
            buyer_ref=buyer_ref,
            request_data=request_data,
        )
        self._operations[op.id] = op
        logger.info(f"Tracking operation {op.id}: {operation_type} on {seller_name}")
        return op

    def update_from_response(self, op_id: str, response: dict) -> TrackedOperation:
        """Update operation state from a seller response."""
        op = self._operations[op_id]
        op.updated_at = datetime.now(UTC)
        op.response_data = response

        # Extract status from response
        status_str = response.get("status", "").lower().replace("_", "-")
        try:
            op.status = TaskStatus(status_str)
        except ValueError:
            if "error" in response or "errors" in response:
                op.status = TaskStatus.FAILED
                op.error = str(response.get("error") or response.get("errors"))
            else:
                op.status = TaskStatus.COMPLETED

        # Extract IDs
        if "task_id" in response:
            op.task_id = response["task_id"]
        if "context_id" in response:
            op.context_id = response["context_id"]
        if "media_buy_id" in response:
            op.media_buy_id = response["media_buy_id"]

        logger.info(f"Operation {op_id} -> {op.status.value}")
        return op

    def mark_failed(self, op_id: str, error: str) -> TrackedOperation:
        """Mark an operation as failed due to local error."""
        op = self._operations[op_id]
        op.status = TaskStatus.FAILED
        op.error = error
        op.updated_at = datetime.now(UTC)
        return op

    def get(self, op_id: str) -> TrackedOperation | None:
        return self._operations.get(op_id)

    def get_by_buyer_ref(self, buyer_ref: str) -> TrackedOperation | None:
        for op in self._operations.values():
            if op.buyer_ref == buyer_ref:
                return op
        return None

    def get_pending(self) -> list[TrackedOperation]:
        """Get operations that need polling (submitted or working)."""
        return [
            op
            for op in self._operations.values()
            if op.status in (TaskStatus.SUBMITTED, TaskStatus.WORKING)
        ]

    def list_all(self) -> list[TrackedOperation]:
        return sorted(self._operations.values(), key=lambda o: o.created_at, reverse=True)
