"""Synthetic marker-owned fact fixtures; never migrate or write the configured main."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

from backend.tests.security_postgres_fixtures import owned_store
from backend.tests.test_memory_intelligence import ConstantBGE, GoldenLLM
from kulai_memory.application import Memory
from kulai_memory.application.fact_memory import StageBatch, StageSource
from kulai_memory.application.intelligence import MemoryIntelligenceService
from kulai_memory.fact_persistence.migration import upgrade_owned_facts
from kulai_memory.fact_persistence.owned import open_owned_fact_memory
from kulai_memory.persistence import PostgresMemoryRepository


@asynccontextmanager
async def fact_store(*, security=False, install=True):
    async with owned_store(security=security) as (owned, config, target, factory, _):
        if install:
            await upgrade_owned_facts(owned, config=config)
        llm, bge = GoldenLLM(), ConstantBGE()
        intelligence = MemoryIntelligenceService(llm_provider=llm, embedding_provider=bge)
        async with open_owned_fact_memory(owned, config=config, intelligence=intelligence) as local:
            yield SimpleNamespace(owned=owned, config=config, target=target, factory=factory,
                                  local=local, service=local.service, storage=local.storage,
                                  principal=local.principal, intelligence=intelligence, llm=llm, bge=bge)


async def create_memory(state, content):
    memory = Memory(content=content)
    async with state.factory() as session:
        async with session.begin():
            await PostgresMemoryRepository(db=session).create(memory)
    return memory


async def analyze_memory(state, memory_id):
    source = await state.storage.memory_document(memory_id)
    extraction = await state.intelligence.extract(source)
    return StageSource(source=source, extraction=extraction)


async def staged_memory(state, content):
    memory = await create_memory(state, content)
    item = await analyze_memory(state, memory.id)
    result = await state.service.stage(StageBatch(sources=(item,)))
    return memory, item, result
