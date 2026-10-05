from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_desktop_launcher_is_in_process_without_web_server() -> None:
    path = ROOT / "scripts" / "desktop.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    assert "kulai_memory.desktop.app" in imports
    assert not any(
        module.split(".", maxsplit=1)[0] in {"fastapi", "uvicorn", "httpx"}
        for module in imports
    )
