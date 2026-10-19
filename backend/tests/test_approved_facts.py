"""Fact-RAG contracts without PostgreSQL or GPU."""

import asyncio
from uuid import uuid4

import pytest

from backend.tests.approved_facts_support import AnswerLLM, HookBGE
from kulai_memory.application.approved_facts import ApprovedFactsService, FactRagError, FactReference
from kulai_memory.application.approved_facts.models import ApprovedFact, ApprovedCollection, FactRelation
from kulai_memory.application.approved_facts.service import POLICY, VERIFY_POLICY, INSUFFICIENT
from kulai_memory.application.intelligence.models import EvidenceSpan
from kulai_memory.fact_persistence.retrieval import _fact


def fact():
    text = "Backend projektu X wykorzystuje Flask."
    return ApprovedFact(reference=FactReference(fact_id=uuid4(),fact_version=1,candidate_id=uuid4(),
        memory_id=uuid4(),revision=1,incarnation=uuid4(),generation=1,source_sha256='a'*64),
        lock_version=2,text=text,information_type='fact',subject='backend',subject_kind='project',
        subject_scope='named:backend',project_context='X',temporal_context=None,polarity='positive',
        evidence=(EvidenceSpan(quote=text,start=0,end=len(text)),))


class Reader:
    def __init__(self, facts=(),relations=()):
        self.facts = facts
        self.relations = relations
        self.checks = 0

    async def read(self, *, limit, **kwargs):
        return ApprovedCollection(facts=self.facts[:limit],relations=self.relations,more_candidates=len(self.facts)>limit)

    async def revalidate(self, facts, relations):
        self.checks += 1
        if any(f not in self.facts for f in facts): raise FactRagError('source_invalid')


def build(reader=None, llm=None, **kwargs):
    return ApprovedFactsService(reader=reader or Reader((fact(),)), embedding_provider=HookBGE(),
        llm_provider=llm or AnswerLLM(), **kwargs)


@pytest.mark.parametrize('top_k',[0,21,True,1.0,'5'])
def test_top_k_validation_before_providers(top_k):
    with pytest.raises(FactRagError,match='invalid_input'):
        asyncio.run(build().ask('backend',top_k=top_k))


@pytest.mark.parametrize('query',['', ' ', 'x'*4001, None])
def test_query_validation(query):
    with pytest.raises(FactRagError,match='invalid_input'): asyncio.run(build().ask(query))


def test_empty_collection_never_calls_llm():
    llm = AnswerLLM()
    result = asyncio.run(build(Reader(),llm).ask('backend'))
    assert result.answer == INSUFFICIENT and not result.citations and not llm.calls


@pytest.mark.parametrize('field',['fact_id','candidate_id','memory_id','incarnation','fact_version','revision','generation','source_sha256'])
def test_entire_citation_identity_is_checked(field):
    llm = AnswerLLM()
    original=fact()
    def mutate(prompt,value):
        if prompt == POLICY:
            forged=original.reference.model_dump(mode='json')
            forged[field] = str(uuid4()) if field.endswith('_id') or field=='incarnation' else ('b'*64 if field=='source_sha256' else 99)
            value['citations']=[forged]  # The model cannot supply identity metadata at all.
        return value
    llm.transform = mutate
    with pytest.raises(FactRagError):
        asyncio.run(build(Reader((original,)),llm=llm).ask('backend'))
    assert len(llm.calls)==1  # Never retry a malformed answer.


@pytest.mark.parametrize('damage',['extra','string_version','duplicates','missing','bad_json'])
def test_strict_json_and_citations(damage):
    llm = AnswerLLM()
    def mutate(prompt,value):
        if prompt != POLICY: return value
        if damage=='extra': value['extra']='untrusted'
        if damage=='string_version': value['citations']=[{'citation_id':'F1','revision':'1'}]
        if damage=='duplicates': value['citations']*=2
        if damage=='missing': value['citations']=[]
        return 'not JSON' if damage=='bad_json' else value
    llm.transform=mutate
    with pytest.raises(FactRagError): asyncio.run(build(llm=llm).ask('backend'))
    assert len(llm.calls)==1


def test_verifier_rejects_unsupported_claim_even_with_valid_ids():
    llm=AnswerLLM()
    llm.transform=lambda prompt,value: {'supported':False} if prompt==VERIFY_POLICY else {**value,'answer':'Flask was removed in 2027.'}
    with pytest.raises(FactRagError,match='ungrounded_answer'): asyncio.run(build(llm=llm).ask('backend'))


def test_context_and_candidate_limits_are_explicit():
    reader=Reader(tuple(fact() for _ in range(4)))
    result=asyncio.run(build(reader,candidate_limit=2,context_chars=512).ask('backend',top_k=1))
    assert not result.sufficient_context and result.context_chars<=512
    assert {'candidate_limit','top_k','context_chars'}<=set(result.partial_reasons)


def test_stale_final_context_fails_closed_without_returning_draft():
    reader, llm=Reader((fact(),)),AnswerLLM()
    async def delete(request):
        if request.messages[0].content==POLICY: reader.facts=()
    llm.hook=delete
    with pytest.raises(FactRagError,match='source_invalid'): asyncio.run(build(reader,llm).ask('backend'))


def test_result_repr_and_safe_error_do_not_contain_private_text():
    result=asyncio.run(build().ask('backend'))
    assert 'Flask' not in repr(result) and 'Flask' not in repr(result.citations[0])
    assert str(FactRagError('PRIVATE_SECRET'))=='fact_rag.operation_failed'


def test_invalid_provenance_row_never_escapes_raw_validation_error():
    with pytest.raises(FactRagError,match='invalid_provenance'):
        _fact({'payload':{'PRIVATE_SENTINEL':'bad source'}})


def test_standalone_retrieval_revalidates_after_embedding():
    async def scenario():
        reader=Reader((fact(),))
        rag=build(reader)
        async def changed(request):
            if rag.bge.calls: reader.facts=()
        rag.bge.hook=changed
        with pytest.raises(FactRagError,match='source_invalid'): await rag.retrieve('backend')
    asyncio.run(scenario())


def test_cancel_during_demo_provisioning_waits_for_marker_then_cleans(monkeypatch):
    from kulai_memory import fact_rag_demo as demo
    async def scenario():
        started,release=asyncio.Event(),asyncio.Event()
        capability=object()
        dropped=[]
        async def create(**kwargs):
            started.set()
            await release.wait()
            return capability
        async def drop(owned,**kwargs): dropped.append(owned)
        monkeypatch.setattr(demo,'create_owned_temporary_database',create)
        monkeypatch.setattr(demo,'drop_owned_temporary_database',drop)
        async def enter():
            async with demo.synthetic_session(): raise AssertionError('Cancelled session was entered')
        task=asyncio.create_task(enter())
        await started.wait(); task.cancel(); await asyncio.sleep(0); release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert dropped==[capability]
    asyncio.run(scenario())


def test_missing_supersession_counterpart_is_rejected():
    a,b=fact(),fact()
    text='Od października backend projektu X wykorzystuje FastAPI zamiast Flask.'
    b=b.model_copy(update={'text':text,'evidence':(EvidenceSpan(quote=text,start=0,end=len(text)),)})
    relation=FactRelation(id=uuid4(),left_id=a.reference.candidate_id,right_id=b.reference.candidate_id,
        kind='possible_supersession',status='PENDING',lock_version=1)
    llm=AnswerLLM()
    def omit(prompt,value):
        if prompt==POLICY: value['citations']=value['citations'][:1]
        return value
    llm.transform=omit
    with pytest.raises(FactRagError,match='invalid_citations'):
        asyncio.run(build(Reader((a,b),(relation,)),llm).ask('backend'))


@pytest.mark.parametrize('token',['F20','UNKNOWN_FACT_ID','00000000-0000-0000-0000-000000000000'])
def test_unknown_citation_token_never_resolves_to_a_source(token):
    llm=AnswerLLM()
    llm.transform=lambda prompt,value: {**value,'citations':[token]} if prompt==POLICY else value
    with pytest.raises(FactRagError,match='invalid_citations'): asyncio.run(build(llm=llm).ask('backend'))


@pytest.mark.parametrize('damage',['model','dimension','zero'])
def test_incompatible_embedding_never_reaches_the_llm(damage):
    class BrokenBGE(HookBGE):
        async def embed(self,request):
            if damage=='dimension': self.dimension=512
            response=await super().embed(request)
            if damage=='model': return response.model_copy(update={'model_id':'other-model'})
            if damage=='zero':
                return response.model_copy(update={'embeddings':tuple(v.model_copy(update={'values':(0.0,)*1024}) for v in response.embeddings)})
            return response
    rag=build()
    rag.bge=BrokenBGE()
    with pytest.raises(FactRagError,match='embedding_failed'): asyncio.run(rag.ask('backend'))
    assert len(rag.bge.calls)==1 and not rag.llm.calls


def test_candidate_character_limit_does_not_truncate_evidence():
    reader=Reader(tuple(fact() for _ in range(5)))
    rag=build(reader,candidate_chars=2000)
    result=asyncio.run(rag.retrieve('backend',top_k=5))
    assert 'candidate_chars' in result.partial_reasons and len(result.hits)<5
    assert all(h.fact.evidence[0].quote==h.fact.text for h in result.hits)
