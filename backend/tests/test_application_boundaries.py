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
    "kulai_provider_whisper",
    "kulai_vector_store_pgvector",
    "faster_whisper",
    "pgvector",
    "psycopg",
    "psycopg2",
    "PyQt6",
    "PySide6",
    "requests",
    "sounddevice",
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
                if module.startswith("kulai_memory.desktop"):
                    violations.append(f"{path.name}:{node.lineno}: {module}")
                if module.startswith(("kulai_memory.api", "kulai_memory.server")):
                    violations.append(f"{path.name}:{node.lineno}: {module}")

    assert violations == []


def test_desktop_and_server_adapters_remain_independent() -> None:
    package_root = APPLICATION_ROOT.parent

    def imported_modules(root: Path) -> list[str]:
        modules: list[str] = []
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules.append(node.module)
        return modules

    desktop_imports = imported_modules(package_root / "desktop")
    server_imports = imported_modules(package_root / "server")
    server_imports.extend(imported_modules(package_root / "api"))

    assert not any(module.startswith("kulai_memory.server") for module in desktop_imports)
    assert not any(module.startswith("kulai_memory.api") for module in desktop_imports)
    assert not any(module.startswith("kulai_memory.desktop") for module in server_imports)
    assert not any(
        module.split(".", maxsplit=1)[0] in {"PySide6", "sounddevice"}
        for module in server_imports
    )


def test_desktop_ui_does_not_assemble_database_or_whisper_in_widgets() -> None:
    path = APPLICATION_ROOT.parent / "desktop" / "app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    forbidden = {
        "kulai_provider_whisper",
        "sqlalchemy",
        "kulai_memory.persistence",
        "kulai_memory.whisper_provider",
    }
    violations: list[str] = []
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        for module in modules:
            if any(module == item or module.startswith(f"{item}.") for item in forbidden):
                violations.append(f"{node.lineno}: {module}")

    assert violations == []


def test_voice_session_has_no_memory_or_persistence_coupling() -> None:
    path = APPLICATION_ROOT / "voice_session.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.level and node.module in {"memory", "persistence"}:
            violations.append(node.module)
        if node.module.startswith("kulai_memory.persistence"):
            violations.append(node.module)

    assert violations == []
