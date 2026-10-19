"""Extend the existing single-operation worker, including read cancellation."""

import asyncio
import threading

from PySide6.QtCore import Signal

from ..fact_review.worker import FactReviewThread
from ..fact_review.models import safe_message


class WorkspaceThread(FactReviewThread):
    operations=FactReviewThread.operations | {'ask','sources','check_display','open_citation','handles','mutate'}
    reads=frozenset({'ask','sources','check_display','open_citation'})
    progress=Signal(int,str)
    denied=Signal(int,str)

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self._cancel_read=threading.Event()

    def submit(self,token,operation,value):
        if not self._busy: self._cancel_read.clear()
        return super().submit(token,operation,value)

    async def _call(self,backend,operation,value,token):
        backend.notify=lambda stage:self.progress.emit(token,stage)
        if operation in self.reads and self._cancel_read.is_set(): raise asyncio.CancelledError
        try:
            return await super()._call(backend,operation,value,token)
        except Exception as error:
            code=getattr(error,'code','operation_failed')
            self.denied.emit(token,code if code.startswith(('facts.','fact_rag.')) else 'operation_failed')
            raise

    @staticmethod
    def error_message(error):
        code=getattr(error,'code','')
        return {
            'fact_rag.invalid_citations':'Model podał nieprawidłowe cytowania. Odpowiedź zablokowano. Możesz zadać pytanie ponownie lub otworzyć zatwierdzone źródła.',
            'fact_rag.ungrounded_answer':'Nie potwierdzono zgodności odpowiedzi ze źródłami. Draft został zablokowany. Możesz zadać nowe pytanie lub obejrzeć źródła.',
            'fact_rag.source_invalid':'Źródło lub zgoda zmieniły się. Odpowiedź i cytowania wyczyszczono. Zadaj nowe pytanie.',
            'fact_rag.generation_failed':'Model jest niedostępny lub nie zwrócił poprawnego wyniku. Możesz obejrzeć zatwierdzone źródła.',
            'fact_rag.embedding_failed':'Nie udało się wykonać wyszukiwania BGE. Możesz obejrzeć zatwierdzone źródła.',
        }.get(code,safe_message(error))

    def cancel_read(self,token,operation):
        if operation not in self.reads: return
        self._cancel_read.set()
        if self._loop is not None and not self._loop.is_closed():
            def cancel():
                task=self._active
                if task is not None and task.get_name()==f'{token}:{operation}': task.cancel()
            self._loop.call_soon_threadsafe(cancel)
