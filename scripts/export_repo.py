"""Export the repository's reviewable text files into one UTF-8 document."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_NAME = "kod_repo_do_analizy.txt"
VENDOR_PATH = Path("vendor/kulai_modules")
MAX_FILE_SIZE_BYTES = 2_000_000

TEXT_EXTENSIONS = frozenset(
    {
        ".bat",
        ".cfg",
        ".cmd",
        ".css",
        ".graphql",
        ".gradle",
        ".html",
        ".in",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".kts",
        ".mako",
        ".md",
        ".pro",
        ".properties",
        ".proto",
        ".ps1",
        ".py",
        ".pyi",
        ".scss",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
TEXT_FILENAMES = frozenset(
    {
        ".dockerignore",
        ".env.example",
        ".gitignore",
        ".gitmodules",
        "Dockerfile",
        "LICENSE",
        "Makefile",
        "README",
    }
)
EXCLUDED_REPORT_FILENAMES = frozenset(
    {
        DEFAULT_OUTPUT_NAME,
        "audyt.txt",
        "auth_final_hardening.txt",
        "auth_hardening.txt",
        "kod_do_analizy.txt",
        "kulai_billing_access_module.txt",
        "kulai_billing_access_polish.txt",
        "moj_kod_do_oceny.txt",
        "payments_processing_recovery.txt",
        "payments_processing_recovery_fix.txt",
        "plans.txt",
        "tasks.txt",
        "wynik.txt",
    }
)
SECRET_FILENAMES = frozenset(
    {
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "secrets.json",
        "secrets.toml",
        "secrets.yaml",
        "secrets.yml",
    }
)
SECRET_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})
SEPARATOR = "=" * 80


class ExportError(RuntimeError):
    """A controlled failure while collecting or writing the export."""


@dataclass(frozen=True, slots=True)
class ExportStats:
    output_path: Path
    file_count: int
    skipped_count: int
    output_size: int
    vendor_included: bool


def _git_files(repository: Path) -> list[Path]:
    command = (
        "git",
        "-C",
        str(repository),
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ExportError("Git is required to collect repository files.") from exc
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.decode("utf-8", errors="replace").strip()
        message = details or "Git could not list repository files."
        raise ExportError(message) from exc

    try:
        names = completed.stdout.decode("utf-8").split("\0")
    except UnicodeDecodeError as exc:
        raise ExportError("Git returned a file name that is not valid UTF-8.") from exc
    return [Path(name) for name in names if name]


def collect_repository_files(root: Path, *, include_vendor: bool) -> list[Path]:
    """Collect tracked and non-ignored untracked files in stable order."""

    root = root.resolve()
    relative_paths = {
        path for path in _git_files(root) if not path.is_relative_to(VENDOR_PATH)
    }

    if include_vendor:
        vendor_root = root / VENDOR_PATH
        if not vendor_root.is_dir():
            raise ExportError(f"Vendor directory is missing: {vendor_root}")
        relative_paths.update(VENDOR_PATH / path for path in _git_files(vendor_root))

    return sorted(relative_paths, key=lambda path: path.as_posix())


def _is_secret_file(path: Path) -> bool:
    name = path.name.lower()
    if name == ".env.example":
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    if name in SECRET_FILENAMES:
        return True
    return path.suffix.lower() in SECRET_SUFFIXES


def _is_supported_text_file(path: Path) -> bool:
    return path.name in TEXT_FILENAMES or path.suffix.lower() in TEXT_EXTENSIONS


def _is_output_file(path: Path, output_path: Path) -> bool:
    try:
        return path.resolve() == output_path.resolve()
    except OSError:
        return False


def should_export_file(path: Path, *, output_path: Path) -> bool:
    """Return whether a candidate is safe and useful for a code review."""

    if path.is_symlink() or not path.is_file():
        return False
    if _is_output_file(path, output_path):
        return False
    if path.name.lower() in EXCLUDED_REPORT_FILENAMES:
        return False
    if _is_secret_file(path) or not _is_supported_text_file(path):
        return False
    try:
        if path.stat().st_size > MAX_FILE_SIZE_BYTES:
            return False
        with path.open("rb") as source:
            if b"\0" in source.read(8192):
                return False
    except OSError:
        return False
    return True


def _write_header(
    output: TextIO,
    *,
    root: Path,
    vendor_included: bool,
) -> None:
    output.write("KulAI Memory repository export\n")
    output.write(f"Repository: {root.name}\n")
    output.write(f"Vendor included: {'yes' if vendor_included else 'no'}\n")
    output.write(
        "Generated from Git-tracked and non-ignored untracked text files. "
        "Secrets, binaries, symlinks, large files, reports, and generated "
        "exports are omitted.\n"
    )


def _write_file(output: TextIO, *, relative_path: Path, content: str) -> None:
    output.write(f"\n\n{SEPARATOR}\n")
    output.write(f"FILE: {relative_path.as_posix()}\n")
    output.write(f"{SEPARATOR}\n")
    output.write(content)
    if content and not content.endswith("\n"):
        output.write("\n")


def export_repository(
    root: Path,
    output_path: Path,
    *,
    include_vendor: bool = False,
) -> ExportStats:
    """Write a safe, deterministic repository export and return its statistics."""

    root = root.resolve()
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    candidates = collect_repository_files(root, include_vendor=include_vendor)

    exported_count = 0
    skipped_count = 0
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            _write_header(
                temporary,
                root=root,
                vendor_included=include_vendor,
            )
            for relative_path in candidates:
                path = root / relative_path
                if not should_export_file(path, output_path=output_path):
                    skipped_count += 1
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    skipped_count += 1
                    continue
                _write_file(
                    temporary,
                    relative_path=relative_path,
                    content=content,
                )
                exported_count += 1

        os.replace(temporary_path, output_path)
        temporary_path = None
    except OSError as exc:
        raise ExportError(f"Could not write export: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return ExportStats(
        output_path=output_path,
        file_count=exported_count,
        skipped_count=skipped_count,
        output_size=output_path.stat().st_size,
        vendor_included=include_vendor,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--output",
        type=Path,
        default=Path(DEFAULT_OUTPUT_NAME),
        help=f"output path, relative to the repository (default: {DEFAULT_OUTPUT_NAME})",
    )
    result.add_argument(
        "--include-vendor",
        action="store_true",
        help="include files from vendor/kulai_modules",
    )
    return result


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path = PROJECT_ROOT,
) -> int:
    args = parser().parse_args(argv)
    output_path = args.output
    if not output_path.is_absolute():
        output_path = root / output_path
    try:
        stats = export_repository(
            root,
            output_path,
            include_vendor=args.include_vendor,
        )
    except ExportError as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1

    print(f"Saved: {stats.output_path}")
    print(f"Files exported: {stats.file_count}")
    print(f"Files skipped: {stats.skipped_count}")
    print(f"Size: {stats.output_size} bytes")
    print(f"Vendor included: {'yes' if stats.vendor_included else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
