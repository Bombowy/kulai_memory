# KulAI Memory

KulAI Studio project `kulai_memory`.

## Reproducibility

- Source template: `blank`
- Current pinned KulAI commit: `925b08a13aab9a62e080103185446543a7795794`
- KulAI monorepo submodule: `vendor/kulai_modules`

`kulai.project.json` and the submodule gitlink define the current pin. The
`aa5b4bc93d3b05cd84fcf681f54d848f74d630c5` value in
`.kulai/bootstrap-baseline.json` is the immutable project-generation baseline.
The same historical value in `.kulai/migrations.json` records the source used
to compose the reusable `kvectorstorepg_0001` migration. It changes only when
that reusable migration graph is deliberately recomposed.

## Direct modules

- `kulai-db`
- `kulai-fastapi-core`
- `kulai-fastapi-health`
- `kulai-provider-ollama`
- `kulai-provider-ollama-embeddings`
- `kulai-provider-whisper`
- `kulai-rag`
- `kulai-tasks`
- `kulai-vector-store-pgvector`

## Automatically required modules

- `kulai-logging`
- `kulai-config`
- `kulai-errors`
- `kulai-cache`
- `kulai-embeddings`
- `kulai-llm`
- `kulai-transcription`
- `kulai-vector-store`

## Runtime status

Core FastAPI runtime generated. The ASGI entry point is `kulai_memory.main:app`, and `GET /health` is available. Auth and business-module wiring have not been generated yet.

The application core includes provider-neutral Whisper transcription and
idempotent transcript ingestion into PostgreSQL Memory. Empty VAD-filtered
transcripts create no Memory; callers identify retries with a stable UUID.

## Bootstrap and local run

Use KulAI Studio's project detail page to create the isolated `.venv`, install
and verify the project, run generated tests, and create the initial local
commit. Copy `backend/.env.example` to `backend/.env` before local runtime.

The canonical local PostgreSQL commands are:

```text
.venv\Scripts\python.exe scripts\postgres.py up
.venv\Scripts\python.exe scripts\postgres.py status
.venv\Scripts\python.exe scripts\postgres.py down
```

The wrapper always passes `--env-file backend/.env` to Compose. `down` preserves
the named data volume. For this workflow, keep `DATABASE_URL` unset and use the
single `DB_*` configuration in `backend/.env`; ambient database environment
overrides are rejected. Compose provides PostgreSQL 18 with pgvector, matching
the canonical backup/restore major, and does not run schema migrations. Run
migrations explicitly, then verify the database:

```text
.venv\Scripts\python.exe scripts\migrate.py upgrade
.venv\Scripts\python.exe scripts\db_doctor.py
```

Create a full custom-format PostgreSQL backup and verify it in an automatically
owned temporary database with:

```text
.venv\Scripts\python.exe scripts\db_backup.py --output <backup.dump>
.venv\Scripts\python.exe scripts\db_restore_smoke.py <backup.dump>
.venv\Scripts\python.exe scripts\db_backup_restore_drill.py
```

Backups can contain all user data and should be stored outside the repository.
The backup tool requires compatible PostgreSQL client tools (`pg_dump` and, for
the restore smoke, `pg_restore`, `createdb`, and `dropdb`). It never installs
them. The real repository integration tests are opt-in and accept only a
loopback development/test database:

```text
$env:KULAI_RUN_POSTGRES_INTEGRATION = "1"
.venv\Scripts\python.exe -m pytest backend\tests\integration -q
```

After bootstrap, run `.venv` Python with `scripts/dev.py`; the server binds to
`127.0.0.1`. The data-safety contract is in `docs/DATA_SAFETY.md`.
