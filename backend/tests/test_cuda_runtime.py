from __future__ import annotations

import os

import pytest

from kulai_memory.runtime import CudaRuntimeConfigurationError, ProcessCudaDllScope


def test_cuda_scope_changes_only_process_path_and_restores_it(tmp_path) -> None:
    directory = tmp_path / "cuda"
    directory.mkdir()
    original = os.environ.get("PATH", "")
    scope = ProcessCudaDllScope(directory)

    scope.activate()
    scope.activate()
    assert os.environ["PATH"].split(os.pathsep)[0] == str(directory.resolve())
    scope.close()
    scope.close()

    assert os.environ.get("PATH", "") == original


def test_cuda_scope_rejects_missing_directory_without_path_change(tmp_path) -> None:
    original = os.environ.get("PATH", "")
    scope = ProcessCudaDllScope(tmp_path / "missing")

    with pytest.raises(CudaRuntimeConfigurationError):
        scope.activate()

    assert os.environ.get("PATH", "") == original
