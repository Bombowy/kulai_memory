"""Await model work while observing transport disconnects without polling."""
import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


class ClientDisconnected(Exception):
    """The transport ended; callers must not send further events."""


async def until_disconnect(operation: Awaitable[Any], receive: Callable[[], Awaitable[dict]],
                           *, protocol_error: type[Exception] | None = None):
    async def watch():
        while True:
            try:
                message = await receive()
            except Exception as exc:
                logger.warning("assistant watcher failed stage=disconnect_watch error_type=%s", type(exc).__name__)
                raise
            if message["type"] in ("http.disconnect", "websocket.disconnect"):
                return
            if protocol_error is not None:
                raise protocol_error()

    work = asyncio.create_task(operation)
    watcher = asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            watcher.result()
            raise ClientDisconnected()
        return work.result()
    finally:
        watcher.cancel()
        work.cancel()
        # A cancelled Whisper task drains inference before releasing its WAV.
        await asyncio.gather(work, watcher, return_exceptions=True)
