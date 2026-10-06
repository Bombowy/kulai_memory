"""Process-local CUDA DLL exposure without system configuration changes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class CudaRuntimeConfigurationError(RuntimeError):
    """The optional CUDA DLL directory cannot be activated safely."""


class ProcessCudaDllScope:
    """Temporarily expose an optional CUDA DLL directory to this process."""

    def __init__(self, directory: Path | None) -> None:
        self._directory = directory
        self._original_path: str | None = None
        self._dll_handle: Any = None

    @property
    def active(self) -> bool:
        return self._original_path is not None or self._dll_handle is not None

    def activate(self) -> None:
        if self._directory is None or self.active:
            return
        directory = self._directory.expanduser().resolve()
        if not directory.is_dir():
            raise CudaRuntimeConfigurationError

        self._original_path = os.environ.get("PATH", "")
        entries = self._original_path.split(os.pathsep)
        normalized = {
            os.path.normcase(os.path.abspath(entry)) for entry in entries if entry
        }
        if os.path.normcase(str(directory)) not in normalized:
            os.environ["PATH"] = str(directory) + os.pathsep + self._original_path
        if os.name == "nt" and hasattr(os, "add_dll_directory"):
            try:
                self._dll_handle = os.add_dll_directory(str(directory))
            except OSError as exc:
                self.close()
                raise CudaRuntimeConfigurationError from exc

    def close(self) -> None:
        handle = self._dll_handle
        self._dll_handle = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        if self._original_path is not None:
            os.environ["PATH"] = self._original_path
            self._original_path = None
