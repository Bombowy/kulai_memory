"""Context/response boundaries of conversational Qwen, independent of V1 ASK."""
import asyncio
from datetime import UTC, datetime
import json
from uuid import uuid4

from kulai_llm import LLMFinishReason, LLMResponse
import pytest

from backend.tests.test_memory_rag import FakeLLM, evidence
from kulai_memory.application.rag import build_memory_context
from kulai_memory.conversations.domain import ConversationGenerationError, Message
from kulai_memory.conversations.generation import (
    MAX_HISTORY_CHARS, MAX_PROMPT_CHARS, bounded_history, generate_conversation, retrieval_query,
)


def history(count, *, question_size=8, answer_size=8):
    conversation_id, now = uuid4(), datetime.now(UTC)
    result = []
    for index in range(count):
        request = uuid4()
        for role, size, sequence in (("user", question_size, index * 2 + 1), ("assistant", answer_size, index * 2 + 2)):
            result.append(Message(message_id=uuid4(), request_id=request, conversation_id=conversation_id,
                role=role, content=str(index) + "x" * size, sequence=sequence, created_at=now,
                input_kind="text" if role == "user" else None))
    return tuple(result)


class ConversationalLLM(FakeLLM):
    def __init__(self, *, answer="Działamy dalej.", ids=(), finish=LLMFinishReason.STOP):
        super().__init__(answer=answer, ids=ids)
        self.output.pop("sufficient_context")
        self.finish = finish

    async def generate(self, request):
        response = await super().generate(request)
        return response.model_copy(update={"finish_reason": self.finish})


def test_context_is_a_chronological_suffix_of_at_most_twelve_complete_turns():
    previous = history(16)
    selected = bounded_history(previous)
    assert selected == previous[-24:]
    large = history(12, question_size=5000, answer_size=5000)
    selected = bounded_history(large)
    assert len(selected) == 4 and selected == large[-4:]
    assert sum(len(m.content) for m in selected) <= MAX_HISTORY_CHARS
    assert bounded_history(large, max_chars=2) == ()  # Never cut a question or answer.


@pytest.mark.parametrize("mutation", ["partial", "different-request", "gap", "different-conversation"])
def test_incomplete_or_mixed_history_cannot_reach_qwen(mutation):
    previous = list(history(2))
    if mutation == "partial": previous.pop()
    elif mutation == "different-request": previous[1] = previous[1].model_copy(update={"request_id": uuid4()})
    elif mutation == "gap": previous[2] = previous[2].model_copy(update={"sequence": 13})
    else: previous[2] = previous[2].model_copy(update={"conversation_id": uuid4()})
    with pytest.raises(ConversationGenerationError):
        bounded_history(tuple(previous))


def test_prompt_uses_roles_current_question_and_optional_memory_with_no_citation_requirement():
    async def run():
        llm, previous = ConversationalLLM(), history(2)
        result = await generate_conversation(query="Rozwiń poprzednią odpowiedź.", history=previous,
            context=build_memory_context(evidence()), llm_provider=llm)
        assert result.answer == "Działamy dalej." and not result.memory_ids
        messages = llm.calls[0].messages
        assert [m.role.value for m in messages] == ["system", "user", "assistant", "user", "assistant", "user"]
        assert [m.content for m in messages[1:-1]] == [m.content for m in previous]
        assert json.loads(messages[-1].content) == dict(question="Rozwiń poprzednią odpowiedź.", memory_evidence={"memories": []})
        assert sum(len(m.content) for m in messages) <= MAX_PROMPT_CHARS
    asyncio.run(run())


def test_memory_citations_are_only_from_supplied_evidence_and_preserve_identity():
    async def run():
        context = build_memory_context(evidence("Synthetic package costs 79 PLN."))
        memory_id = context.hits[0].memory.id
        llm = ConversationalLLM(answer="Pakiet kosztuje 79 zł.", ids=(str(memory_id), str(memory_id)))
        result = await generate_conversation(query="Ile kosztuje pakiet?", history=(), context=context, llm_provider=llm)
        assert result.memory_ids == (memory_id,)
        llm.output["used_memory_ids"] = [str(uuid4())]
        with pytest.raises(ConversationGenerationError):
            await generate_conversation(query="Ile?", history=(), context=context, llm_provider=llm)
    asyncio.run(run())


@pytest.mark.parametrize("finish", [LLMFinishReason.LENGTH, LLMFinishReason.OTHER, None])
def test_valid_json_with_unfinished_generation_is_rejected(finish):
    with pytest.raises(ConversationGenerationError):
        asyncio.run(generate_conversation(query="Pytanie?", history=(), context=build_memory_context(evidence()),
                                         llm_provider=ConversationalLLM(finish=finish)))


@pytest.mark.parametrize("payload", ["partial JSON", '{"answer":" ","used_memory_ids":[]}',
    '{"answer":"x","used_memory_ids":[],"unexpected":true}',
    '{"answer":"x","answer":"y","used_memory_ids":[]}'])
def test_invalid_structured_answer_is_rejected(payload):
    class Invalid(ConversationalLLM):
        async def generate(self, request):
            return LLMResponse(text=payload, provider_id="ollama", finish_reason=LLMFinishReason.STOP)
    with pytest.raises(ConversationGenerationError):
        asyncio.run(generate_conversation(query="Pytanie?", history=(), context=build_memory_context(evidence()),
                                         llm_provider=Invalid()))


def test_retrieval_pronouns_get_recent_subject_within_existing_query_limit():
    previous = history(2, question_size=9000, answer_size=9000)
    query = retrieval_query("A ile to kosztuje?", previous)
    assert query.endswith("A ile to kosztuje?") and previous[-1].content[-100:] in query
    assert len(query) <= 10_000
    assert retrieval_query("x" * 10000, previous) == "x" * 10000


def test_total_prompt_budget_includes_escaped_json_current_question():
    async def run():
        llm = ConversationalLLM()
        await generate_conversation(query="\n" * 9999 + "?", history=history(12, question_size=6000, answer_size=6000),
            context=build_memory_context(evidence("a" * 10000)), llm_provider=llm)
        assert sum(len(m.content) for m in llm.calls[0].messages) <= MAX_PROMPT_CHARS
        assert (len(llm.calls[0].messages) - 2) % 2 == 0
    asyncio.run(run())
