from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _git(*arguments: str) -> str:
    command = ["git", *arguments]
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise AssertionError("Git is required for reproducibility checks") from error

    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip()
        raise AssertionError(
            f"{' '.join(command)} failed with exit code {completed.returncode}: "
            f"{details or 'no diagnostic output'}"
        )
    return completed.stdout.strip()


def test_current_manifest_matches_gitlink() -> None:
    manifest = json.loads(
        (PROJECT_ROOT / "kulai.project.json").read_text(encoding="utf-8")
    )
    manifest_commit = manifest["kulai"]["commit"]
    vendor_relative_path = "vendor/kulai_modules"
    vendor_path = PROJECT_ROOT / "vendor" / "kulai_modules"
    stage_line = _git("ls-files", "--stage", "--", vendor_relative_path)

    assert stage_line, f"{vendor_relative_path} is not tracked as a gitlink"
    stage_fields = stage_line.split(maxsplit=3)
    assert len(stage_fields) == 4, (
        f"unexpected git index entry for {vendor_relative_path}: {stage_line!r}"
    )
    git_mode, gitlink_commit, _stage, indexed_path = stage_fields
    assert indexed_path == vendor_relative_path, (
        f"unexpected git index path for vendor: {indexed_path!r}"
    )
    assert git_mode == "160000", (
        f"{vendor_relative_path} must be a gitlink with mode 160000, got {git_mode}"
    )
    assert vendor_path.is_dir(), (
        f"{vendor_relative_path} is not initialized; initialize the required submodule"
    )

    vendor_toplevel = Path(
        _git("-C", str(vendor_path), "rev-parse", "--show-toplevel")
    ).resolve()
    assert vendor_toplevel == vendor_path.resolve(), (
        f"{vendor_relative_path} is not initialized as its own Git worktree"
    )
    vendor_head = _git("-C", str(vendor_path), "rev-parse", "HEAD")
    vendor_status = _git("-C", str(vendor_path), "status", "--porcelain")

    assert manifest_commit == gitlink_commit, (
        "current KulAI manifest pin does not match the vendor gitlink: "
        f"manifest={manifest_commit}, gitlink={gitlink_commit}"
    )
    assert vendor_head == gitlink_commit, (
        "checked-out vendor HEAD does not match the vendor gitlink: "
        f"vendor_head={vendor_head}, gitlink={gitlink_commit}"
    )
    assert vendor_status == "", (
        f"{vendor_relative_path} working tree must be clean:\n{vendor_status}"
    )


def test_migration_graph_has_exactly_one_head() -> None:
    config = Config(str(PROJECT_ROOT / "backend" / "alembic.ini"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1


def test_reusable_migration_provenance_matches_copied_revision() -> None:
    provenance = json.loads(
        (PROJECT_ROOT / ".kulai" / "migrations.json").read_text(encoding="utf-8")
    )
    revision = "kvectorstorepg_0001"
    migration = (
        PROJECT_ROOT
        / "backend"
        / "migrations"
        / "versions"
        / "kvectorstorepg_0001_initial.py"
    )

    assert provenance["source_heads"] == [revision]
    assert provenance["revision_fingerprints"][revision] == hashlib.sha256(
        migration.read_bytes()
    ).hexdigest()


def test_compose_pins_pgvector_postgres_18_image() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "image: pgvector/pgvector:0.8.6-pg18-bookworm" in compose
    assert "image: postgres:" not in compose
    assert "image: pgvector/pgvector:latest" not in compose
    assert ":-" not in compose
    for key in ("DB_NAME", "DB_USER", "DB_PASSWORD", "DB_PORT"):
        assert f"${{{key}:?" in compose


def test_persistence_has_no_hidden_transaction_or_create_all_calls() -> None:
    source_root = PROJECT_ROOT / "backend" / "src" / "kulai_memory"
    violations: list[str] = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Attribute) and function.attr == "create_all":
                violations.append(f"{path}: create_all")
            if (
                "persistence" in path.parts
                and isinstance(function, ast.Attribute)
                and function.attr in {"commit", "rollback"}
            ):
                violations.append(f"{path}: {function.attr}")
    assert violations == []
