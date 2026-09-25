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
                "profile": "CYRENE_PLATFORM_RUNTIME_V1_LOCAL_GPU",
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


def test_acceptance_driver_orchestrates_entire_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    runtime = _runtime(tmp_path)

    # Mock snapshot download
    # 中文：Mock 快照下载。
    def snapshot_download(**_kwargs: object) -> str:
        snapshot = tmp_path / "cache" / "huggingface" / "snapshots" / "pinned"
        snapshot.mkdir(parents=True, exist_ok=True)
        (snapshot / "config.json").write_text('{"model_type":"qwen2"}')
        return str(snapshot)

    monkeypatch.setattr(module, "snapshot_download", snapshot_download)

    calls: list[tuple[str, str, str]] = []

    class MockClient:
        def request(
            self,
            product: str,
            method: str,
            path: str,
            *,
            body: object = None,
            data: bytes | None = None,
            key: str | None = None,
        ) -> object:
            calls.append((product, method, path))
            if product == "catalyst":
                if method == "POST" and path == "/api/v1/datasets":
                    return {"id": "ds-1", "name": "Acceptance Dataset"}
                if method == "POST" and "preparations" in path and "?" in path:
                    return {"id": "prep-1", "datasetId": "ds-1"}
                if method == "PATCH" and path == "/api/v1/preparations/prep-1/mapping":
                    return {"status": "ok"}
                if method == "PATCH" and path.endswith("/split"):
                    return {"status": "ok"}
                if method == "POST" and path.endswith("/confirm"):
                    return {"status": "ok"}
                if method == "POST" and path == "/api/v1/preparations/prep-1/publish":
                    return {
                        "datasetVersion": {
                            "id": "dv-1",
                            "uri": "cyrene://catalyst/dataset-versions/dv-1",
                        }
                    }
                if (
                    method == "POST"
                    and path == "/api/v1/dataset-versions/dv-1/actions/send-to-yield"
                ):
                    return {"targetResource": {"id": "td-1"}}
                if method == "GET" and path == "/api/v1/preparations/prep-2":
                    return {"id": "prep-2", "datasetId": "ds-1"}
                if method == "POST" and path == "/api/v1/preparations/prep-2/publish":
                    return {
                        "datasetVersion": {
                            "id": "dv-2",
                            "uri": "cyrene://catalyst/dataset-versions/dv-2",
                        }
                    }
            elif product == "yield":
                if method == "PATCH" and path == "/api/v1/training-drafts/td-1":
                    return {"status": "ok"}
                if method == "POST" and path == "/api/v1/training-drafts/td-1/actions/start":
                    return {
                        "id": "trun-1",
                        "uri": "cyrene://yield/training-runs/trun-1",
                    }
                if method == "GET" and path == "/api/v1/training-runs/trun-1":
                    return {
                        "id": "trun-1",
                        "state": "COMPLETED",
                        "result": {
                            "id": "tres-1",
                            "adapterArtifact": {"kind": "adapter", "id": "art-lora-1"},
                            "modelVersion": {
                                "id": "mv-1",
                                "architecture": {"kind": "BASE_PLUS_LORA"},
                            },
                        },
                    }
                if (
                    method == "POST"
                    and path == "/api/v1/training-results/tres-1/actions/send-to-reactor"
                ):
                    return {"targetResource": {"id": "dd-1"}}
                if method == "POST" and path == "/api/v1/training-runs/trun-1/actions/cancel":
                    return {"id": "trun-1", "state": "CANCELED"}
            elif product == "reactor":
                if method == "GET" and path == "/api/v1/serving-bindings":
                    return [{"id": "sb-1", "name": "local-rtx"}]
                if method == "GET" and path == "/api/v1/serving-bindings/sb-1/node":
                    return {"nodeRef": "node-rtx4080"}
                if method == "POST" and path == "/api/v1/deployment-drafts/dd-1/actions/deploy":
                    return {"id": "dep-1", "status": "PENDING"}
                if method == "GET" and path == "/api/v1/deployments/dep-1":
                    return {"id": "dep-1", "status": "READY", "endpointId": "ep-1"}
                if method == "GET" and path == "/api/v1/endpoints/ep-1":
                    return {"id": "ep-1", "resourceVersion": "v1"}
                if method == "GET" and path == "/api/v1/exchange-receivers":
                    return [{"receiverId": "rec-1", "providerBindingIds": ["pb-1"]}]
                if method == "POST" and path == "/api/v1/endpoints/ep-1/actions/send-to-exchange":
                    return {"route": {"id": "rt-1"}}
                if method == "POST" and path == "/api/v1/deployments/dep-1/actions/stop":
                    return {"id": "dep-1", "status": "STOPPED"}
            elif product == "exchange":
                if method == "POST" and path == "/api/v1/gateway-endpoints":
                    return {"id": "gep-1"}
                if method == "GET" and path == "/api/v1/gateway-routes/rt-1":
                    return {"id": "rt-1", "status": "ACTIVE", "resourceVersion": "v1"}
                if method == "POST" and path == "/api/v1/gateway-route-drafts/rt-1/actions/confirm":
                    return {"id": "rt-1", "status": "ACTIVE"}
                if method == "POST" and path == "/api/v1/gateway-endpoints/gep-1/actions/disable":
                    return {"id": "gep-1", "state": "DISABLED"}
                if method == "POST" and path == "/v1/chat/completions":
                    return {
                        "choices": [{"message": {"content": "Cyrene is an autonomous platform."}}]
                    }
            elif product == "navigator":
                if (
                    method == "POST"
                    and "sessions" in path
                    and not path.endswith("append")
                    and not path.endswith("send-to-echo")
                ):
                    return {"writerToken": "tok-1", "epoch": 1}
                if method == "POST" and path.endswith("/append"):
                    return {"appended": 4}
                if method == "GET" and "/sessions/" in path:
                    return {"revision": 4}
                if method == "POST" and path.endswith("/actions/send-to-echo"):
                    return {"targetResource": {"id": "ei-1"}}
            elif product == "echo":
                if method == "GET" and path == "/api/v1/evaluation-inputs/ei-1":
                    return {"id": "ei-1"}
                if method == "POST" and path == "/api/v1/evaluation-suites":
                    return {"id": "es-1"}
                if method == "POST" and path == "/api/v1/evaluation-inputs/ei-1/actions/evaluate":
                    return {"id": "er-1"}
                if method == "POST" and path == "/api/v1/evaluation-runs/er-1/annotations":
                    return {"id": "ann-1"}
                if method == "POST" and path == "/api/v1/feedback-sets":
                    return {"id": "fs-1"}
                if (
                    method == "POST"
                    and path == "/api/v1/feedback-sets/fs-1/actions/send-to-catalyst"
                ):
                    return {"targetResource": {"id": "prep-2"}}
            raise AssertionError(f"Unexpected API call: {product} {method} {path}")

    state: dict[str, object] = {}
    args = argparse.Namespace(
        action="acceptance",
        name="Acceptance Test",
        dataset_file=None,
        runtime_config=runtime,
        repository="owner/model",
        revision="a" * 40,
        allow_offline_base_import=True,
        eval_binding="exact-match-plugin",
        epochs=1.0,
        max_steps=1,
        max_length=512,
        template="default",
        rank=8,
        alpha=16,
        binding_index=1,
        receiver_index=1,
        provider_index=1,
        model_pattern="test-model",
        workspace="test-workspace",
        reviewer="Test Reviewer",
        public_url="http://127.0.0.1:8000",
        auth_policy_ref="policy:test",
        poll_interval=0.01,
        timeout=10.0,
    )
    client = MockClient()
    result = module.perform(args, state, client)
    assert result["status"] == "PASS"
    assert result["lifecycleLoop"] == "TEXT_MODEL_LIFECYCLE_V1_FIRST_USABLE_LOOP"
    # A passing acceptance releases the live route, deployment, and run.
    # 中文：验收通过时会释放正在使用的 Route、部署和运行任务。
    # 中文：验收通过后释放活动 route、deployment 和 run。
    assert ("exchange", "POST", "/api/v1/gateway-endpoints/gep-1/actions/disable") in calls
    assert ("reactor", "POST", "/api/v1/deployments/dep-1/actions/stop") in calls
    assert ("yield", "POST", "/api/v1/training-runs/trun-1/actions/cancel") in calls


def test_acceptance_refuses_to_silently_import_the_base_model_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    monkeypatch.setattr(module, "snapshot_download", lambda **_kwargs: str(tmp_path))
    calls: list[tuple[str, str, str]] = []

    class RejectingClient:
        def request(
            self,
            product: str,
            method: str,
            path: str,
            *,
            body: object = None,
            data: bytes | None = None,
            key: str | None = None,
        ) -> object:
            del body, data, key
            calls.append((product, method, path))
            if product == "reactor":
                raise AssertionError("reactor is unavailable")
            if method == "POST" and path == "/api/v1/datasets":
                return {"id": "ds-1"}
            if method == "POST" and "preparations?" in path:
                return {"id": "prep-1", "datasetId": "ds-1"}
            if method == "POST" and path == "/api/v1/preparations/prep-1/publish":
                return {
                    "datasetVersion": {
                        "id": "dv-1",
                        "uri": "cyrene://catalyst/dataset-versions/dv-1",
                    }
                }
            if method == "POST" and path.endswith("/actions/send-to-yield"):
                return {"targetResource": {"id": "td-1"}}
            if product == "catalyst":
                return {"status": "ok"}
            raise AssertionError(f"Unexpected API call: {product} {method} {path}")

    args = argparse.Namespace(
        action="acceptance",
        name="Acceptance Test",
        dataset_file=None,
        runtime_config=_runtime(tmp_path),
        repository="owner/model",
        revision="a" * 40,
        allow_offline_base_import=False,
        eval_binding="exact-match-plugin",
        epochs=1.0,
        max_steps=1,
        max_length=512,
        template="default",
        rank=8,
        alpha=16,
        binding_index=1,
        receiver_index=1,
        provider_index=1,
        model_pattern="test-model",
        workspace="test-workspace",
        reviewer="Test Reviewer",
        public_url="http://127.0.0.1:8000",
        auth_policy_ref="policy:test",
        poll_interval=0.01,
        timeout=10.0,
    )
    with pytest.raises(RuntimeError, match="Reactor base model import failed"):
        module.perform(args, {}, RejectingClient())
    assert ("reactor", "GET", "/api/v1/serving-bindings") in calls
