"""Prometheus metrics for the Nexflo Buyer.

Tracks HTTP request latency, seller call latency, operation lifecycle,
and circuit breaker state.
"""

from prometheus_client import Counter, Histogram, Gauge

# --- HTTP ---

http_requests_total = Counter(
    "nxflo_http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)

http_request_duration_seconds = Histogram(
    "nxflo_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "path"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# --- Seller calls ---

seller_calls_total = Counter(
    "nxflo_seller_calls_total",
    "Total MCP tool calls to sellers",
    ["seller", "tool", "outcome"],  # outcome: success, error, circuit_open
)

seller_call_duration_seconds = Histogram(
    "nxflo_seller_call_duration_seconds",
    "Seller MCP call latency",
    ["seller", "tool"],
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

# --- Operations ---

operations_created_total = Counter(
    "nxflo_operations_created_total",
    "Operations created by type",
    ["operation_type"],
)

operations_current = Gauge(
    "nxflo_operations_current",
    "Current operations by status",
    ["status"],
)

# --- Circuit breakers ---

circuit_breaker_state = Gauge(
    "nxflo_circuit_breaker_open",
    "Whether a seller circuit breaker is open (1=open, 0=closed)",
    ["seller_url"],
)
