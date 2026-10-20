"""V2 schema and storage proofs; DDL/DML only on verified marker-owned databases."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import sys
from uuid import uuid4

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.integration.test_memory_lifecycle_postgres import snapshot
from backend.tests.integration.test_automatic_indexing_postgres import CheckedProvider, create, indexer
from kulai_memory.conversations import (
    CompletedTurn, ConversationNotFound, ConversationPersistenceError, IdempotencyConflict,
)
from kulai_memory.conversations.domain import MAX_MESSAGE_CHARS
from kulai_memory.conversation_persistence import PostgresConversationRepository
from kulai_memory.conversation_persistence.schema import conversations, messages, metadata, turns
from kulai_memory.database_safety import require_owned_database, run_database_doctor


@asynccontextmanager
async def owned_conversations():
    async with owned_store() as (owned, config, target, factory, _):
        await require_owned_database(owned, config=config)
        engine = factory.kw["bind"]
        async with engine.begin() as connection:
            assert await connection.scalar(sa.text("SELECT current_database()")) == owned.name
            assert await connection.scalar(sa.text(
                "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = current_database()"
            )) == owned.marker
            await connection.run_sync(metadata.create_all)
        yield owned, target, factory, PostgresConversationRepository(factory)
        assert engine.pool.checkedout() == 0


def completed(number=0, **overrides):
    values = dict(request_id=uuid4(), question=f"Pytanie {number}: gdzie jest klucz?",
                  answer=f"Answer {number}: the key is in the drawer.", input_kind="voice")
    return CompletedTurn(**{**values, **overrides})


async def counts(factory):
    async with factory() as db:
        return tuple([await db.scalar(sa.select(sa.func.count()).select_from(table))
                      for table in (turns, messages)])


def test_pl_en_multiple_turns_independence_and_canonical_vector_security_unchanged():
    async def scenario():
        async with owned_conversations() as (_, target, factory, repository):
            # Nonempty synthetic Memory/vector fixtures make equality meaningful.
            memory = await create(factory)
            worker = indexer(factory, CheckedProvider(factory.kw["bind"]))
            try:
                await worker.ensure(memory=memory)
            finally:
                await worker.aclose()
            before = await snapshot(factory)
            assert before.memories.count == before.vectors.count == 1
            first = await repository.create(title="Polska rozmowa")
            second = await repository.create(title="English conversation")
            assert first.created_at == first.last_activity_at
            assert (await repository.recent_messages(conversation_id=first.conversation_id)).messages == ()
            writes = []
            for number in range(3):
                writes.append(await repository.append_turn(conversation_id=first.conversation_id,
                    completed=completed(number, input_kind="text", memory_ids=(memory.id,))))
                await repository.append_turn(conversation_id=second.conversation_id,
                    completed=completed(number, question=f"Where is item {number}?", answer="W szufladzie."))
            left = (await repository.recent_messages(conversation_id=first.conversation_id)).messages
            right = (await repository.recent_messages(conversation_id=second.conversation_id)).messages
            assert [m.sequence for m in left] == [1, 2, 3, 4, 5, 6]
            assert all(m.conversation_id == first.conversation_id for m in left)
            assert all(m.conversation_id == second.conversation_id for m in right)
            assert not {m.message_id for m in left} & {m.message_id for m in right}
            assert [m.content for m in left[::2]] == [completed(n).question for n in range(3)]
            assert all(m.input_kind.value == "text" and not m.memory_ids for m in left[::2])
            assert all(m.input_kind is None and m.memory_ids == (memory.id,) for m in left[1::2])
            assert all(w.created for w in writes)
            assert await snapshot(factory) == before
            assert (await run_database_doctor(async_url=target.async_url, require_security=True)).ok
            assert await counts(factory) == (6, 12)
    asyncio.run(asyncio.wait_for(scenario(), 120))


def test_reopen_engine_and_new_process_restore_identical_conversation():
    async def scenario():
        async with owned_conversations() as (owned, target, factory, repository):
            conversation = await repository.create()
            for n in range(3):
                await repository.append_turn(conversation_id=conversation.conversation_id, completed=completed(n))
            expected = await repository.recent_messages(conversation_id=conversation.conversation_id)
            await factory.kw["bind"].dispose()
            restarted_engine = create_async_engine(target.async_url, echo=False, hide_parameters=True)
            try:
                restarted = PostgresConversationRepository(async_sessionmaker(restarted_engine, expire_on_commit=False))
                assert await restarted.recent_messages(conversation_id=conversation.conversation_id) == expected
            finally:
                await restarted_engine.dispose()
            process = await asyncio.create_subprocess_exec(sys.executable, "-m", "backend.tests.conversations_postgres_worker",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                data = json.dumps(dict(database=owned.name, marker=owned.marker, conversation_id=str(conversation.conversation_id)))
                output, error = await asyncio.wait_for(process.communicate(data.encode()), 30)
                assert process.returncode == 0 and error == b""
                assert json.loads(output) == expected.model_dump(mode="json")
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
    asyncio.run(asyncio.wait_for(scenario(), 120))


def test_concurrent_turns_are_adjacent_and_duplicate_request_is_exactly_once():
    async def scenario():
        async with owned_conversations() as (_, _, factory, repository):
            conversation = await repository.create()
            identity = conversation.conversation_id
            inputs = [completed(n) for n in range(8)]
            writes = await asyncio.gather(*(repository.append_turn(conversation_id=identity, completed=value) for value in inputs))
            assert sorted(w.turn.turn_number for w in writes) == list(range(1, 9))
            history = (await repository.recent_messages(conversation_id=identity)).messages
            assert [m.sequence for m in history] == list(range(1, 17))
            assert len({m.message_id for m in history}) == 16
            for user, answer in zip(history[::2], history[1::2]):
                assert user.request_id == answer.request_id
                source = next(value for value in inputs if value.request_id == user.request_id)
                assert user.content == source.question and answer.content == source.answer
            new = completed(99)
            duplicates = await asyncio.gather(*(repository.append_turn(conversation_id=identity, completed=new) for _ in range(8)))
            assert sum(w.created for w in duplicates) == 1
            assert all(w.turn == duplicates[0].turn for w in duplicates)
            before = await repository.list_conversations()
            assert not (await repository.append_turn(conversation_id=identity, completed=new)).created
            assert await repository.list_conversations() == before
            assert await counts(factory) == (9, 18)
    asyncio.run(asyncio.wait_for(scenario(), 120))


def test_reused_request_with_changed_payload_or_conversation_is_conflict():
    async def scenario():
        async with owned_conversations() as (_, _, factory, repository):
            left, right = await repository.create(), await repository.create()
            turn = completed()
            await repository.append_turn(conversation_id=left.conversation_id, completed=turn)
            before = await repository.list_conversations()
            for changes in ({"question": "different"}, {"answer": "different"}, {"input_kind": "text"}, {"memory_ids": (uuid4(),)}):
                with pytest.raises(IdempotencyConflict):
                    await repository.append_turn(conversation_id=left.conversation_id,
                                                 completed=CompletedTurn(**{**turn.model_dump(), **changes}))
            with pytest.raises(IdempotencyConflict):
                await repository.append_turn(conversation_id=right.conversation_id, completed=turn)
            assert await repository.list_conversations() == before
            assert await counts(factory) == (1, 2)
    asyncio.run(asyncio.wait_for(scenario(), 120))


def test_lost_commit_acknowledgement_retries_without_duplicate_turn():
    async def scenario():
        async with owned_conversations() as (_, _, factory, repository):
            conversation = await repository.create()
            payload = completed()
            uncertain = PostgresConversationRepository(async_sessionmaker(
                factory.kw["bind"], expire_on_commit=False, info={"lose_ack": True},
            ))

            def lose_ack(session):
                if session.info.get("lose_ack"):
                    raise ConnectionError("Synthetic failure after real COMMIT")

            sa.event.listen(AsyncSession.sync_session_class, "after_commit", lose_ack)
            try:
                with pytest.raises(ConversationPersistenceError):
                    await uncertain.append_turn(conversation_id=conversation.conversation_id, completed=payload)
            finally:
                sa.event.remove(AsyncSession.sync_session_class, "after_commit", lose_ack)
            assert await counts(factory) == (1, 2)
            retry = await repository.append_turn(conversation_id=conversation.conversation_id, completed=payload)
            assert not retry.created and retry.turn.turn_number == 1
            assert await counts(factory) == (1, 2)
    asyncio.run(asyncio.wait_for(scenario(), 120))


@pytest.mark.parametrize("failure", ["exception", "disconnect", "cancel"])
def test_failure_between_question_and_answer_rolls_back_entire_turn(failure, caplog):
    async def scenario():
        async with owned_conversations() as (_, _, factory, repository):
            conversation = await repository.create()
            before = await repository.list_conversations()
            entered = asyncio.Event()
            class FailingSession(AsyncSession):
                async def execute(self, statement, *args, **kwargs):
                    result = await super().execute(statement, *args, **kwargs)
                    if isinstance(statement, sa.sql.dml.Insert) and statement.table is messages:
                        entered.set()  # The question INSERT actually reached PostgreSQL.
                        if failure == "disconnect":
                            await (await self.connection()).invalidate()
                            raise ConnectionError("PRIVATE_CONVERSATION_FAULT")
                        if failure == "cancel":
                            await asyncio.Event().wait()
                        raise RuntimeError("PRIVATE_CONVERSATION_FAULT")
                    return result
            broken = PostgresConversationRepository(async_sessionmaker(factory.kw["bind"], class_=FailingSession, expire_on_commit=False))
            payload = completed()
            task = asyncio.create_task(broken.append_turn(conversation_id=conversation.conversation_id, completed=payload))
            await asyncio.wait_for(entered.wait(), 10)
            if failure == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(ConversationPersistenceError) as caught:
                    await task
                assert caught.value.__cause__ is None and "PRIVATE" not in str(caught.value)
            assert await counts(factory) == (0, 0)
            assert await repository.list_conversations() == before
            assert not (await repository.recent_messages(conversation_id=conversation.conversation_id)).messages
            retry = await repository.append_turn(conversation_id=conversation.conversation_id, completed=payload)
            assert retry.created and retry.turn.turn_number == 1
            assert "PRIVATE_CONVERSATION_FAULT" not in caplog.text
    asyncio.run(asyncio.wait_for(scenario(), 120))


def test_two_conversations_do_not_share_write_lock():
    async def scenario():
        async with owned_conversations() as (_, _, factory, repository):
            left, right = await repository.create(), await repository.create()
            entered, release = asyncio.Event(), asyncio.Event()
            class BlockedSession(AsyncSession):
                async def execute(self, statement, *args, **kwargs):
                    result = await super().execute(statement, *args, **kwargs)
                    if isinstance(statement, sa.sql.dml.Insert) and statement.table is messages:
                        entered.set()
                        await release.wait()
                    return result
            blocked = PostgresConversationRepository(async_sessionmaker(factory.kw["bind"], class_=BlockedSession, expire_on_commit=False))
            task = asyncio.create_task(blocked.append_turn(conversation_id=left.conversation_id, completed=completed()))
            try:
                await asyncio.wait_for(entered.wait(), 10)
                result = await asyncio.wait_for(repository.append_turn(conversation_id=right.conversation_id, completed=completed()), 5)
                assert result.created
                # Readers must not see the uncommitted user half in another session.
                assert not (await repository.recent_messages(conversation_id=left.conversation_id)).messages
            finally:
                release.set()
                await task
    asyncio.run(asyncio.wait_for(scenario(), 120))


def test_bounded_keyset_pages_activity_ties_and_database_length_constraints():
    async def scenario():
        async with owned_conversations() as (_, _, factory, repository):
            created = [await repository.create(title=f"Conversation {n}") for n in range(5)]
            target = created[0].conversation_id
            for n in range(4):
                await repository.append_turn(conversation_id=target, completed=completed(n))
            assert (await repository.list_conversations(limit=1)).conversations[0].conversation_id == target
            # Tie timestamps to prove UUID is a strict second key, not OFFSET.
            async with factory.begin() as db:
                timestamp = await db.scalar(sa.select(sa.func.max(conversations.c.last_activity_at)))
                await db.execute(sa.update(conversations).values(last_activity_at=timestamp))
            ids, cursor = [], None
            while True:
                page = await repository.list_conversations(limit=2, cursor=cursor)
                assert len(page.conversations) <= 2
                ids.extend(c.conversation_id for c in page.conversations)
                cursor = page.next_cursor
                if cursor is None:
                    break
            assert ids == sorted((c.conversation_id for c in created), reverse=True)
            pages, before = [], None
            while True:
                page = await repository.recent_messages(conversation_id=target, limit=3, before_sequence=before)
                assert len(page.messages) <= 3
                assert [m.sequence for m in page.messages] == sorted(m.sequence for m in page.messages)
                pages.append([m.sequence for m in page.messages])
                before = page.next_before_sequence
                if before is None:
                    break
            assert pages == [[6, 7, 8], [3, 4, 5], [1, 2]]
            for limit in (0, 101, True, "2"):
                with pytest.raises(ValueError):
                    await repository.list_conversations(limit=limit)
                with pytest.raises(ValueError):
                    await repository.recent_messages(conversation_id=target, limit=limit)
            for invalid in (0, -1, True, 2**63, "2"):
                with pytest.raises(ValueError):
                    await repository.recent_messages(conversation_id=target, before_sequence=invalid)
            with pytest.raises(ConversationNotFound):
                await repository.append_turn(conversation_id=uuid4(), completed=completed())
            with pytest.raises(ConversationNotFound):
                await repository.recent_messages(conversation_id=uuid4())
            with pytest.raises(ValidationError):
                await repository.append_turn(conversation_id=target,
                    completed=completed().model_copy(update={"answer": "x" * (MAX_MESSAGE_CHARS + 1)}))
            write = await repository.append_turn(conversation_id=target,
                completed=completed(question="ą" * MAX_MESSAGE_CHARS, answer="z" * MAX_MESSAGE_CHARS))
            assert len(write.turn.user.content) == MAX_MESSAGE_CHARS
            # DB checks also protect direct SQL callers, not just Pydantic inputs.
            for content in (" " * 10, "x" * (MAX_MESSAGE_CHARS + 1)):
                with pytest.raises(sa.exc.IntegrityError):
                    async with factory.begin() as db:
                        await db.execute(sa.update(messages).where(messages.c.message_id == write.turn.user.message_id).values(content=content))
    asyncio.run(asyncio.wait_for(scenario(), 120))
