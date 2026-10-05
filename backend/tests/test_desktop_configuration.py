from __future__ import annotations

import tomllib
from pathlib import Path

from kulai_memory.settings import Settings


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_desktop_dependencies_are_an_optional_group() -> None:
    metadata = tomllib.loads(
        (BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    desktop = metadata["project"]["optional-dependencies"]["desktop"]

    assert desktop == ["PySide6>=6.11,<7", "sounddevice>=0.5.6,<1"]
    assert not any(requirement.lower().startswith("numpy") for requirement in desktop)


def test_cuda_dll_directory_is_optional_and_blank_is_unset(tmp_path: Path) -> None:
    default = Settings(_env_file=None)
    blank = Settings(_env_file=None, kulai_cuda_dll_dir="")
    configured = Settings(_env_file=None, kulai_cuda_dll_dir=tmp_path)

    assert default.kulai_cuda_dll_dir is None
    assert blank.kulai_cuda_dll_dir is None
    assert configured.kulai_cuda_dll_dir == tmp_path
