"""Lifecycle bootstrap authority tests. | 生命周期 bootstrap 权威边界测试。"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


def _module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "text-lifecycle-v1.py"
    spec = importlib.util.spec_from_file_location("text_lifecycle_v1", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime(tmp_path: Path) -> Path:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    path = tmp_path / "runtime.json"
    path.write_text(
        json.dumps(
            {
                "profile": "CYRENE_TEXT_LIFECYCLE_V1_LOCAL_GPU",
                "status": "READY",
                "runtimeHome": str(tmp_path),
                "artifactRoot": str(artifact_root),
            }
        )
    )
    path.chmod(0o600)
    return path


def test_base_import_publishes_artifact_without_calling_reactor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    runtime = _runtime(tmp_path)

    def snapshot_download(**_kwargs: object) -> str:
        snapshot = tmp_path / "cache" / "huggingface" / "snapshots" / "pinned"
        snapshot.mkdir(parents=True)
        (snapshot / "config.json").write_text('{"model_type":"llama"}')
        return str(snapshot)

    monkeypatch.setattr(module, "snapshot_download", snapshot_download)

    class RejectingClient:
        def request(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("base import must not call a live Product")

    state: dict[str, object] = {}
    result = module.perform(
        argparse.Namespace(
            action="base-import",
            runtime_config=runtime,
            repository="owner/model",
            revision="a" * 40,
        ),
        state,
        RejectingClient(),
    )
    assert result == state["baseImport"]
    assert result["modelArtifact"]["kind"] == "model"
    assert result["modelInfo"]["source"]["revision"] == "a" * 40


def test_base_import_fails_closed_when_snapshot_escapes_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    runtime = _runtime(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (outside / "config.json").write_text("{}")
    monkeypatch.setattr(module, "snapshot_download", lambda **_kwargs: str(outside))
    with pytest.raises(ValueError, match="escaped"):
        module.import_base_artifact(runtime, "owner/model", "b" * 40)
