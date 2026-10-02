"""Run the canonical local PostgreSQL Compose workflow."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / "backend" / ".env"
COMPOSE_FILE = PROJECT_ROOT / "compose.yaml"
REQUIRED_ENV_KEYS = ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME")
DATABASE_ENV_KEYS = (*REQUIRED_ENV_KEYS, "DATABASE_URL")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=("up", "down", "status"))
    return result


def compose_arguments(command: str) -> list[str]:
    arguments = [
        "docker",
        "compose",
        "--env-file",
        str(ENV_FILE),
        "-f",
        str(COMPOSE_FILE),
    ]
    if command == "up":
        return [*arguments, "up", "-d", "postgres"]
    if command == "down":
        return [*arguments, "stop", "postgres"]
    return [*arguments, "ps", "postgres"]


def _environment_key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            raise ValueError(f"backend/.env line {line_number} is invalid.")
        key, value = stripped.split("=", maxsplit=1)
        key = key.strip()
        if not key:
            raise ValueError(f"backend/.env line {line_number} has no key.")
        values[key] = value.strip()
    return values


def _unquoted(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def validate_environment_file() -> None:
    if ENV_FILE.is_symlink() or not ENV_FILE.is_file():
        raise FileNotFoundError(
            "backend/.env is missing; create it from backend/.env.example."
        )
    values = _environment_key_values(ENV_FILE)
    missing = [
        key
        for key in REQUIRED_ENV_KEYS
        if values.get(key, "").strip() in {"", '""', "''"}
    ]
    if missing:
        raise ValueError(
            "backend/.env is missing required Compose keys: " + ", ".join(missing)
        )
    if _unquoted(values["DB_HOST"]).casefold() not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("DB_HOST must be a loopback host for local Compose.")
    try:
        port = int(_unquoted(values["DB_PORT"]))
    except ValueError as exc:
        raise ValueError("DB_PORT must be an integer between 1 and 65535.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("DB_PORT must be an integer between 1 and 65535.")
    if values.get("DATABASE_URL", "").strip() not in {"", '""', "''"}:
        raise ValueError(
            "DATABASE_URL must be empty for the canonical local Compose workflow; "
            "use DB_* values from backend/.env."
        )
    overrides = [key for key in DATABASE_ENV_KEYS if os.environ.get(key)]
    if overrides:
        raise ValueError(
            "Unset database environment overrides before using the local Compose "
            "workflow: "
            + ", ".join(overrides)
        )


def compose_subprocess_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in DATABASE_ENV_KEYS:
        environment.pop(key, None)
    return environment


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        validate_environment_file()
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if shutil.which("docker") is None:
        print("Docker is not available; install or start it separately.", file=sys.stderr)
        return 2
    completed = subprocess.run(
        compose_arguments(args.command),
        cwd=PROJECT_ROOT,
        check=False,
        env=compose_subprocess_environment(),
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
