"""Circuit breaker for seller agent connections.

Prevents cascading failures when a seller is consistently unreachable.
Three states:
  CLOSED  — normal operation, requests flow through
  OPEN    — seller is down, requests fail fast without connecting
  HALF_OPEN — after cooldown, allows one probe request to test recovery

Transitions:
  CLOSED -> OPEN: after `failure_threshold` consecutive failures
  OPEN -> HALF_OPEN: after `recovery_timeout` seconds
  HALF_OPEN -> CLOSED: on success
  HALF_OPEN -> OPEN: on failure (resets cooldown)
"""

import asyncio
import logging
import time
from enum import Enum

from src.metrics import circuit_breaker_state

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the circuit is open."""

    def __init__(self, seller_name: str, remaining_seconds: float):
        self.seller_name = seller_name
        self.remaining_seconds = remaining_seconds
        super().__init__(
            f"Circuit open for {seller_name}, retry in {remaining_seconds:.0f}s"
        )


class CircuitBreaker:
    """Per-seller circuit breaker.

    Uses an asyncio.Lock to protect state transitions from concurrent coroutines.
    In HALF_OPEN state, only one probe request is allowed through at a time.
    """

    def __init__(
        self,
        seller_url: str = "",
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ):
        self._seller_url = seller_url
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._lock = asyncio.Lock()
        self._half_open_in_flight = False

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def _update_gauge(self) -> None:
        if self._seller_url:
            circuit_breaker_state.labels(seller_url=self._seller_url).set(
                1 if self._state == CircuitState.OPEN else 0
            )

    async def record_success(self) -> None:
        async with self._lock:
            self._failure_count = 0
            self._half_open_in_flight = False
            self._state = CircuitState.CLOSED
            self._update_gauge()

    async def record_failure(self) -> None:
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            self._half_open_in_flight = False
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
            self._update_gauge()

    async def allow_request(self) -> bool:
        async with self._lock:
            current = self.state
            if current == CircuitState.CLOSED:
                return True
            if current == CircuitState.HALF_OPEN:
                if self._half_open_in_flight:
                    return False  # Only one probe at a time
                self._half_open_in_flight = True
                return True
            return False  # OPEN

    def remaining_cooldown(self) -> float:
        if self._state != CircuitState.OPEN:
            return 0.0
        elapsed = time.monotonic() - self._last_failure_time
        return max(0.0, self.recovery_timeout - elapsed)


class CircuitBreakerRegistry:
    """Manages circuit breakers for all sellers."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ):
        self._breakers: dict[str, CircuitBreaker] = {}
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout

    def get(self, seller_url: str) -> CircuitBreaker:
        key = seller_url.rstrip("/")
        # dict.setdefault is atomic in CPython, avoids TOCTOU
        return self._breakers.setdefault(
            key,
            CircuitBreaker(
                seller_url=key,
                failure_threshold=self._failure_threshold,
                recovery_timeout=self._recovery_timeout,
            ),
        )

    def status_summary(self) -> dict[str, str]:
        return {url: cb.state.value for url, cb in self._breakers.items()}


# Global registry
circuit_breakers = CircuitBreakerRegistry()
