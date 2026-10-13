"""Read-only validation/fingerprints of the independent security schema."""
from dataclasses import dataclass
from datetime import timezone
import hashlib
import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kulai_memory.security.models import SecurityError
from .schema_v1 import DEVICE_TABLE, TOKEN_TABLE, VERSION_TABLE, SECURITY_REVISION
from .schema_v2 import PAIRING_TABLE, RATE_TABLE, SECURITY_HEAD
from .schema_v3 import SESSION_TABLE, REFRESH_TABLE, SECURITY_HEAD as SESSION_HEAD

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

PAIRING_COLUMNS = {
    PAIRING_TABLE: {'id': ('uuid', False), 'invite_digest': ('text', False),
        'receiver_digest': ('text', True), 'client_id': ('uuid', True), 'device_id': ('uuid', True),
        'device_label': ('text', True), 'state': ('text', False), 'created_at': ('timestamptz', False),
        'expires_at': ('timestamptz', False), 'approved_at': ('timestamptz', True), 'token_id': ('uuid', True),
        'actions': ('_text', False), 'token_ttl_seconds': ('int4', False), 'attempts': ('int4', False)},
    RATE_TABLE: {'id': ('int4', False), 'window_start': ('timestamptz', False), 'attempts': ('int4', False)},
}
PAIRING_CONSTRAINTS = {
    PAIRING_TABLE: {
        ('p', 'PRIMARY KEY (id)'), ('u', 'UNIQUE (invite_digest)'), ('u', 'UNIQUE (device_id)'), ('u', 'UNIQUE (token_id)'),
        ('f', f'FOREIGN KEY (device_id) REFERENCES {DEVICE_TABLE}(id) ON DELETE RESTRICT'),
        ('f', f'FOREIGN KEY (token_id) REFERENCES {TOKEN_TABLE}(id) ON DELETE RESTRICT'),
        ('c', "CHECK (((invite_digest ~ '^[0-9a-f]{64}$'::text) AND ((receiver_digest IS NULL) OR (receiver_digest ~ '^[0-9a-f]{64}$'::text))))"),
        ('c', "CHECK ((state = ANY (ARRAY['invited'::text, 'pending'::text, 'approved'::text, 'claimed'::text, 'denied'::text, 'cancelled'::text])))"),
        ('c', "CHECK (((expires_at > created_at) AND (expires_at <= (created_at + '00:05:00'::interval))))"),
        ('c', 'CHECK (((attempts >= 0) AND (attempts <= 8) AND (token_ttl_seconds >= 1) AND (token_ttl_seconds <= 86400)))'),
        ('c', next(d for k, d in EXPECTED_CONSTRAINTS[TOKEN_TABLE] if k == 'c' and 'cardinality' in d)),
        ('c', "CHECK ((((state = 'invited'::text) AND (receiver_digest IS NULL) AND (device_id IS NULL) AND (client_id IS NULL) AND (device_label IS NULL)) OR ((state <> 'invited'::text) AND (receiver_digest IS NOT NULL) AND (device_id IS NOT NULL) AND (client_id IS NOT NULL) AND (device_label IS NOT NULL))))"),
        ('c', "CHECK ((((state = ANY (ARRAY['approved'::text, 'claimed'::text])) AND (approved_at IS NOT NULL)) OR ((state <> ALL (ARRAY['approved'::text, 'claimed'::text])) AND (approved_at IS NULL))))"),
        ('c', "CHECK ((((state = 'claimed'::text) AND (token_id IS NOT NULL)) OR ((state <> 'claimed'::text) AND (token_id IS NULL))))"),
        ('c', "CHECK (((device_label IS NULL) OR (device_label ~ '^[A-Za-z0-9 ._-]{1,48}$'::text)))"),
    },
    RATE_TABLE: {('p', 'PRIMARY KEY (id)'), ('c', 'CHECK (((id = 1) AND (attempts >= 0) AND (attempts <= 120)))')},
}

SESSION_COLUMNS = {
    SESSION_TABLE: {'id': ('uuid', False), 'device_id': ('uuid', False), 'pairing_id': ('uuid', False),
        'created_at': ('timestamptz', False), 'absolute_expires_at': ('timestamptz', False),
        'revoked_at': ('timestamptz', True), 'actions': ('_text', False),
        'access_ttl_seconds': ('int4', False), 'generation': ('int4', False)},
    REFRESH_TABLE: {'id': ('uuid', False), 'session_id': ('uuid', False), 'access_id': ('uuid', False),
        'digest': ('text', False), 'issued_at': ('timestamptz', False), 'expires_at': ('timestamptz', False),
        'consumed_at': ('timestamptz', True)},
}
SESSION_CONSTRAINTS = {
    SESSION_TABLE: {
        ('p', 'PRIMARY KEY (id)'), ('u', 'UNIQUE (device_id)'), ('u', 'UNIQUE (pairing_id)'),
        ('f', f'FOREIGN KEY (device_id) REFERENCES {DEVICE_TABLE}(id) ON DELETE RESTRICT'),
        ('f', f'FOREIGN KEY (pairing_id) REFERENCES {PAIRING_TABLE}(id) ON DELETE RESTRICT'),
        ('c', "CHECK (((absolute_expires_at > created_at) AND (absolute_expires_at <= (created_at + '90 days'::interval))))"),
        ('c', 'CHECK (((access_ttl_seconds >= 1) AND (access_ttl_seconds <= 3600) AND (generation >= 0) AND (generation <= 4096)))'),
        ('c', next(d for k, d in EXPECTED_CONSTRAINTS[TOKEN_TABLE] if k == 'c' and 'cardinality' in d)),
    },
    REFRESH_TABLE: {
        ('p', 'PRIMARY KEY (id)'), ('u', 'UNIQUE (access_id)'), ('u', 'UNIQUE (digest)'),
        ('f', f'FOREIGN KEY (session_id) REFERENCES {SESSION_TABLE}(id) ON DELETE RESTRICT'),
        ('f', f'FOREIGN KEY (access_id) REFERENCES {TOKEN_TABLE}(id) ON DELETE RESTRICT'),
        ('c', "CHECK ((digest ~ '^[0-9a-f]{64}$'::text))"),
        ('c', "CHECK (((expires_at > issued_at) AND (expires_at <= (issued_at + '30 days'::interval))))"),
        ('c', 'CHECK (((consumed_at IS NULL) OR (consumed_at >= issued_at)))'),
    },
}


@dataclass(frozen=True, slots=True)
class SchemaState:
    present: bool
    valid: bool
    revision: str | None = None


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
    if (VERSION_TABLE, 'r') not in present:
        return SchemaState(True, False)
    revisions = tuple((await connection.execute(text(f"SELECT version_num FROM public.{VERSION_TABLE} ORDER BY version_num"))).scalars())
    if revisions not in ((SECURITY_REVISION,), (SECURITY_HEAD,), (SESSION_HEAD,)):
        return SchemaState(True, False)
    v3 = revisions == (SESSION_HEAD,)
    v2 = revisions in ((SECURITY_HEAD,), (SESSION_HEAD,))
    expected_columns = {**EXPECTED_COLUMNS, **(PAIRING_COLUMNS if v2 else {}), **(SESSION_COLUMNS if v3 else {})}
    if present != {(name, 'r') for name in expected_columns}:
        return SchemaState(True, False)
    names = ','.join("'" + name + "'" for name in expected_columns)  # Constants, never request input.
    triggers = await connection.scalar(text(f"""
        SELECT EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_class r ON r.oid=t.tgrelid
          JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal
          AND r.relname IN ({names}))
    """))
    if triggers:
        return SchemaState(True, False)
    rows = await connection.execute(text(f"""
        SELECT table_name,column_name,udt_name,is_nullable,column_default,character_maximum_length
        FROM information_schema.columns WHERE table_schema='public'
          AND table_name IN ({names})
    """))
    columns = {name: {} for name in expected_columns}
    for table, column, kind, nullable, default, length in rows:
        if default is not None or length != (32 if table == VERSION_TABLE else None):
            return SchemaState(True, False)
        columns[table][column] = (kind, nullable == "YES")
    if columns != expected_columns:
        return SchemaState(True, False)
    rows = await connection.execute(text(f"""
        SELECT r.relname,c.contype::text,pg_get_constraintdef(c.oid),c.convalidated,c.condeferrable
        FROM pg_constraint c JOIN pg_class r ON r.oid=c.conrelid JOIN pg_namespace n ON n.oid=r.relnamespace
        WHERE n.nspname='public' AND r.relname IN
          ({names})
    """))
    constraints = {name: set() for name in expected_columns}
    for table, kind, definition, validated, deferrable in rows:
        if not validated or deferrable:
            return SchemaState(True, False)
        constraints[table].add((kind, definition))
    expected = {name: set(values) for name, values in {**EXPECTED_CONSTRAINTS,
        **(PAIRING_CONSTRAINTS if v2 else {}), **(SESSION_CONSTRAINTS if v3 else {})}.items()}
    if int(await connection.scalar(text("SHOW server_version_num"))) >= 180000:
        for name, columns in expected_columns.items():
            expected[name].update(("n", f"NOT NULL {column}") for column, (_, nullable) in columns.items() if not nullable)
    if constraints != expected:
        return SchemaState(True, False)
    indexed = await connection.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM pg_index i JOIN pg_class r ON r.oid=i.indexrelid
          JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname='public'
          AND r.relname='ix_security_tokens_device' AND i.indisvalid AND i.indisready
          AND pg_get_indexdef(i.indexrelid) =
            'CREATE INDEX ix_security_tokens_device ON public.kulai_security_tokens USING btree (device_id)')
    """))
    if v2:
        indexed = indexed and await connection.scalar(text("""
            SELECT EXISTS(SELECT 1 FROM pg_index i JOIN pg_class r ON r.oid=i.indexrelid
              JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname='public'
              AND r.relname='ix_security_pairings_expiry' AND i.indisvalid AND i.indisready
              AND pg_get_indexdef(i.indexrelid) =
                'CREATE INDEX ix_security_pairings_expiry ON public.kulai_security_pairings USING btree (expires_at)')
        """))
    if v3:
        for name, definition in {
            'ix_security_refreshes_session': 'CREATE INDEX ix_security_refreshes_session ON public.kulai_security_refreshes USING btree (session_id)',
            'ux_security_refreshes_active': 'CREATE UNIQUE INDEX ux_security_refreshes_active ON public.kulai_security_refreshes USING btree (session_id) WHERE (consumed_at IS NULL)',
        }.items():
            indexed = indexed and await connection.scalar(text('''
                SELECT EXISTS(SELECT 1 FROM pg_index i JOIN pg_class r ON r.oid=i.indexrelid
                  JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname='public'
                  AND r.relname=:name AND i.indisvalid AND i.indisready
                  AND pg_get_indexdef(i.indexrelid)=:definition)
            '''), {'name': name, 'definition': definition})
    return SchemaState(True, bool(indexed), revisions[0])


@dataclass(frozen=True, slots=True)
class SecurityFingerprint:
    count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SecuritySnapshot:
    revision: tuple[str, ...]
    devices: SecurityFingerprint
    tokens: SecurityFingerprint
    pairings: SecurityFingerprint | None = None
    pairing_rate: SecurityFingerprint | None = None
    sessions: SecurityFingerprint | None = None
    refreshes: SecurityFingerprint | None = None


async def _fingerprint(connection: AsyncConnection, table: str) -> SecurityFingerprint:
    digest, count = hashlib.sha256(), 0
    result = await connection.stream(text(f"SELECT * FROM public.{table} ORDER BY id"))
    async for row in result:
        values = {}
        for key, value in row._mapping.items():
            if hasattr(value, "utcoffset"):
                value = value.astimezone(timezone.utc).isoformat()
            elif value is not None and (isinstance(value, UUID) or key in {'id', 'device_id', 'project_id', 'client_id', 'token_id'}):
                # PL/EN: retain v1/v2 canonical encoding, including the rate counter's integer id.
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
    return SecuritySnapshot((state.revision,), await _fingerprint(connection, DEVICE_TABLE),
        await _fingerprint(connection, TOKEN_TABLE),
        await _fingerprint(connection, PAIRING_TABLE) if state.revision in (SECURITY_HEAD, SESSION_HEAD) else None,
        await _fingerprint(connection, RATE_TABLE) if state.revision in (SECURITY_HEAD, SESSION_HEAD) else None,
        await _fingerprint(connection, SESSION_TABLE) if state.revision == SESSION_HEAD else None,
        await _fingerprint(connection, REFRESH_TABLE) if state.revision == SESSION_HEAD else None)
