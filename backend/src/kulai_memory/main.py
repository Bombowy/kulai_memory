"""ASGI entry point for the generated application."""

from .app_factory import create_app

app = create_app()
