"""One guarded database, one approval service, one Fact-RAG; no public API."""

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker
from kulai_vector_store_pgvector import PgVectorStore, PgVectorStoreConfig

from ...application import MemoryLifecycleService
from ...application.approved_facts import ApprovedFactsService, FactRagError
from ...application.approved_facts.models import ApprovedFact, FactAnswer, FactReference, FactRelation
from ...application.approved_facts.service import VERIFY_POLICY
from ...application.fact_memory import FactMemoryError
from ...application.fact_memory.models import ReviewSnapshot
from ...deletion_persistence import delete_memory
from ...embedding_provider import create_embedding_provider
from ...fact_persistence.retrieval import OwnedApprovedFactsReader, SELECT, _fact
from ...fact_rag_demo import synthetic_session, SyntheticBGE, SyntheticAnswerLLM
from ...llm_provider import create_llm_provider
from ...persistence import PostgresMemoryRepository
from ...settings import get_settings
from ..fact_review.backend import FactReviewBackend


@dataclass(frozen=True)
class Display:
    id: UUID
    facts: tuple[ApprovedFact, ...] = field(repr=False)
    answer: FactAnswer | None = field(default=None, repr=False)
    partial: bool = False
    checked_at: float = field(default_factory=monotonic)


@dataclass(frozen=True)
class Citation:
    snapshot: ReviewSnapshot = field(repr=False)
    reference: FactReference
    checked_at: float = field(default_factory=monotonic)


@dataclass(frozen=True)
class SourceHandle:
    label: str
    memory_id: UUID
    revision: int | None
    token: str
    archived: bool
    deleted: bool


@dataclass(frozen=True)
class Mutation:
    source: SourceHandle
    action: str


class ProgressProvider:
    """Borrow a provider; report constant stages, never prompt/query content."""
    def __init__(self, provider, owner):
        self.provider, self.owner = provider, owner
        self.provider_id, self.capabilities = provider.provider_id, provider.capabilities

    async def embed(self, request):
        self.owner.notify(("BGE" if self.owner.real_models else "CONTROLLED")+": wyszukiwanie w ograniczonej kolekcji…")
        return await self.provider.embed(request)

    async def generate(self, request):
        prefix="Qwen" if self.owner.real_models else "CONTROLLED"
        self.owner.notify(prefix+(": weryfikacja grounding…" if request.messages[0].content == VERIFY_POLICY
                                 else ": generowanie odpowiedzi…"))
        return await self.provider.generate(request)


class WorkspaceBackend(FactReviewBackend):
    def __init__(self, local, *, database_name, source_ids, bge, llm, real_models=False):
        super().__init__(local, database_name=database_name)
        self.source_ids = tuple(source_ids)
        self.real_models = real_models
        self.notify = lambda stage: None
        self.reader = OwnedApprovedFactsReader(local.storage)
        self.rag = ApprovedFactsService(reader=self.reader,
            embedding_provider=ProgressProvider(bge,self), llm_provider=ProgressProvider(llm,self))
        self._stamp = None  # Only references/versions; no cached answer, text or evidence.

    def _display(self, facts, answer=None, partial=False):
        id = uuid4()
        ids = {f.reference.candidate_id for f in facts}
        relations = tuple(r for r in answer.relations if {r.left_id,r.right_id} <= ids) if answer else ()
        self._stamp = (id, tuple((f.reference,f.lock_version) for f in facts), relations)
        return Display(id, facts, answer, partial)

    async def ask(self, query):
        self._stamp = None
        self.notify("Wyszukiwanie zatwierdzonych faktów…")
        answer = await self.rag.ask(query)
        return self._display(answer.citations, answer, answer.partial_coverage)

    async def sources(self, unused):
        self._stamp = None
        collection = await self.reader.read(limit=20)
        return self._display(collection.facts, partial=collection.more_candidates or collection.more_relations)

    async def _checked(self, id, candidate=None):
        try:
            if self._stamp is None or id != self._stamp[0]:
                raise FactRagError("source_invalid")
            _, expected, relations = self._stamp
            ids = [ref.candidate_id for ref, _ in expected]
            if candidate is not None and candidate not in ids:
                raise FactRagError("invalid_citations")
            async with self._local.storage._tx(read_only=True) as repo:
                rows = await repo.rows(SELECT + " AND c.id=ANY(CAST(:ids AS uuid[])) ORDER BY c.id LIMIT 20", {"ids":ids})
                relation_rows = await self.reader._relations(repo, ids, [r.id for r in relations])
                snapshot = await repo.snapshot(candidate) if candidate is not None else None
            facts = {_f.reference.candidate_id:_f for _f in map(_fact,rows)}
            if len(facts)!=len(expected) or any(
                facts.get(ref.candidate_id) is None or facts[ref.candidate_id].reference!=ref
                or facts[ref.candidate_id].lock_version!=version for ref,version in expected):
                raise FactRagError("source_invalid")
            current = {r.id:r for r in (FactRelation(**dict(row)) for row in relation_rows)}
            if len(current)!=len(relations) or any(current.get(r.id)!=r for r in relations):
                raise FactRagError("source_invalid")
            return snapshot
        except BaseException:
            self._stamp = None
            raise

    async def check_display(self, id):
        await self._checked(id)
        return id, monotonic()

    async def open_citation(self, value):
        id, candidate = value
        snapshot = await self._checked(id,candidate)
        reference = next(ref for ref,_ in self._stamp[1] if ref.candidate_id==candidate)
        return Citation(snapshot,reference)

    async def handles(self, unused):
        async with self._local.storage._tx(read_only=True) as repo:
            rows = await repo.rows("""SELECT e.memory_id,m.revision,m.archived_at,e.deleted_at,
                'epoch:'||e.incarnation::text||':'||e.generation::text AS token
                FROM kulai_fact_memory_epochs e LEFT JOIN memories m ON m.id=e.memory_id
                WHERE e.memory_id=ANY(CAST(:ids AS uuid[]))""", {"ids":list(self.source_ids)})
        by_id={r['memory_id']:r for r in rows}
        return tuple(SourceHandle(chr(65+i),id,by_id[id]['revision'],by_id[id]['token'],
            by_id[id]['archived_at'] is not None,by_id[id]['deleted_at'] is not None)
            for i,id in enumerate(self.source_ids))

    async def mutate(self, value):
        if (not isinstance(value,Mutation) or value.source.memory_id not in self.source_ids
            or value.action not in {'edit','archive','restore','delete'}):
            raise FactMemoryError('forbidden')
        source=value.source
        self._stamp=None
        async with self._local.storage._tx() as repo:
            current=await repo.memory(source.memory_id,lock=True)
            if current is None or current['token']!=source.token or current['revision']!=source.revision:
                raise FactMemoryError('source_invalid')
            # Reuse canonical services on THIS verified connection/savepoint.
            factory=async_sessionmaker(repo.db,expire_on_commit=False,join_transaction_mode='create_savepoint')
            if value.action=='delete':
                await delete_memory(memory_id=source.memory_id,session_factory=factory,expected_revision=source.revision)
            else:
                async with factory() as session:
                    async with session.begin():
                        lifecycle=MemoryLifecycleService(repository=PostgresMemoryRepository(db=session),
                            store=PgVectorStore(db=session,config=PgVectorStoreConfig(dimension=1024)))
                        if value.action=='edit':
                            await lifecycle.edit(memory_id=source.memory_id,expected_revision=source.revision,
                                content=current['content']+'\nTEST DATA: źródło zostało edytowane.')
                        elif value.action=='archive': await lifecycle.archive(memory_id=source.memory_id)
                        else: await lifecycle.restore(memory_id=source.memory_id)
        return await self.handles(None)


@asynccontextmanager
async def workspace_backend(*, real_models=False):
    async with synthetic_session() as (owned,local), AsyncExitStack() as stack:
        bge,llm=SyntheticBGE(),SyntheticAnswerLLM()
        stack.push_async_callback(bge.aclose)
        if real_models:
            settings=get_settings()
            if settings.kulai_embedding_model!='bge-m3:567m-fp16' or settings.kulai_llm_model!='qwen3.5:9b':
                raise FactRagError('invalid_input')
            bge=await stack.enter_async_context(create_embedding_provider(settings=settings))
            llm=await stack.enter_async_context(create_llm_provider(settings=settings))
        async with local.storage._tx(read_only=True) as repo:
            rows=await repo.rows('SELECT id FROM memories ORDER BY created_at,id LIMIT 5')
        if len(rows)!=4: raise FactMemoryError('invalid_input')
        yield WorkspaceBackend(local,database_name=owned.name,source_ids=tuple(r['id'] for r in rows),bge=bge,llm=llm,real_models=real_models)
