"""Async operation tracker for AdCP task lifecycle.

Tracks media buy operations through the AdCP state machine:
    submitted -> working -> completed | failed | input-required

Pattern from: adcp/docs/building/implementation/orchestrator-design.mdx

Dual-layer: in-memory dict for speed + SQLite for crash recovery.
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
    REJECTED = "rejected"  # Seller rejected the request
    UNKNOWN = "unknown"  # Unrecognized status from seller


# Polling intervals per status (seconds). None = don't poll.
POLLING_INTERVALS: dict[TaskStatus, int | None] = {
    TaskStatus.WORKING: 5,
    TaskStatus.SUBMITTED: 60,
    TaskStatus.INPUT_REQUIRED: None,  # Wait for human input, don't poll
}

# Terminal statuses — no further polling needed
TERMINAL_STATUSES = frozenset({
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELED,
    TaskStatus.REJECTED,
})


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
    # Phase 2 fields
    application_context: dict = field(default_factory=dict)  # Opaque context echoed by sellers
    webhook_config: dict | None = None  # pushNotificationConfig used for this op
    input_required_message: str | None = None  # Human-readable message for HITL
    input_required_data: dict | None = None  # Structured input requirements


class OperationTracker:
    """Tracks all in-flight and completed AdCP operations.

    In-memory dict with optional SQLite persistence.
    """

    def __init__(self):
        self._operations: dict[str, TrackedOperation] = {}
        self._db_enabled = False

    async def init_db(self):
        """Initialize database tables and load existing operations."""
        from src.models.schema import init_db, async_session, OperationRecord
        from sqlalchemy import select

        await init_db()
        self._db_enabled = True

        # Load existing operations from DB
        async with async_session() as session:
            result = await session.execute(select(OperationRecord))
            for row in result.scalars():
                op = TrackedOperation(
                    id=row.id,
                    operation_type=row.operation_type,
                    seller_name=row.seller_name,
                    seller_url=row.seller_url,
                    status=TaskStatus(row.status),
                    task_id=row.task_id,
                    context_id=row.context_id,
                    media_buy_id=row.media_buy_id,
                    buyer_ref=row.buyer_ref,
                    request_data=row.request_data or {},
                    response_data=row.response_data or {},
                    error=row.error,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                    poll_count=row.poll_count,
                )
                self._operations[op.id] = op

        logger.info(f"Loaded {len(self._operations)} operations from database")

    async def _persist(self, op: TrackedOperation):
        """Persist an operation to SQLite."""
        if not self._db_enabled:
            return

        from src.models.schema import async_session, OperationRecord
        from sqlalchemy import select

        async with async_session() as session:
            existing = await session.get(OperationRecord, op.id)
            if existing:
                existing.status = op.status.value
                existing.task_id = op.task_id
                existing.context_id = op.context_id
                existing.media_buy_id = op.media_buy_id
                existing.response_data = op.response_data
                existing.error = op.error
                existing.poll_count = op.poll_count
                existing.updated_at = op.updated_at
            else:
                session.add(OperationRecord(
                    id=op.id,
                    operation_type=op.operation_type,
                    seller_name=op.seller_name,
                    seller_url=op.seller_url,
                    status=op.status.value,
                    task_id=op.task_id,
                    context_id=op.context_id,
                    media_buy_id=op.media_buy_id,
                    buyer_ref=op.buyer_ref,
                    request_data=op.request_data,
                    response_data=op.response_data,
                    error=op.error,
                    poll_count=op.poll_count,
                    created_at=op.created_at,
                    updated_at=op.updated_at,
                ))
            await session.commit()

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

        # Extract HITL data when input is required
        if op.status == TaskStatus.INPUT_REQUIRED:
            op.input_required_message = response.get("message", "")
            op.input_required_data = {
                k: v for k, v in response.items()
                if k not in ("status", "message", "context_id", "task_id")
            }

        # Preserve application context if echoed back
        if "context" in response and isinstance(response["context"], dict):
            op.application_context = response["context"]

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
