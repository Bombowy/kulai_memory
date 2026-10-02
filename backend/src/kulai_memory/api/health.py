"""Health adapter using the real KulAI health router factory."""

from fastapi import APIRouter
from kulai_fastapi_health import (
    create_health_router as create_kulai_health_router,
)


def create_health_router() -> APIRouter:
    """Create the KulAI liveness router without DB readiness wiring."""

    return create_kulai_health_router(get_db=None)
