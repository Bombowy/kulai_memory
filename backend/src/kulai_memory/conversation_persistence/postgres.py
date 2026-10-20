"""SQLAlchemy/asyncpg adapter with per-call transactions and bounded reads."""

from __future__ import annotations

from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kulai_memory.conversations import (
    CompletedTurn, Conversation, ConversationCursor, ConversationError,
    ConversationNotFound, ConversationPage, ConversationPersistenceError,
    IdempotencyConflict, Message, MessagePage, Turn, TurnWrite,
)
from kulai_memory.conversations.domain import MAX_SEQUENCE, identity, page_size, title_value

from .schema import conversations, messages, turns


def _conversation(row) -> Conversation:
    return Conversation(**{name: row[name] for name in
                           ("conversation_id", "title", "created_at", "last_activity_at")})


def _message(row) -> Message:
    return Message(**{name: row[name] for name in Message.model_fields})


class PostgresConversationRepository:
    """Own each transaction; never hold one open during AI generation.

    A conversation row serializes writes only to that conversation. Each turn
    reserves a globally unique request UUID, then inserts its question and answer
    and advances activity/counter in the same transaction. Cancellation propagates
    and the session context rolls back. A lost commit acknowledgement is resolved
    by retrying the exact same completed turn, never by automatic write retries.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def create(self, *, title: str | None = None) -> Conversation:
        title = title_value(title)
        try:
            async with self._sessions() as db, db.begin():
                row = (await db.execute(sa.insert(conversations).values(
                    conversation_id=uuid4(), title=title,
                ).returning(conversations))).mappings().one()
                result = _conversation(row)
            return result
        except Exception:
            raise ConversationPersistenceError() from None

    async def _stored_turn(self, db: AsyncSession, request_id: UUID) -> Turn:
        rows = (await db.execute(sa.select(messages).where(
            messages.c.request_id == request_id,
        ).order_by(messages.c.sequence).limit(2))).mappings().all()
        if len(rows) != 2 or rows[0]["role"] != "user" or rows[1]["role"] != "assistant":
            raise ConversationPersistenceError()
        return Turn(request_id=request_id, conversation_id=rows[0]["conversation_id"],
                    turn_number=rows[0]["turn_number"], user=_message(rows[0]), assistant=_message(rows[1]))

    async def append_turn(self, *, conversation_id: UUID, completed: CompletedTurn) -> TurnWrite:
        identity(conversation_id)
        # Revalidate even model_construct()/model_copy() inputs before SQL writes.
        completed = CompletedTurn.model_validate(completed.model_dump())
        try:
            # Keep session cleanup outside transaction cleanup, including a
            # failed/lost COMMIT acknowledgement after PostgreSQL committed.
            async with self._sessions() as db, db.begin():
                current = (await db.execute(sa.select(conversations).where(
                    conversations.c.conversation_id == conversation_id,
                ).with_for_update())).mappings().one_or_none()
                if current is None:
                    raise ConversationNotFound()
                number = current["last_turn"] + 1
                inserted = await db.scalar(insert(turns).values(
                    request_id=completed.request_id, conversation_id=conversation_id, turn_number=number,
                ).on_conflict_do_nothing(index_elements=[turns.c.request_id]).returning(turns.c.request_id))
                if inserted is None:
                    original = await self._stored_turn(db, completed.request_id)
                    if (original.conversation_id != conversation_id or original.user.content != completed.question
                            or original.assistant.content != completed.answer
                            or original.user.input_kind != completed.input_kind
                            or original.assistant.memory_ids != completed.memory_ids):
                        raise IdempotencyConflict()
                    result = TurnWrite(turn=original, created=False)
                else:
                    timestamp = await db.scalar(sa.select(sa.func.greatest(
                        sa.func.clock_timestamp(), current["last_activity_at"],
                    )))
                    common = dict(request_id=completed.request_id, conversation_id=conversation_id,
                                  turn_number=number, created_at=timestamp)
                    await db.execute(sa.insert(messages).values(
                        **common, message_id=uuid4(), sequence=number * 2 - 1, role="user",
                        content=completed.question, input_kind=completed.input_kind.value, memory_ids=[],
                    ))
                    await db.execute(sa.insert(messages).values(
                        **common, message_id=uuid4(), sequence=number * 2, role="assistant",
                        content=completed.answer, input_kind=None, memory_ids=list(completed.memory_ids),
                    ))
                    await db.execute(sa.update(conversations).where(
                        conversations.c.conversation_id == conversation_id,
                    ).values(last_turn=number, last_activity_at=timestamp))
                    result = TurnWrite(turn=await self._stored_turn(db, completed.request_id), created=True)
            # No successful return before the transaction has actually committed.
            return result
        except ConversationError:
            raise
        except Exception:
            raise ConversationPersistenceError() from None

    async def get_turn(self, *, request_id: UUID) -> Turn | None:
        identity(request_id)
        try:
            async with self._sessions() as db, db.begin():
                await db.execute(sa.text("SET TRANSACTION READ ONLY"))
                exists = await db.scalar(sa.select(turns.c.request_id).where(turns.c.request_id == request_id))
                return None if exists is None else await self._stored_turn(db, request_id)
        except ConversationError:
            raise
        except Exception:
            raise ConversationPersistenceError() from None

    async def recent_messages(self, *, conversation_id: UUID, limit: int = 50,
                              before_sequence: int | None = None) -> MessagePage:
        identity(conversation_id)
        page_size(limit)
        if before_sequence is not None and (type(before_sequence) is not int or not 1 <= before_sequence <= MAX_SEQUENCE):
            raise ValueError("History cursor must be a positive signed bigint sequence.")
        try:
            async with self._sessions() as db, db.begin():
                await db.execute(sa.text("SET TRANSACTION READ ONLY"))
                exists = await db.scalar(sa.select(conversations.c.conversation_id).where(
                    conversations.c.conversation_id == conversation_id,
                ))
                if exists is None:
                    raise ConversationNotFound()
                statement = sa.select(messages).where(messages.c.conversation_id == conversation_id)
                if before_sequence is not None:
                    statement = statement.where(messages.c.sequence < before_sequence)
                rows = (await db.execute(statement.order_by(messages.c.sequence.desc()).limit(limit + 1))).mappings().all()
                selected = tuple(_message(row) for row in reversed(rows[:limit]))
                return MessagePage(messages=selected,
                                   next_before_sequence=selected[0].sequence if len(rows) > limit else None)
        except ConversationError:
            raise
        except Exception:
            raise ConversationPersistenceError() from None

    async def list_conversations(self, *, limit: int = 20,
                                 cursor: ConversationCursor | None = None) -> ConversationPage:
        page_size(limit)
        if cursor is not None:
            cursor = ConversationCursor.model_validate(cursor.model_dump())
        try:
            async with self._sessions() as db, db.begin():
                await db.execute(sa.text("SET TRANSACTION READ ONLY"))
                statement = sa.select(conversations)
                if cursor is not None:
                    statement = statement.where(sa.tuple_(
                        conversations.c.last_activity_at, conversations.c.conversation_id,
                    ) < sa.tuple_(cursor.before_activity_at, cursor.before_id))
                rows = (await db.execute(statement.order_by(
                    conversations.c.last_activity_at.desc(), conversations.c.conversation_id.desc(),
                ).limit(limit + 1))).mappings().all()
                selected = tuple(_conversation(row) for row in rows[:limit])
                next_cursor = ConversationCursor(
                    before_activity_at=selected[-1].last_activity_at, before_id=selected[-1].conversation_id,
                ) if len(rows) > limit else None
                return ConversationPage(conversations=selected, next_cursor=next_cursor)
        except Exception:
            raise ConversationPersistenceError() from None
