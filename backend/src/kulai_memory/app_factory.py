"""FastAPI application factory backed by KulAI core."""

from contextlib import asynccontextmanager
from collections.abc import Callable

from fastapi import FastAPI
from kulai_fastapi_core import create_kulai_app
from kulai_fastapi_core.logging_middleware import RequestLoggingMiddleware

from .api.health import create_health_router
from .api.memory_ws import RuntimeFactory, create_memory_ws_router
from .api.memory_http import create_memory_http_router
from .api.speech_http import create_speech_http_router
from .api.assistant_ws import create_assistant_ws_router
from .settings import get_settings
from .security import Authenticator, AuthorizationPolicy, SecurityConfig, SecurityMode, SecurityReader
from .security.storage import DenyAllSecurityReader
from .api.authentication import AuthenticationMiddleware
from .api.pairing_http import create_pairing_router
from .api.session_http import create_session_router
from .security.sessions import SessionService
from .pairing.domain import PairingService
from .security_persistence.runtime import PostgresSecurityRuntime, ReaderBinding
from .remote_ingress import RemoteIngressConfig, RemoteIngressError


def create_app(*, voice_runtime_factory: RuntimeFactory | None = None,
               security_config: SecurityConfig | None = None,
               security_reader: SecurityReader | None = None,
               authenticator: Authenticator | None = None,
               authorization_policy: AuthorizationPolicy | None = None,
               security_runtime_factory: Callable[[], PostgresSecurityRuntime] | None = None,
               pairing_service: PairingService | None = None,
               session_service: SessionService | None = None,
               remote_ingress_config: RemoteIngressConfig | None = None) -> FastAPI:
    """Build the generated host application."""

    settings = get_settings()
    config = security_config or SecurityConfig(mode=settings.kulai_security_mode)
    if authenticator is not None and security_reader is not None:
        raise ValueError("Configure one authentication source.")
    if security_runtime_factory is not None and (authenticator is not None or security_reader is not None):
        raise ValueError("Configure one authentication source.")
    managed = config.mode is SecurityMode.SECURE and authenticator is None and security_reader is None
    remote = None
    if remote_ingress_config is not None and not isinstance(remote_ingress_config, RemoteIngressConfig):
        raise RemoteIngressError()
    if remote_ingress_config is not None and remote_ingress_config.enabled:
        from .remote_ingress.boundary import RemoteBinding
        if not managed or pairing_service is not None or session_service is not None:
            raise RemoteIngressError()
        remote = RemoteBinding(remote_ingress_config, config)
    binding = ReaderBinding() if managed else None
    memory_router = (
        create_memory_ws_router()
        if voice_runtime_factory is None
        else create_memory_ws_router(runtime_factory=voice_runtime_factory)
    )
    app = create_kulai_app(
        title=settings.app_name,
        version="0.1.0",
        debug=False,  # Never return debug tracebacks containing private input.
        register_exceptions=False,  # Vendor fallback logs/reflects raw request paths.
        docs_url="/docs" if config.mode is SecurityMode.LOCAL_DEV else None,
        openapi_url="/openapi.json" if config.mode is SecurityMode.LOCAL_DEV else None,
        redoc_url="/redoc" if config.mode is SecurityMode.LOCAL_DEV else None,
        routers=(create_health_router(), memory_router, create_memory_http_router(), create_assistant_ws_router(),
                 create_speech_http_router(), create_pairing_router(service=pairing_service),
                 create_session_router(service=session_service)),
    )
    # PL: logger vendor ufa nagłówkom klienta. EN: replace at the host boundary;
    # log only fixed operation/status fields, without modifying the submodule.
    app.user_middleware = [m for m in app.user_middleware if m.cls is not RequestLoggingMiddleware]
    app.state.remote_ingress_binding = remote
    if binding is not None:
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(active_app):
            security = (security_runtime_factory or PostgresSecurityRuntime)()
            active_app.state.security_runtime = security
            try:
                if remote is not None:
                    security.configure_recovery_gate(remote.config.recovery_gate)
                await security.startup()
                if remote is not None:
                    remote.runtime = security
                    await remote.require_ready()
                if security.ready:
                    binding.reader = security
                    async with original_lifespan(active_app) as state:
                        yield state
                else:
                    # PL/EN: no AI startup/reconciliation while security is unavailable.
                    yield
            finally:
                if remote is not None:
                    remote.runtime = None
                binding.reader = None
                await security.shutdown()

        app.router.lifespan_context = lifespan
    app.add_middleware(AuthenticationMiddleware, config=config,
        authenticator=authenticator or Authenticator(security_reader or binding or DenyAllSecurityReader()),
        policy=authorization_policy or AuthorizationPolicy(), remote_binding=remote)
    if remote is not None:
        from .remote_ingress.boundary import RemoteIngressGuard
        app.add_middleware(RemoteIngressGuard, binding=remote)
    return app
