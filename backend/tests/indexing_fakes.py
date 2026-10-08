"""In-memory indexer for isolated transport/controller tests."""

from kulai_memory.application.indexing import (
    EnsureMemoryIndexedResult, IndexReconciliationReport, MemoryIndexingState,
)


class FakeRuntimeIndexer:
    def __init__(self):
        self.calls = []
        self.closed = 0
        self.reconciliations = 0
        self.error = None
        self.report = IndexReconciliationReport()

    async def ensure(self, *, memory):
        self.calls.append(memory)
        if self.error is not None:
            raise self.error
        return EnsureMemoryIndexedResult(memory.id, MemoryIndexingState.INDEXED)

    async def reconcile(self, *, limit=100):
        self.reconciliations += 1
        return self.report

    async def aclose(self):
        self.closed += 1
