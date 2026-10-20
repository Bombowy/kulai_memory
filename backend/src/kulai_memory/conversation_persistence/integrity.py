"""Read-only V2 catalog validation and streaming fingerprints for existing backups."""
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from uuid import UUID

from sqlalchemy import text

CONVERSATIONS = "kulai_conversations"
TURNS = "kulai_conversation_turns"
MESSAGES = "kulai_conversation_messages"
TABLES = (CONVERSATIONS, TURNS, MESSAGES)
NAMES = ",".join("'" + name + "'" for name in TABLES)  # Fixed identifiers only.

# PostgreSQL catalog forms of the frozen 0005 schema, including defaults.
EXPECTED_COLUMNS = {
    CONVERSATIONS: {
        "conversation_id": ("uuid", False, None, None), "title": ("varchar", True, 200, None),
        "created_at": ("timestamptz", False, None, "now()"),
        "last_activity_at": ("timestamptz", False, None, "now()"), "last_turn": ("int8", False, None, "0"),
    },
    TURNS: {"request_id": ("uuid", False, None, None), "conversation_id": ("uuid", False, None, None),
            "turn_number": ("int8", False, None, None)},
    MESSAGES: {
        **{name: ("uuid", False, None, None) for name in ("message_id", "request_id", "conversation_id")},
        "turn_number": ("int8", False, None, None), "sequence": ("int8", False, None, None),
        "role": ("text", False, None, None), "content": ("text", False, None, None),
        "created_at": ("timestamptz", False, None, None), "input_kind": ("text", True, None, None),
        "memory_ids": ("_uuid", False, None, "'{}'::uuid[]"),
    },
}
EXPECTED_CONSTRAINTS = {
    CONVERSATIONS: {
        ("p", "PRIMARY KEY (conversation_id)"),
        ("c", "CHECK (((last_turn >= 0) AND (last_turn <= '4611686018427387903'::bigint)))"),
        ("c", "CHECK ((last_activity_at >= created_at))"),
        ("c", "CHECK (((title IS NULL) OR ((title)::text ~ '[^[:space:]]'::text)))"),
    },
    TURNS: {
        ("p", "PRIMARY KEY (request_id)"), ("u", "UNIQUE (conversation_id, turn_number)"),
        ("u", "UNIQUE (request_id, conversation_id, turn_number)"),
        ("f", "FOREIGN KEY (conversation_id) REFERENCES kulai_conversations(conversation_id) ON DELETE CASCADE"),
        ("c", "CHECK (((turn_number >= 1) AND (turn_number <= '4611686018427387903'::bigint)))"),
    },
    MESSAGES: {
        ("p", "PRIMARY KEY (message_id)"), ("u", "UNIQUE (conversation_id, sequence)"),
        ("u", "UNIQUE (request_id, role)"),
        ("f", "FOREIGN KEY (request_id, conversation_id, turn_number) REFERENCES kulai_conversation_turns(request_id, conversation_id, turn_number) ON DELETE CASCADE"),
        ("c", "CHECK (((char_length(content) >= 1) AND (char_length(content) <= 16000) AND (content ~ '[^[:space:]]'::text)))"),
        ("c", "CHECK (((cardinality(memory_ids) <= 20) AND (array_position(memory_ids, NULL::uuid) IS NULL) AND ((cardinality(memory_ids) = 0) OR (array_ndims(memory_ids) = 1))))"),
        ("c", "CHECK ((((role = 'user'::text) AND (input_kind = ANY (ARRAY['text'::text, 'voice'::text])) AND (input_kind IS NOT NULL) AND (sequence = ((2 * turn_number) - 1)) AND (cardinality(memory_ids) = 0)) OR ((role = 'assistant'::text) AND (input_kind IS NULL) AND (sequence = (2 * turn_number)))))"),
    },
}
EXPECTED_INDEXES = {
    "ix_conversations_activity_id": f"CREATE INDEX ix_conversations_activity_id ON public.{CONVERSATIONS} USING btree (last_activity_at DESC, conversation_id DESC)",
    **{name: f"CREATE UNIQUE INDEX {name} ON public.{table} USING btree ({columns})" for name, table, columns in (
        (CONVERSATIONS + "_pkey", CONVERSATIONS, "conversation_id"),
        (TURNS + "_pkey", TURNS, "request_id"), (MESSAGES + "_pkey", MESSAGES, "message_id"),
        ("uq_conversation_turn_number", TURNS, "conversation_id, turn_number"),
        ("uq_conversation_turn_identity", TURNS, "request_id, conversation_id, turn_number"),
        ("uq_conversation_message_sequence", MESSAGES, "conversation_id, sequence"),
        ("uq_conversation_message_role", MESSAGES, "request_id, role"),
    )},
}


@dataclass(frozen=True, slots=True)
class ConversationSchema:
    tables: tuple[str, ...]
    valid: bool


async def inspect_conversation_schema(connection) -> ConversationSchema:
    rows = list(await connection.execute(text(f"""
        SELECT r.relname,r.relkind::text,r.relrowsecurity,r.relforcerowsecurity
        FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace
        WHERE n.nspname='public' AND r.relname IN ({NAMES})
    """)))
    present = tuple(sorted(row[0] for row in rows))
    if set(present) != set(TABLES) or any(row[1] != 'r' or row[2] or row[3] for row in rows):
        return ConversationSchema(present, False)
    columns = {name: {} for name in TABLES}
    for table, column, kind, nullable, length, default in await connection.execute(text(f"""
        SELECT table_name,column_name,udt_name,is_nullable,character_maximum_length,column_default
        FROM information_schema.columns WHERE table_schema='public' AND table_name IN ({NAMES})
    """)):
        columns[table][column] = (kind, nullable == "YES", length, default)
    if columns != EXPECTED_COLUMNS:
        return ConversationSchema(present, False)
    actual = {name: set() for name in TABLES}
    for table, kind, definition, validated, deferrable in await connection.execute(text(f"""
        SELECT r.relname,c.contype::text,pg_get_constraintdef(c.oid),c.convalidated,c.condeferrable
        FROM pg_constraint c JOIN pg_class r ON r.oid=c.conrelid JOIN pg_namespace n ON n.oid=r.relnamespace
        WHERE n.nspname='public' AND r.relname IN ({NAMES})
    """)):
        if not validated or deferrable:
            return ConversationSchema(present, False)
        # BETWEEN initially creates a nested AND. pg_dump's deparsed CHECK is
        # reparsed as one AND on restore. Accept only these two exact forms;
        # do not strip arbitrary parentheses or weaken the constraint checks.
        if definition == "CHECK ((((char_length(content) >= 1) AND (char_length(content) <= 16000)) AND (content ~ '[^[:space:]]'::text)))":
            definition = "CHECK (((char_length(content) >= 1) AND (char_length(content) <= 16000) AND (content ~ '[^[:space:]]'::text)))"
        if kind != "n":  # PostgreSQL 18 exposes NOT NULL separately; columns verified above.
            actual[table].add((kind, definition))
    if actual != EXPECTED_CONSTRAINTS:
        return ConversationSchema(present, False)
    indexes = {}
    for name, definition, valid, ready in await connection.execute(text(f"""
        SELECT i.relname,pg_get_indexdef(x.indexrelid),x.indisvalid,x.indisready
        FROM pg_index x JOIN pg_class t ON t.oid=x.indrelid JOIN pg_namespace n ON n.oid=t.relnamespace
        JOIN pg_class i ON i.oid=x.indexrelid WHERE n.nspname='public' AND t.relname IN ({NAMES})
    """)):
        if not valid or not ready:
            return ConversationSchema(present, False)
        indexes[name] = definition
    return ConversationSchema(present, indexes == EXPECTED_INDEXES)


async def conversation_rows_valid(connection) -> bool:
    """One MVCC statement checks complete pairs, contiguous turns and activity."""
    return bool(await connection.scalar(text("""
        SELECT NOT EXISTS (
            SELECT 1 FROM public.kulai_conversations c
            LEFT JOIN LATERAL (SELECT count(*) AS n, max(turn_number) AS last
                FROM public.kulai_conversation_turns WHERE conversation_id=c.conversation_id) t ON true
            WHERE c.last_turn <> t.n OR c.last_turn <> coalesce(t.last,0)
        ) AND NOT EXISTS (
            SELECT 1 FROM public.kulai_conversation_turns t
            LEFT JOIN public.kulai_conversation_messages m ON m.request_id=t.request_id
            GROUP BY t.request_id HAVING count(m.message_id) <> 2
        ) AND NOT EXISTS (
            SELECT 1 FROM public.kulai_conversation_messages m
            JOIN public.kulai_conversations c USING (conversation_id)
            WHERE m.created_at < c.created_at OR m.created_at > c.last_activity_at
              OR cardinality(m.memory_ids) <> (SELECT count(DISTINCT x) FROM unnest(m.memory_ids) AS x)
        )
    """)))


@dataclass(frozen=True, slots=True)
class ConversationFingerprint:
    count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    conversations: ConversationFingerprint
    turns: ConversationFingerprint
    messages: ConversationFingerprint


def _json_value(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


async def conversation_snapshot(connection) -> ConversationSnapshot:
    fingerprints = []
    for table, key in zip(TABLES, ("conversation_id", "request_id", "message_id")):
        count, digest = 0, hashlib.sha256()
        stream = await connection.stream(text(f"SELECT * FROM public.{table} ORDER BY {key}"),
                                         execution_options={"yield_per": 256})
        try:
            async for batch in stream.mappings().partitions(256):
                for row in batch:
                    payload = json.dumps({name: _json_value(value) for name, value in row.items()},
                                         sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    digest.update(len(payload).to_bytes(8, "big"))
                    digest.update(payload)
                    count += 1
        finally:
            await stream.close()
        fingerprints.append(ConversationFingerprint(count, digest.hexdigest()))
    return ConversationSnapshot(*fingerprints)
