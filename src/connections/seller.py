"""MCP client wrapper for connecting to seller agents.

Adapted from the Prebid Sales Agent's mcp_client.py pattern:
- StreamableHttpTransport for HTTP/SSE
- Credential per account mode (agent bearer, or operator credential when the seller
  uses an account-id namespace)
- Optional RFC 9421 request signing of tools/call requests the seller verifies
- Bounded retry budget per operation (<= 3 retries, <= 300 s cumulative)
"""

import functools
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport

from src.accounts.mode import AccountMode
from src.brand.resolver import build_brand_ref, domain_from_url
from src.buying.requests import build_get_products_request, new_idempotency_key, with_version
from src.config import settings
from src.connections import retry
from src.connections.circuit_breaker import CircuitOpenError, circuit_breakers
from src.connections.errors import SellerConnectionError, SellerError, SellerToolError
from src.discovery.registry import SellerAgent
from src.metrics import seller_call_duration_seconds, seller_calls_total
from src.signing.auth import AdcpSigningAuth
from src.signing.keys import SigningKey, load_signing_key_from_settings

logger = logging.getLogger(__name__)

__all__ = [
    "CircuitOpenError",
    "SellerConnectionError",
    "SellerError",
    "SellerToolError",
    "call_seller_tool",
    "connect_to_seller",
]

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)")
_SUCCESS_KEYS = ("products", "accounts", "media_buy_id", "media_buys", "formats", "task_id")


def _build_headers(agent: SellerAgent) -> dict[str, str]:
    """Auth headers: the operator credential in operator-authorized mode, else ours."""
    token = agent.token
    if agent.account_mode == AccountMode.OPERATOR_AUTHORIZED and agent.operator_token:
        token = agent.operator_token
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


@functools.cache
def _signing_key() -> SigningKey | None:
    return load_signing_key_from_settings() if settings.signing_enabled else None


def _speaks_32_or_later(agent: SellerAgent) -> bool:
    for version in agent.supported_versions:
        match = _VERSION_RE.match(version)
        if match and (int(match.group(1)), int(match.group(2))) >= (3, 2):
            return True
    return False


def _has_webhook_authentication(arguments: dict[str, Any]) -> bool:
    push = arguments.get("push_notification_config")
    if isinstance(push, dict) and push.get("authentication"):
        return True
    for account in arguments.get("accounts") or []:
        for config in (account or {}).get("notification_configs") or []:
            if isinstance(config, dict) and config.get("authentication"):
                return True
    return False


def should_sign(agent: SellerAgent, tool_name: str, arguments: dict[str, Any]) -> bool:
    """Sign when the seller verifies signatures for this operation under the 3.2 profile."""
    caps = agent.request_signing_capabilities
    if caps.get("supported") is not True or caps.get("covers_content_digest") == "forbidden":
        return False
    if not _speaks_32_or_later(agent):
        return False  # 3.0/3.1 verifiers expect the legacy Base64URL profile
    operations = {
        *(caps.get("supported_for") or []),
        *(caps.get("warn_for") or []),
        *(caps.get("required_for") or []),
    }
    return tool_name in operations or _has_webhook_authentication(arguments)


def _build_auth(agent: SellerAgent) -> AdcpSigningAuth | None:
    key = _signing_key()
    if key is None:
        return None
    return AdcpSigningAuth(key, lambda name, args: should_sign(agent, name, args))


def _status_code(exc: BaseException) -> int | None:
    """Find an HTTP status code anywhere in an exception chain/group."""
    stack: list[BaseException] = [exc]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
        stack.extend(getattr(current, "exceptions", ()) or ())
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                stack.append(linked)
    return None


@asynccontextmanager
async def connect_to_seller(agent: SellerAgent) -> AsyncIterator[Client]:
    """Connect to a seller agent via MCP.

    Yields a connected MCP Client.

    Usage:
        async with connect_to_seller(agent) as client:
            result = await client.call_tool("get_products", {...})
    """
    url = agent.url.rstrip("/")
    transport = StreamableHttpTransport(
        url=url, headers=_build_headers(agent), auth=_build_auth(agent)
    )
    client = Client(transport=transport)

    try:
        async with client:
            logger.debug(f"Connected to {agent.name} at {url}")
            yield client
    except Exception as e:
        raise SellerConnectionError(
            f"Failed to connect to {agent.name} at {url}: {e}", status_code=_status_code(e)
        ) from e


def _first_json_text(result: Any) -> dict[str, Any] | None:
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
            return parsed if isinstance(parsed, dict) else {"result": parsed}
    return None


def parse_tool_result(result: Any) -> dict[str, Any]:
    """Prefer structuredContent; fall back to JSON (or raw) text content."""
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and structured:
        return structured
    parsed = _first_json_text(result)
    return parsed if parsed is not None else {"status": "empty_response"}


def _adcp_error(payload: dict[str, Any]) -> dict[str, Any] | None:
    error = payload.get("adcp_error")
    if isinstance(error, dict):
        return error
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return errors[0]
    return None


def tool_error_from_result(tool_name: str, result: Any) -> SellerToolError:
    """Build a SellerToolError from an MCP isError result."""
    payload = parse_tool_result(result)
    error = _adcp_error(payload) or {}
    message = str(
        error.get("message") or payload.get("raw") or payload.get("message") or "tool error"
    )
    return SellerToolError(
        f"{tool_name} failed: {message}",
        code=error.get("code"),
        recovery=retry.recovery_for(error) if error else "transient",
        retry_after=retry.normalize_retry_after(error.get("retry_after")),
        payload=payload,
    )


def transient_response_error(payload: dict[str, Any]) -> SellerToolError | None:
    """A non-isError payload carrying only transient AdCP errors (e.g. RATE_LIMITED)."""
    errors = payload.get("errors")
    if not isinstance(errors, list) or not errors or any(k in payload for k in _SUCCESS_KEYS):
        return None
    if not all(isinstance(e, dict) and retry.recovery_for(e) == "transient" for e in errors):
        return None
    first = errors[0]
    return SellerToolError(
        str(first.get("message") or first.get("code") or "transient error"),
        code=first.get("code"),
        recovery="transient",
        retry_after=retry.normalize_retry_after(first.get("retry_after")),
        payload=payload,
    )


def retry_permitted(agent: SellerAgent, tool_name: str, params: dict[str, Any]) -> bool:
    """Reads always; mutations only with an idempotency_key on a replay-safe seller."""
    if tool_name not in retry.MUTATING_TASKS:
        return True
    return bool(params.get("idempotency_key")) and agent.idempotency_supported


def _record(agent: SellerAgent, tool_name: str, outcome: str, start: float) -> None:
    seller_calls_total.labels(seller=agent.name, tool=tool_name, outcome=outcome).inc()
    seller_call_duration_seconds.labels(seller=agent.name, tool=tool_name).observe(
        time.perf_counter() - start
    )


async def _call_once(agent: SellerAgent, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
    cb = circuit_breakers.get(agent.url)
    if not await cb.allow_request():
        seller_calls_total.labels(seller=agent.name, tool=tool_name, outcome="circuit_open").inc()
        raise CircuitOpenError(agent.name, cb.remaining_cooldown())

    start = time.perf_counter()
    try:
        async with connect_to_seller(agent) as client:
            result = await client.call_tool(tool_name, params, raise_on_error=False)
    except SellerConnectionError:
        await cb.record_failure()
        _record(agent, tool_name, "error", start)
        raise

    if getattr(result, "is_error", False):
        error = tool_error_from_result(tool_name, result)
        if error.recovery == "transient":
            await cb.record_failure()
        else:
            await cb.record_success()  # account/validation errors are not seller health
        _record(agent, tool_name, "error", start)
        raise error

    await cb.record_success()
    _record(agent, tool_name, "success", start)
    return parse_tool_result(result)


async def call_seller_tool(
    agent: SellerAgent,
    tool_name: str,
    params: dict[str, Any],
    *,
    budget: retry.RetryBudget | None = None,
) -> dict[str, Any]:
    """Call a tool on a seller agent and return the parsed response.

    Transient failures are retried under a bounded per-operation budget; correctable
    and terminal errors are raised immediately. Mutations are retried only when they
    carry an idempotency_key and the seller declares idempotency replay support.
    """
    budget = budget or retry.RetryBudget.from_settings()
    allowed = retry_permitted(agent, tool_name, params)
    while True:
        try:
            response = await _call_once(agent, tool_name, params)
        except SellerError as err:
            delay = (
                budget.next_delay(err.retry_after)
                if allowed and err.recovery == "transient"
                else None
            )
            if delay is None:
                err.retry_count = budget.retries_attempted
                raise
            logger.warning(
                "%s on %s failed (%s); retry %d/%d in %.1fs",
                tool_name,
                agent.name,
                err,
                budget.retries_attempted,
                budget.max_retries,
                delay,
            )
            await retry.sleep(delay)
            continue
        transient = transient_response_error(response)
        delay = budget.next_delay(transient.retry_after) if transient and allowed else None
        if delay is None:
            if transient is not None:
                logger.warning(
                    "%s on %s: transient errors persisted after retry budget", tool_name, agent.name
                )
            return response
        logger.warning(
            "%s on %s returned %s; retrying in %.1fs", tool_name, agent.name, transient.code, delay
        )
        await retry.sleep(delay)


async def get_seller_capabilities(agent: SellerAgent) -> dict[str, Any]:
    """Get a seller's capabilities by listing available tools.

    Not all agents implement get_adcp_capabilities, so we fall back
    to listing tools as a discovery mechanism.
    """
    async with connect_to_seller(agent) as client:
        tools = await client.list_tools()
        tool_names = [t.name for t in tools] if tools else []
        return {
            "agent": agent.name,
            "url": agent.url,
            "tools": tool_names,
            "tool_count": len(tool_names),
        }


async def get_seller_products(
    agent: SellerAgent,
    brief: str,
    brand_name: str = "Nexflo",
    brand_url: str = "https://nexflo.ai",
    *,
    brand: dict[str, Any] | None = None,
    account: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Get products from a seller matching a brief (AdCP 3.2: buying_mode + BrandRef).

    ``brand_name`` is kept for call-site compatibility; 3.2 brand references carry only
    the domain (resolved from ``brand_url`` when ``brand`` is not given).
    """
    brand_ref = brand or build_brand_ref(domain_from_url(brand_url))
    logger.debug("get_products for %s (%s) on %s", brand_name, brand_ref["domain"], agent.name)
    return await call_seller_tool(
        agent, "get_products", build_get_products_request(brief, brand_ref, account)
    )


async def get_delivery(agent: SellerAgent, media_buy_id: str) -> dict[str, Any]:
    """Get delivery metrics for a media buy."""
    return await call_seller_tool(
        agent, "get_media_buy_delivery", {"media_buy_ids": [media_buy_id]}
    )


# --- AdCP Protocol Tools (Phase 2) ---


async def get_adcp_capabilities_tool(
    agent: SellerAgent, protocols: list[str] | None = None
) -> dict[str, Any]:
    """Call get_adcp_capabilities on a seller to discover protocol support.

    This is the protocol-recommended way to discover what a seller supports,
    rather than just listing MCP tools.
    """
    params: dict[str, Any] = {}
    if protocols:
        params["protocols"] = protocols
    return await call_seller_tool(agent, "get_adcp_capabilities", params)


async def update_media_buy(
    agent: SellerAgent,
    media_buy_id: str,
    account: dict[str, Any],
    paused: bool | None = None,
    end_time: str | None = None,
    packages: list[dict] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Update an existing media buy using PATCH semantics (3.2: account + idempotency_key)."""
    params: dict[str, Any] = {
        "idempotency_key": idempotency_key or new_idempotency_key(),
        "account": account,
        "media_buy_id": media_buy_id,
    }
    if paused is not None:
        params["paused"] = paused
    if end_time:
        params["end_time"] = end_time
    if packages:
        params["packages"] = packages
    return await call_seller_tool(agent, "update_media_buy", with_version(params))


async def sync_creatives(
    agent: SellerAgent,
    creatives: list[dict],
    account: dict[str, Any],
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Upload/sync creative assets (3.2: account + idempotency_key required)."""
    params: dict[str, Any] = {
        "idempotency_key": idempotency_key or new_idempotency_key(),
        "account": account,
        "creatives": creatives,
    }
    return await call_seller_tool(agent, "sync_creatives", with_version(params))


async def provide_performance_feedback(
    agent: SellerAgent,
    media_buy_id: str,
    performance_index: float,
    measurement_period: dict[str, Any],
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Share performance outcomes with a seller."""
    return await call_seller_tool(
        agent,
        "provide_performance_feedback",
        {
            "idempotency_key": idempotency_key or new_idempotency_key(),
            "media_buy_id": media_buy_id,
            "performance_index": performance_index,
            "measurement_period": measurement_period,
        },
    )


async def get_signals(
    agent: SellerAgent,
    brief: str,
    platforms: list[dict] | None = None,
) -> dict[str, Any]:
    """Discover audience signals from a signals agent.

    Dstillery (and potentially others) expect signal_spec, not brief.
    """
    params: dict[str, Any] = {"signal_spec": brief}
    if platforms:
        params["platforms"] = platforms
    return await call_seller_tool(agent, "get_signals", params)


async def activate_signal(
    agent: SellerAgent,
    signal_id: str,
    platform: dict[str, Any],
) -> dict[str, Any]:
    """Activate a signal for use in campaigns."""
    return await call_seller_tool(
        agent,
        "activate_signal",
        {"signal_id": signal_id, "platform": platform},
    )


async def tasks_get(
    agent: SellerAgent,
    task_id: str,
    include_result: bool = True,
    budget: retry.RetryBudget | None = None,
) -> dict[str, Any]:
    """Poll a specific task status using the standard tasks/get tool."""
    return await call_seller_tool(
        agent,
        "tasks/get",
        {"task_id": task_id, "include_result": include_result},
        budget=budget,
    )


async def tasks_list(
    agent: SellerAgent,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """List tasks on a seller using the standard tasks/list tool."""
    params: dict[str, Any] = {}
    if filters:
        params["filters"] = filters
    return await call_seller_tool(agent, "tasks/list", params)
