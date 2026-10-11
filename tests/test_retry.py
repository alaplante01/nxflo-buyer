"""Bounded retry budget (<= 3 retries, <= 300 s) and recovery classification."""

from typing import Any

import pytest

from src.config import settings
from src.connections import retry
from src.connections import seller as seller_mod
from src.connections.errors import SellerConnectionError, SellerToolError
from src.connections.session import SellerSession
from src.discovery.registry import SellerAgent


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(retry, "sleep", fake_sleep)
    return recorded


def agent(idempotent: bool = True) -> SellerAgent:
    caps = {"adcp": {"idempotency": {"supported": True, "replay_ttl_seconds": 86400}}} if idempotent else {}
    return SellerAgent(name="S", url="https://s.example/mcp", capabilities=caps)


def scripted(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_call_once(_agent: SellerAgent, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append({"tool": tool, "params": params})
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(seller_mod, "_call_once", fake_call_once)
    return calls


def test_budget_is_clamped_to_spec_ceilings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_max_retries", 10)
    monkeypatch.setattr(settings, "mcp_retry_budget_seconds", 3600.0)
    budget = retry.RetryBudget.from_settings()
    assert budget.max_retries == 3 and budget.max_seconds == 300.0


def test_budget_counts_retries_and_cumulative_time() -> None:
    clock = Clock()
    budget = retry.RetryBudget(clock=clock, rng=lambda: 0.5)
    assert budget.next_delay() == 2.0
    assert budget.next_delay() == 4.0
    assert budget.next_delay() == 8.0
    assert budget.next_delay() is None

    clock = Clock()
    timed = retry.RetryBudget(clock=clock)
    assert timed.next_delay(200) == 200.0
    clock.now = 200.0
    assert timed.next_delay(200) is None  # 200 + 200 > 300 s


def test_retry_after_normalization() -> None:
    assert retry.normalize_retry_after(0.2) == 1.0
    assert retry.normalize_retry_after(2.1) == 3.0
    assert retry.normalize_retry_after(99999) == 3600.0
    assert retry.normalize_retry_after(float("nan")) is None
    assert retry.normalize_retry_after("5") is None
    assert retry.normalize_retry_after(True) is None


def test_recovery_classification() -> None:
    assert retry.recovery_for({"code": "RATE_LIMITED"}) == "transient"
    assert retry.recovery_for({"code": "ACCOUNT_NOT_FOUND"}) == "terminal"
    assert retry.recovery_for({"code": "VALIDATION_ERROR"}) == "correctable"
    assert retry.recovery_for({"code": "GO_FOREVER"}) == "transient"
    assert retry.recovery_for({"code": "X", "recovery": "weird"}) == "terminal"
    assert retry.recovery_for({"code": "RATE_LIMITED", "recovery": "correctable"}) == "correctable"


async def test_transient_errors_retry_then_succeed(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    calls = scripted(monkeypatch, [SellerConnectionError("boom"), SellerConnectionError("boom"), {"products": []}])
    assert await seller_mod.call_seller_tool(agent(), "get_products", {"brief": "x"}) == {"products": []}
    assert len(calls) == 3 and len(sleeps) == 2


async def test_budget_exhaustion_surfaces_error_with_retry_count(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    calls = scripted(monkeypatch, [SellerConnectionError("down")] * 5)
    with pytest.raises(SellerConnectionError) as info:
        await seller_mod.call_seller_tool(agent(), "get_products", {})
    assert len(calls) == 4 and info.value.retry_count == 3 and len(sleeps) == 3


async def test_retry_after_is_honoured(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    limited = SellerToolError("slow down", code="RATE_LIMITED", recovery="transient", retry_after=7.0)
    scripted(monkeypatch, [limited, {"products": []}])
    await seller_mod.call_seller_tool(agent(), "get_products", {})
    assert sleeps == [7.0]


@pytest.mark.parametrize(
    "error",
    [
        SellerToolError("bad", code="VALIDATION_ERROR", recovery="correctable", retry_after=5),
        SellerToolError("gone", code="ACCOUNT_NOT_FOUND", recovery="terminal"),
        SellerConnectionError("unauthorized", status_code=401),
    ],
)
async def test_non_transient_errors_are_not_retried(monkeypatch: pytest.MonkeyPatch, sleeps: list[float], error: Exception) -> None:
    calls = scripted(monkeypatch, [error, {"ok": True}])
    with pytest.raises(type(error)):
        await seller_mod.call_seller_tool(agent(), "get_products", {})
    assert len(calls) == 1 and sleeps == []


async def test_mutations_retry_only_with_idempotency_on_replay_safe_seller(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    calls = scripted(monkeypatch, [SellerConnectionError("reset"), {"media_buy_id": "mb1"}])
    params = {"idempotency_key": "nxflo-0123456789abcdef"}
    assert await seller_mod.call_seller_tool(agent(), "create_media_buy", params) == {"media_buy_id": "mb1"}
    assert len(calls) == 2 and calls[0]["params"] is calls[1]["params"]  # same key replayed

    calls = scripted(monkeypatch, [SellerConnectionError("reset"), {"media_buy_id": "mb1"}])
    with pytest.raises(SellerConnectionError):
        await seller_mod.call_seller_tool(agent(idempotent=False), "create_media_buy", params)
    assert len(calls) == 1

    calls = scripted(monkeypatch, [SellerConnectionError("reset"), {"media_buy_id": "mb1"}])
    with pytest.raises(SellerConnectionError):
        await seller_mod.call_seller_tool(agent(), "create_media_buy", {})
    assert len(calls) == 1


async def test_transient_error_payload_is_retried(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    limited = {"errors": [{"code": "RATE_LIMITED", "message": "slow", "retry_after": 2}]}
    calls = scripted(monkeypatch, [limited, {"products": [{"product_id": "p"}]}])
    result = await seller_mod.call_seller_tool(agent(), "get_products", {})
    assert result["products"] and len(calls) == 2 and sleeps == [2.0]

    terminal = {"errors": [{"code": "ACCOUNT_NOT_FOUND", "message": "no"}]}
    calls = scripted(monkeypatch, [terminal])
    assert await seller_mod.call_seller_tool(agent(), "get_products", {}) == terminal
    assert len(calls) == 1


async def test_context_expiry_retry_consumes_the_same_budget(monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    expired = SellerToolError("context not found", code="SESSION_NOT_FOUND", recovery="correctable")
    calls = scripted(monkeypatch, [expired, {"ok": True}])
    session = SellerSession(agent())
    session.context_id = "stale"
    assert await session.call_with_retry("get_products", {}) == {"ok": True}
    assert len(calls) == 2 and "context_id" not in calls[1]["params"]


def test_connection_error_status_classification() -> None:
    assert SellerConnectionError("x", status_code=401).recovery == "terminal"
    assert SellerConnectionError("x", status_code=429).recovery == "transient"
    assert SellerConnectionError("x", status_code=503).recovery == "transient"
    assert SellerConnectionError("x").recovery == "transient"
