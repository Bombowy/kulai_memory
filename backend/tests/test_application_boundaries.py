from __future__ import annotations

import ast
from pathlib import Path


APPLICATION_ROOT = (
    Path(__file__).resolve().parents[1] / "src" / "kulai_memory" / "application"
)
FORBIDDEN_IMPORT_ROOTS = {
    "aiohttp",
    "alembic",
    "asyncpg",
    "fastapi",
    "httpx",
    "kulai_db",
    "kulai_vector_store_pgvector",
    "pgvector",
    "psycopg",
    "psycopg2",
    "PyQt6",
    "PySide6",
    "requests",
    "sqlalchemy",
    "starlette",
    "tkinter",
    "uvicorn",
    "websockets",
}


def test_application_core_has_no_transport_ui_or_database_imports() -> None:
    violations: list[str] = []

    for path in APPLICATION_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)

            for module in modules:
                if module.split(".", maxsplit=1)[0] in FORBIDDEN_IMPORT_ROOTS:
                    violations.append(f"{path.name}:{node.lineno}: {module}")

    assert violations == []
