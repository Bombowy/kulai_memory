"""One local AI lane and explicit resource ownership, without actual models."""
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from kulai_db import DbConfig

from backend.tests.desktop_rag_fakes import FakeDesktopRag, FakeOwnedBGE
from backend.tests.indexing_fakes import FakeRuntimeIndexer
from backend.tests.test_desktop_controller import FakeEngine, FakeProvider, FakeSessionFactory
from backend.tests.test_server_runtime import CollectingSink
from kulai_memory.database_safety import CheckResult, DoctorReport
from backend.tests.test_server_transport import HistoryRepository
from kulai_memory.application import Memory
from kulai_memory.server.runtime import ServerRuntimeError, VoiceMemoryServerRuntime
from kulai_memory.settings import Settings


class Probe:
    def __init__(self):
        self.active = self.maximum = 0
        self.started, self.release = asyncio.Queue(), asyncio.Queue()

    async def run(self, label):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        await self.started.put(label)
        try:
            await self.release.get()
        finally:
            self.active -= 1


class Harness:
    def __init__(self):
        self.bge, self.rag, self.indexer = FakeOwnedBGE(), FakeDesktopRag(), FakeRuntimeIndexer()
        self.whisper, self.engine = FakeProvider(("synthetic question",)), FakeEngine()
        self.sessions, self.repository = FakeSessionFactory(), HistoryRepository()
        self.counts = dict(whisper=0, bge=0, qwen_rag=0, engine=0)

    def factory(self, name, value):
        def create(*args, **kwargs):
            self.counts[name] += 1
            return value
        return create

    def runtime(self):
        def indexer(**kwargs):
            assert kwargs["provider"] is self.bge
            return self.indexer
        def rag(**kwargs):
            assert kwargs["embedding_provider"] is self.bge
            return self.factory("qwen_rag", self.rag)()
        async def doctor(url):
            return DoctorReport((CheckResult(name="test", ok=True),))
        return VoiceMemoryServerRuntime(settings=Settings(_env_file=None, kulai_vector_dimension=1024),
            database_config_factory=lambda: DbConfig(user="synthetic", password="synthetic", name="owned_test"),
            doctor=doctor, provider_factory=self.factory("whisper", self.whisper),
            embedding_provider_factory=self.factory("bge", self.bge), rag_runtime_factory=rag,
            indexer_factory=indexer, engine_factory=self.factory("engine", self.engine),
            session_factory_builder=lambda ignored: self.sessions,
            repository_factory=lambda ignored: self.repository)


def test_shared_resources_reused_for_note_question_text_and_history(tmp_path):
    async def scenario():
        h = Harness()
        runtime = h.runtime()
        await runtime.startup()
        await runtime.startup()
        assert not h.rag.requests and h.rag.entered == 1
        for _ in range(2):
            session = runtime.create_voice_session(event_sink=CollectingSink(), session_id=uuid4())
            await session.start()
            t = await runtime.transcribe(session=session, audio_path=tmp_path / "synthetic.wav")
            await runtime.ingest(transcription=t, ingestion_id=uuid4(), session_id=session.session_id)
            await runtime.ask_memory(query="text question")
            t = await runtime.transcribe_question(audio_path=tmp_path / "synthetic.wav")
            await runtime.ask_memory(query=t.text)
            page = await runtime.list_memories()
            assert page.items
            assert (await runtime.get_memory(memory_id=page.items[0].id)).content == "synthetic question"
        assert h.counts == dict(whisper=1, bge=1, qwen_rag=1, engine=1)
        assert len(h.whisper.requests) == len(h.rag.requests) == 4
        assert len(h.indexer.calls) == 2
        await runtime.shutdown()
        await runtime.shutdown()
        assert h.bge.closed == h.rag.closed == h.indexer.closed == h.engine.dispose_count == 1
    asyncio.run(scenario())


def test_single_ai_lane_and_db_history_progress_while_model_is_blocked(tmp_path):
    async def scenario():
        h, probe = Harness(), Probe()
        stt = h.whisper.transcribe
        async def transcribe(request):
            await probe.run("stt")
            return await stt(request)
        h.whisper.transcribe = transcribe
        h.rag.before = lambda: probe.run("rag")
        index = h.indexer.ensure
        async def ensure(**kwargs):
            # Voice note canonical session is already closed before waiting here.
            assert all(s.closed for s in h.sessions.sessions)
            await probe.run("index")
            return await index(**kwargs)
        h.indexer.ensure = ensure
        runtime = h.runtime()
        await runtime.startup()
        session = runtime.create_voice_session(event_sink=CollectingSink(), session_id=uuid4())
        await session.start()
        async def note():
            t = await runtime.transcribe(session=session, audio_path=tmp_path / "note.wav")
            return await runtime.ingest(transcription=t, ingestion_id=uuid4(), session_id=uuid4())
        async def question():
            t = await runtime.transcribe_question(audio_path=tmp_path / "question.wav")
            return await runtime.ask_memory(query=t.text)
        tasks = [asyncio.create_task(note()), asyncio.create_task(runtime.ask_memory(query="text")),
                 asyncio.create_task(question())]
        labels = []
        for _ in range(5):
            labels.append(await probe.started.get())
            # Read does not acquire the lane. No DB session remains open.
            await asyncio.wait_for(runtime.list_memories(), 2)
            assert all(s.closed and s.commits in (0, 1) for s in h.sessions.sessions)
            assert probe.active == 1 and probe.started.empty()
            await probe.release.put(None)
        await asyncio.gather(*tasks)
        assert sorted(labels) == ["index", "rag", "rag", "stt", "stt"]
        assert probe.maximum == 1
        await runtime.shutdown()
        print("server.model_lane.max_active=1;history_while_busy=PASS;clients=Whisper:1,BGE:1,Qwen:1,engine:1")
    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_shutdown_cancels_network_rag_and_closes_once():
    async def scenario():
        h = Harness()
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def block():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        h.rag.before = block
        runtime = h.runtime()
        await runtime.startup()
        ask = asyncio.create_task(runtime.ask_memory(query="question"))
        await entered.wait()
        await asyncio.wait_for(runtime.shutdown(), 2)
        assert ask.cancelled() and cancelled.is_set()
        assert not runtime._inference_tasks and not runtime._ai_callers
        assert h.bge.closed == h.rag.closed == h.indexer.closed == h.engine.dispose_count == 1
    asyncio.run(scenario())


def test_cancelled_whisper_and_shutdown_drain_before_resource_release(tmp_path):
    async def scenario():
        h = Harness()
        entered, release = asyncio.Event(), asyncio.Event()
        original = h.whisper.transcribe
        path = tmp_path / "controlled.wav"
        path.write_bytes(b"synthetic")
        async def thread_backed(request):
            entered.set()
            await release.wait()
            assert path.exists() and h.engine.dispose_count == h.bge.closed == 0
            return await original(request)
        h.whisper.transcribe = thread_backed
        runtime = h.runtime()
        await runtime.startup()
        task = asyncio.create_task(runtime.transcribe_question(audio_path=path))
        await entered.wait()
        task.cancel()
        shutdown = asyncio.create_task(runtime.shutdown())
        # Deterministic scheduling handshake; no timer/sleep for inference ordering.
        await asyncio.get_running_loop().run_in_executor(None, lambda: None)
        assert not task.done() and not shutdown.done() and h.bge.closed == 0
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await shutdown
        assert h.bge.closed == h.rag.closed == h.engine.dispose_count == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["bge", "rag", "enter", "reconcile"])
def test_partial_startup_failure_cleans_all_prior_resources(stage):
    async def scenario():
        h = Harness()
        runtime = h.runtime()
        def fail(**kwargs):
            raise RuntimeError("private sentinel")
        async def async_fail(**kwargs):
            raise RuntimeError("private sentinel")
        if stage == "bge":
            runtime._embedding_provider_factory = fail
        elif stage == "rag":
            runtime._rag_runtime_factory = fail
        elif stage == "enter":
            h.rag.__aenter__ = async_fail
        else:
            h.indexer.reconcile = async_fail
        with pytest.raises(ServerRuntimeError):
            await runtime.startup()
        assert h.engine.dispose_count == 1 and runtime.provider is None
        if stage != "bge":
            assert h.bge.closed == h.indexer.closed == 1
        if stage in ("enter", "reconcile"):
            assert h.rag.closed == 1
        await runtime.shutdown()
        assert h.engine.dispose_count == 1
    asyncio.run(scenario())
