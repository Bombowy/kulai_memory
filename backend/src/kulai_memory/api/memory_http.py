"""Local read-only history/detail and canonical text RAG transport."""
import asyncio
import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException

from kulai_memory.application.history import MemoryDetail, MemoryHistoryCursor, MemoryHistoryPage
from kulai_memory.application.rag import MemoryRagError
from kulai_memory.server.runtime import ServerRuntimeError
from .assistant_contract import AssistantRequest, AssistantResponse
from .cancellation import ClientDisconnected, until_disconnect
from .server_access import server_runtime

logger = logging.getLogger(__name__)


class _SafeRoute(APIRoute):
    invalid_message = "Invalid assistant request."
    operation_message = "Memory assistant could not complete the operation."
    operation_stage = "http_operation"

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={"detail": self.invalid_message})
            except ClientDisconnected:
                raise asyncio.CancelledError() from None
            except HTTPException as exc:
                # FastAPI's body parser raises the base Starlette exception,
                # not necessarily the FastAPI subclass. Never echo its detail.
                messages = {400: self.invalid_message, 422: self.invalid_message,
                            404: "Memory not found.", 503: "Memory assistant is unavailable."}
                if exc.status_code in (400, 422):
                    logger.warning("assistant request rejected stage=http_request_validation error_type=%s code=http.invalid_request",
                                   type(exc).__name__)
                return JSONResponse(status_code=exc.status_code, content={"detail": messages.get(
                    exc.status_code, self.operation_message)})
            except ServerRuntimeError:
                return JSONResponse(status_code=503, content={"detail": "Memory assistant is unavailable."})
            except Exception as exc:
                codes = {"rag.retrieval_failed", "rag.generation_failed", "rag.invalid_citations"}
                code = exc.code if isinstance(exc, MemoryRagError) and exc.code in codes else "none"
                stage = {"rag.retrieval_failed": "rag_retrieval",
                         "rag.generation_failed": "rag_generation",
                         "rag.invalid_citations": "rag_citations"}.get(code, self.operation_stage)
                logger.warning("assistant request failed stage=%s error_type=%s code=%s",
                               stage, type(exc).__name__, code)
                return JSONResponse(status_code=500, content={
                    "detail": self.operation_message})

        return safe


def create_memory_http_router() -> APIRouter:
    router = APIRouter(route_class=_SafeRoute)

    @router.get("/api/memories", response_model=MemoryHistoryPage)
    async def history(request: Request, archived: bool = False, limit: int = Query(50, ge=1, le=100),
                      before_created_at: datetime | None = None, before_id: UUID | None = None):
        if (before_created_at is None) != (before_id is None) or (
            before_created_at is not None and before_created_at.utcoffset() is None
        ):
            raise HTTPException(422, "Invalid assistant request.")
        cursor = MemoryHistoryCursor(before_created_at, before_id) if before_id is not None else None
        return await server_runtime(request.app).list_memories(archived=archived, limit=limit, cursor=cursor)

    @router.get("/api/memories/{memory_id}", response_model=MemoryDetail)
    async def detail(request: Request, memory_id: UUID):
        result = await server_runtime(request.app).get_memory(memory_id=memory_id)
        if result is None:
            raise HTTPException(404, "Memory not found.")
        return result

    @router.post("/api/assistant/ask", response_model=AssistantResponse)
    async def ask(request: Request, body: AssistantRequest):
        runtime = server_runtime(request.app)
        result = await until_disconnect(runtime.ask_memory(query=body.query, top_k=body.top_k), request.receive)
        return AssistantResponse.from_result(result)

    return router
