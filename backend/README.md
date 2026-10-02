# KulAI Memory backend

Host package: `kulai_memory`.

The core FastAPI runtime and `/health` endpoint are generated. KulAI modules remain pinned by `../kulai.project.json` and the `../vendor/kulai_modules` submodule. Auth and business wiring are not generated yet.

ASGI entry point: `kulai_memory.main:app`. Copy `backend/.env.example` to `backend/.env` before local runtime. After Studio bootstrap, run the root `scripts/dev.py` with the project `.venv` Python.
