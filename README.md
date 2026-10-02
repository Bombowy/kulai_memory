# KulAI Memory

KulAI Studio project `kulai_memory`.

## Reproducibility

- Source template: `blank`
- Pinned KulAI commit: `aa5b4bc93d3b05cd84fcf681f54d848f74d630c5`
- KulAI monorepo submodule: `vendor/kulai_modules`

The manifest stores the final direct selection and exact KulAI commit. The dependency closure is reconstructed from that pinned source.

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

## Bootstrap and local run

Use KulAI Studio's project detail page to create the isolated `.venv`, install and verify the project, run generated tests, and create the initial local commit. No push is performed. Copy `backend/.env.example` to `backend/.env` before local runtime (`Copy-Item backend\.env.example backend\.env` on Windows or `cp backend/.env.example backend/.env` on POSIX). For database-resolved projects, run `docker compose up -d`; Compose provides PostgreSQL infrastructure only and does not run schema migrations. After bootstrap, run `.venv` Python with `scripts/dev.py`; the server binds to `127.0.0.1`.
