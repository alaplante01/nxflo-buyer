"""Publisher registration API for the Nexflo publisher acquisition pipeline.

Publishers register here to get a site_id, which they paste into the WordPress plugin.
"""

import logging
import secrets

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.models.publisher import PublisherRecord
from src.models.schema import async_session
from src.utils import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/publishers", tags=["publishers"])


def _generate_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(8)}"


# --- Request/Response Models ---


class PublisherCreateRequest(BaseModel):
    name: str = Field(..., description="Publisher or site name")
    domain: str = Field(..., description="Root domain, e.g. example.com")
    email: str | None = Field(default=None, description="Contact email (optional)")


class PublisherResponse(BaseModel):
    id: str
    site_id: str
    name: str
    domain: str
    email: str | None
    status: str
    created_at: str
    snippet: str = Field(description="HTML snippet to paste into the site")


def _to_response(pub: PublisherRecord) -> PublisherResponse:
    snippet = (
        f'<script src="https://static.nexflo.ai/prebid-wrapper.js" '
        f'data-site-id="{pub.site_id}" async></script>'
    )
    return PublisherResponse(
        id=pub.id,
        site_id=pub.site_id,
        name=pub.name,
        domain=pub.domain,
        email=pub.email,
        status=pub.status,
        created_at=pub.created_at.isoformat(),
        snippet=snippet,
    )


# --- Endpoints ---


@router.post("", response_model=PublisherResponse, status_code=201)
async def register_publisher(req: PublisherCreateRequest):
    """Register a new publisher and return their site_id + install snippet."""
    domain = req.domain.lower().strip().lstrip("www.")

    pub = PublisherRecord(
        id=_generate_id("pub"),
        site_id=_generate_id("site"),
        name=req.name,
        domain=domain,
        email=req.email,
        status="active",
        created_at=utcnow(),
    )

    try:
        async with async_session() as session:
            try:
                session.add(pub)
                await session.commit()
                await session.refresh(pub)
            except IntegrityError:
                await session.rollback()
                result = await session.execute(
                    select(PublisherRecord).where(PublisherRecord.domain == domain)
                )
                existing = result.scalar_one_or_none()
                if existing:
                    return _to_response(existing)
                raise HTTPException(status_code=409, detail="Domain already registered")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to register publisher domain=%s", domain)
        raise HTTPException(status_code=500, detail="Registration failed")

    return _to_response(pub)


@router.get("/{site_id}", response_model=PublisherResponse)
async def get_publisher(site_id: str):
    """Get publisher info by site_id."""
    async with async_session() as session:
        result = await session.execute(
            select(PublisherRecord).where(PublisherRecord.site_id == site_id)
        )
        pub = result.scalar_one_or_none()

    if not pub:
        raise HTTPException(status_code=404, detail="Publisher not found")
    return _to_response(pub)


@router.get("/by-domain/{domain}", response_model=PublisherResponse)
async def get_publisher_by_domain(domain: str):
    """Look up a publisher by domain. Used by PBS to validate account IDs."""
    domain = domain.lower().strip().lstrip("www.")
    async with async_session() as session:
        result = await session.execute(
            select(PublisherRecord).where(PublisherRecord.domain == domain)
        )
        pub = result.scalar_one_or_none()

    if not pub:
        raise HTTPException(status_code=404, detail="Publisher not found")
    return _to_response(pub)
