"""Pure ASGI middleware for cross-cutting concerns.

Uses raw ASGI protocol instead of BaseHTTPMiddleware
to avoid known issues with streaming responses.
"""

import logging
import time
from collections import defaultdict

from src.logging_config import correlation_id, new_correlation_id
from src.metrics import http_requests_total, http_request_duration_seconds

logger = logging.getLogger(__name__)


class CorrelationIdMiddleware:
    """Assigns a correlation ID to each request, records Prometheus metrics, and logs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Extract or generate correlation ID
        cid = None
        for name, value in scope.get("headers", []):
            if name == b"x-correlation-id":
                cid = value.decode("latin-1")
                break

        if cid:
            correlation_id.set(cid)
        else:
            cid = new_correlation_id()

        method = scope.get("method", "")
        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-correlation-id", cid.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            route = scope.get("route")
            route_path = getattr(route, "path", scope.get("path", ""))

            http_requests_total.labels(
                method=method, path=route_path, status=status_code
            ).inc()
            http_request_duration_seconds.labels(
                method=method, path=route_path
            ).observe(elapsed)

            logger.info(
                "%s %s %d %.1fms",
                method,
                scope.get("path", ""),
                status_code,
                elapsed * 1000,
            )


class RateLimitMiddleware:
    """Simple in-memory sliding-window rate limiter per client IP."""

    def __init__(self, app, rate: int = 100, window: float = 60.0):
        self.app = app
        self.rate = rate
        self.window = window
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._call_count = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        key = client[0] if client else "unknown"

        now = time.monotonic()
        hits = self._hits[key]
        cutoff = now - self.window
        hits[:] = [t for t in hits if t > cutoff]

        if len(hits) >= self.rate:
            await self._send_429(scope, receive, send)
            return

        hits.append(now)

        # Periodic cleanup of stale IPs
        self._call_count += 1
        if self._call_count % 1000 == 0:
            self._cleanup(now)

        await self.app(scope, receive, send)

    async def _send_429(self, scope, receive, send):
        body = b"Rate limit exceeded"
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"text/plain"),
                (b"retry-after", str(int(self.window)).encode()),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })

    def _cleanup(self, now: float):
        cutoff = now - self.window
        stale = [k for k, v in self._hits.items() if not v or v[-1] < cutoff]
        for k in stale:
            del self._hits[k]
