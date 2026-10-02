"""Validated non-secret configuration shared by runtime and migrations."""

from __future__ import annotations

from .settings import Settings, get_settings


class DeploymentConfigurationError(ValueError):
    safe_message = "Set KULAI_VECTOR_DIMENSION to a positive integer in backend/.env."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


def require_deployment_settings() -> Settings:
    try:
        settings = get_settings()
    except Exception as exc:
        raise DeploymentConfigurationError from exc
    if settings.kulai_vector_dimension is None:
        raise DeploymentConfigurationError
    return settings


def migration_x_arguments() -> tuple[tuple[str, str], ...]:
    settings = require_deployment_settings()
    return (
        ("kulai_vector_dimension", str(settings.kulai_vector_dimension)),
    )
