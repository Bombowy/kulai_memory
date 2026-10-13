"""The actual dev launcher never silently downgrades a configured SECURE mode."""
from functools import lru_cache
import sys

import pytest

from scripts import dev
from kulai_memory import settings as module
from kulai_memory.security import SecurityMode


@pytest.mark.parametrize("configured,flag,expected", [
    (None, None, SecurityMode.LOCAL_DEV),
    (SecurityMode.SECURE, None, SecurityMode.SECURE),
    (SecurityMode.LOCAL_DEV, None, SecurityMode.LOCAL_DEV),
    (None, "SECURE", SecurityMode.SECURE),
    (SecurityMode.SECURE, "LOCAL_DEV", SecurityMode.LOCAL_DEV),
])
def test_launcher_explicit_mode_loopback_proxy_and_log_configuration(monkeypatch, configured, flag, expected):
    monkeypatch.delenv("KULAI_SECURITY_MODE", raising=False)
    @lru_cache(maxsize=1)
    def get_settings():
        import os
        if "KULAI_SECURITY_MODE" in os.environ or configured is None:
            return module.Settings(_env_file=None)
        return module.Settings(_env_file=None, kulai_security_mode=configured)
    monkeypatch.setattr(module, "get_settings", get_settings)
    calls = []
    monkeypatch.setattr(dev.uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["scripts/dev.py", *(["--security-mode", flag] if flag else [])])
    dev.main()
    assert get_settings().kulai_security_mode is expected
    assert calls[0]["host"] == "127.0.0.1" and calls[0]["port"] == 8000
    assert calls[0]["proxy_headers"] is False and calls[0]["access_log"] is False
    assert calls[0]["log_level"] == "warning" and calls[0]["reload"] is True
