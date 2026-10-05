"""Environment-backed host application settings."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Environment-backed host runtime settings."""

    model_config = SettingsConfigDict(
        env_file=BACKEND_ROOT / ".env", env_prefix="", extra="ignore"
    )

    app_name: str = "KulAI Memory"
    app_env: str = "dev"
    debug: bool = False
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "kulai"
    db_password: str = "replace-me"
    db_name: str = "kulai_memory"
    database_url: str | None = None
    kulai_vector_dimension: int | None = Field(default=None, gt=0)
    kulai_whisper_model: str = "large-v3"
    kulai_whisper_device: str = "cuda"
    kulai_whisper_compute_type: str = "int8_float16"
    kulai_whisper_vad_filter: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one immutable-by-convention settings snapshot."""

    return Settings()
