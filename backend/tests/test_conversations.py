"""Domain limits and V1 metadata isolation; no database connections."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kulai_memory.conversations import CompletedTurn, ConversationCursor, InputKind
from kulai_memory.conversations.domain import MAX_MEMORY_REFERENCES, MAX_MESSAGE_CHARS, page_size, title_value


@pytest.mark.parametrize("field,value", [
    ("question", ""), ("answer", " \n\t"), ("question", "NUL\x00"),
    ("answer", "x" * (MAX_MESSAGE_CHARS + 1)), ("question", 123),
    ("answer", None), ("input_kind", "audio"),
    ("memory_ids", tuple(uuid4() for _ in range(MAX_MEMORY_REFERENCES + 1))),
])
def test_reject_incomplete_or_unbounded_turn(field, value):
    values = dict(request_id=uuid4(), question="Pytanie?", answer="Answer.", input_kind="voice")
    values[field] = value
    with pytest.raises(ValidationError):
        CompletedTurn(**values)


def test_exact_limit_unicode_references_and_no_audio_or_arbitrary_fields():
    reference = uuid4()
    turn = CompletedTurn(request_id=uuid4(), question="ż" * MAX_MESSAGE_CHARS,
                         answer="English answer.", input_kind=InputKind.TEXT, memory_ids=(reference,))
    assert len(turn.question) == MAX_MESSAGE_CHARS and turn.memory_ids == (reference,)
    with pytest.raises(ValidationError):
        CompletedTurn(**{**turn.model_dump(), "audio": b"not-stored"})
    with pytest.raises(ValidationError):
        CompletedTurn(**{**turn.model_dump(), "memory_ids": (reference, reference)})
    with pytest.raises(ValidationError):
        turn.answer = "mutated"


@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.5, "10"])
def test_page_size_is_bounded_and_strict(limit):
    with pytest.raises(ValueError):
        page_size(limit)


@pytest.mark.parametrize("title", ["", " \n", "x" * 201, 123, "nul\x00"])
def test_title_limits(title):
    with pytest.raises(ValueError):
        title_value(title)


def test_cursor_requires_timezone_and_title_is_optional():
    assert title_value(None) is None and title_value("Rozmowa / Conversation")
    assert ConversationCursor(before_id=uuid4(), before_activity_at=datetime.now(UTC))
    with pytest.raises(ValidationError):
        ConversationCursor(before_id=uuid4(), before_activity_at=datetime.now())


def test_schema_does_not_register_in_v1_shared_metadata_or_security_graph():
    from kulai_db import Base
    from kulai_memory.database_safety import expected_alembic_heads
    from kulai_memory.security_persistence.migration import security_migration_heads
    from kulai_memory.conversation_persistence.schema import metadata

    assert metadata is not Base.metadata
    assert len(metadata.tables) == 3
    assert not any("conversation" in name for name in Base.metadata.tables)
    assert expected_alembic_heads() == ("kulai_memory_0005",)
    assert security_migration_heads() == ("kulai_security_0003",)
