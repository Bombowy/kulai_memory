from types import SimpleNamespace
from uuid import UUID

import pytest
from kulai_vector_store import VectorMetric

from kulai_memory.application.rag import MemoryCitation, MemoryContext, MemoryRagResult, MemoryRagRetrievalMetadata
from kulai_memory.database_safety import CheckResult, DoctorReport
from kulai_memory.llm_provider import create_llm_provider
from kulai_memory.settings import Settings
from scripts import ask_memory as cli

PRIVATE = 'PRIVATE_PROMPT_DB_PASSWORD_SENTINEL'


@pytest.fixture
def harness(monkeypatch):
    state = SimpleNamespace(
        settings=Settings(_env_file=None, kulai_vector_dimension=1024),
        config=SimpleNamespace(async_url='postgresql+asyncpg://u:secret@localhost/local'),
        calls=dict(doctor=0, runtime=0, closed=0, engine=0, disposed=0, ask=0), failure=None,
        doctor=DoctorReport((
            CheckResult('alembic.expected_head', True, 'kulai_memory_0004'),
            CheckResult('alembic.current', True, ['kulai_memory_0004']),
            CheckResult('vector.embedding_dimension', True,
                        {'actual_dimension': 1024, 'configured_dimension': 1024}),
        )),
    )
    async def doctor(**kwargs):
        state.calls['doctor'] += 1
        return state.doctor
    async def dispose():
        state.calls['disposed'] += 1
    def engine(*args, **kwargs):
        assert kwargs == dict(echo=False, hide_parameters=True)
        state.calls['engine'] += 1
        return SimpleNamespace(dispose=dispose)
    class Runtime:
        def __init__(self, **kwargs):
            state.calls['runtime'] += 1
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            state.calls['closed'] += 1
        async def ask_with_context(self, *, query, top_k):
            state.calls['ask'] += 1
            state.top_k = top_k
            if state.failure:
                raise RuntimeError(PRIVATE)
            return MemoryRagResult(
                query=query, answer='Wenus', sufficient_context=True,
                citations=(MemoryCitation(memory_id=UUID(int=1), rank=1, score=0.8),),
                retrieval=MemoryRagRetrievalMetadata(metric=VectorMetric.COSINE,
                    retrieved_count=1, context_memory_count=1, context_chars=10, max_context_chars=12000),
            ), MemoryContext(text=PRIVATE, hits=(), max_chars=12000)
    monkeypatch.setattr(cli, 'get_settings', lambda: state.settings)
    monkeypatch.setattr(cli, 'database_config', lambda: state.config)
    monkeypatch.setattr(cli, 'run_database_doctor', doctor)
    monkeypatch.setattr(cli, 'create_async_engine', engine)
    monkeypatch.setattr(cli, 'async_sessionmaker', lambda *a, **kw: object())
    monkeypatch.setattr(cli, 'MemoryRagRuntime', Runtime)
    return state


def test_normal_cli_has_answer_and_provenance_without_context(harness, capsys):
    assert cli.main(['--query', 'synthetic question', '--top-k', '20']) == 0
    out = capsys.readouterr()
    assert 'query=synthetic question' in out.out and 'answer=Wenus' in out.out
    assert 'sufficient_context=true' in out.out and f'citations={UUID(int=1)}' in out.out
    assert not out.err and PRIVATE not in out.out and harness.top_k == 20
    assert harness.calls == dict(doctor=1, runtime=1, closed=1, engine=1, disposed=1, ask=1)


def test_context_debug_is_explicit_and_help_warns(harness, capsys):
    assert 'displays user Memory content' in cli.parser().format_help()
    assert cli.main(['--query', 'synthetic', '--show-context']) == 0
    assert 'context=' + PRIVATE in capsys.readouterr().out


@pytest.mark.parametrize('args', [[], ['--query', PRIVATE, '--top-k', '0'],
    ['--query', PRIVATE, '--top-k', '21'], ['--query', PRIVATE, '--top-k', PRIVATE],
    ['--query', PRIVATE, '--unknown', PRIVATE]])
def test_parser_hides_input_and_does_no_io(harness, capsys, args):
    with pytest.raises(SystemExit):
        cli.main(args)
    assert not any(harness.calls.values()) and PRIVATE not in capsys.readouterr().err


@pytest.mark.parametrize('query', ['', ' \t', 'x' * 10001])
def test_invalid_query_before_database(harness, query, capsys):
    assert cli.main(['--query', query]) == 1
    assert not any(harness.calls.values()) and 'retrieval.invalid_query' in capsys.readouterr().err


@pytest.mark.parametrize('guard', ['env', 'db', 'embedding', 'dimension', 'llm', 'head', 'revision', 'doctor', 'actual_dimension'])
def test_local_schema_and_model_guards(harness, capsys, guard):
    if guard == 'env': harness.settings.app_env = 'production'
    elif guard == 'db': harness.config.async_url = 'postgresql+asyncpg://u:secret@192.0.2.1/db'
    elif guard == 'embedding': harness.settings.kulai_embedding_model = PRIVATE
    elif guard == 'dimension': harness.settings.kulai_vector_dimension = 768
    elif guard == 'llm': harness.settings.kulai_llm_model = ' '
    else:
        changes = {
            'head': CheckResult('alembic.expected_head', True, 'kulai_memory_0005'),
            'revision': CheckResult('alembic.current', True, ['kulai_memory_0003']),
            'doctor': CheckResult('database.connection', False, message=PRIVATE),
            'actual_dimension': CheckResult('vector.embedding_dimension', True,
                                        {'actual_dimension': 768, 'configured_dimension': 1024}),
        }
        change = changes[guard]
        harness.doctor = DoctorReport(tuple(c for c in harness.doctor.checks if c.name != change.name) + (change,))
    assert cli.main(['--query', 'synthetic']) == 1
    assert harness.calls['runtime'] == 0 and PRIVATE not in capsys.readouterr().err


def test_generation_failure_is_safe_and_resources_disposed(harness, capsys, caplog):
    harness.failure = True
    assert cli.main(['--query', 'synthetic']) == 1
    out = capsys.readouterr()
    assert not out.out and PRIVATE not in out.err + caplog.text
    assert harness.calls['closed'] == harness.calls['disposed'] == 1


@pytest.mark.parametrize('host', ['http://192.0.2.1:11434', 'https://example.org',
                                'http://user:password@localhost:11434', 'http://localhost:11434/private'])
def test_llm_factory_rejects_remote_or_credentialed_hosts_without_client(host, monkeypatch):
    from kulai_memory import llm_provider
    def forbidden(config):
        pytest.fail('Client must not be constructed for invalid configuration')
    monkeypatch.setattr(llm_provider, 'OllamaLLMProvider', forbidden)
    from kulai_memory.application.rag import MemoryRagError
    with pytest.raises(MemoryRagError):
        create_llm_provider(settings=Settings(_env_file=None, kulai_ollama_base_url=host))


def test_llm_factory_explicit_model_host_timeout_and_no_thinking(monkeypatch):
    from kulai_memory import llm_provider
    monkeypatch.setattr(llm_provider, 'OllamaLLMProvider', lambda config: config)
    config = create_llm_provider(settings=Settings(_env_file=None))
    assert config.model == 'qwen3.5:9b' and config.host == 'http://127.0.0.1:11434'
    assert config.timeout_seconds == 180.0 and config.think is False
    assert not config.allow_model_hint
