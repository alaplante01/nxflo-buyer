"""Bounded retry budget and AdCP error recovery classification.

Spec (docs/building/operating/transport-errors#recovery-behavior and
docs/building/by-layer/L3/error-handling#retry-logic):
- buyer agents MUST enforce a bounded retry budget per operation: at most 3 retries
  and at most 300 s of cumulative retry duration; every scheduled retry consumes it;
- only errors whose effective recovery is ``transient`` are retried automatically
  (``correctable`` / ``terminal`` never, even with ``retry_after``);
- ``retry_after`` is rounded up then clamped to [1, 3600]; without it, use jittered
  exponential backoff (base 2 s, cap 60 s, +/-25%);
- unknown codes default to ``transient`` but still consume the budget; unknown
  ``recovery`` values are treated as ``terminal``.
"""

import asyncio
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.config import settings

SPEC_MAX_RETRIES = 3
SPEC_MAX_RETRY_SECONDS = 300.0
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 60.0

# Standard code -> recovery when an error omits `recovery` (transport-errors table).
CODE_RECOVERY: dict[str, str] = {
    "RATE_LIMITED": "transient",
    "SERVICE_UNAVAILABLE": "transient",
    "CONFLICT": "transient",
    "INVALID_REQUEST": "correctable",
    "AUTH_MISSING": "correctable",
    "AUTH_INVALID": "terminal",
    "AUTH_REQUIRED": "correctable",
    "POLICY_VIOLATION": "correctable",
    "PRODUCT_NOT_FOUND": "correctable",
    "PRODUCT_UNAVAILABLE": "correctable",
    "PROPOSAL_EXPIRED": "correctable",
    "PROPOSAL_NOT_FOUND": "correctable",
    "MULTI_FINALIZE_UNSUPPORTED": "correctable",
    "REQUOTE_REQUIRED": "correctable",
    "BUDGET_TOO_LOW": "correctable",
    "CREATIVE_REJECTED": "correctable",
    "UNSUPPORTED_FEATURE": "correctable",
    "AUDIENCE_TOO_SMALL": "correctable",
    "ACCOUNT_MOVED": "correctable",
    "ACCOUNT_IDENTITY_CONFLICT": "correctable",
    "ACCOUNT_SETUP_REQUIRED": "correctable",
    "ACCOUNT_AMBIGUOUS": "correctable",
    "COMPLIANCE_UNSATISFIED": "correctable",
    "GOVERNANCE_DENIED": "correctable",
    "MEDIA_BUY_NOT_FOUND": "correctable",
    "PACKAGE_NOT_FOUND": "correctable",
    "CREATIVE_NOT_FOUND": "correctable",
    "SIGNAL_NOT_FOUND": "correctable",
    "SESSION_NOT_FOUND": "correctable",
    "SESSION_TERMINATED": "correctable",
    "REFERENCE_NOT_FOUND": "correctable",
    "VALIDATION_ERROR": "correctable",
    "ACCOUNT_NOT_FOUND": "terminal",
    "ACCOUNT_PAYMENT_REQUIRED": "terminal",
    "ACCOUNT_SUSPENDED": "terminal",
    "BUDGET_EXHAUSTED": "terminal",
    "CONFIGURATION_ERROR": "terminal",
}
KNOWN_RECOVERIES = frozenset({"transient", "correctable", "terminal"})

# Operations that change seller state; retried only with an idempotency_key on a
# seller that explicitly declares adcp.idempotency.supported: true.
MUTATING_TASKS = frozenset(
    {
        "create_media_buy",
        "update_media_buy",
        "buy_products",
        "accept_proposal",
        "control_media_buy",
        "sync_creatives",
        "sync_accounts",
        "sync_catalogs",
        "sync_event_sources",
        "sync_audiences",
        "sync_governance",
        "activate_signal",
        "provide_performance_feedback",
        "report_usage",
        "log_event",
        "acquire_rights",
        "request_proposals",
        "refine_proposals",
        "decline_proposals",
    }
)

Sleep = Callable[[float], Any]


def recovery_for(error: dict[str, Any]) -> str:
    """Effective recovery for an AdCP error object."""
    declared = error.get("recovery")
    if isinstance(declared, str):
        return declared if declared in KNOWN_RECOVERIES else "terminal"
    return CODE_RECOVERY.get(str(error.get("code", "")), "transient")


def normalize_retry_after(value: object) -> float | None:
    """ceil, then clamp to [1, 3600]; non-numeric or non-finite values are absent."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value):
        return None
    return float(min(3600, max(1, math.ceil(value))))


def backoff_delay(retry_index: int, rng: Callable[[], float] = random.random) -> float:
    """Jittered exponential backoff: base 2 s, cap 60 s, +/-25%."""
    raw = min(BACKOFF_BASE_SECONDS * (2**retry_index), BACKOFF_CAP_SECONDS)
    return raw * (0.75 + rng() * 0.5)


@dataclass
class RetryBudget:
    """Per-operation retry budget (<= 3 retries, <= 300 s cumulative)."""

    max_retries: int = SPEC_MAX_RETRIES
    max_seconds: float = SPEC_MAX_RETRY_SECONDS
    clock: Callable[[], float] = time.monotonic
    rng: Callable[[], float] = random.random
    retries_attempted: int = 0
    started_at: float = field(default=-1.0)

    def __post_init__(self) -> None:
        self.max_retries = max(0, min(self.max_retries, SPEC_MAX_RETRIES))
        self.max_seconds = max(0.0, min(self.max_seconds, SPEC_MAX_RETRY_SECONDS))
        if self.started_at < 0:
            self.started_at = self.clock()

    @classmethod
    def from_settings(cls) -> "RetryBudget":
        return cls(
            max_retries=settings.mcp_max_retries, max_seconds=settings.mcp_retry_budget_seconds
        )

    @property
    def exhausted(self) -> bool:
        return self.retries_attempted >= self.max_retries

    def next_delay(self, retry_after: object = None) -> float | None:
        """Consume one retry and return its delay, or None when the budget is spent."""
        if self.exhausted:
            return None
        hinted = normalize_retry_after(retry_after)
        delay = hinted if hinted is not None else backoff_delay(self.retries_attempted, self.rng)
        elapsed = self.clock() - self.started_at
        if elapsed + delay > self.max_seconds:
            return None
        self.retries_attempted += 1
        return delay


async def sleep(seconds: float) -> None:
    """Indirection so tests can replace the wait."""
    await asyncio.sleep(seconds)
