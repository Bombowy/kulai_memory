"""Read-only validation/fingerprints of the independent security schema."""
from dataclasses import dataclass
from datetime import timezone
import hashlib
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kulai_memory.security.models import SecurityError
from .schema_v1 import DEVICE_TABLE, TOKEN_TABLE, VERSION_TABLE, SECURITY_REVISION

EXPECTED_COLUMNS = {
    DEVICE_TABLE: {"id": ("uuid", False), "status": ("text", False), "created_at": ("timestamptz", False)},
    TOKEN_TABLE: {"id": ("uuid", False), "device_id": ("uuid", False), "digest": ("text", False),
        "issued_at": ("timestamptz", False), "expires_at": ("timestamptz", False),
        "revoked_at": ("timestamptz", True), "actions": ("_text", False), "project_id": ("uuid", True)},
    VERSION_TABLE: {"version_num": ("varchar", False)},
}
EXPECTED_CONSTRAINTS = {
    DEVICE_TABLE: {
        ("p", "PRIMARY KEY (id)"),
        ("c", "CHECK ((status = ANY (ARRAY['pending'::text, 'trusted'::text, 'revoked'::text])))"),
    },
    TOKEN_TABLE: {
        ("p", "PRIMARY KEY (id)"), ("u", "UNIQUE (digest)"),
        ("f", f"FOREIGN KEY (device_id) REFERENCES {DEVICE_TABLE}(id) ON DELETE RESTRICT"),
        ("c", "CHECK ((digest ~ '^[0-9a-f]{64}$'::text))"),
        ("c", "CHECK ((expires_at > issued_at))"),
        ("c", "CHECK (((cardinality(actions) >= 1) AND (cardinality(actions) <= 4) AND "
               "(array_position(actions, NULL::text) IS NULL) AND "
               "(actions <@ ARRAY['memory.read'::text, 'memory.write'::text, 'assistant.ask'::text, 'assistant.speech_plan'::text])))"),
    },
    VERSION_TABLE: {("p", "PRIMARY KEY (version_num)")},
}


@dataclass(frozen=True, slots=True)
class SchemaState:
    present: bool
    valid: bool


async def inspect_security_schema(connection: AsyncConnection) -> SchemaState:
    rows = await connection.execute(text("""
        SELECT r.relname, r.relkind::text, r.relrowsecurity, r.relforcerowsecurity
        FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace
        WHERE n.nspname='public' AND r.relname LIKE 'kulai\\_security\\_%' ESCAPE '\\'
          AND r.relkind IN ('r','p','v','m','f')
    """))
    rows = list(rows)
    present = {(row[0], row[1]) for row in rows}
    if any(row[2] or row[3] for row in rows):
        return SchemaState(True, False)
    if not present:
        return SchemaState(False, True)
    if present != {(name, "r") for name in EXPECTED_COLUMNS}:
        return SchemaState(True, False)
    triggers = await connection.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_class r ON r.oid=t.tgrelid
          JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal
          AND r.relname IN ('kulai_security_devices','kulai_security_tokens','kulai_security_version'))
    """))
    if triggers:
        return SchemaState(True, False)
    rows = await connection.execute(text("""
        SELECT table_name,column_name,udt_name,is_nullable,column_default,character_maximum_length
        FROM information_schema.columns WHERE table_schema='public'
          AND table_name IN ('kulai_security_devices','kulai_security_tokens','kulai_security_version')
    """))
    columns = {name: {} for name in EXPECTED_COLUMNS}
    for table, column, kind, nullable, default, length in rows:
        if default is not None or length != (32 if table == VERSION_TABLE else None):
            return SchemaState(True, False)
        columns[table][column] = (kind, nullable == "YES")
    if columns != EXPECTED_COLUMNS:
        return SchemaState(True, False)
    rows = await connection.execute(text("""
        SELECT r.relname,c.contype::text,pg_get_constraintdef(c.oid),c.convalidated,c.condeferrable
        FROM pg_constraint c JOIN pg_class r ON r.oid=c.conrelid JOIN pg_namespace n ON n.oid=r.relnamespace
        WHERE n.nspname='public' AND r.relname IN
          ('kulai_security_devices','kulai_security_tokens','kulai_security_version')
    """))
    constraints = {name: set() for name in EXPECTED_COLUMNS}
    for table, kind, definition, validated, deferrable in rows:
        if not validated or deferrable:
            return SchemaState(True, False)
        constraints[table].add((kind, definition))
    expected = {name: set(values) for name, values in EXPECTED_CONSTRAINTS.items()}
    if int(await connection.scalar(text("SHOW server_version_num"))) >= 180000:
        for name, columns in EXPECTED_COLUMNS.items():
            expected[name].update(("n", f"NOT NULL {column}") for column, (_, nullable) in columns.items() if not nullable)
    if constraints != expected:
        return SchemaState(True, False)
    revisions = tuple((await connection.execute(text(f"SELECT version_num FROM {VERSION_TABLE} ORDER BY version_num"))).scalars())
    indexed = await connection.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM pg_index i JOIN pg_class r ON r.oid=i.indexrelid
          JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname='public'
          AND r.relname='ix_security_tokens_device' AND i.indisvalid AND i.indisready
          AND pg_get_indexdef(i.indexrelid) =
            'CREATE INDEX ix_security_tokens_device ON public.kulai_security_tokens USING btree (device_id)')
    """))
    return SchemaState(True, revisions == (SECURITY_REVISION,) and bool(indexed))


@dataclass(frozen=True, slots=True)
class SecurityFingerprint:
    count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SecuritySnapshot:
    revision: tuple[str, ...]
    devices: SecurityFingerprint
    tokens: SecurityFingerprint


async def _fingerprint(connection: AsyncConnection, table: str) -> SecurityFingerprint:
    digest, count = hashlib.sha256(), 0
    result = await connection.stream(text(f"SELECT * FROM public.{table} ORDER BY id"))
    async for row in result:
        values = {}
        for key, value in row._mapping.items():
            if hasattr(value, "utcoffset"):
                value = value.astimezone(timezone.utc).isoformat()
            elif key in {"id", "device_id", "project_id"} and value is not None:
                value = str(value)
            values[key] = value
        payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        count += 1
    return SecurityFingerprint(count, digest.hexdigest())


async def security_snapshot(connection: AsyncConnection) -> SecuritySnapshot | None:
    state = await inspect_security_schema(connection)
    if not state.valid:
        raise SecurityError()
    if not state.present:
        return None
    return SecuritySnapshot((SECURITY_REVISION,), await _fingerprint(connection, DEVICE_TABLE),
                            await _fingerprint(connection, TOKEN_TABLE))
