"""Public answer projection; never expose query, evidence or model internals."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kulai_memory.application.rag import INSUFFICIENT_CONTEXT_ANSWER, MemoryCitation, MemoryRagResult


class AssistantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    query: str = Field(min_length=1, max_length=10000, strict=True)
    top_k: int = Field(default=5, ge=1, le=20, strict=True)

    @field_validator("query")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Invalid assistant request.")
        return value


class AssistantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    status: Literal["answered", "insufficient_context"]
    answer: str
    citations: tuple[MemoryCitation, ...]

    @classmethod
    def from_result(cls, result: MemoryRagResult) -> "AssistantResponse":
        return cls(status="answered" if result.sufficient_context else "insufficient_context",
                   answer=result.answer if result.sufficient_context else INSUFFICIENT_CONTEXT_ANSWER,
                   citations=result.citations if result.sufficient_context else ())
