"""A whole turn in the host's existing AI lane; no providers or DB lifecycle here."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

from .domain import (
    CompletedTurn, ConversationAnswer, ConversationGenerationError, ConversationPersistenceError,
    ConversationQuestion, IdempotencyConflict, InputKind, Message, TurnWrite, identity,
)
from .ports import ConversationRepository

MAX_HISTORY_TURNS = 12


class ConversationService:
    def __init__(self, *, repository: ConversationRepository,
                 generate_answer: Callable[..., Awaitable[ConversationAnswer]],
                 run_ai: Callable[..., Awaitable[TurnWrite]]) -> None:
        self._repository, self._generate, self._run_ai = repository, generate_answer, run_ai

    async def ask(self, *, conversation_id: UUID, request_id: UUID,
                  question: str, input_kind: InputKind | str) -> TurnWrite:
        identity(conversation_id)
        identity(request_id)
        request = ConversationQuestion(conversation_id=conversation_id, request_id=request_id,
                                       question=question, input_kind=input_kind)
        # The lane includes lookup, history, generation and append. Thus queued
        # duplicate requests cannot regenerate and concurrent follow-ups see the
        # last completed turn. No database transaction spans model work.
        return await self._run_ai(lambda: self._answer(request))

    async def _answer(self, request: ConversationQuestion) -> TurnWrite:
        stored = await self._repository.get_turn(request_id=request.request_id)
        if stored is not None:
            if (stored.conversation_id != request.conversation_id or stored.user.content != request.question
                    or stored.user.input_kind != request.input_kind):
                raise IdempotencyConflict()
            return TurnWrite(turn=stored, created=False)
        page = await self._repository.recent_messages(conversation_id=request.conversation_id,
                                                     limit=2 * MAX_HISTORY_TURNS)
        history: tuple[Message, ...] = page.messages
        if any(message.conversation_id != request.conversation_id for message in history):
            raise ConversationPersistenceError()
        try:
            answer = await self._generate(query=request.question, history=history)
            answer = ConversationAnswer.model_validate(answer.model_dump())
        except Exception:
            raise ConversationGenerationError() from None
        # Also reject a provider that swallowed cancellation and returned text.
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        return await self._repository.append_turn(conversation_id=request.conversation_id,
            completed=CompletedTurn(request_id=request.request_id, question=request.question,
                                    answer=answer.answer, input_kind=request.input_kind, memory_ids=answer.memory_ids))
