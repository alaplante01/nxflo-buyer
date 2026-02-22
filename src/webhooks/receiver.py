"""Webhook receiver for AdCP push notifications.

Handles task status updates and reporting data from seller agents.
Uses URL-based routing per the AdCP MCP Guide:
  POST /webhooks/adcp/{task_type}/{operation_id}

Reference: https://docs.adcontextprotocol.org/docs/building/integration/mcp-guide
"""

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.config import settings
from src.utils import utcnow

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
    Signed material: timestamp bytes + raw body bytes (no decode/re-encode round-trip)
    """
    if not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("utf-8") + body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature, f"sha256={expected}")


def verify_bearer_auth(auth_header: str, expected_token: str) -> bool:
    """Verify Bearer token authentication."""
    if not auth_header.startswith("Bearer "):
        return False
    return hmac.compare_digest(auth_header[7:], expected_token)


async def _verify_auth(request: Request, body: bytes, operation_id: str):
    """Verify webhook authentication using the scheme configured for this operation.

    Checks the operation's stored webhook_config to determine which auth scheme
    was negotiated, then verifies accordingly.
    Uses the stable webhook secret from config (generated once at startup if not set).
    """
    from src.webhooks.config import get_webhook_secret

    secret = get_webhook_secret()

    tracker = _get_tracker()
    op = tracker.get(operation_id)

    # Determine scheme from stored webhook config, fall back to global setting
    scheme = settings.webhook_auth_scheme
    if op and op.webhook_config:
        auth_cfg = op.webhook_config.get("authentication", {})
        schemes = auth_cfg.get("schemes", [])
        if schemes:
            scheme = schemes[0]
        stored_cred = auth_cfg.get("credentials")
        if stored_cred:
            secret = stored_cred

    if scheme == "HMAC-SHA256":
        # AdCP spec headers (preferred), fall back to legacy x-webhook-* headers
        signature = (
            request.headers.get("x-adcp-signature")
            or request.headers.get("x-webhook-signature", "")
        )
        timestamp = (
            request.headers.get("x-adcp-timestamp")
            or request.headers.get("x-webhook-timestamp", "")
        )
        if not signature or not timestamp:
            raise HTTPException(status_code=401, detail="Missing HMAC signature headers")
        if not verify_hmac_signature(body, signature, timestamp, secret):
            raise HTTPException(status_code=401, detail="Invalid HMAC signature")
    elif scheme.lower() == "bearer":
        auth_header = request.headers.get("authorization", "")
        if not verify_bearer_auth(auth_header, secret):
            raise HTTPException(status_code=401, detail="Invalid Bearer token")
    else:
        logger.warning(f"Unknown webhook auth scheme: {scheme}")


async def _check_idempotency(event_id: str, task_id: str, operation_id: str, status: str, timestamp: str) -> bool:
    """Check if this webhook event was already processed. Records it if new."""
    from src.models.schema import async_session, WebhookEventRecord

    ts = utcnow()
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        pass

    async with async_session() as session:
        existing = await session.get(WebhookEventRecord, event_id)
        if existing:
            logger.info(f"Duplicate webhook event {event_id}, skipping")
            return True

        session.add(WebhookEventRecord(
            event_id=event_id,
            task_id=task_id,
            operation_id=operation_id,
            status=status,
            timestamp=ts,
        ))
        await session.commit()
        return False


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
    2. Parse payload
    3. Check for replay attacks (timestamp within 5 minutes)
    4. Deduplicate via idempotency check
    5. Update operation tracker
    6. Return 200
    """
    tracker = _get_tracker()
    body = await request.body()

    # 1. Verify authentication
    await _verify_auth(request, body, operation_id)

    # 2. Parse payload
    try:
        payload = WebhookPayload.model_validate_json(body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid payload: {e}")

    # 3. Check timestamp freshness (replay protection)
    try:
        ts = datetime.fromisoformat(payload.timestamp.replace("Z", "+00:00"))
        if abs((datetime.now(UTC) - ts).total_seconds()) > 300:
            raise HTTPException(status_code=400, detail="Timestamp too old")
    except (ValueError, TypeError):
        pass  # Non-ISO timestamps are accepted but not replay-protected

    # 4. Idempotency check
    event_id = f"{payload.task_id}:{payload.status}:{payload.timestamp}"
    is_duplicate = await _check_idempotency(
        event_id=event_id,
        task_id=payload.task_id,
        operation_id=operation_id,
        status=payload.status,
        timestamp=payload.timestamp,
    )
    if is_duplicate:
        return {"status": "already_processed"}

    # 5. Find and update the operation
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

    # Verify authentication
    await _verify_auth(request, body, operation_id)

    try:
        payload = json.loads(body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    # Idempotency: deduplicate by report_id or payload hash
    report_id = payload.get("report_id") or hashlib.sha256(body).hexdigest()[:16]
    event_id = f"report:{operation_id}:{report_id}"
    is_duplicate = await _check_idempotency(
        event_id=event_id,
        task_id=operation_id,
        operation_id=operation_id,
        status="reporting",
        timestamp=payload.get("timestamp", datetime.now(UTC).isoformat()),
    )
    if is_duplicate:
        return {"status": "already_processed"}

    op = tracker.get(operation_id)
    if op:
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
