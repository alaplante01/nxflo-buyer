"""Unit tests with mocked external dependencies.

These tests can run in CI without network access or live seller agents.
"""

import hashlib
import hmac
import time
from unittest.mock import AsyncMock

import pytest


# --- Circuit Breaker ---


class TestCircuitBreaker:
    @pytest.fixture
    def breaker(self):
        from src.connections.circuit_breaker import CircuitBreaker

        return CircuitBreaker(
            seller_url="http://test", failure_threshold=3, recovery_timeout=1.0
        )

    @pytest.mark.asyncio
    async def test_starts_closed(self, breaker):
        from src.connections.circuit_breaker import CircuitState

        assert breaker.state == CircuitState.CLOSED
        assert await breaker.allow_request() is True

    @pytest.mark.asyncio
    async def test_opens_after_threshold(self, breaker):
        from src.connections.circuit_breaker import CircuitState

        for _ in range(3):
            await breaker.record_failure()
        assert breaker.state == CircuitState.OPEN
        assert await breaker.allow_request() is False

    @pytest.mark.asyncio
    async def test_half_open_after_timeout(self, breaker):
        from src.connections.circuit_breaker import CircuitState

        for _ in range(3):
            await breaker.record_failure()

        # Fast-forward past recovery timeout
        breaker._last_failure_time = time.monotonic() - 2.0
        assert breaker.state == CircuitState.HALF_OPEN
        assert await breaker.allow_request() is True

    @pytest.mark.asyncio
    async def test_half_open_only_one_probe(self, breaker):
        for _ in range(3):
            await breaker.record_failure()
        breaker._last_failure_time = time.monotonic() - 2.0

        assert await breaker.allow_request() is True  # First probe
        assert await breaker.allow_request() is False  # Second blocked

    @pytest.mark.asyncio
    async def test_closes_on_success(self, breaker):
        from src.connections.circuit_breaker import CircuitState

        for _ in range(3):
            await breaker.record_failure()
        breaker._last_failure_time = time.monotonic() - 2.0

        await breaker.allow_request()  # HALF_OPEN probe
        await breaker.record_success()
        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_reopens_on_half_open_failure(self, breaker):
        from src.connections.circuit_breaker import CircuitState

        for _ in range(3):
            await breaker.record_failure()
        breaker._last_failure_time = time.monotonic() - 2.0

        await breaker.allow_request()  # HALF_OPEN probe
        await breaker.record_failure()
        assert breaker.state == CircuitState.OPEN


# --- HMAC / Bearer Verification ---


class TestWebhookVerification:
    def test_valid_hmac(self):
        from src.webhooks.receiver import verify_hmac_signature

        secret = "test-secret"
        body = b'{"task_id": "123", "status": "completed"}'
        timestamp = "2024-01-01T00:00:00Z"

        expected = hmac.new(
            secret.encode("utf-8"),
            timestamp.encode("utf-8") + body,
            hashlib.sha256,
        ).hexdigest()
        signature = f"sha256={expected}"

        assert verify_hmac_signature(body, signature, timestamp, secret) is True

    def test_invalid_hmac(self):
        from src.webhooks.receiver import verify_hmac_signature

        assert verify_hmac_signature(b"body", "sha256=wrong", "ts", "secret") is False

    def test_missing_prefix(self):
        from src.webhooks.receiver import verify_hmac_signature

        assert verify_hmac_signature(b"body", "wrong-format", "ts", "secret") is False

    def test_valid_bearer(self):
        from src.webhooks.receiver import verify_bearer_auth

        assert verify_bearer_auth("Bearer my-token", "my-token") is True

    def test_invalid_bearer(self):
        from src.webhooks.receiver import verify_bearer_auth

        assert verify_bearer_auth("Bearer wrong", "my-token") is False

    def test_missing_bearer_prefix(self):
        from src.webhooks.receiver import verify_bearer_auth

        assert verify_bearer_auth("my-token", "my-token") is False


# --- Webhook Config Redaction ---


class TestRedaction:
    def test_redacts_credentials(self):
        from src.api.routes import _redact_webhook_config

        config = {
            "url": "https://example.com/webhook",
            "authentication": {
                "schemes": ["HMAC-SHA256"],
                "credentials": "super-secret-key",
            },
        }
        redacted = _redact_webhook_config(config)
        assert redacted["authentication"]["credentials"] == "***REDACTED***"
        assert redacted["url"] == "https://example.com/webhook"
        # Original not mutated
        assert config["authentication"]["credentials"] == "super-secret-key"

    def test_none_passthrough(self):
        from src.api.routes import _redact_webhook_config

        assert _redact_webhook_config(None) is None

    def test_no_auth_passthrough(self):
        from src.api.routes import _redact_webhook_config

        config = {"url": "https://example.com/webhook"}
        assert _redact_webhook_config(config) == config


# --- Operation Tracker ---


class TestOperationTracker:
    @pytest.fixture
    def tracker(self):
        from src.buying.tracker import OperationTracker

        return OperationTracker()

    @pytest.mark.asyncio
    async def test_create_operation(self, tracker):
        from src.buying.tracker import TaskStatus

        op = await tracker.create(
            operation_type="create_media_buy",
            seller_name="Test Seller",
            seller_url="https://test.com/mcp",
            buyer_ref="ref-123",
            request_data={"budget": 1000},
        )
        assert op.status == TaskStatus.PENDING
        assert op.seller_name == "Test Seller"
        assert op.buyer_ref == "ref-123"
        # Operation should be retrievable
        assert tracker.get(op.id) is op

    @pytest.mark.asyncio
    async def test_update_from_response_submitted(self, tracker):
        from src.buying.tracker import TaskStatus

        op = await tracker.create(
            operation_type="create_media_buy",
            seller_name="Test",
            seller_url="https://test.com",
            buyer_ref="ref",
            request_data={},
        )
        updated = tracker.update_from_response(op.id, {
            "status": "submitted",
            "task_id": "task-abc",
            "media_buy_id": "buy-xyz",
        })
        assert updated.status == TaskStatus.SUBMITTED
        assert updated.task_id == "task-abc"
        assert updated.media_buy_id == "buy-xyz"

    @pytest.mark.asyncio
    async def test_unknown_status(self, tracker):
        from src.buying.tracker import TaskStatus

        op = await tracker.create(
            operation_type="test",
            seller_name="Test",
            seller_url="https://test.com",
            buyer_ref="ref",
            request_data={},
        )
        updated = tracker.update_from_response(op.id, {"status": "banana"})
        assert updated.status == TaskStatus.UNKNOWN

    @pytest.mark.asyncio
    async def test_error_status(self, tracker):
        from src.buying.tracker import TaskStatus

        op = await tracker.create(
            operation_type="test",
            seller_name="Test",
            seller_url="https://test.com",
            buyer_ref="ref",
            request_data={},
        )
        updated = tracker.update_from_response(op.id, {
            "status": "something-weird",
            "error": "Something broke",
        })
        assert updated.status == TaskStatus.FAILED
        assert updated.error == "Something broke"

    @pytest.mark.asyncio
    async def test_mark_failed(self, tracker):
        from src.buying.tracker import TaskStatus

        op = await tracker.create(
            operation_type="test",
            seller_name="Test",
            seller_url="https://test.com",
            buyer_ref="ref",
            request_data={},
        )
        failed = tracker.mark_failed(op.id, "Connection timeout")
        assert failed.status == TaskStatus.FAILED
        assert failed.error == "Connection timeout"

    @pytest.mark.asyncio
    async def test_get_pending(self, tracker):
        op = await tracker.create(
            operation_type="test",
            seller_name="Test",
            seller_url="https://test.com",
            buyer_ref="ref",
            request_data={},
        )
        # PENDING status is not polled (not in get_pending)
        assert len(tracker.get_pending()) == 0

        tracker.update_from_response(op.id, {"status": "submitted", "task_id": "t1"})
        assert len(tracker.get_pending()) == 1

    @pytest.mark.asyncio
    async def test_get_by_buyer_ref(self, tracker):
        op = await tracker.create(
            operation_type="test",
            seller_name="Test",
            seller_url="https://test.com",
            buyer_ref="unique-ref",
            request_data={},
        )
        found = tracker.get_by_buyer_ref("unique-ref")
        assert found is op
        assert tracker.get_by_buyer_ref("nonexistent") is None

    @pytest.mark.asyncio
    async def test_input_required_extracts_data(self, tracker):
        from src.buying.tracker import TaskStatus

        op = await tracker.create(
            operation_type="create_media_buy",
            seller_name="Test",
            seller_url="https://test.com",
            buyer_ref="ref",
            request_data={},
        )
        tracker.update_from_response(op.id, {
            "status": "input-required",
            "message": "Please approve the budget",
            "approval_options": ["approve", "reject"],
        })
        assert op.status == TaskStatus.INPUT_REQUIRED
        assert op.input_required_message == "Please approve the budget"
        assert "approval_options" in op.input_required_data


# --- Rate Limiter ---


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_allows_under_limit(self):
        from src.middleware import RateLimitMiddleware

        calls = []

        async def mock_app(scope, receive, send):
            calls.append(1)

        mw = RateLimitMiddleware(mock_app, rate=5, window=60.0)
        scope = {"type": "http", "client": ("127.0.0.1", 8080)}

        for _ in range(5):
            await mw(scope, None, AsyncMock())

        assert len(calls) == 5

    @pytest.mark.asyncio
    async def test_blocks_over_limit(self):
        from src.middleware import RateLimitMiddleware

        calls = []

        async def mock_app(scope, receive, send):
            calls.append(1)

        sent_messages = []

        async def mock_send(message):
            sent_messages.append(message)

        mw = RateLimitMiddleware(mock_app, rate=2, window=60.0)
        scope = {"type": "http", "client": ("127.0.0.1", 8080)}

        await mw(scope, None, AsyncMock())  # 1
        await mw(scope, None, AsyncMock())  # 2
        await mw(scope, None, mock_send)  # 3 — should be blocked

        assert len(calls) == 2
        assert any(m.get("status") == 429 for m in sent_messages)

    @pytest.mark.asyncio
    async def test_separate_keys(self):
        from src.middleware import RateLimitMiddleware

        calls = []

        async def mock_app(scope, receive, send):
            calls.append(1)

        mw = RateLimitMiddleware(mock_app, rate=1, window=60.0)

        await mw({"type": "http", "client": ("1.1.1.1", 80)}, None, AsyncMock())
        await mw({"type": "http", "client": ("2.2.2.2", 80)}, None, AsyncMock())

        assert len(calls) == 2  # Different IPs, both allowed

    @pytest.mark.asyncio
    async def test_passthrough_non_http(self):
        from src.middleware import RateLimitMiddleware

        calls = []

        async def mock_app(scope, receive, send):
            calls.append(1)

        mw = RateLimitMiddleware(mock_app, rate=1, window=60.0)
        await mw({"type": "websocket"}, None, AsyncMock())

        assert len(calls) == 1  # Non-HTTP passes through
