"""Bounded, transport-neutral conversation values. Text only; no canonical Memory."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_MESSAGE_CHARS = 16_000
MAX_TITLE_CHARS = 200
MAX_MEMORY_REFERENCES = 20
MAX_PAGE_SIZE = 100
MAX_SEQUENCE = 2**63 - 1


class Value(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, validate_default=True,
    )


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class InputKind(str, Enum):
    TEXT = "text"
    VOICE = "voice"


def valid_text(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise ValueError("Text must be nonempty and contain no NUL characters.")
    return value


def title_value(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_TITLE_CHARS:
        raise ValueError("Title must be a string of at most 200 characters.")
    return valid_text(value)


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware.")
    return value.astimezone(UTC)


def page_size(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_PAGE_SIZE:
        raise ValueError("Page size must be between 1 and 100.")
    return value


def identity(value: UUID) -> UUID:
    if not isinstance(value, UUID):
        raise ValueError("An UUID identity is required.")
    return value


class CompletedTurn(Value):
    """Accept only a successfully generated question/answer, never partial output.

    The caller invokes append only after successful generation. The data layer
    neither calls an LLM nor invents an answer on error/cancellation. Retries must
    retain the same request UUID and exact payload, including reference order.
    """

    request_id: UUID
    question: str = Field(strict=True, max_length=MAX_MESSAGE_CHARS)
    answer: str = Field(strict=True, max_length=MAX_MESSAGE_CHARS)
    input_kind: InputKind
    memory_ids: tuple[UUID, ...] = Field(default=(), max_length=MAX_MEMORY_REFERENCES)

    _text = field_validator("question", "answer")(valid_text)

    @field_validator("memory_ids")
    @classmethod
    def unique_references(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Memory reference identities must be unique.")
        return value


class Conversation(Value):
    conversation_id: UUID
    title: str | None = Field(default=None, strict=True, max_length=MAX_TITLE_CHARS)
    created_at: datetime
    last_activity_at: datetime

    _times = field_validator("created_at", "last_activity_at")(utc)

    @field_validator("title")
    @classmethod
    def optional_title(cls, value: str | None) -> str | None:
        return title_value(value)

    @model_validator(mode="after")
    def activity_order(self) -> Conversation:
        if self.last_activity_at < self.created_at:
            raise ValueError("Activity cannot precede creation.")
        return self


class Message(Value):
    message_id: UUID
    conversation_id: UUID
    request_id: UUID
    role: Role
    content: str = Field(strict=True, max_length=MAX_MESSAGE_CHARS)
    created_at: datetime
    sequence: int = Field(strict=True, ge=1, le=MAX_SEQUENCE)
    # Origin applies to the user question. Assistant output has no input mode.
    input_kind: InputKind | None = None
    # Soft references: deleting a canonical Memory must not be blocked by chat.
    memory_ids: tuple[UUID, ...] = Field(default=(), max_length=MAX_MEMORY_REFERENCES)

    _time = field_validator("created_at")(utc)
    _text = field_validator("content")(valid_text)

    @model_validator(mode="after")
    def role_fields(self) -> Message:
        if self.role == Role.USER and (self.input_kind is None or self.memory_ids):
            raise ValueError("Only user messages carry input mode; references belong to the answer.")
        if self.role == Role.ASSISTANT and self.input_kind is not None:
            raise ValueError("Assistant output has no input mode.")
        if len(set(self.memory_ids)) != len(self.memory_ids):
            raise ValueError("Memory reference identities must be unique.")
        return self


class Turn(Value):
    request_id: UUID
    conversation_id: UUID
    turn_number: int = Field(strict=True, ge=1, le=MAX_SEQUENCE // 2)
    user: Message
    assistant: Message

    @model_validator(mode="after")
    def complete_pair(self) -> Turn:
        if (self.user.role != Role.USER or self.assistant.role != Role.ASSISTANT
                or self.user.message_id == self.assistant.message_id
                or self.user.sequence != 2 * self.turn_number - 1
                or self.assistant.sequence != 2 * self.turn_number
                or any(m.conversation_id != self.conversation_id or m.request_id != self.request_id
                       for m in (self.user, self.assistant))):
            raise ValueError("A turn must contain the ordered user/assistant pair for its identity.")
        return self


class TurnWrite(Value):
    turn: Turn
    created: bool


class ConversationCursor(Value):
    before_activity_at: datetime
    before_id: UUID

    _time = field_validator("before_activity_at")(utc)


class ConversationPage(Value):
    conversations: tuple[Conversation, ...]
    next_cursor: ConversationCursor | None = None


class MessagePage(Value):
    """A bounded window returned oldest first; cursor reads the older window."""

    messages: tuple[Message, ...]
    next_before_sequence: int | None = None


class ConversationError(RuntimeError):
    safe_message = "The conversation operation could not be completed."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class ConversationPersistenceError(ConversationError):
    pass


class ConversationNotFound(ConversationError):
    safe_message = "The conversation does not exist."


class IdempotencyConflict(ConversationError):
    safe_message = "The request identifier was already used for a different turn."
