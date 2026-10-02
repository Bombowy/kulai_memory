"""Register resolved KulAI ORM models without touching the database."""

from kulai_vector_store_pgvector import register_orm_models as register_vector_store_pgvector_models
from .deployment import require_deployment_settings

def register_orm_models() -> None:
    """Populate shared metadata for Alembic and runtime startup."""

    settings = require_deployment_settings()
    register_vector_store_pgvector_models(dimension=settings.kulai_vector_dimension)
