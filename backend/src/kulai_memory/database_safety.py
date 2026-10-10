"""Read-only diagnostics and guarded PostgreSQL administration helpers."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue

from alembic.config import Config
from alembic.script import ScriptDirectory
from kulai_db import DbConfig, build_db_config
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from .settings import get_settings


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = PROJECT_ROOT / "backend"
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
BACKUP_TEST_DATABASE_PREFIX = "kulai_memory_backup_test_"
RESTORE_DATABASE_PREFIX = "kulai_memory_restore_test_"
OWNED_DATABASE_PREFIXES = {
    "backup": BACKUP_TEST_DATABASE_PREFIX,
    "restore": RESTORE_DATABASE_PREFIX,
}
SAFE_LOCAL_APP_ENVS = frozenset({"dev", "development", "local", "test"})
RESERVED_DATABASES = frozenset({"postgres", "template0", "template1"})
OwnedDatabaseKind = Literal["backup", "restore"]


@dataclass(frozen=True, slots=True)
class PostgresConnection:
    """Connection fields suitable for PostgreSQL command-line clients."""

    host: str | None
    port: int | None
    user: str | None
    password: str | None = field(repr=False)
    database: str

    def command_arguments(self, *, include_database: bool = True) -> list[str]:
        arguments = ["--no-password"]
        if self.host:
            arguments.extend(("--host", self.host))
        if self.port:
            arguments.extend(("--port", str(self.port)))
        if self.user:
            arguments.extend(("--username", self.user))
        if include_database:
            arguments.extend(("--dbname", self.database))
        return arguments

    def subprocess_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.pop("DATABASE_URL", None)
        environment.pop("DB_PASSWORD", None)
        environment.pop("PGPASSWORD", None)
        if self.password is not None:
            environment["PGPASSWORD"] = self.password
        return environment


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    value: JsonValue | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def as_json_compatible(self) -> dict[str, JsonValue]:
        return {
            "ok": self.ok,
            "checks": [asdict(check) for check in self.checks],
        }


class DatabaseSafetyError(RuntimeError):
    """Controlled administration failure with a safe, host-authored message."""


PRE_MIGRATION_FAILURES = frozenset({
    "alembic.current",
    "table.memory_ingestion_tombstones",
    "schema.memory_ingestion_tombstones",
    "constraint.memory_ingestion_tombstones",
})
_PRE_MIGRATION_REQUIRED_CHECKS = PRE_MIGRATION_FAILURES | frozenset({
    "alembic.expected_head", "database.connection", "database.transaction_read_only",
    "postgres.version", "extension.vector", "table.memories",
    "table.kulai_vector_records", "schema.memories",
    "constraint.memories_ingestion_id_unique", "vector.embedding_dimension",
    "database.read_query",
})


def pre_migration_failures(source: str, head: str) -> frozenset[str]:
    """Enumerate reviewed transitions only, never a generic doctor bypass."""
    if (source, head) == ("kulai_memory_0002", "kulai_memory_0003"):
        return PRE_MIGRATION_FAILURES
    lifecycle = frozenset({"alembic.current", "schema.memories", "constraint.memories_revision_positive"})
    if (source, head) == ("kulai_memory_0003", "kulai_memory_0004"):
        return lifecycle
    if (source, head) == ("kulai_memory_0002", "kulai_memory_0004"):
        return PRE_MIGRATION_FAILURES | lifecycle
    raise DatabaseSafetyError("This pre-migration transition is not supported.")


def validate_backup_doctor(
    report: DoctorReport, *, pre_migration_from: str | None = None,
) -> None:
    """Keep strict defaults; permit only exact reviewed legacy-schema gaps."""

    if pre_migration_from is None:
        if not report.ok:
            raise DatabaseSafetyError("Database invariants failed; full doctor PASS is required.")
        return

    heads = expected_alembic_heads()
    if len(heads) != 1:
        raise DatabaseSafetyError("Pre-migration mode requires exactly one local Alembic head.")
    if pre_migration_from == heads[0]:
        raise DatabaseSafetyError("Source is already the local head; use strict backup/restore.")
    allowed = pre_migration_failures(pre_migration_from, heads[0])

    checks = {check.name: check for check in report.checks}
    if (
        len(checks) != len(report.checks)
        or not (_PRE_MIGRATION_REQUIRED_CHECKS | allowed).issubset(checks)
        or "database.diagnostics" in checks
    ):
        raise DatabaseSafetyError("Pre-migration doctor report is incomplete or contains diagnostics errors.")
    if checks["alembic.expected_head"].value != heads[0]:
        raise DatabaseSafetyError("Doctor and local Alembic head do not agree.")
    if checks["alembic.current"].value != [pre_migration_from]:
        raise DatabaseSafetyError("Database revision does not match the requested pre-migration source.")
    failures = {check.name for check in report.checks if not check.ok}
    if failures != allowed:
        raise DatabaseSafetyError("Doctor failures do not match the exact allowed pre-migration gap.")
    if pre_migration_from == "kulai_memory_0002" and checks["schema.memory_ingestion_tombstones"].value != {"columns": []}:
        raise DatabaseSafetyError("Pre-migration mode requires the pending tombstone table to be absent.")
    if heads[0] == "kulai_memory_0004":
        columns = checks["schema.memories"].value
        if not isinstance(columns, dict):
            raise DatabaseSafetyError("Legacy Memory schema could not be verified.")
        for name, expected in LEGACY_MEMORY_COLUMNS.items():
            if columns.get(name) != dict(type=expected[0], nullable=expected[1], max_length=expected[2]):
                raise DatabaseSafetyError("Legacy Memory schema is incompatible.")
        absent = dict(type=None, nullable=None, max_length=None)
        if any(columns.get(name) != absent for name in ("revision", "archived_at")):
            raise DatabaseSafetyError("Pre-migration lifecycle columns must be absent.")
        if checks["constraint.memories_revision_positive"].value != {"exists": False, "valid": False}:
            raise DatabaseSafetyError("Pre-migration lifecycle constraint must be absent.")


def validate_backup_output(output: Path, *, force: bool) -> Path:
    """Require a regular output outside the repo, without symlink/junction traversal."""

    absolute = Path(os.path.abspath(output.expanduser()))
    for component in (absolute, *absolute.parents):
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            raise ValueError("Backup output must not traverse a symbolic link or junction.")
    resolved = absolute.resolve()
    root = PROJECT_ROOT.resolve()
    if absolute.is_relative_to(root) or resolved.is_relative_to(root):
        raise ValueError("Backup output must be outside the repository.")
    if resolved.exists() and not force:
        raise FileExistsError("Output already exists; pass --force to replace it.")
    if resolved.exists() and not resolved.is_file():
        raise ValueError("Backup output must be a regular file path.")
    if not resolved.parent.is_dir():
        raise FileNotFoundError("Backup output directory does not exist.")
    return resolved


@dataclass(frozen=True, slots=True)
class MemoryFingerprint:
    count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class VectorFingerprint:
    count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class TombstoneFingerprint:
    count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DatabaseSnapshot:
    """Complete durable state; None denotes the absent, pre-migration table."""

    revision: tuple[str, ...]
    memories: MemoryFingerprint
    vectors: VectorFingerprint
    tombstones: TombstoneFingerprint | None

    def __post_init__(self) -> None:
        if self.tombstones is None and self.revision != ("kulai_memory_0002",):
            raise DatabaseSafetyError("Tombstone absence is valid only for the 0002 snapshot.")


@dataclass(frozen=True, slots=True)
class OwnedTemporaryDatabase:
    """Capability identifying one temporary database owned by this process."""

    name: str
    marker: str = field(repr=False)
    kind: OwnedDatabaseKind


def database_config() -> DbConfig:
    """Build the same database configuration used by the host application."""

    return build_db_config(get_settings())


def _url(config: DbConfig) -> URL:
    url = make_url(config.async_url)
    if not url.database:
        raise ValueError("The configured database name is missing.")
    return url


def postgres_connection(config: DbConfig | None = None) -> PostgresConnection:
    url = _url(config or database_config())
    return PostgresConnection(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        database=url.database or "",
    )


def async_database_url(*, database: str, config: DbConfig | None = None) -> str:
    url = _url(config or database_config()).set(database=database)
    return url.render_as_string(hide_password=False)


def database_config_for_database(
    database: str,
    *,
    config: DbConfig | None = None,
) -> DbConfig:
    active_config = config or database_config()
    return active_config.model_copy(
        update={
            "name": database,
            "database_url": async_database_url(
                database=database,
                config=active_config,
            ),
        }
    )


def database_host_is_loopback(config: DbConfig | None = None) -> bool:
    """Return whether the effective database URL resolves to a local host name."""

    host = _url(config or database_config()).host
    if host is None:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_restore_source(
    config: DbConfig,
    *,
    app_env: str,
) -> PostgresConnection:
    """Allow destructive temporary-DB administration only on a local dev server."""

    if app_env.casefold() not in SAFE_LOCAL_APP_ENVS:
        raise ValueError("Restore smoke requires a development or test APP_ENV.")
    if not database_host_is_loopback(config):
        raise ValueError("Restore smoke requires a loopback database host.")
    connection = postgres_connection(config)
    if (
        connection.database.casefold() in RESERVED_DATABASES
        or any(
            connection.database.startswith(prefix)
            for prefix in OWNED_DATABASE_PREFIXES.values()
        )
    ):
        raise ValueError("The configured source database is not a safe restore source.")
    return connection


def expected_alembic_heads() -> tuple[str, ...]:
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    return tuple(sorted(script.get_heads()))


def safe_error_message(exc: BaseException, *, operation: str) -> str:
    """Return an actionable error that cannot include connection credentials."""

    return f"{operation} failed ({type(exc).__name__})."


def _version_key(path: Path) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in path.parents[1].name.split("."))
    except ValueError:
        return ()


def find_postgres_tool(name: str) -> Path:
    """Find a PostgreSQL client without modifying PATH or installing software."""

    resolved = shutil.which(name)
    if resolved:
        return Path(resolved).resolve()

    if os.name == "nt":
        candidates: list[Path] = []
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            root_value = os.environ.get(variable)
            if not root_value:
                continue
            postgres_root = Path(root_value) / "PostgreSQL"
            if not postgres_root.is_dir():
                continue
            candidates.extend(
                candidate
                for candidate in postgres_root.glob(f"*/bin/{name}.exe")
                if candidate.is_file()
            )
        if candidates:
            return max(candidates, key=_version_key).resolve()

    raise FileNotFoundError(
        f"{name} was not found in PATH or a standard PostgreSQL installation."
    )


_VERSION_PATTERN = re.compile(r"(?P<version>\d+(?:\.\d+)+)")


def postgres_tool_version(path: Path) -> str:
    completed = subprocess.run(
        [str(path), "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Could not determine the version of {path.name}.")
    match = _VERSION_PATTERN.search(completed.stdout)
    if match is None:
        raise RuntimeError(f"Could not parse the version of {path.name}.")
    return match.group("version")


def version_major(version: str) -> int:
    return int(version.split(".", maxsplit=1)[0])


def run_postgres_tool(
    executable: Path,
    arguments: list[str],
    *,
    connection: PostgresConnection,
) -> subprocess.CompletedProcess[str]:
    """Run a PostgreSQL client while keeping its password off the command line."""

    return subprocess.run(
        [str(executable), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=connection.subprocess_environment(),
    )


async def memory_fingerprint(connection: AsyncConnection) -> MemoryFingerprint:
    """Hash canonical Memory rows without exposing their contents."""

    digest = hashlib.sha256()
    count = 0
    lifecycle = bool(await connection.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM information_schema.columns
          WHERE table_schema='public' AND table_name='memories' AND column_name='revision')
    """)))
    fields = "id, ingestion_id, content, source_kind, session_id, metadata_json, created_at"
    if lifecycle:
        fields += ", revision, archived_at"
    result = await connection.stream(text(f"SELECT {fields} FROM memories ORDER BY id"))
    async for row in result:
        mapping = row._mapping
        created_at = mapping["created_at"]
        if isinstance(created_at, datetime):
            created_at_value = created_at.astimezone(UTC).isoformat()
        else:
            created_at_value = str(created_at)
        payload = {
                "id": str(mapping["id"]),
                "ingestion_id": str(mapping["ingestion_id"]),
                "content": mapping["content"],
                "source_kind": mapping["source_kind"],
                "session_id": (
                    str(mapping["session_id"])
                    if mapping["session_id"] is not None
                    else None
                ),
                "metadata": mapping["metadata_json"],
                "created_at": created_at_value,
            }
        if lifecycle:
            archived_at = mapping["archived_at"]
            if archived_at is not None and (not isinstance(archived_at, datetime) or archived_at.tzinfo is None):
                raise DatabaseSafetyError("Memory archive timestamp is incompatible.")
            payload.update(revision=mapping["revision"], archived_at=(archived_at.astimezone(UTC).isoformat() if archived_at is not None else None))
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest.update(len(canonical).to_bytes(8, "big"))
        digest.update(canonical)
        count += 1
    return MemoryFingerprint(count=count, sha256=digest.hexdigest())


async def vector_fingerprint(connection: AsyncConnection) -> VectorFingerprint:
    """Hash complete pgvector rows without exposing vector or metadata values."""

    digest = hashlib.sha256()
    count = 0
    result = await connection.stream(
        text(
            """
            SELECT pk, namespace_key, record_id, embedding::text AS embedding_text,
                   vector_dims(embedding) AS embedding_dimension,
                   metadata_json, created_at, updated_at
            FROM kulai_vector_records
            ORDER BY namespace_key, record_id, pk
            """
        )
    )
    async for row in result:
        mapping = row._mapping
        canonical = json.dumps(
            {
                "pk": mapping["pk"],
                "namespace": mapping["namespace_key"],
                "record_id": mapping["record_id"],
                "embedding": mapping["embedding_text"],
                "embedding_dimension": mapping["embedding_dimension"],
                "metadata": mapping["metadata_json"],
                "created_at": mapping["created_at"].astimezone(UTC).isoformat(),
                "updated_at": mapping["updated_at"].astimezone(UTC).isoformat(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest.update(len(canonical).to_bytes(8, "big"))
        digest.update(canonical)
        count += 1
    return VectorFingerprint(count=count, sha256=digest.hexdigest())


async def tombstone_fingerprint(connection: AsyncConnection) -> TombstoneFingerprint:
    """Hash technical identities and UTC timestamps without exposing rows."""

    digest = hashlib.sha256()
    count = 0
    result = await connection.stream(text(
        "SELECT ingestion_id, memory_id, deleted_at "
        "FROM memory_ingestion_tombstones ORDER BY ingestion_id"
    ))
    async for row in result:
        deleted_at = row._mapping["deleted_at"]
        if not isinstance(deleted_at, datetime) or deleted_at.utcoffset() is None:
            raise DatabaseSafetyError("Tombstone timestamp must be timezone-aware.")
        canonical = json.dumps(
            {
                "ingestion_id": str(row._mapping["ingestion_id"]),
                "memory_id": str(row._mapping["memory_id"]),
                "deleted_at": deleted_at.astimezone(UTC).isoformat(timespec="microseconds"),
            },
            ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        digest.update(len(canonical).to_bytes(8, "big"))
        digest.update(canonical)
        count += 1
    return TombstoneFingerprint(count=count, sha256=digest.hexdigest())


async def database_snapshot_url(
    url: str, *, pre_migration_from: str | None = None,
) -> DatabaseSnapshot:
    """Read all durable tables in one consistent, read-only transaction.

    Backup/restore callers additionally require validate_backup_doctor. Absence
    is represented only for explicitly supported source revision 0002.
    """

    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            try:
                await connection.execute(text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                ))
                revisions = tuple((await connection.execute(text(
                    "SELECT version_num FROM alembic_version ORDER BY version_num"
                ))).scalars())
                present = await connection.scalar(text(
                    "SELECT to_regclass('public.memory_ingestion_tombstones') IS NOT NULL"
                ))
                if pre_migration_from is not None:
                    heads = expected_alembic_heads()
                    if len(heads) != 1 or revisions != (pre_migration_from,):
                        raise DatabaseSafetyError("Invalid pre-migration tombstone absence state.")
                    pre_migration_failures(pre_migration_from, heads[0])
                    if pre_migration_from == "kulai_memory_0002":
                        if present:
                            raise DatabaseSafetyError("Legacy tombstone table must be absent.")
                        tombstones = None
                    else:
                        if not present:
                            raise DatabaseSafetyError("Tombstone table is required.")
                        tombstones = await tombstone_fingerprint(connection)
                else:
                    if not present:
                        raise DatabaseSafetyError("Strict snapshot requires the tombstone table.")
                    tombstones = await tombstone_fingerprint(connection)
                return DatabaseSnapshot(
                    revision=revisions,
                    memories=await memory_fingerprint(connection),
                    vectors=await vector_fingerprint(connection),
                    tombstones=tombstones,
                )
            finally:
                await connection.rollback()
    except DatabaseSafetyError:
        raise
    except Exception:
        raise DatabaseSafetyError("Database snapshot could not be read safely.") from None
    finally:
        await engine.dispose()


LEGACY_MEMORY_COLUMNS: dict[str, tuple[str, bool, int | None]] = {
    "id": ("uuid", False, None),
    "ingestion_id": ("uuid", False, None),
    "content": ("text", False, None),
    "source_kind": ("varchar", False, 32),
    "session_id": ("uuid", True, None),
    "metadata_json": ("jsonb", False, None),
    "created_at": ("timestamptz", False, None),
}

EXPECTED_MEMORY_COLUMNS = {
    **LEGACY_MEMORY_COLUMNS,
    "revision": ("int4", False, None),
    "archived_at": ("timestamptz", True, None),
}


_VECTOR_TYPE_PATTERN = re.compile(r"^vector\((?P<dimension>[1-9][0-9]*)\)$")


def vector_embedding_dimension_check(
    *,
    configured_dimension: object,
    actual_type: object,
) -> CheckResult:
    """Compare configured dimension with PostgreSQL's formatted column type."""

    configured_valid = (
        isinstance(configured_dimension, int)
        and not isinstance(configured_dimension, bool)
        and configured_dimension > 0
    )
    match = (
        _VECTOR_TYPE_PATTERN.fullmatch(actual_type)
        if isinstance(actual_type, str)
        else None
    )
    actual_dimension = int(match.group("dimension")) if match else None
    ok = configured_valid and actual_dimension == configured_dimension
    if not configured_valid:
        message = "KULAI_VECTOR_DIMENSION must be a positive integer."
    elif actual_dimension is None:
        message = "The embedding column is not a dimension-bound vector type."
    elif actual_dimension != configured_dimension:
        message = "The embedding column dimension does not match configuration."
    else:
        message = None
    return CheckResult(
        name="vector.embedding_dimension",
        ok=ok,
        value={
            "configured_dimension": (
                configured_dimension if configured_valid else None
            ),
            "actual_type": actual_type if isinstance(actual_type, str) else None,
            "actual_dimension": actual_dimension,
        },
        message=message,
    )


async def _tombstone_schema_checks(connection: AsyncConnection) -> tuple[CheckResult, ...]:
    """Inspect the technical retirement table without reading its records."""

    present = await connection.scalar(
        text("SELECT to_regclass('public.memory_ingestion_tombstones') IS NOT NULL"),
    )
    columns = await connection.execute(text(
        """
        SELECT column_name, udt_name, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'memory_ingestion_tombstones'
        """
    ))
    actual = {
        row.column_name: (row.udt_name, row.is_nullable == "YES", row.column_default)
        for row in columns
    }
    expected = {
        "ingestion_id": ("uuid", False),
        "memory_id": ("uuid", False),
        "deleted_at": ("timestamptz", False),
    }
    schema_ok = (
        set(actual) == set(expected)
        and all(actual[name][:2] == spec for name, spec in expected.items())
        and actual["deleted_at"][2] in {"now()", "CURRENT_TIMESTAMP"}
    )
    constraints = await connection.execute(text(
        """
        SELECT CAST(constraint_record.contype AS text), pg_get_constraintdef(constraint_record.oid)
        FROM pg_constraint AS constraint_record
        JOIN pg_class AS relation ON relation.oid = constraint_record.conrelid
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public'
          AND relation.relname = 'memory_ingestion_tombstones'
        """
    ))
    definitions = set(tuple(row) for row in constraints)
    constraints_ok = (
        ("p", "PRIMARY KEY (ingestion_id)") in definitions
        and ("u", "UNIQUE (memory_id)") in definitions
        and not any(kind == "f" for kind, _ in definitions)
    )
    return (
        CheckResult(name="table.memory_ingestion_tombstones", ok=bool(present)),
        CheckResult(
            name="schema.memory_ingestion_tombstones", ok=schema_ok,
            value={"columns": sorted(actual)},
            message=None if schema_ok else "The ingestion tombstone schema is incompatible.",
        ),
        CheckResult(
            name="constraint.memory_ingestion_tombstones", ok=constraints_ok,
            message=None if constraints_ok else "The ingestion tombstone constraints are incompatible.",
        ),
    )


async def run_database_doctor(*, async_url: str | None = None) -> DoctorReport:
    """Inspect required database invariants using read-only SQL only."""

    checks: list[CheckResult] = []
    try:
        configured_dimension: object = get_settings().kulai_vector_dimension
    except Exception:
        configured_dimension = None
    heads = expected_alembic_heads()
    checks.append(
        CheckResult(
            name="alembic.expected_head",
            ok=len(heads) == 1,
            value=heads[0] if len(heads) == 1 else list(heads),
            message=None if len(heads) == 1 else "The migration graph must have one head.",
        )
    )

    try:
        url = async_url or database_config().async_url
        engine = create_async_engine(url, pool_pre_ping=True)
    except Exception as exc:
        checks.append(
            vector_embedding_dimension_check(
                configured_dimension=configured_dimension,
                actual_type=None,
            )
        )
        checks.append(
            CheckResult(
                name="database.connection",
                ok=False,
                message=safe_error_message(exc, operation="Database configuration"),
            )
        )
        return DoctorReport(tuple(checks))

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            checks.append(CheckResult(name="database.connection", ok=True))
            transaction_read_only = await connection.scalar(
                text("SHOW transaction_read_only")
            )
            checks.append(
                CheckResult(
                    name="database.transaction_read_only",
                    ok=transaction_read_only == "on",
                    value=transaction_read_only,
                )
            )

            server = (
                await connection.execute(
                    text(
                        "SELECT current_setting('server_version'), "
                        "current_setting('server_version_num')"
                    )
                )
            ).one()
            checks.append(
                CheckResult(
                    name="postgres.version",
                    ok=True,
                    value={"display": server[0], "number": server[1]},
                )
            )

            revision_table = await connection.scalar(
                text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
            )
            revisions: tuple[str, ...] = ()
            if revision_table:
                result = await connection.execute(
                    text("SELECT version_num FROM alembic_version ORDER BY version_num")
                )
                revisions = tuple(result.scalars().all())
            expected = heads[0] if len(heads) == 1 else None
            checks.append(
                CheckResult(
                    name="alembic.current",
                    ok=revisions == ((expected,) if expected is not None else ()),
                    value=list(revisions),
                    message=(
                        None
                        if revisions == ((expected,) if expected is not None else ())
                        else "Database revision does not match the local migration head."
                    ),
                )
            )

            vector_version = await connection.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
            checks.append(
                CheckResult(
                    name="extension.vector",
                    ok=vector_version is not None,
                    value=vector_version,
                    message=None if vector_version else "The vector extension is missing.",
                )
            )

            table_row = (
                await connection.execute(
                    text(
                        "SELECT "
                        "to_regclass('public.memories') IS NOT NULL, "
                        "to_regclass('public.kulai_vector_records') IS NOT NULL"
                    )
                )
            ).one()
            checks.extend(
                (
                    CheckResult(name="table.memories", ok=bool(table_row[0])),
                    CheckResult(
                        name="table.kulai_vector_records", ok=bool(table_row[1])
                    ),
                )
            )

            columns_result = await connection.execute(
                text(
                    """
                    SELECT column_name, udt_name, is_nullable,
                           character_maximum_length
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'memories'
                    """
                )
            )
            actual_columns = {
                row.column_name: (
                    row.udt_name,
                    row.is_nullable == "YES",
                    row.character_maximum_length,
                )
                for row in columns_result
            }
            schema_ok = all(
                actual_columns.get(name) == expected_column
                for name, expected_column in EXPECTED_MEMORY_COLUMNS.items()
            )
            checks.append(
                CheckResult(
                    name="schema.memories",
                    ok=schema_ok,
                    value={
                        name: {
                            "type": actual_columns.get(name, (None, None, None))[0],
                            "nullable": actual_columns.get(name, (None, None, None))[1],
                            "max_length": actual_columns.get(name, (None, None, None))[2],
                        }
                        for name in EXPECTED_MEMORY_COLUMNS
                    },
                    message=None if schema_ok else "The memories schema is incompatible.",
                )
            )

            ingestion_id_unique = await connection.scalar(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_constraint AS constraint_record
                        JOIN pg_class AS relation
                          ON relation.oid = constraint_record.conrelid
                        JOIN pg_namespace AS namespace
                          ON namespace.oid = relation.relnamespace
                        WHERE namespace.nspname = 'public'
                          AND relation.relname = 'memories'
                          AND constraint_record.conname =
                              'uq_memories_ingestion_id'
                          AND constraint_record.contype = 'u'
                          AND pg_get_constraintdef(constraint_record.oid) =
                              'UNIQUE (ingestion_id)'
                    )
                    """
                )
            )
            checks.append(
                CheckResult(
                    name="constraint.memories_ingestion_id_unique",
                    ok=bool(ingestion_id_unique),
                    message=(
                        None
                        if ingestion_id_unique
                        else "The Memory ingestion identifier is not unique."
                    ),
                )
            )

            embedding_type = await connection.scalar(
                text(
                    """
                    SELECT format_type(attribute.atttypid, attribute.atttypmod)
                    FROM pg_attribute AS attribute
                    JOIN pg_class AS relation
                      ON relation.oid = attribute.attrelid
                    JOIN pg_namespace AS namespace
                      ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = 'public'
                      AND relation.relname = 'kulai_vector_records'
                      AND attribute.attname = 'embedding'
                      AND attribute.attnum > 0
                      AND NOT attribute.attisdropped
                    """
                )
            )
            checks.append(
                vector_embedding_dimension_check(
                    configured_dimension=configured_dimension,
                    actual_type=embedding_type,
                )
            )

            checks.extend(await _tombstone_schema_checks(connection))

            revision_constraint = await connection.scalar(text("""
                  SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c
                  JOIN pg_class r ON r.oid=c.conrelid JOIN pg_namespace n ON n.oid=r.relnamespace
                  WHERE n.nspname='public' AND r.relname='memories'
                    AND c.conname='ck_memories_revision_positive' AND c.contype='c'
            """))
            revision_valid = revision_constraint == "CHECK ((revision >= 1))"
            checks.append(CheckResult(name="constraint.memories_revision_positive", ok=revision_valid,
                value={"exists": revision_constraint is not None, "valid": revision_valid},
                message=None if revision_valid else "The Memory revision constraint is incompatible."))

            query_value = await connection.scalar(text("SELECT 1"))
            checks.append(
                CheckResult(name="database.read_query", ok=query_value == 1)
            )
            await connection.rollback()
    except Exception as exc:
        if not any(check.name == "database.connection" for check in checks):
            checks.append(CheckResult(name="database.connection", ok=False))
        if not any(check.name == "vector.embedding_dimension" for check in checks):
            checks.append(
                vector_embedding_dimension_check(
                    configured_dimension=configured_dimension,
                    actual_type=None,
                )
            )
        checks.append(
            CheckResult(
                name="database.diagnostics",
                ok=False,
                message=safe_error_message(exc, operation="Database diagnostics"),
            )
        )
    finally:
        await engine.dispose()

    return DoctorReport(tuple(checks))


async def database_exists(database: str, *, config: DbConfig | None = None) -> bool:
    url = async_database_url(database="postgres", config=config)
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            exists = await connection.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = :name)"),
                {"name": database},
            )
            await connection.rollback()
            return bool(exists)
    finally:
        await engine.dispose()


def validate_owned_database_name(
    database: str,
    *,
    source_database: str,
    kind: OwnedDatabaseKind,
) -> None:
    prefix = OWNED_DATABASE_PREFIXES[kind]
    pattern = re.compile(rf"^{re.escape(prefix)}[0-9a-f]{{32}}$")
    if not pattern.fullmatch(database):
        raise ValueError("Temporary database name does not match its safety prefix.")
    if database == source_database or database.casefold() in RESERVED_DATABASES:
        raise ValueError("Temporary database must differ from protected databases.")


def validate_restore_database_name(database: str, *, source_database: str) -> None:
    validate_owned_database_name(
        database,
        source_database=source_database,
        kind="restore",
    )


def _validate_owned_marker(marker: str, *, kind: OwnedDatabaseKind) -> None:
    label = "backup-test" if kind == "backup" else "restore"
    if not re.fullmatch(rf"kulai-memory-{label}:[0-9a-f]{{32}}", marker):
        raise ValueError("Invalid temporary database ownership marker.")


async def set_owned_database_marker(
    owned: OwnedTemporaryDatabase,
    *,
    config: DbConfig | None = None,
) -> None:
    source = postgres_connection(config)
    validate_owned_database_name(
        owned.name,
        source_database=source.database,
        kind=owned.kind,
    )
    _validate_owned_marker(owned.marker, kind=owned.kind)
    engine = create_async_engine(async_database_url(database="postgres", config=config))
    statement = f'COMMENT ON DATABASE "{owned.name}" IS \'{owned.marker}\''
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def get_owned_database_marker(
    owned: OwnedTemporaryDatabase,
    *,
    config: DbConfig | None = None,
) -> str | None:
    source = postgres_connection(config)
    validate_owned_database_name(
        owned.name,
        source_database=source.database,
        kind=owned.kind,
    )
    _validate_owned_marker(owned.marker, kind=owned.kind)
    engine = create_async_engine(async_database_url(database="postgres", config=config))
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            marker = await connection.scalar(
                text(
                    "SELECT shobj_description(oid, 'pg_database') "
                    "FROM pg_database WHERE datname = :name"
                ),
                {"name": owned.name},
            )
            await connection.rollback()
            return marker
    finally:
        await engine.dispose()


async def require_owned_database(
    owned: OwnedTemporaryDatabase,
    *,
    config: DbConfig | None = None,
) -> None:
    marker = await get_owned_database_marker(owned, config=config)
    if marker != owned.marker:
        raise RuntimeError("Temporary database ownership marker does not match.")


async def create_owned_temporary_database(
    *,
    kind: OwnedDatabaseKind,
    config: DbConfig | None = None,
) -> OwnedTemporaryDatabase:
    active_config = config or database_config()
    source = postgres_connection(active_config)
    label = "backup-test" if kind == "backup" else "restore"
    owned = OwnedTemporaryDatabase(
        name=f"{OWNED_DATABASE_PREFIXES[kind]}{uuid4().hex}",
        marker=f"kulai-memory-{label}:{uuid4().hex}",
        kind=kind,
    )
    validate_owned_database_name(
        owned.name,
        source_database=source.database,
        kind=kind,
    )
    if await database_exists(owned.name, config=active_config):
        raise RuntimeError("Generated temporary database already exists.")

    createdb = find_postgres_tool("createdb")
    completed = await asyncio.to_thread(run_postgres_tool,
        createdb,
        [
            *source.command_arguments(include_database=False),
            "--maintenance-db",
            "postgres",
            "--template",
            "template0",
            owned.name,
        ],
        connection=source,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"createdb failed with exit code {completed.returncode}.")
    try:
        await set_owned_database_marker(owned, config=active_config)
        await require_owned_database(owned, config=active_config)
    except Exception as exc:
        raise RuntimeError(
            f"Created {owned.name}, but its ownership marker could not be confirmed; "
            "the database was not dropped."
        ) from exc
    return owned


async def drop_owned_temporary_database(
    owned: OwnedTemporaryDatabase,
    *,
    config: DbConfig | None = None,
) -> None:
    active_config = config or database_config()
    source = postgres_connection(active_config)
    validate_owned_database_name(
        owned.name,
        source_database=source.database,
        kind=owned.kind,
    )
    await require_owned_database(owned, config=active_config)
    dropdb = find_postgres_tool("dropdb")
    completed = await asyncio.to_thread(run_postgres_tool,
        dropdb,
        [
            *source.command_arguments(include_database=False),
            "--maintenance-db",
            "postgres",
            owned.name,
        ],
        connection=source,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"dropdb failed with exit code {completed.returncode}.")
    if await database_exists(owned.name, config=active_config):
        raise RuntimeError("Temporary database still exists after dropdb.")


async def restore_archive_to_owned_database(
    backup: Path,
    owned: OwnedTemporaryDatabase,
    *,
    config: DbConfig | None = None,
) -> None:
    active_config = config or database_config()
    source = postgres_connection(active_config)
    archive = backup.expanduser().resolve()
    if not archive.is_file():
        raise FileNotFoundError("Backup file does not exist.")
    await require_owned_database(owned, config=active_config)
    pg_restore = find_postgres_tool("pg_restore")
    catalog = await asyncio.to_thread(run_postgres_tool,
        pg_restore,
        ["--list", str(archive)],
        connection=source,
    )
    if catalog.returncode != 0:
        raise RuntimeError("pg_restore could not read the backup catalog.")

    target = PostgresConnection(
        host=source.host,
        port=source.port,
        user=source.user,
        password=source.password,
        database=owned.name,
    )
    completed = await asyncio.to_thread(run_postgres_tool,
        pg_restore,
        [
            *target.command_arguments(),
            "--exit-on-error",
            "--single-transaction",
            "--no-owner",
            "--no-privileges",
            str(archive),
        ],
        connection=target,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"pg_restore failed with exit code {completed.returncode}."
        )
