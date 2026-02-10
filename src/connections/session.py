"""MCP session wrapper with automatic context_id management.

Per the AdCP MCP Guide, MCP requires explicit context_id management
(unlike A2A which handles it automatically). This class tracks context_id
per seller, injects it into requests, and extracts it from responses.

Reference: https://docs.adcontextprotocol.org/docs/building/integration/mcp-guide
"""

import logging
from typing import Any

from src.discovery.registry import SellerAgent

logger = logging.getLogger(__name__)


class SellerSession:
    """Manages an MCP session with a single seller agent.

    Automatically tracks context_id across calls, injects application-level
    context, and tracks async task_ids for reconciliation.
    """

    def __init__(self, agent: SellerAgent):
        self.agent = agent
        self.context_id: str | None = None
        self.active_tasks: dict[str, dict[str, Any]] = {}  # task_id -> metadata

    async def call(
        self,
        tool: str,
        params: dict[str, Any],
        context: dict[str, Any] | None = None,
        push_notification_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call a tool on the seller with automatic context management.

        - Injects context_id from previous responses
        - Optionally injects application-level context (opaque, echoed back)
        - Optionally injects pushNotificationConfig for async webhooks
        - Saves context_id from the response
        - Tracks task_id for any async operations
        """
        # Build the full params with protocol fields
        full_params = dict(params)

        if self.context_id:
            full_params["context_id"] = self.context_id

        if context is not None:
            full_params["context"] = context

        if push_notification_config is not None:
            full_params["push_notification_config"] = push_notification_config

        # Make the MCP call
        response = await self._call_raw(tool, full_params)

        # Extract and save context_id
        if "context_id" in response:
            self.context_id = response["context_id"]

        # Track async tasks
        if "task_id" in response:
            self.active_tasks[response["task_id"]] = {
                "tool": tool,
                "status": response.get("status", "unknown"),
            }

        return response

    async def call_with_retry(
        self,
        tool: str,
        params: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Call with context expiration handling.

        If the seller indicates the context is expired/not found,
        resets context_id and retries once.
        """
        try:
            return await self.call(tool, params, **kwargs)
        except Exception as e:
            error_msg = str(e).lower()
            if "context" in error_msg and ("not found" in error_msg or "expired" in error_msg):
                logger.info(f"Context expired for {self.agent.name}, resetting and retrying")
                self.reset()
                return await self.call(tool, params, **kwargs)
            raise

    async def _call_raw(self, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        """Delegate to call_seller_tool for consistent circuit breaking and metrics."""
        from src.connections.seller import call_seller_tool

        return await call_seller_tool(self.agent, tool, params)

    def reset(self):
        """Reset the session — clear context_id and active tasks."""
        self.context_id = None
        self.active_tasks.clear()

    def get_active_task_ids(self) -> list[str]:
        """Return all task IDs currently being tracked."""
        return list(self.active_tasks.keys())

    def __repr__(self) -> str:
        return (
            f"SellerSession(agent={self.agent.name!r}, "
            f"context_id={self.context_id!r}, "
            f"active_tasks={len(self.active_tasks)})"
        )
