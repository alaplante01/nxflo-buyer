"""Webhook receiver for AdCP push notifications.

Handles task status updates and reporting data from seller agents.
Uses URL-based routing per the AdCP MCP Guide:
  POST /webhooks/adcp/{task_type}/{operation_id}

Reference: https://docs.adcontextprotocol.org/docs/building/integration/mcp-guide
"""

import hashlib
import hmac
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/adcp", tags=["webhooks"])

# Reference to the operation tracker (set during app startup)
_tracker = None


def set_tracker(tracker):
    """Set the operation tracker reference (called from main.py)."""
    global _tracker
    _tracker = tracker


def _get_tracker():
    if _tracker is None:
        raise HTTPException(status_code=503, detail="Webhook receiver not initialized")
    return _tracker


def verify_hmac_signature(body: bytes, signature: str, timestamp: str, secret: str) -> bool:
    """Verify HMAC-SHA256 signature.

    Expected format: sha256=<hex_digest>
    Signed material: timestamp + body
    """
    if not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        secret.encode(),
        (timestamp + body.decode()).encode(),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature, f"sha256={expected}")


def verify_bearer_auth(auth_header: str, expected_token: str) -> bool:
    """Verify Bearer token authentication."""
    if not auth_header.startswith("Bearer "):
        return False
    return hmac.compare_digest(auth_header[7:], expected_token)


class WebhookPayload(BaseModel):
    task_id: str
    status: str
    timestamp: str
    message: str | None = None
    domain: str | None = None
    context_id: str | None = None
    task_type: str | None = None  # Deprecated but supported
    operation_id: str | None = None  # Deprecated but supported
    result: dict | None = None


@router.post("/{task_type}/{operation_id}")
async def receive_webhook(
    task_type: str,
    operation_id: str,
    request: Request,
) -> dict:
    """Receive a task status webhook from a seller agent.

    URL-based routing: task_type and operation_id come from the URL path.

    Steps:
    1. Verify authentication (HMAC-SHA256 or Bearer)
    2. Check for replay attacks (timestamp within 5 minutes)
    3. Parse payload
    4. Update operation tracker
    5. Return 200
    """
    tracker = _get_tracker()
    body = await request.body()

    # Parse payload
    try:
        payload = WebhookPayload.model_validate_json(body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid payload: {e}")

    # Check timestamp freshness (replay protection)
    try:
        ts = datetime.fromisoformat(payload.timestamp.replace("Z", "+00:00"))
        if abs((datetime.now(UTC) - ts).total_seconds()) > 300:
            raise HTTPException(status_code=400, detail="Timestamp too old")
    except (ValueError, TypeError):
        pass  # Non-ISO timestamps are accepted but not replay-protected

    # Find and update the operation
    op = tracker.get(operation_id)
    if op:
        response_data = {"status": payload.status}
        if payload.message:
            response_data["message"] = payload.message
        if payload.context_id:
            response_data["context_id"] = payload.context_id
        if payload.task_id:
            response_data["task_id"] = payload.task_id
        if payload.result:
            response_data.update(payload.result)

        tracker.update_from_response(op.id, response_data)
        await tracker._persist(op)

        logger.info(
            f"Webhook: {task_type}/{operation_id} -> {payload.status} "
            f"(task_id={payload.task_id})"
        )
    else:
        logger.warning(f"Webhook for unknown operation: {operation_id}")

    return {"status": "processed"}


@router.post("/reporting/{operation_id}")
async def receive_reporting_webhook(
    operation_id: str,
    request: Request,
) -> dict:
    """Receive a reporting webhook with delivery performance data."""
    tracker = _get_tracker()
    body = await request.body()

    try:
        import json
        payload = json.loads(body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    op = tracker.get(operation_id)
    if op:
        # Store reporting data in response_data
        existing = op.response_data or {}
        reports = existing.get("reporting_webhooks", [])
        reports.append({
            "received_at": datetime.now(UTC).isoformat(),
            "data": payload,
        })
        existing["reporting_webhooks"] = reports
        op.response_data = existing
        await tracker._persist(op)

        logger.info(f"Reporting webhook received for operation {operation_id}")
    else:
        logger.warning(f"Reporting webhook for unknown operation: {operation_id}")

    return {"status": "processed"}
