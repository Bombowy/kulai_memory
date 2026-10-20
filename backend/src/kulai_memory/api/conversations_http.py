"""Thin authenticated HTTP adapter for the durable V2 conversation service."""
import asyncio
import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import Field, field_validator
from starlette.exceptions import HTTPException

from kulai_memory.conversations import (
    Conversation, ConversationCursor, ConversationNotFound, ConversationPage,
    ConversationPersistenceError, IdempotencyConflict, MessagePage, TurnWrite,
)
from kulai_memory.conversations.domain import (
    ConversationGenerationError, InputKind, MAX_SEQUENCE, Value, title_value, valid_text,
)
from kulai_memory.server.runtime import ServerRuntimeError
from .cancellation import ClientDisconnected, until_disconnect
from .server_access import server_runtime

logger = logging.getLogger(__name__)


class ConversationCreateRequest(Value):
    title: str | None = Field(default=None, strict=True, max_length=200)
    _title = field_validator("title")(title_value)


class ConversationAskRequest(Value):
    request_id: UUID
    question: str = Field(strict=True, max_length=10_000)
    input_kind: InputKind = InputKind.TEXT
    _text = field_validator("question")(valid_text)


class _ConversationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request: Request):
            try:
                return await handler(request)
            except ClientDisconnected:
                raise asyncio.CancelledError() from None
            except RequestValidationError:
                return JSONResponse({"detail": "Invalid conversation request."}, status_code=422)
            except HTTPException as exc:
                return JSONResponse({"detail": "Invalid conversation request."}, status_code=exc.status_code)
            except ConversationNotFound:
                return JSONResponse({"detail": "Conversation not found."}, status_code=404)
            except IdempotencyConflict:
                return JSONResponse({"detail": "Request identity conflicts with an existing turn."}, status_code=409)
            except ConversationGenerationError:
                return JSONResponse({"detail": "Conversation answer could not be generated."}, status_code=502)
            except (ConversationPersistenceError, ServerRuntimeError):
                return JSONResponse({"detail": "Conversation service is unavailable."}, status_code=503)
            except Exception as exc:
                logger.warning("conversation operation failed error_type=%s", type(exc).__name__)
                return JSONResponse({"detail": "Conversation operation failed."}, status_code=500)
        return safe


def create_conversations_router() -> APIRouter:
    router = APIRouter(route_class=_ConversationRoute)

    @router.post("/api/conversations", response_model=Conversation, status_code=201)
    async def create(request: Request, body: ConversationCreateRequest | None = None):
        return await until_disconnect(server_runtime(request.app).create_conversation(
            title=body.title if body is not None else None), request.receive)

    @router.get("/api/conversations", response_model=ConversationPage)
    async def conversations(request: Request, limit: int = Query(20, ge=1, le=100),
                            before_activity_at: datetime | None = None, before_id: UUID | None = None):
        if (before_activity_at is None) != (before_id is None) or (
                before_activity_at is not None and before_activity_at.utcoffset() is None):
            raise HTTPException(422)
        cursor = ConversationCursor(before_activity_at=before_activity_at, before_id=before_id) if before_id else None
        return await server_runtime(request.app).list_conversations(limit=limit, cursor=cursor)

    @router.get("/api/conversations/{conversation_id}/messages", response_model=MessagePage)
    async def messages(request: Request, conversation_id: UUID, limit: int = Query(50, ge=1, le=100),
                       before_sequence: int | None = Query(None, ge=1, le=MAX_SEQUENCE)):
        return await server_runtime(request.app).conversation_messages(conversation_id=conversation_id,
            limit=limit, before_sequence=before_sequence)

    @router.post("/api/conversations/{conversation_id}/ask", response_model=TurnWrite)
    async def ask(request: Request, conversation_id: UUID, body: ConversationAskRequest):
        return await until_disconnect(server_runtime(request.app).ask_conversation(
            conversation_id=conversation_id, request_id=body.request_id,
            question=body.question, input_kind=body.input_kind), request.receive)

    return router
