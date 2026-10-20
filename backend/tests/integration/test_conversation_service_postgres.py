"""Conversation inference on synthetic marker-owned PostgreSQL, never the user's DB."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.test_conversation_generation import ConversationalLLM
from backend.tests.test_desktop_controller import FakeProvider
from backend.tests.integration.test_automatic_indexing_postgres import CheckedProvider, create
from kulai_memory import rag_runtime
from kulai_memory.conversation_persistence import PostgresConversationRepository
from kulai_memory.conversations import ConversationNotFound, IdempotencyConflict
from kulai_memory.conversations.domain import ConversationGenerationError
from kulai_memory.conversations import generation
from kulai_memory.database_safety import database_snapshot_url
from kulai_memory.server.runtime import VoiceMemoryServerRuntime
from kulai_memory.settings import Settings


def canonical(snapshot):
    return snapshot.memories, snapshot.vectors, snapshot.tombstones, snapshot.security


class CheckedQwen(ConversationalLLM):
    def __init__(self, factory):
        super().__init__()
        self.factory, self.before, self.attempts = factory, None, 0

    async def generate(self, request):
        self.attempts += 1
        assert self.factory.kw["bind"].pool.checkedout() == 0
        if self.before is not None:
            await self.before()
        return await super().generate(request)


@asynccontextmanager
async def running_conversations(monkeypatch, *, real=False, security=False):
    async with owned_store(security=security) as (owned, config, target, factory, _):
        engine = factory.kw["bind"]
        bge, llm = CheckedProvider(engine), CheckedQwen(factory)
        kwargs = {}
        if not real:
            monkeypatch.setattr(rag_runtime, "create_llm_provider", lambda **kw: llm)
            kwargs["embedding_provider_factory"] = lambda **kw: bge
        runtime = VoiceMemoryServerRuntime(settings=Settings(_env_file=None, kulai_vector_dimension=1024),
            provider_factory=lambda settings: FakeProvider(("unused synthetic audio",)),
            database_config_factory=lambda: target, engine_factory=lambda config: engine,
            session_factory_builder=lambda engine: factory, **kwargs)
        try:
            await runtime.startup()
            yield SimpleNamespace(owned=owned, config=config, target=target, factory=factory, runtime=runtime,
                                  repository=PostgresConversationRepository(factory), llm=llm, bge=bge)
        finally:
            await runtime.shutdown()


def test_five_polish_turns_reconnect_and_independent_conversation_without_memories(monkeypatch):
    async def run():
        async with running_conversations(monkeypatch) as h:
            first, second = await h.repository.create(), await h.repository.create()
            before = await database_snapshot_url(h.target.async_url)
            assert before.memories.count == before.vectors.count == 0
            questions = ["Robimy projekt Latarnia: notes kosztuje 12 zł, sprzedajemy po 30 zł.",
                         "A ile zarobimy na 10 sztukach?", "Jak to promować za 300 zł?",
                         "Rozwiń poprzednią odpowiedź.", "Jaka była pierwotna cena i koszt?"]
            reopened = None
            try:
                for index, question in enumerate(questions):
                    if index == 3:
                        await h.factory.kw["bind"].dispose()
                        reopened = create_async_engine(h.target.async_url, echo=False, hide_parameters=True)
                        h.factory.configure(bind=reopened)
                    h.llm.output["answer"] = "Odpowiedź " + str(index)
                    result = await h.runtime.ask_conversation(conversation_id=first.conversation_id,
                        request_id=uuid4(), question=question, input_kind="voice" if index == 0 else "text")
                    assert result.created and result.turn.turn_number == index + 1
                    prompt = h.llm.calls[-1].messages
                    assert len(prompt) == index * 2 + 2
                    assert [message.content for message in prompt[1:-1:2]] == questions[:index]
                    assert json.loads(prompt[-1].content)["question"] == question
                    assert not result.turn.assistant.memory_ids
                    assert await h.repository.get_turn(request_id=result.turn.request_id) == result.turn
                result = await h.runtime.ask_conversation(conversation_id=second.conversation_id,
                    request_id=uuid4(), question="Zaczynamy inną rozmowę o kosmosie.", input_kind="text")
                assert result.turn.turn_number == 1 and len(h.llm.calls[-1].messages) == 2
                assert "Latarnia" not in h.llm.calls[-1].messages[-1].content
                assert len((await h.repository.recent_messages(conversation_id=first.conversation_id)).messages) == 10
                assert len((await h.repository.recent_messages(conversation_id=second.conversation_id)).messages) == 2
                assert canonical(await database_snapshot_url(h.target.async_url)) == canonical(before)
            finally:
                if reopened is not None:
                    await reopened.dispose()
    asyncio.run(asyncio.wait_for(run(), 120))


def test_concurrent_duplicate_requests_replay_without_qwen_and_conflicts_reject(monkeypatch):
    async def run():
        async with running_conversations(monkeypatch) as h:
            first, second = await h.repository.create(), await h.repository.create()
            request = dict(conversation_id=first.conversation_id, request_id=uuid4(), question="Pytanie", input_kind="text")
            replies = await asyncio.gather(*(h.runtime.ask_conversation(**request) for _ in range(8)))
            assert sum(reply.created for reply in replies) == h.llm.attempts == 1
            assert all(reply.turn == replies[0].turn for reply in replies)
            before = await database_snapshot_url(h.target.async_url)
            h.llm.error = RuntimeError("A replay must never reach Qwen")
            await h.factory.kw["bind"].dispose()  # Replay with a new DB connection too.
            assert not (await h.runtime.ask_conversation(**request)).created
            assert await database_snapshot_url(h.target.async_url) == before
            for changes in ({"question": "Zmiana"}, {"input_kind": "voice"}, {"conversation_id": second.conversation_id}):
                with pytest.raises(IdempotencyConflict):
                    await h.runtime.ask_conversation(**{**request, **changes})
            with pytest.raises(ConversationNotFound):
                await h.runtime.ask_conversation(**{**request, "conversation_id": uuid4(), "request_id": uuid4()})
            assert h.llm.attempts == 1
    asyncio.run(asyncio.wait_for(run(), 120))


def test_postgres_history_window_contains_only_twelve_latest_complete_turns(monkeypatch):
    async def run():
        async with running_conversations(monkeypatch) as h:
            from kulai_memory.conversations import CompletedTurn
            conversation = await h.repository.create()
            for index in range(16):
                await h.repository.append_turn(conversation_id=conversation.conversation_id,
                    completed=CompletedTurn(request_id=uuid4(), question=f"Seed {index}",
                                            answer=f"Answer {index}", input_kind="text"))
            result = await h.runtime.ask_conversation(conversation_id=conversation.conversation_id,
                request_id=uuid4(), question="Rozwiń poprzednią odpowiedź.", input_kind="text")
            messages = h.llm.calls[-1].messages
            assert result.turn.turn_number == 17 and len(messages) == 26
            assert [m.content for m in messages[1:-1:2]] == [f"Seed {index}" for index in range(4, 16)]
            assert [m.content for m in messages[2:-1:2]] == [f"Answer {index}" for index in range(4, 16)]
    asyncio.run(asyncio.wait_for(run(), 120))


def test_concurrent_distinct_requests_see_previous_turn_and_share_existing_ai_lane(monkeypatch):
    async def run():
        async with running_conversations(monkeypatch) as h:
            conversation = await h.repository.create()
            entered, release = asyncio.Event(), asyncio.Event()
            async def existing_ai_operation():
                entered.set()
                await release.wait()
            busy = asyncio.create_task(h.runtime._run_ai(existing_ai_operation))
            await entered.wait()
            pending = [asyncio.create_task(h.runtime.ask_conversation(conversation_id=conversation.conversation_id,
                       request_id=uuid4(), question=f"Pytanie {i}", input_kind="text")) for i in range(5)]
            await asyncio.sleep(.02)
            assert h.llm.attempts == 0
            assert not (await h.runtime.list_memories()).items  # Existing reads are still available.
            release.set()
            replies = await asyncio.gather(*pending)
            await busy
            assert [reply.turn.turn_number for reply in replies] == [1, 2, 3, 4, 5]
            assert [len(request.messages) for request in h.llm.calls] == [2, 4, 6, 8, 10]
    asyncio.run(asyncio.wait_for(run(), 120))


@pytest.mark.parametrize("failure", ["exception", "timeout", "cancel", "unfinished", "invalid-citation"])
def test_failed_or_cancelled_generation_never_appends_and_retry_can_succeed(monkeypatch, failure, caplog):
    async def run():
        async with running_conversations(monkeypatch) as h:
            conversation = await h.repository.create()
            request = dict(conversation_id=conversation.conversation_id, request_id=uuid4(), question="Pytanie", input_kind="voice")
            before = await database_snapshot_url(h.target.async_url)
            entered = asyncio.Event()
            async def blocked():
                entered.set()
                await asyncio.Event().wait()
            if failure == "exception": h.llm.error = RuntimeError("PRIVATE_GENERATION_ERROR")
            elif failure in {"timeout", "cancel"}:
                h.llm.before = blocked
                if failure == "timeout": monkeypatch.setattr(generation, "LLM_TIMEOUT_SECONDS", .05)
            elif failure == "unfinished": h.llm.finish = "length"
            else: h.llm.output["used_memory_ids"] = [str(uuid4())]
            task = asyncio.create_task(h.runtime.ask_conversation(**request))
            if failure == "cancel":
                await entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError): await task
            else:
                with pytest.raises(ConversationGenerationError) as caught:
                    await task
                assert "PRIVATE" not in str(caught.value) and caught.value.__cause__ is None
            assert await database_snapshot_url(h.target.async_url) == before
            assert h.factory.kw["bind"].pool.checkedout() == 0
            h.llm.error = h.llm.before = None
            h.llm.finish = "stop"
            h.llm.output["used_memory_ids"] = []
            retry = await h.runtime.ask_conversation(**request)
            assert retry.created and retry.turn.turn_number == 1
            assert "PRIVATE_GENERATION_ERROR" not in caplog.text
    asyncio.run(asyncio.wait_for(run(), 120))


def test_memory_used_in_answer_is_cited_and_classic_ask_remains_grounded(monkeypatch):
    async def run():
        async with running_conversations(monkeypatch) as h:
            empty = await h.runtime.ask_memory(query="Zaproponuj pomysł na produkt.")
            assert not empty.sufficient_context and h.llm.attempts == 0  # V1 still refuses without Memory.
            memory = await create(h.factory, "Syntetyczny pakiet Aurora kosztuje 79 zł.")
            await h.runtime._run_ai(lambda: h.runtime._indexer.ensure(memory=memory))
            before = await database_snapshot_url(h.target.async_url)
            h.llm.output = dict(answer="Pakiet Aurora kosztuje 79 zł.", used_memory_ids=[str(memory.id)])
            conversation = await h.repository.create()
            reply = await h.runtime.ask_conversation(conversation_id=conversation.conversation_id,
                request_id=uuid4(), question="Jaka jest cena pakietu w zapisanej Memory?", input_kind="text")
            assert reply.turn.assistant.memory_ids == (memory.id,)
            payload = json.loads(h.llm.calls[-1].messages[-1].content)
            assert payload["memory_evidence"]["memories"][0]["memory_id"] == str(memory.id)
            h.llm.output["sufficient_context"] = True
            classic = await h.runtime.ask_memory(query="Ile kosztuje Aurora?")
            assert classic.sufficient_context and classic.citations[0].memory_id == memory.id
            assert canonical(await database_snapshot_url(h.target.async_url)) == canonical(before)
    asyncio.run(asyncio.wait_for(run(), 120))


def test_request_after_lost_commit_acknowledgement_returns_stored_answer_without_generation(monkeypatch):
    async def run():
        async with running_conversations(monkeypatch) as h:
            from kulai_memory.conversations.service import ConversationService
            from kulai_memory.conversations import ConversationPersistenceError
            class LostAcknowledgement(PostgresConversationRepository):
                async def append_turn(self, **kwargs):
                    await super().append_turn(**kwargs)
                    raise ConversationPersistenceError()
            service = ConversationService(repository=LostAcknowledgement(h.factory),
                generate_answer=h.runtime._rag.answer_conversation, run_ai=h.runtime._run_ai)
            conversation = await h.repository.create()
            request = dict(conversation_id=conversation.conversation_id, request_id=uuid4(), question="Pytanie", input_kind="text")
            with pytest.raises(ConversationPersistenceError): await service.ask(**request)
            recovered = await service.ask(**request)
            assert not recovered.created and recovered.turn.turn_number == 1 and h.llm.attempts == 1
    asyncio.run(asyncio.wait_for(run(), 120))


def test_real_qwen_five_turns_reconnect_and_memory_grounding(monkeypatch):
    if not all(os.environ.get(name) == "1" for name in ("KULAI_RUN_OLLAMA_INTEGRATION", "KULAI_RUN_LLM_INTEGRATION")):
        pytest.skip("Enable local Qwen/BGE integration; only synthetic owned DB data is used.")
    async def run():
        async with running_conversations(monkeypatch, real=True) as h:
            first, second = await h.repository.create(), await h.repository.create()
            provider = h.runtime._rag._llm
            generate = provider.generate
            requests = []
            async def observed(request):
                assert h.factory.kw["bind"].pool.checkedout() == 0
                requests.append(request)
                return await generate(request)
            monkeypatch.setattr(provider, "generate", observed)
            questions = [
                "Nasz fikcyjny projekt to Niebieska Latarnia. Sprzedajemy notesy A5 po 30 zł, koszt jednostkowy 12 zł, "
                "budżet promocji 300 zł. Potwierdź te ustalenia jednym zdaniem.",
                "A ile zarobimy na 10 sztukach przed kosztami promocji? Podaj kwotę po odjęciu kosztu produkcji.",
                "Jak to promować za ustalony budżet? Podaj trzy krótkie pomysły.",
                "Rozwiń poprzednią odpowiedź, wybierając pierwszy pomysł. Użyj nazwy naszego projektu. Maksymalnie trzy zdania.",
                "Jaka była cena jednostkowa, koszt jednostkowy i budżet? Podaj tylko trzy kwoty z krótkimi etykietami.",
            ]
            trace, reopened = [], None
            try:
                for index, question in enumerate(questions):
                    if index == 3:
                        await h.factory.kw["bind"].dispose()
                        reopened = create_async_engine(h.target.async_url, echo=False, hide_parameters=True)
                        h.factory.configure(bind=reopened)
                    result = await h.runtime.ask_conversation(conversation_id=first.conversation_id,
                        request_id=uuid4(), question=question, input_kind="voice" if index == 0 else "text")
                    assert result.created and result.turn.turn_number == index + 1
                    assert not result.turn.assistant.memory_ids
                    trace.append(result.turn.assistant.content)
                    assert len(requests[-1].messages) == 2 + 2 * index
                    print("QWEN_REAL_TURN=" + json.dumps(dict(turn=index + 1, answer=trace[-1]), ensure_ascii=False), flush=True)
                assert "180" in trace[1]
                assert "latarnia" in trace[3].lower() and "niebieska" in trace[3].lower()
                assert all(amount in trace[4] for amount in ("30", "12", "300"))
                before_memory = await database_snapshot_url(h.target.async_url)
                assert before_memory.memories.count == before_memory.vectors.count == 0
                independent = await h.runtime.ask_conversation(conversation_id=second.conversation_id,
                    request_id=uuid4(), question="Nasz osobny projekt to Zielony Most. Powtórz tylko jego nazwę.", input_kind="text")
                assert "zielony most" in independent.turn.assistant.content.lower()
                assert "latarnia" not in independent.turn.assistant.content.lower()
                assert len(requests[-1].messages) == 2
                memory = await create(h.factory, "Według syntetycznego cennika pakiet Aurora kosztuje 79 zł.")
                await h.runtime._run_ai(lambda: h.runtime._indexer.ensure(memory=memory))
                grounded = await h.runtime.ask_conversation(conversation_id=second.conversation_id,
                    request_id=uuid4(), question="Ile według zapisanej Memory kosztuje pakiet Aurora? Podaj cenę.", input_kind="text")
                assert "79" in grounded.turn.assistant.content and grounded.turn.assistant.memory_ids == (memory.id,)
                replay = await h.runtime.ask_conversation(conversation_id=second.conversation_id,
                    request_id=grounded.turn.request_id, question=grounded.turn.user.content, input_kind="text")
                assert not replay.created and replay.turn == grounded.turn
                assert len(requests) == 7  # Replay never invokes Qwen again.
                print("QWEN_REAL_SYNTHETIC=" + json.dumps(dict(model="qwen3.5:9b", turns=5, answers=trace,
                    reconnected_after_turn=3, memories_during_conversation=0, independent=True,
                    exact_memory_citation=True, replay=True), ensure_ascii=False))
            finally:
                if reopened is not None:
                    await reopened.dispose()
    asyncio.run(asyncio.wait_for(run(), 600))
