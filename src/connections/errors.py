"""Seller interaction error hierarchy (kept import-light to avoid cycles)."""

from typing import Any


class SellerError(Exception):
    """Base class for every failure talking to a seller agent."""

    recovery: str = "transient"
    retry_after: float | None = None
    retry_count: int = 0


class SellerConnectionError(SellerError):
    """Transport-level failure (connect, HTTP status, protocol) reaching a seller."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        # 4xx other than 408/429 (auth, signature, not found) are not retryable.
        if status_code is not None and 400 <= status_code < 500 and status_code not in (408, 429):
            self.recovery = "terminal"


class SellerToolError(SellerError):
    """The seller answered the tool call with an AdCP error (MCP isError / errors[])."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        recovery: str = "transient",
        retry_after: float | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.recovery = recovery
        self.retry_after = retry_after
        self.payload = payload or {}
