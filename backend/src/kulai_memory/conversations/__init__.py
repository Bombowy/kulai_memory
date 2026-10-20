"""Conversation domain and repository port; no transport or Memory side effects."""

from .domain import (
    CompletedTurn, Conversation, ConversationCursor, ConversationError,
    ConversationNotFound, ConversationPage, ConversationPersistenceError,
    IdempotencyConflict, InputKind, Message, MessagePage, Role, Turn, TurnWrite,
)
from .ports import ConversationRepository

__all__ = [
    "CompletedTurn", "Conversation", "ConversationCursor", "ConversationError",
    "ConversationNotFound", "ConversationPage", "ConversationPersistenceError",
    "ConversationRepository", "IdempotencyConflict", "InputKind", "Message",
    "MessagePage", "Role", "Turn", "TurnWrite",
]
