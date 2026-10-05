from __future__ import annotations

import pytest

from scripts import stt_smoke


def test_cache_detection_is_best_effort_without_huggingface_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(name: str):
        assert name == "huggingface_hub"
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(stt_smoke.importlib, "import_module", unavailable)

    assert stt_smoke._is_cached("large-v3") is None
