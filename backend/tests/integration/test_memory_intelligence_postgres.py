"""All fixture writes are confined to marker-owned PostgreSQL; preview itself is read-only."""

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import text

from backend.tests.integration.test_memory_postgres import _owned_migrated_session_factory, _require_opt_in
from backend.tests.integration.test_memory_retrieval_postgres import _observe_reads, _seed_memories
from backend.tests.test_memory_intelligence import CASES, ConstantBGE, GoldenLLM
from kulai_memory import memory_intelligence_runtime as host
from kulai_memory.application import Memory, MemoryIndexingService
from kulai_memory.application.intelligence import IntelligenceError, SourceDocument
from kulai_memory.database_safety import database_exists, database_snapshot_url
from kulai_memory.indexing_persistence import index_memory
from kulai_memory.settings import Settings


@pytest.mark.parametrize("failure", [None, "stale_vector", "generation"])
def test_owned_preview_preserves_all_fingerprints_and_releases_sessions(monkeypatch, failure):
    _require_opt_in()

    async def scenario():
        async with _owned_migrated_session_factory() as factory:
            engine = factory.kw["bind"]
            owned_name = engine.url.database
            url = engine.url.render_as_string(hide_password=False)
            llm, bge = GoldenLLM(relation="possible_supersession", replacement_side="left"), ConstantBGE()
            memories = (
                Memory(id=UUID(int=501), content=CASES["pl_flask"]["text"]),
                Memory(id=UUID(int=502), content=CASES["pl_other"]["text"]),
            )
            await _seed_memories(factory, memories)
            indexing = MemoryIndexingService(provider=bge, expected_dimension=1024)
            for memory in memories:
                await index_memory(memory=memory, service=indexing, session_factory=factory)
            if failure == "stale_vector":
                # A controlled fixture mutation on the owned DB only, before the read-only measurement.
                async with factory() as session:
                    await session.execute(text("UPDATE memories SET revision=2 WHERE id=:id"), {"id": memories[0].id})
                    await session.commit()
            if failure == "generation":
                def mutate(prompt, output):
                    from kulai_memory.application.intelligence.prompts import RELATE_PROMPT
                    return "invalid synthetic output" if prompt == RELATE_PROMPT else output
                llm.mutate = mutate
            original_generate, original_embed = llm.generate, bge.embed
            async def generate(request):
                assert engine.pool.checkedout() == 0
                return await original_generate(request)
            async def embed(request):
                assert engine.pool.checkedout() == 0
                return await original_embed(request)
            llm.generate, bge.embed = generate, embed
            monkeypatch.setattr(host, "create_llm_provider", lambda **kwargs: llm)
            monkeypatch.setattr(host, "create_embedding_provider", lambda **kwargs: bge)
            before = await database_snapshot_url(url)
            statements, commits, remove = _observe_reads(engine)
            try:
                async with host.MemoryIntelligenceRuntime(
                    settings=Settings(_env_file=None, kulai_vector_dimension=1024), session_factory=factory,
                ) as runtime:
                    document = SourceDocument(source_id="new", text=CASES["pl_switch"]["text"])
                    if failure:
                        with pytest.raises(IntelligenceError):
                            await runtime.preview(document, compare_main=True)
                    else:
                        result = await runtime.preview(document, compare_main=True)
                        assert result.comparison_scope == "local_and_indexed_main" and not result.exhaustive
                        assert any(r.relation == "possible_supersession" for r in result.relations)
                        assert next(r for r in result.relations if r.relation == "possible_supersession").replacement_candidate_id == "new:1"
                        assert any(r.relation == "unrelated" for r in result.relations)
                        origins = [c.provenance for e in result.extractions[1:] for c in e.candidates]
                        assert {p.memory_id for p in origins} == {m.id for m in memories}
                        assert all(p.revision == 1 for p in origins)
            finally:
                remove()
            assert commits == [] and set(statements) <= {"SELECT", "SET", "SHOW"}
            assert "SELECT" in statements and "SET" in statements
            assert await database_snapshot_url(url) == before
            assert engine.pool.checkedout() == 0 and llm.closed == bge.closed == 1
            print(f"owned_preview={failure or 'success'};fingerprints_unchanged=true;writes=0;clients_closed=true")
        assert not await database_exists(owned_name)
    asyncio.run(asyncio.wait_for(scenario(), timeout=120))
