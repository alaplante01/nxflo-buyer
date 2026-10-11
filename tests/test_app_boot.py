"""Boot the real FastAPI app (lifespan, migrations through 004, MCP mount) offline.

Seller discovery is stubbed so no seller agent or registry is contacted, and the
database is a temporary SQLite file.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.buying.orchestrator import BuyingOrchestrator
from src.config import settings
from src.discovery.registry import SellerAgent
from src.models import schema


@pytest.fixture
def offline_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'boot.db'}"
    engine = create_async_engine(url)
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setenv("NXFLO_DATABASE_URL", url)  # alembic env.py prefers the env var
    monkeypatch.setattr(schema, "async_session", async_sessionmaker(engine, expire_on_commit=False))

    async def no_discovery(self: BuyingOrchestrator, probe: bool = True) -> list[SellerAgent]:
        self._sellers = []
        return []

    monkeypatch.setattr(BuyingOrchestrator, "discover_sellers", no_discovery)
    from src.main import app

    with TestClient(app) as client:
        yield client


def test_app_serves_rest_mcp_and_jwks(offline_app: TestClient) -> None:
    assert offline_app.get("/health").json()["status"] == "ok"
    assert offline_app.get("/accounts").json() == {"accounts": [], "count": 0}
    assert offline_app.get("/.well-known/jwks.json").status_code == 200

    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    call = offline_app.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "get_adcp_capabilities", "arguments": {}},
    })
    assert call.status_code == 200, call.text
    caps = call.json()["result"]["structuredContent"]
    assert caps["identity"]["brand_json_url"] == settings.resolved_brand_json_url()
    assert caps["supported_protocols"] == ["media_buy"]
