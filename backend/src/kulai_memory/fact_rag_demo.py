"""Synthetic owned-only local demo. No auto approval, production mode or HTTP."""

import asyncio
import json
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import UUID, uuid4

from kulai_llm import LLMCapabilities, LLMResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .application.approved_facts import ApprovedFactsService, FactRagError
from .application.approved_facts.service import POLICY, VERIFY_POLICY
from .application.fact_memory import FactMemoryError
from .application.intelligence import MemoryIntelligenceService
from .database_safety import (SAFE_LOCAL_APP_ENVS, create_owned_temporary_database, database_config,
    database_config_for_database, database_host_is_loopback, drop_owned_temporary_database)
from .desktop.fact_review.demo import (SyntheticLLM, SyntheticBGE, install_demo_canonical, seed_demo)
from .embedding_provider import create_embedding_provider
from .fact_persistence.migration import upgrade_owned_facts
from .fact_persistence.owned import open_owned_fact_memory
from .fact_persistence.retrieval import OwnedApprovedFactsReader
from .llm_provider import create_llm_provider
from .settings import get_settings


class SyntheticAnswerLLM:
    """Fixed corpus answers for contract/demo tests; never presented as real Qwen."""
    provider_id = "ollama"
    capabilities = LLMCapabilities(supports_system_messages=True, supports_json_schema=True,
        supports_temperature=True, supports_max_output_tokens=True)

    async def generate(self, request):
        payload = json.loads(request.messages[1].content)
        if request.messages[0].content == VERIFY_POLICY:
            output = {"supported": True}  # An explicit controlled test oracle, not semantic verification.
        elif request.messages[0].content == POLICY:
            query = payload["query"].casefold()
            facts = payload["context"]["facts"]
            selected = []
            answer = ""
            if "mcp" in query:
                selected = [f for f in facts if f["information_type"] == "plan" and "mcp" in f["text"].casefold()]
                answer = "Potwierdzony jest tylko plan dodania MCP w przyszłym roku. Wdrożenie nie jest potwierdzone."
            elif "prefer" in query:
                selected = [f for f in facts if f["information_type"] == "preference"]
                answer = "Autor preferuje pracę z Pythonem."
            elif any(word in query for word in ("backend", "wcześniej", "kiedy", "flask", "potwierdzone")):
                backend = [f for f in facts if f["project_context"] == "X" and f["information_type"] == "fact"]
                switched = any("zamiast" in f["text"].casefold() for f in backend)
                if switched and len(backend) == 2:
                    selected = backend
                    if any(word in query for word in ("całkowicie", "entirely")):
                        answer = "Źródła potwierdzają zastąpienie Flask przez FastAPI w backendzie X, ale nie całkowite usunięcie Flask."
                    elif any(word in query for word in ("kiedy", "when")):
                        answer = "Zmianę opisano jako obowiązującą od października. Nie podano roku."
                    elif any(word in query for word in ("wcześniej", "before")):
                        answer = "Backend X wcześniej wykorzystywał Flask; drugie źródło opisuje przejście na FastAPI."
                    elif "potwierdzone" in query:
                        answer = "Źródła opisują Flask oraz przejście na FastAPI od października. possible_supersession pozostaje sugestią relacji."
                    else:
                        answer = "Według nowszego kontekstu backend X od października wykorzystuje FastAPI zamiast Flask. Starsze źródło wskazuje Flask; roku nie podano."
                elif len(backend) == 1 and "zamiast" not in backend[0]["text"].casefold():
                    selected = backend
                    answer = "Backend projektu X wykorzystuje Flask."
            output = {"answer": answer or "Brak informacji.", "sufficient_context": bool(selected),
                      "citations": [f["citation_id"] for f in selected]}
        else:
            raise FactRagError("generation_failed")
        return LLMResponse(text=json.dumps(output), provider_id="ollama", model_id="qwen3.5:9b")


@asynccontextmanager
async def synthetic_session():
    """Always provision our own fresh marker; never accept a user DB selector."""
    settings, config = get_settings(), database_config()
    if settings.app_env.lower() not in SAFE_LOCAL_APP_ENVS or not database_host_is_loopback(config):
        raise FactMemoryError("owned_database_required")
    provisioning = asyncio.create_task(create_owned_temporary_database(kind="backup", config=config))
    try:
        owned = await asyncio.shield(provisioning)
    except asyncio.CancelledError:
        # createdb runs in a worker thread. Let marker installation finish so
        # cleanup has a verified capability; never discover/drop by a prefix.
        owned = await provisioning
        await drop_owned_temporary_database(owned, config=config)
        raise
    try:
        await install_demo_canonical(owned, config=config)
        await upgrade_owned_facts(owned, config=config)
        target = database_config_for_database(owned.name, config=config)
        async with AsyncExitStack() as stack:
            llm, bge = SyntheticLLM(), SyntheticBGE()
            stack.push_async_callback(llm.aclose)
            stack.push_async_callback(bge.aclose)
            intelligence = MemoryIntelligenceService(llm_provider=llm, embedding_provider=bge)
            engine = create_async_engine(target.async_url, echo=False, hide_parameters=True)
            stack.push_async_callback(engine.dispose)
            local = await stack.enter_async_context(open_owned_fact_memory(owned, config=config, intelligence=intelligence))
            await seed_demo(local, async_sessionmaker(engine, expire_on_commit=False), intelligence)
            yield owned, local
    finally:
        await drop_owned_temporary_database(owned, config=config)


async def console(*, real_models=False, input_fn=input, output=print):
    """Review commands are explicit human decisions, ASK is strictly read-only."""
    async with synthetic_session() as (owned, local), AsyncExitStack() as stack:
        bge, llm = SyntheticBGE(), SyntheticAnswerLLM()
        stack.push_async_callback(bge.aclose)
        if real_models:
            settings = get_settings()
            if settings.kulai_llm_model != "qwen3.5:9b" or settings.kulai_embedding_model != "bge-m3:567m-fp16":
                raise FactRagError("invalid_input")
            bge = await stack.enter_async_context(create_embedding_provider(settings=settings))
            llm = await stack.enter_async_context(create_llm_provider(settings=settings))
        rag = ApprovedFactsService(reader=OwnedApprovedFactsReader(local.storage), embedding_provider=bge, llm_provider=llm)
        output("TEST DATA | " + owned.name + " | facts v2 | ASK READ ONLY")
        output("RAG: " + ("real Qwen/BGE" if real_models else "CONTROLLED providers, no AI quality claim"))
        output("Extraction/approval verification: controlled annotations, genuine FactMemoryService decisions.")
        output("list | relations | approve UUID | reject UUID | revoke UUID | approve-relation UUID | ask QUESTION | quit")
        output("No automatic approvals. Closing drops only this marker-owned demo DB.")
        while True:
            try:
                # Commands are serial, with no transaction/provider operation
                # pending while waiting. A blocking stdin thread would keep
                # asyncio.run's executor alive after Ctrl+C on Windows.
                command = input_fn("fact-rag> ").strip()
            except EOFError:
                break
            if not command: continue
            if command in {"quit", "exit"}: break
            verb, _, argument = command.partition(" ")
            try:
                if verb == "list":
                    async with local.storage._tx(read_only=True) as repo:
                        rows = await repo.rows("SELECT id,status,information_type,payload FROM public.kulai_fact_candidates ORDER BY id LIMIT 100")
                    for r in rows:
                        output(f"{r['id']} {r['status']} {r['information_type']}: {r['payload']['fact']['text']}")
                elif verb == "relations":
                    for status in ("PENDING", "APPROVED"):
                        for r in await local.service.list_relations(status=status):
                            output(f"{r.id} {r.status} {r.relation} {r.left_id} -> {r.right_id}")
                elif verb in {"approve", "reject", "revoke", "approve-relation"}:
                    target = UUID(argument.strip())
                    if verb == "approve-relation":
                        rows = await local.service.list_relations()
                        record = next(r for r in rows if r.id == target)
                        method = local.service.approve_relation
                    else:
                        record = await local.service.get_candidate(target)
                        if record is None: raise ValueError
                        method = {"approve": local.service.approve, "reject": local.service.reject,
                                  "revoke": local.service.revoke_approval}[verb]
                    request_id = uuid4()
                    output("Decision request: " + str(request_id))
                    try:
                        receipt = await method(target, principal=local.principal, request_id=request_id,
                                               expected_version=record.lock_version)
                    except FactMemoryError as exc:
                        if exc.code == "facts.operation_failed":
                            output("Uncertain decision. No automatic retry; stopping this demo safely.")
                            break
                        raise
                    output("Recorded: " + receipt.action + " request=" + str(receipt.request_id))
                elif verb == "ask":
                    result = await rag.ask(argument)
                    output(result.answer)
                    output("ASK READ ONLY | partial=" + str(result.partial_coverage) + " " + ",".join(result.partial_reasons))
                    for f in result.citations:
                        output(json.dumps(f.model_dump(mode="json"), ensure_ascii=False))
                else:
                    output("Unknown command.")
            except (FactMemoryError, FactRagError) as exc:
                output(exc.code)
            except (ValueError, StopIteration):
                output("Invalid command or unknown target.")
    return 0
