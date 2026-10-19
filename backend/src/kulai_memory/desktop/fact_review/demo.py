"""Known synthetic annotations, real owned PostgreSQL, real human decision service.

No Ollama, networking, production mode, external DB selector or test imports.
"""

import asyncio
import json
from argparse import Namespace
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from kulai_embeddings import EmbeddingCapabilities, EmbeddingResponse, EmbeddingVector
from kulai_llm import LLMCapabilities, LLMResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ...application import Memory
from ...application.fact_memory import FactMemoryError, StageBatch, StageSource
from ...application.intelligence import MemoryIntelligenceService
from ...application.intelligence.prompts import EXTRACT_PROMPT, VERIFY_PROMPT, RELATE_PROMPT
from ...database_safety import (
    SAFE_LOCAL_APP_ENVS, create_owned_temporary_database, database_config,
    database_config_for_database, database_host_is_loopback, drop_owned_temporary_database,
    postgres_connection, require_owned_database, validate_owned_database_name,
)
from ...fact_persistence.migration import upgrade_owned_facts
from ...fact_persistence.owned import open_owned_fact_memory
from ...persistence import PostgresMemoryRepository
from ...settings import get_settings
from .backend import FactReviewBackend


DEMO_TEXTS = (
    "Backend projektu X wykorzystuje Flask.",
    "Od października backend projektu X wykorzystuje FastAPI zamiast Flask.",
    "W przyszłym roku planuję dodać MCP.",
    "Preferuję pracę z Pythonem.",
)


def annotations():
    rows = []
    for index, content in enumerate(DEMO_TEXTS):
        rows.append(dict(text=content, information_type=("fact", "fact", "plan", "preference")[index],
            subject="backend" if index < 2 else "autor", subject_kind="project" if index < 2 else "person",
            subject_is_author=index >= 2, project_context="X" if index < 2 else None,
            technologies=(["Flask"], ["FastAPI", "Flask"], ["MCP"], ["Python"])[index],
            polarity="positive", temporal_context=(None, "Od października", "W przyszłym roku", None)[index],
            evidence_quotes=[content]))
    return rows


class SyntheticLLM:
    """Controlled, fail-closed corpus provider, explicitly labelled in the demo."""
    provider_id = "ollama"
    capabilities = LLMCapabilities(supports_system_messages=True, supports_json_schema=True,
        supports_temperature=True, supports_max_output_tokens=True)

    def __init__(self):
        self.closed = False

    async def generate(self, request):
        payload = json.loads(request.messages[1].content)
        prompt = request.messages[0].content
        expected = dict(zip(DEMO_TEXTS, annotations()))
        if prompt == EXTRACT_PROMPT:
            content = payload["source"]["text"]
            if content not in expected:
                raise FactMemoryError("semantic_rejected")
            output = {"candidates": [expected[content]]}
        elif prompt == VERIFY_PROMPT:
            draft = expected.get(payload["source"]["text"])
            output = {"verdicts": [dict(candidate_id=c["candidate_id"],
                verdict="supported" if draft == c["fact"] else "unsupported",
                reason="supported" if draft == c["fact"] else "not_entailed") for c in payload["candidates"]]}
        else:
            if prompt != RELATE_PROMPT:
                raise FactMemoryError("semantic_rejected")
            verdicts = []
            for pair in payload["pairs"]:
                left, right = pair["left"], pair["right"]
                switch = next((c for c in (left, right) if c["fact"]["text"] == DEMO_TEXTS[1]), None)
                is_switch = {left["fact"]["text"], right["fact"]["text"]} == set(DEMO_TEXTS[:2])
                verdicts.append(dict(pair_id=pair["pair_id"], relation="possible_supersession" if is_switch else "unrelated",
                    rationale="Syntetyczna adnotacja: jawna zmiana technologii od października." if is_switch else "Brak relacji w adnotacji demo.",
                    replacement_candidate_id=switch["candidate_id"] if is_switch else None))
            output = {"verdicts": verdicts}
        return LLMResponse(text=json.dumps(output), provider_id="ollama", model_id="qwen3.5:9b")

    async def aclose(self):
        self.closed = True


class SyntheticBGE:
    provider_id = "ollama"
    capabilities = EmbeddingCapabilities(supports_batch=True)

    def __init__(self):
        self.closed = False

    async def embed(self, request):
        return EmbeddingResponse(provider_id="ollama", model_id="bge-m3:567m-fp16", dimension=1024,
            embeddings=tuple(EmbeddingVector(values=(1.0,) + (0.0,) * 1023) for _ in request.inputs))

    async def aclose(self):
        self.closed = True


async def install_demo_canonical(owned, *, config):
    """Run the EXISTING graph on a verified borrowed owned connection, never env URLs."""
    await require_owned_database(owned, config=config)
    validate_owned_database_name(owned.name, source_database=postgres_connection(config).database, kind=owned.kind)
    target = database_config_for_database(owned.name, config=config)
    engine = create_async_engine(target.async_url, hide_parameters=True, echo=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL lock_timeout='5s'"))
            actual, marker = (await connection.execute(text("SELECT current_database(),shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()"))).one()
            if actual != owned.name or marker != owned.marker:
                raise FactMemoryError("owned_database_required")
            if await connection.scalar(text("SELECT to_regclass('public.memories') IS NOT NULL")):
                raise FactMemoryError("invalid_input")
            def migrate(sync):
                cfg = Config(str(Path(__file__).resolve().parents[4] / "alembic.ini"))
                cfg.cmd_opts = Namespace(x=["kulai_vector_dimension=1024"])
                script = ScriptDirectory.from_config(cfg)
                if script.get_heads() != ["kulai_memory_0004"]:
                    raise FactMemoryError("schema_required")
                with EnvironmentContext(cfg, script, fn=lambda rev, ctx: script._upgrade_revs("kulai_memory_0004", rev)) as env:
                    env.configure(connection=sync, transactional_ddl=True)
                    env.run_migrations()
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


async def seed_demo(local, factory, intelligence):
    async with factory() as session:
        async with session.begin():
            memories = [await PostgresMemoryRepository(db=session).create(Memory(content=content)) for content in DEMO_TEXTS]
    docs = tuple([await local.storage.memory_document(m.id, speaker_id="synthetic-demo-author") for m in memories])
    preview = await intelligence.preview(docs[0], docs[1:], top_k=5)
    await local.service.stage(StageBatch(sources=tuple(StageSource(source=source, extraction=extraction)
        for source, extraction in zip(docs, preview.extractions)), relations=preview.relations))


@asynccontextmanager
async def demo_backend():
    settings, config = get_settings(), database_config()
    if settings.app_env.lower() not in SAFE_LOCAL_APP_ENVS or not database_host_is_loopback(config):
        raise FactMemoryError("owned_database_required")
    # Provisioning is allowed to finish even when close is requested: the
    # worker then exits this context and drops exactly this capability's DB.
    owned = await create_owned_temporary_database(kind="backup", config=config)
    try:
        await install_demo_canonical(owned, config=config)
        await upgrade_owned_facts(owned, config=config)
        target = database_config_for_database(owned.name, config=config)
        async with AsyncExitStack() as stack:
            llm, bge = SyntheticLLM(), SyntheticBGE()
            stack.push_async_callback(llm.aclose)
            stack.push_async_callback(bge.aclose)
            intelligence = MemoryIntelligenceService(llm_provider=llm, embedding_provider=bge)
            engine = create_async_engine(target.async_url, hide_parameters=True, echo=False)
            stack.push_async_callback(engine.dispose)
            local = await stack.enter_async_context(open_owned_fact_memory(owned, config=config, intelligence=intelligence))
            await seed_demo(local, async_sessionmaker(engine, expire_on_commit=False), intelligence)
            yield FactReviewBackend(local, database_name=owned.name)
    finally:
        # Never find/drop by a prefix; exact generated name AND marker required.
        await drop_owned_temporary_database(owned, config=config)
