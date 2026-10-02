"""FastAPI application factory backed by KulAI core."""

from fastapi import FastAPI
from kulai_fastapi_core import create_kulai_app

from .api.health import create_health_router
from .settings import get_settings


def create_app() -> FastAPI:
    """Build the generated host application."""

    settings = get_settings()
    return create_kulai_app(
        title=settings.app_name,
        version="0.1.0",
        debug=settings.debug,
        routers=(create_health_router(),),
    )
