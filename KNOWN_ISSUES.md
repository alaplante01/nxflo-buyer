# Known Issues

Tracked issues and limitations as of 2026-02-10 (v0.4.0).

## High Priority

### H1: No authentication on API endpoints
**Severity**: HIGH | **Effort**: Medium

The REST API (`/buy`, `/operations`, `/media-buy/*`, etc.) has no authentication.
Anyone who can reach the service can trigger purchases.

**Recommendation**: Add API key or JWT middleware. At minimum, require a
`NXFLO_API_KEY` env var and check `Authorization: Bearer <key>` on all
non-health endpoints.

---

### H2: No connection pooling for MCP clients
**Severity**: HIGH | **Effort**: Medium

Each `call_seller_tool` creates a new `StreamableHttpTransport` + `Client`,
opens a connection, makes the call, and tears it down. Under high concurrency
this means excessive TCP handshakes.

**Recommendation**: Maintain a shared `httpx.AsyncClient` per seller with
connection pooling. Requires changes to how `fastmcp` transports are constructed
or a custom transport wrapper.

**Mitigating factor**: Current call volume is low (minutes between calls, not
milliseconds). This becomes important at scale.

---

### H3: Rate limiter is in-memory only
**Severity**: MEDIUM | **Effort**: Low

The `RateLimitMiddleware` stores hit counts in a Python dict. Behind a load
balancer with multiple instances, each instance has its own counter — a client
could get N * rate_limit throughput.

**Recommendation**: For multi-instance deployments, use Redis-backed rate
limiting (e.g. `slowapi` with Redis backend) or rely on API Gateway/ALB
rate limiting.

---

## Medium Priority

### M1: SellerSession creates duplicate connections alongside call_seller_tool
**Severity**: MEDIUM | **Effort**: Low

After the refactor, `SellerSession._call_raw` delegates to `call_seller_tool`,
which creates its own connection via `connect_to_seller`. The session no longer
manages its own client — it's purely a context_id tracker. This is correct
behavior but means the session's `call_with_retry` retry (on context expiration)
creates two full connections.

**Recommendation**: Acceptable for now. Could optimize by passing an existing
client to `call_seller_tool` if needed.

---

### M2: Orchestrator uses global singleton pattern
**Severity**: MEDIUM | **Effort**: Medium

`routes.orchestrator` is a module-level global set during lifespan. This makes
testing harder and prevents running multiple orchestrator instances.

**Recommendation**: Use FastAPI dependency injection (`Depends()`) with
`app.state.orchestrator` instead of a module-level global.

---

### M3: No graceful shutdown drain for in-flight requests
**Severity**: MEDIUM | **Effort**: Medium

When the service shuts down, the lifespan only stops the poller. In-flight API
requests or MCP calls may be interrupted.

**Recommendation**: Add a shutdown signal that stops accepting new requests,
waits for in-flight operations to complete (with timeout), then exits.

---

### M4: Alembic migration runs synchronously in a thread
**Severity**: LOW | **Effort**: Low

`init_db()` uses `asyncio.to_thread()` because Alembic's `command.upgrade()`
internally calls `asyncio.run()`. This works but is a workaround. If Alembic
adds native async support, this should be updated.

---

### M5: BackgroundPoller does not use circuit breaker directly
**Severity**: LOW | **Effort**: Low

The poller calls `tasks_get` via `seller.py`, which goes through the circuit
breaker. However, if a seller's circuit is open, the poller will get
`CircuitOpenError` for every pending operation on that seller — generating
noisy warning logs every 5 seconds.

**Recommendation**: Check circuit breaker state in `_poll_loop` before
attempting to poll operations for a given seller.

---

### M6: Webhook `_verify_auth` uses stored credentials from webhook_config
**Severity**: MEDIUM | **Effort**: Low

`_verify_auth` reads the `credentials` field from the operation's stored
`webhook_config`. If the webhook_config is compromised in the database, an
attacker could set arbitrary credentials. This is defense-in-depth only —
requires database write access.

**Recommendation**: Always verify against the server-side secret
(`get_webhook_secret()`), not stored credentials. The stored credentials are
what was *sent* to the seller, not what should be *accepted*.

---

## Low Priority / Future Work

### L1: No integration tests with mocked MCP server
Only unit tests exist. Integration tests that spin up a mock seller MCP server
and test the full flow (discover -> products -> buy -> poll) would catch
protocol-level issues.

### L2: No OpenAPI schema validation in CI
The API responses don't have strict schema validation tests. Could use
`schemathesis` or `hypothesis` for property-based API testing.

### L3: Prometheus metrics not scraped in production
`/metrics` endpoint exists but there's no Prometheus server configured in the
AWS deployment. Need to add a Prometheus sidecar or use CloudWatch metrics
adapter.

### L4: No structured error responses
API errors use FastAPI's default `HTTPException` format. Could standardize on
RFC 7807 Problem Details for consistent error payloads.

### L5: Database connection pool not tuned for production load
PostgreSQL pool settings (`pool_size=5`, `max_overflow=10`) are defaults.
Should be tuned based on expected concurrency and Aurora Serverless v2 limits.

### L6: No request body size limit
POST endpoints don't enforce a max request body size. A large payload could
cause memory issues. Should add a middleware or reverse proxy limit.

### L7: seller.py response parsing is fragile
`call_seller_tool` tries `json.loads(item.text)` on the first text content
item. If a seller returns multiple content items or non-JSON text, only the
first is used. Structured content fallback exists but isn't well-tested.

### L8: No retry budget / backoff on poller failures
The `BackgroundPoller` retries forever with no backoff. If a seller returns
transient errors, the poller hammers it every 5/60 seconds indefinitely.
Should implement exponential backoff per-operation.
