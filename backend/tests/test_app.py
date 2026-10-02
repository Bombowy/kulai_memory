"""Contract tests for the generated core runtime."""

from fastapi.testclient import TestClient

from kulai_memory.main import app


def test_application_metadata() -> None:
    assert app.title == "KulAI Memory"


def test_health() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
