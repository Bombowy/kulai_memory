"""Run project migrations against the database configured in backend/.env."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
SRC_ROOT = BACKEND_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from kulai_memory.deployment import (
    DeploymentConfigurationError,
    migration_x_arguments,
)

ENV_FILE = BACKEND_ROOT / ".env"
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=("heads", "current", "upgrade"))
    return result


def alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


def require_environment() -> bool:
    if ENV_FILE.is_symlink() or not ENV_FILE.is_file():
        print(
            "backend/.env is missing. Create it from backend/.env.example "
            "before running migrations.",
            file=sys.stderr,
        )
        return False
    return True


def configure_x_arguments(config: Config) -> bool:
    try:
        arguments = migration_x_arguments()
    except DeploymentConfigurationError as exc:
        print(exc.safe_message, file=sys.stderr)
        return False
    config.cmd_opts = argparse.Namespace(
        x=[f"{name}={value}" for name, value in arguments]
    )
    return True


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = alembic_config()
    try:
        if args.command == "heads":
            for head in ScriptDirectory.from_config(config).get_heads():
                print(head)
            return 0
        if not require_environment():
            return 2
        if not configure_x_arguments(config):
            return 2
        if args.command == "current":
            command.current(config)
        else:
            command.upgrade(config, "head")
    except Exception:
        print("Migration command failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
