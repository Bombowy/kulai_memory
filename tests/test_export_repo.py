from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.export_repo import (
    MAX_FILE_SIZE_BYTES,
    export_repository,
    main,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ("git", "init", "--quiet", str(path)),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _git_add(root: Path, *paths: str) -> None:
    subprocess.run(
        ("git", "-C", str(root), "add", "--", *paths),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _file_headers(exported: str) -> list[str]:
    return [
        line.removeprefix("FILE: ")
        for line in exported.splitlines()
        if line.startswith("FILE: ")
    ]


def test_export_includes_reviewable_changes_and_omits_unsafe_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "repository"
    _git_init(root)
    _write(root / ".gitignore", ".env\nignored.py\n")
    _write(root / ".env.example", "DATABASE_URL=postgresql://example\n")
    _write(root / ".env", "TOKEN=top-secret\n")
    _write(root / "README.md", "# Zażółć\n")
    _write(root / "src" / "tracked.py", "VALUE = 'śledzony'\n")
    _write(root / "src" / "untracked.py", "VALUE = 'lokalny'\n")
    _write(root / "ignored.py", "IGNORED = True\n")
    _write(root / "credentials.json", '{"token": "top-secret"}\n')
    _write(root / "wynik.txt", "OLD REPORT\n")
    _write(root / "link.py", "SHOULD_NOT_BE_EXPORTED = True\n")
    (root / "binary.py").write_bytes(b"source\0binary")
    (root / "large.py").write_bytes(b"x" * (MAX_FILE_SIZE_BYTES + 1))
    _write(root / "kod_repo_do_analizy.txt", "PREVIOUS EXPORT\n")
    _git_add(
        root,
        ".gitignore",
        ".env.example",
        "README.md",
        "src/tracked.py",
        "wynik.txt",
        "kod_repo_do_analizy.txt",
    )

    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda self: self.name == "link.py" or original_is_symlink(self),
    )

    output = root / "kod_repo_do_analizy.txt"
    stats = export_repository(root, output)
    exported = output.read_text(encoding="utf-8")

    assert stats.vendor_included is False
    assert "FILE: .env.example" in exported
    assert "FILE: README.md" in exported
    assert "FILE: src/tracked.py" in exported
    assert "FILE: src/untracked.py" in exported
    assert "Zażółć" in exported
    assert "top-secret" not in exported
    assert "OLD REPORT" not in exported
    assert "PREVIOUS EXPORT" not in exported
    assert "SHOULD_NOT_BE_EXPORTED" not in exported
    assert "source\x00binary" not in exported
    assert "FILE: ignored.py" not in exported
    assert "FILE: large.py" not in exported
    assert _file_headers(exported) == sorted(_file_headers(exported))
    assert stats.file_count == len(_file_headers(exported))
    assert stats.output_size == output.stat().st_size


def test_vendor_is_optional_and_cli_supports_custom_output(
    tmp_path: Path,
    capsys,
) -> None:
    root = tmp_path / "repository"
    vendor = root / "vendor" / "kulai_modules"
    _git_init(root)
    _git_init(vendor)
    _write(root / "host.py", "HOST = True\n")
    _write(vendor / "provider.py", "PROVIDER = True\n")
    _git_add(root, "host.py")
    _git_add(vendor, "provider.py")

    host_output = root / "host.txt"
    export_repository(root, host_output)
    assert "FILE: host.py" in host_output.read_text(encoding="utf-8")
    assert "vendor/kulai_modules/provider.py" not in host_output.read_text(
        encoding="utf-8"
    )

    full_output = root / "full.txt"
    result = main(
        ("--include-vendor", "--output", "full.txt"),
        root=root,
    )
    stdout = capsys.readouterr().out
    exported = full_output.read_text(encoding="utf-8")

    assert result == 0
    assert "FILE: host.py" in exported
    assert "FILE: vendor/kulai_modules/provider.py" in exported
    assert f"Saved: {full_output.resolve()}" in stdout
    assert "Vendor included: yes" in stdout
