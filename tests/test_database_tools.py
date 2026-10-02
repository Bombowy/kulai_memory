from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "backend" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from kulai_memory.database_safety import (
    BACKUP_TEST_DATABASE_PREFIX,
    CheckResult,
    DoctorReport,
    OwnedTemporaryDatabase,
    PostgresConnection,
    RESTORE_DATABASE_PREFIX,
    database_host_is_loopback,
    drop_owned_temporary_database,
    run_database_doctor,
    run_postgres_tool,
    safe_error_message,
    validate_owned_database_name,
    validate_restore_database_name,
    validate_restore_source,
    vector_embedding_dimension_check,
)
from scripts import db_backup, db_backup_restore_drill, db_restore_smoke, postgres


def test_compose_wrapper_always_uses_backend_env_file() -> None:
    for command in ("up", "down", "status"):
        arguments = postgres.compose_arguments(command)
        env_index = arguments.index("--env-file")
        assert Path(arguments[env_index + 1]) == PROJECT_ROOT / "backend" / ".env"
        assert "config" not in arguments
        assert "-v" not in arguments
        assert arguments[-1] == "postgres"
    assert "stop" in postgres.compose_arguments("down")
    assert "down" not in postgres.compose_arguments("down")


def test_compose_wrapper_rejects_database_url_and_missing_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    monkeypatch.setattr(postgres, "ENV_FILE", env_file)
    for key in postgres.DATABASE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    env_file.write_text(
        "DB_HOST=localhost\nDB_NAME=db\nDB_USER=user\nDB_PORT=5432\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="DB_PASSWORD"):
        postgres.validate_environment_file()

    env_file.write_text(
        "DB_HOST=localhost\nDB_NAME=db\nDB_USER=user\n"
        "DB_PASSWORD=secret\nDB_PORT=5432\n"
        "DATABASE_URL=postgresql://user:secret@localhost/db\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="DATABASE_URL") as caught:
        postgres.validate_environment_file()
    assert "secret" not in str(caught.value)

    env_file.write_text(
        "DB_HOST=db.example.test\nDB_NAME=db\nDB_USER=user\n"
        "DB_PASSWORD=secret\nDB_PORT=5432\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="loopback"):
        postgres.validate_environment_file()


def test_compose_subprocess_environment_removes_database_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in postgres.DATABASE_ENV_KEYS:
        monkeypatch.setenv(key, "sentinel-secret")
    environment = postgres.compose_subprocess_environment()
    assert all(key not in environment for key in postgres.DATABASE_ENV_KEYS)


@pytest.mark.parametrize(
    ("url", "expected"),
    (
        ("postgresql+asyncpg://u:p@localhost/db", True),
        ("postgresql+asyncpg://u:p@127.0.0.1/db", True),
        ("postgresql+asyncpg://u:p@[::1]/db", True),
        ("postgresql+asyncpg://u:p@db.example.test/db", False),
    ),
)
def test_postgres_integration_loopback_guard(url: str, expected: bool) -> None:
    class Config:
        async_url = url

    assert database_host_is_loopback(Config()) is expected


def test_postgres_command_keeps_password_off_arguments_and_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    connection = PostgresConnection(
        host="localhost",
        port=5432,
        user="kulai",
        password="super-secret-value",
        database="kulai_memory",
    )
    run_postgres_tool(
        Path("pg_dump"),
        connection.command_arguments(),
        connection=connection,
    )

    assert "super-secret-value" not in repr(connection)
    assert "super-secret-value" not in " ".join(captured["arguments"])
    assert captured["environment"]["PGPASSWORD"] == "super-secret-value"


def test_safe_error_message_never_includes_exception_details() -> None:
    secret = "postgresql://user:sentinel-secret@localhost/db"
    message = safe_error_message(RuntimeError(secret), operation="Database check")
    assert "sentinel-secret" not in message
    assert secret not in message


def test_doctor_contains_no_ddl_or_dml() -> None:
    source = inspect.getsource(run_database_doctor).upper()
    for keyword in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP ", "TRUNCATE "):
        assert keyword not in source
    assert "SET TRANSACTION READ ONLY" in source


def test_vector_dimension_check_accepts_configured_vector_type() -> None:
    check = vector_embedding_dimension_check(
        configured_dimension=1024,
        actual_type="vector(1024)",
    )
    assert check.ok is True
    assert check.value == {
        "configured_dimension": 1024,
        "actual_type": "vector(1024)",
        "actual_dimension": 1024,
    }


@pytest.mark.parametrize(
    "actual_type",
    ("vector(768)", "vector", "double precision[]", None),
)
def test_vector_dimension_check_rejects_mismatch_or_wrong_type(
    actual_type: object,
) -> None:
    check = vector_embedding_dimension_check(
        configured_dimension=1024,
        actual_type=actual_type,
    )
    assert check.ok is False


@pytest.mark.parametrize("configured_dimension", (None, 0, -1, True, "1024"))
def test_vector_dimension_check_rejects_missing_or_invalid_configuration(
    configured_dimension: object,
) -> None:
    check = vector_embedding_dimension_check(
        configured_dimension=configured_dimension,
        actual_type="vector(1024)",
    )
    assert check.ok is False
    assert check.value["configured_dimension"] is None


def test_backup_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    output = tmp_path / "backup.dump"
    output.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        asyncio.run(db_backup.create_backup(output, force=False))
    assert output.read_bytes() == b"existing"


def test_failed_backup_removes_only_its_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = DoctorReport(
        (
            CheckResult("alembic.expected_head", True, "head"),
            CheckResult(
                "postgres.version",
                True,
                {"display": "18.6", "number": "180006"},
            ),
        )
    )

    async def fake_doctor(**kwargs):
        del kwargs
        return report

    monkeypatch.setattr(db_backup, "run_database_doctor", fake_doctor)
    monkeypatch.setattr(db_backup, "find_postgres_tool", lambda name: Path(name))
    monkeypatch.setattr(db_backup, "postgres_tool_version", lambda path: "18.6")
    monkeypatch.setattr(
        db_backup,
        "database_config",
        lambda: SimpleNamespace(async_url="postgresql+asyncpg://test/db"),
    )
    monkeypatch.setattr(
        db_backup,
        "postgres_connection",
        lambda config: PostgresConnection("localhost", 5432, "user", "secret", "db"),
    )
    monkeypatch.setattr(
        db_backup,
        "run_postgres_tool",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", "failed"),
    )
    output = tmp_path / "backup.dump"

    with pytest.raises(RuntimeError):
        asyncio.run(db_backup.create_backup(output, force=False))

    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_successful_backup_is_atomic_and_uses_portable_archive_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = DoctorReport(
        (
            CheckResult("alembic.expected_head", True, "head"),
            CheckResult(
                "postgres.version",
                True,
                {"display": "18.6", "number": "180006"},
            ),
        )
    )
    captured: dict[str, object] = {}

    async def fake_doctor(**kwargs):
        del kwargs
        return report

    def fake_run(executable, arguments, *, connection):
        del executable, connection
        captured["arguments"] = arguments
        partial = Path(arguments[arguments.index("--file") + 1])
        partial.write_bytes(b"PGDMP test archive")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(db_backup, "run_database_doctor", fake_doctor)
    monkeypatch.setattr(db_backup, "find_postgres_tool", lambda name: Path(name))
    monkeypatch.setattr(db_backup, "postgres_tool_version", lambda path: "18.6")
    monkeypatch.setattr(
        db_backup,
        "database_config",
        lambda: SimpleNamespace(async_url="postgresql+asyncpg://test/db"),
    )
    monkeypatch.setattr(
        db_backup,
        "postgres_connection",
        lambda config: PostgresConnection("localhost", 5432, "user", "secret", "db"),
    )
    monkeypatch.setattr(db_backup, "run_postgres_tool", fake_run)
    output = tmp_path / "backup.dump"

    size, digest, version = asyncio.run(db_backup.create_backup(output, force=False))

    arguments = captured["arguments"]
    assert "--format=custom" in arguments
    assert "--no-owner" in arguments
    assert "--no-privileges" in arguments
    assert output.read_bytes() == b"PGDMP test archive"
    assert size == output.stat().st_size
    assert len(digest) == 64
    assert version == "18.6"
    assert list(tmp_path.glob("*.partial")) == []


def test_restore_target_guard_accepts_only_generated_names() -> None:
    source = "kulai_memory"
    valid = f"{RESTORE_DATABASE_PREFIX}{'a' * 32}"
    validate_restore_database_name(valid, source_database=source)
    for invalid in (
        source,
        "postgres",
        "kulai_memory_restore_test_existing",
        f"{RESTORE_DATABASE_PREFIX}{'g' * 32}",
    ):
        with pytest.raises(ValueError):
            validate_restore_database_name(invalid, source_database=source)


def test_backup_source_guard_accepts_only_generated_names() -> None:
    source = "kulai_memory"
    valid = f"{BACKUP_TEST_DATABASE_PREFIX}{'b' * 32}"
    validate_owned_database_name(
        valid,
        source_database=source,
        kind="backup",
    )
    for invalid in (
        source,
        "postgres",
        "kulai_memory_backup_test_existing",
        f"{BACKUP_TEST_DATABASE_PREFIX}{'g' * 32}",
    ):
        with pytest.raises(ValueError):
            validate_owned_database_name(
                invalid,
                source_database=source,
                kind="backup",
            )


def test_restore_source_guard_requires_local_non_system_development_database() -> None:
    class Config:
        def __init__(self, url: str) -> None:
            self.async_url = url

    valid = Config("postgresql+asyncpg://u:p@localhost/kulai_memory")
    connection = validate_restore_source(valid, app_env="dev")
    assert connection.database == "kulai_memory"

    with pytest.raises(ValueError, match="APP_ENV"):
        validate_restore_source(valid, app_env="production")
    with pytest.raises(ValueError, match="loopback"):
        validate_restore_source(
            Config("postgresql+asyncpg://u:p@db.example.test/kulai_memory"),
            app_env="dev",
        )
    for reserved in ("postgres", "template0", "template1"):
        with pytest.raises(ValueError, match="not a safe restore source"):
            validate_restore_source(
                Config(f"postgresql+asyncpg://u:p@localhost/{reserved}"),
                app_env="test",
            )
    for prefix in (BACKUP_TEST_DATABASE_PREFIX, RESTORE_DATABASE_PREFIX):
        with pytest.raises(ValueError, match="not a safe restore source"):
            validate_restore_source(
                Config(f"postgresql+asyncpg://u:p@localhost/{prefix}{'a' * 32}"),
                app_env="test",
            )


def test_restore_command_cannot_clean_or_accept_a_target() -> None:
    source = inspect.getsource(db_restore_smoke)
    assert '"--clean"' not in source
    actions = [action.dest for action in db_restore_smoke.parser()._actions]
    assert actions == ["help", "backup"]


def test_owned_database_cleanup_requires_marker_and_never_forces_drop() -> None:
    source = inspect.getsource(drop_owned_temporary_database)
    assert "require_owned_database" in source
    assert '"--force"' not in source


def test_data_drill_accepts_no_database_names() -> None:
    actions = [action.dest for action in db_backup_restore_drill.parser()._actions]
    assert actions == ["help"]


def test_owned_backup_api_rechecks_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = OwnedTemporaryDatabase(
        name=f"{BACKUP_TEST_DATABASE_PREFIX}{'c' * 32}",
        marker=f"kulai-memory-backup-test:{'d' * 32}",
        kind="backup",
    )
    verified: list[OwnedTemporaryDatabase] = []

    async def fake_require(candidate, *, config):
        del config
        verified.append(candidate)

    async def fake_create(output, *, force, config):
        del output, force
        assert config == "target-config"
        return (1, "a" * 64, "18.6")

    monkeypatch.setattr(db_backup, "require_owned_database", fake_require)
    monkeypatch.setattr(
        db_backup,
        "database_config_for_database",
        lambda database, *, config: "target-config",
    )
    monkeypatch.setattr(db_backup, "_create_backup", fake_create)
    result = asyncio.run(
        db_backup.create_owned_database_backup(
            tmp_path / "drill.dump",
            force=False,
            owned=owned,
            config=object(),
        )
    )
    assert verified == [owned]
    assert result == (1, "a" * 64, "18.6")
