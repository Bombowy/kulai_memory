"""Exact final-answer speech planning; no Memory reads or audio synthesis."""
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kulai_memory.application.speech import (
    MAX_SPEECH_CHARS, MAX_SPEECH_SEGMENTS, SpeechPlan, validate_speech_text,
)
from .cancellation import until_disconnect
from .memory_http import _SafeRoute
from .server_access import server_runtime


class _SpeechRoute(_SafeRoute):
    invalid_message = "Invalid speech request."
    operation_message = "Speech planning could not be completed."
    operation_stage = "speech_planning"


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)
    answer: str = Field(min_length=1, max_length=MAX_SPEECH_CHARS)

    @field_validator("answer")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Invalid speech request.")
        return value


class SpeechSegmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)
    text: str = Field(min_length=1, max_length=MAX_SPEECH_CHARS)
    language: Literal["pl", "en"]


class SpeechResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)
    segments: tuple[SpeechSegmentResponse, ...] = Field(min_length=1, max_length=MAX_SPEECH_SEGMENTS)

    @classmethod
    def from_plan(cls, plan: SpeechPlan, *, answer: str) -> "SpeechResponse":
        validate_speech_text(answer)
        SpeechPlan(answer, plan.segments)  # Validate even injected runtimes; never repair.
        return cls(segments=tuple(SpeechSegmentResponse(text=s.text, language=s.language.value) for s in plan.segments))


def create_speech_http_router() -> APIRouter:
    router = APIRouter(route_class=_SpeechRoute)

    @router.post("/api/assistant/speech-plan", response_model=SpeechResponse)
    async def speech_plan(request: Request, body: SpeechRequest):
        plan = await until_disconnect(server_runtime(request.app).plan_speech(answer=body.answer), request.receive)
        return SpeechResponse.from_plan(plan, answer=body.answer)

    return router
