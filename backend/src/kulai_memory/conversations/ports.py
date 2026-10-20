"""Atomic conversation storage contract, independent of SQL and transport."""

from typing import Protocol
from uuid import UUID

from .domain import CompletedTurn, Conversation, ConversationCursor, ConversationPage, MessagePage, Turn, TurnWrite


class ConversationRepository(Protocol):
    async def create(self, *, title: str | None = None) -> Conversation: ...

    async def get_turn(self, *, request_id: UUID) -> Turn | None:
        """Read a committed pair by global request identity, without a write."""
        ...

    async def append_turn(self, *, conversation_id: UUID, completed: CompletedTurn) -> TurnWrite:
        """Commit both messages together; identical request/payload returns original.

        Request identities are globally unique. Changed payload or conversation
        is a conflict. Concurrent turns are ordered by database lock acquisition.
        No single-message append operation is exposed.
        """
        ...

    async def recent_messages(self, *, conversation_id: UUID, limit: int = 50,
                              before_sequence: int | None = None) -> MessagePage: ...

    async def list_conversations(self, *, limit: int = 20,
                                 cursor: ConversationCursor | None = None) -> ConversationPage: ...
