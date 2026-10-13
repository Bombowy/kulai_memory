"""Explicit security upgrade, protected by full backup and actual restore verification."""
import argparse
import asyncio
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend/src"))

from kulai_memory.database_safety import database_config, run_database_doctor
from kulai_memory.security_persistence.migration import security_migration_heads, upgrade_security_with_verified_backup
from kulai_memory.settings import get_settings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("heads", "check", "upgrade"))
    parser.add_argument("--confirm-main-security-schema", action="store_true")
    parser.add_argument("--backup-output", type=Path)
    args = parser.parse_args(argv)
    if args.command == "upgrade" and (not args.confirm_main_security_schema or args.backup_output is None):
        parser.error("upgrade requires --confirm-main-security-schema and --backup-output")
    async def run():
        if args.command == "heads":
            for head in security_migration_heads():
                print(head)
            return 0
        config = database_config()
        if args.command == "check":
            report = await run_database_doctor(async_url=config.async_url, require_security=True)
            print("Security schema: " + ("PASS" if report.ok else "UNAVAILABLE"))
            return 0 if report.ok else 1
        backup = await upgrade_security_with_verified_backup(args.backup_output, config=config, app_env=get_settings().app_env)
        print("Security schema upgraded; strict doctor PASS. Verified backup retained:", backup.path)
        return 0
    try:
        return asyncio.run(run())
    except Exception:
        print("Security schema operation failed safely; retain any backup.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
