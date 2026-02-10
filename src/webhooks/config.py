"""Build pushNotificationConfig and reporting_webhook objects for seller requests.

Per the AdCP MCP Guide, URL-based routing is the recommended pattern:
  url: {base_url}/webhooks/adcp/{task_type}/{operation_id}

Reference: https://docs.adcontextprotocol.org/docs/building/integration/mcp-guide
"""

import logging
import secrets

from src.config import settings

logger = logging.getLogger(__name__)

# Generate a stable secret once at startup if none is configured.
# This secret is used for all webhook registrations and verifications.
_runtime_secret: str | None = None


def get_webhook_secret() -> str:
    """Get the webhook HMAC secret, generating once at startup if needed."""
    global _runtime_secret
    if settings.webhook_secret:
        return settings.webhook_secret
    if _runtime_secret is None:
        _runtime_secret = secrets.token_urlsafe(32)
        logger.warning(
            "No NXFLO_WEBHOOK_SECRET configured — generated ephemeral secret. "
            "Webhooks will break across restarts. Set NXFLO_WEBHOOK_SECRET for production."
        )
    return _runtime_secret


def build_push_notification_config(
    task_type: str,
    operation_id: str,
    base_url: str | None = None,
    auth_scheme: str | None = None,
    secret: str | None = None,
) -> dict:
    """Build a pushNotificationConfig for an MCP tool call.

    Uses URL-based routing as recommended by the protocol:
    url: {base_url}/webhooks/adcp/{task_type}/{operation_id}
    """
    url = base_url or settings.webhook_base_url
    if not url:
        raise ValueError("webhook_base_url must be set to use push notifications")

    url = url.rstrip("/")
    scheme = auth_scheme or settings.webhook_auth_scheme
    cred = secret or get_webhook_secret()

    return {
        "url": f"{url}/webhooks/adcp/{task_type}/{operation_id}",
        "authentication": {
            "schemes": [scheme],
            "credentials": cred,
        },
    }


def build_reporting_webhook(
    operation_id: str,
    frequency: str = "daily",
    metrics: list[str] | None = None,
    base_url: str | None = None,
    auth_scheme: str | None = None,
    secret: str | None = None,
) -> dict:
    """Build a reporting_webhook for create_media_buy.

    Reporting webhooks deliver campaign performance data
    at the specified frequency.
    """
    url = base_url or settings.webhook_base_url
    if not url:
        raise ValueError("webhook_base_url must be set to use reporting webhooks")

    url = url.rstrip("/")
    scheme = auth_scheme or settings.webhook_auth_scheme
    cred = secret or get_webhook_secret()

    return {
        "url": f"{url}/webhooks/adcp/reporting/{operation_id}",
        "authentication": {
            "schemes": [scheme],
            "credentials": cred,
        },
        "reporting_frequency": frequency,
        "requested_metrics": metrics or [
            "impressions", "clicks", "spend",
        ],
    }
