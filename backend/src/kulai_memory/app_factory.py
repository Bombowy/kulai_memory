"""FastAPI application factory backed by KulAI core."""

from fastapi import FastAPI
from kulai_fastapi_core import create_kulai_app

from .api.health import create_health_router
from .api.memory_ws import RuntimeFactory, create_memory_ws_router
from .settings import get_settings


def create_app(*, voice_runtime_factory: RuntimeFactory | None = None) -> FastAPI:
    """Build the generated host application."""

    settings = get_settings()
    memory_router = (
        create_memory_ws_router()
        if voice_runtime_factory is None
        else create_memory_ws_router(runtime_factory=voice_runtime_factory)
    )
    return create_kulai_app(
        title=settings.app_name,
        version="0.1.0",
        debug=settings.debug,
        routers=(create_health_router(), memory_router),
    )
