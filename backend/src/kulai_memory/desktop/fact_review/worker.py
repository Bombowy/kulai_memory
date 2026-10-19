"""One QThread, one asyncio loop, one serialized operation and owned lifetime."""

import asyncio
import threading

from PySide6.QtCore import QThread, Signal

from .models import safe_message


class FactReviewThread(QThread):
    operations = frozenset({"read", "decide", "resolve"})
    ready = Signal(str)
    completed = Signal(int, str, object)
    failed = Signal(int, str, str)
    stopped = Signal(bool)

    def __init__(self, backend_factory, parent=None):
        super().__init__(parent)
        self._factory = backend_factory
        self._closing = threading.Event()
        self._mutex = threading.Lock()
        self._busy = False
        self._loop = self._queue = self._active = None
        self._ready = False
        self.clean_shutdown = False

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
            self.clean_shutdown = True
        except Exception as error:
            self.failed.emit(-1, "startup_or_cleanup", self.error_message(error))
        finally:
            self._ready = False
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
            self._queue = self._active = self._loop = None
            loop.close()
            asyncio.set_event_loop(None)
            self.stopped.emit(self.clean_shutdown)

    async def _serve(self):
        self._queue = asyncio.Queue(maxsize=1)
        async with self._factory() as backend:
            if self._closing.is_set():
                return
            self._ready = True
            self.ready.emit(backend.database_name)
            while not self._closing.is_set():
                command = await self._queue.get()
                if command is None:
                    break
                token, operation, value = command
                result = error_message = None
                try:
                    self._active = asyncio.create_task(self._call(backend, operation, value, token), name=f"{token}:{operation}")
                    result = await self._active
                except asyncio.CancelledError:
                    error_message = "Operacja anulowana. Odśwież widok."
                except Exception as error:
                    error_message = self.error_message(error)
                finally:
                    self._active = None
                    with self._mutex:
                        self._busy = False
                if not self._closing.is_set():
                    if error_message is None:
                        self.completed.emit(token, operation, result)
                    else:
                        self.failed.emit(token, operation, error_message)
                result = error_message = value = command = None

    def submit(self, token, operation, value):
        if operation not in self.operations:
            return False
        with self._mutex:
            if self._closing.is_set() or not self._ready or self._busy:
                return False
            self._busy = True
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (token, operation, value))
        return True

    @staticmethod
    def error_message(error):
        return safe_message(error)

    async def _call(self, backend, operation, value, token):
        return await getattr(backend, operation)(value)

    def request_shutdown(self):
        self._closing.set()
        loop = self._loop
        if loop is not None and not loop.is_closed():
            def stop():
                if self._active is not None:
                    self._active.cancel()
                elif self._queue is not None and self._queue.empty():
                    self._queue.put_nowait(None)
            loop.call_soon_threadsafe(stop)
