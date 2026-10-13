"""Explicit LOCAL_DEV fixtures with real loopback ASGI addresses, never main DB."""
from fastapi.testclient import TestClient as StarletteTestClient

from kulai_memory.app_factory import create_app
from kulai_memory.security import SecurityConfig, SecurityMode


def create_local_app(**kwargs):
    return create_app(security_config=SecurityConfig(mode=SecurityMode.LOCAL_DEV), **kwargs)


class TestClient(StarletteTestClient):
    __test__ = False

    def __init__(self, app, **kwargs):
        super().__init__(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000), **kwargs)

    def websocket_connect(self, url, **kwargs):
        # Starlette uses ws://testserver for relative WS URLs, independently of base_url.
        if url.startswith("/"):
            url = "ws://127.0.0.1" + url
        return super().websocket_connect(url, **kwargs)
