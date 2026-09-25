"""
┌─────────────────────────────────────────────────────────────────────┐
│ Module: Text Lifecycle V1 targeted HTTP integration tests           │
│ Role: Exercise owning Product APIs with immutable tiny fixtures.   │
│ 模块职责：验证跨产品显式交接；训练执行器为测试夹具，不构成 GPU/E2E 验收。 │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import json
import platform
import struct
from pathlib import Path

import httpx
from cy_artifacts import ArtifactKind, LocalArtifactProvider
from cy_exec.training.control_plane import TrainingControlPlane
from cy_exec.training.executors.base import CancelOutcome, ProcessHandle
from cy_exec.training.product_api import create_app as yield_app
from cy_exec.training.runtime import TrainingRuntime
from cyrene_catalyst.api import create_app as catalyst_app
from cyrene_echo.api import create_app as echo_app
from cyrene_navigator.persistence import PersistencePrincipal, create_persistence_app
from cyrene_preflight import AcceleratorFacts, HardwareFacts
from cyrene_reactor_product.api import create_app as reactor_app

MODEL_ARTIFACT_KIND = ArtifactKind("model")


def weights(path: Path) -> None:
    """One bounded safetensors member for Artifact contract tests only.

    中文:仅为 Artifact contract 测试生成一个有限大小的 safetensors 成员。
    """
    header = json.dumps(
        {
            "layer.lora_A.weight": {
                "dtype": "F32",
                "shape": [1, 1],
                "data_offsets": [0, 4],
            }
        }
    ).encode()
    path.write_bytes(struct.pack("<Q", len(header)) + header + b"\0" * 4)


class FixtureExecutor:
    """No model training occurs in this fixture; Product and Artifact code is real.

    中文:此 fixture 不会训练模型;Product 和 Artifact 代码使用真实实现。
    """

    def __init__(self):
        self.launches = []

    def start(self, launch):
        self.launches.append(launch)
        root = Path(launch.work_dir)
        root.mkdir(parents=True, exist_ok=True)
        (root / "adapter_config.json").write_text(
            json.dumps(
                {
                    "peft_type": "LORA",
                    "task_type": "CAUSAL_LM",
                    "base_model_name_or_path": launch.extra["product_spec"]["model"]["path"],
                    "revision": None,
                }
            )
        )
        weights(root / "adapter_model.safetensors")
        return ProcessHandle(pid=1, argv=launch.argv, work_dir=launch.work_dir)

    def poll(self, handle):
        return 0

    def read_new_output(self, handle):
        return []

    def cancel(self, handle, timeout=15):
        return CancelOutcome(stopped=True, cleanup_confirmed=True, leases_released=True)


def http_bus(apps):
    """Send actual HTTP bodies through ASGI; only the network transport is in-process.

    中文:通过 ASGI 发送真实 HTTP 请求正文;只有网络传输仍在进程内执行。
    """

    def handle(request):
        async def dispatch():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=apps[request.url.host])
            ) as client:
                response = await client.request(
                    request.method,
                    str(request.url),
                    content=request.content,
                    headers=request.headers,
                )
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=response.content,
                )

        return asyncio.run(dispatch())

    return httpx.Client(transport=httpx.MockTransport(handle), trust_env=False)


def call(client, method, url, expected=200, **kwargs):
    response = client.request(method, url, **kwargs)
    assert response.status_code == expected, response.text
    return response.json()


def publish_preparation(client, preparation_id):
    prefix = f"http://catalyst/api/v1/preparations/{preparation_id}"
    call(client, "PATCH", prefix + "/split", json={"split": {"trainRatio": 1}})
    call(client, "POST", prefix + "/confirm")
    return call(
        client,
        "POST",
        prefix + "/publish",
        201,
        headers={"Idempotency-Key": "publish:" + preparation_id},
    )["datasetVersion"]


def test_explicit_http_handoffs_keep_artifact_identity_and_feedback_provenance(
    tmp_path,
):
    artifacts = LocalArtifactProvider(tmp_path / "artifacts")
    executor = FixtureExecutor()
    hardware = HardwareFacts.from_node_resource_inventory(
        node_id="unit-node",
        inventory_generation=1,
        architecture=platform.machine().lower(),
        accelerator_runtime="cuda",
        accelerators=[
            AcceleratorFacts(
                device_id="unit-gpu",
                kind="gpu",
                vendor="nvidia",
                features=("fp32",),
                total_memory_bytes=24 * 1024**3,
                allocatable_memory_bytes=24 * 1024**3,
            )
        ],
    )
    runtime = TrainingRuntime(
        executor=executor, artifact_provider=artifacts, hardware_facts=hardware
    )
    control = TrainingControlPlane(
        runtime, tmp_path / "yield" / "runs.json", durable_tiny_attempt=True
    )
    apps = {
        "catalyst": catalyst_app(
            database_path=tmp_path / "catalyst.db",
            artifact_root=tmp_path / "artifacts",
            yield_url="http://yield",
        ),
        "yield": yield_app(
            state_directory=tmp_path / "yield",
            artifact_root=tmp_path / "artifacts",
            control=control,
            reactor_url="http://reactor",
        ),
        "reactor": reactor_app(database_path=tmp_path / "reactor.db"),
        "echo": echo_app(
            database_path=tmp_path / "echo.db",
            artifact_root=tmp_path / "artifacts",
            catalyst_url="http://catalyst",
        ),
        "navigator": create_persistence_app(
            tmp_path / "navigator.db",
            {
                "unit-principal": PersistencePrincipal("unit-user", frozenset({"unit-workspace"})),
            },
            artifact_root=tmp_path / "artifacts",
            echo_url="http://echo",
        ),
    }
    client = http_bus(apps)
    apps["catalyst"].state.lifecycle_actions.client = client
    apps["yield"].state.yield_service._http = client
    apps["echo"].state.echo_lifecycle.client = client
    apps["navigator"].state.echo_handoff.client = client
    dataset = call(
        client,
        "POST",
        "http://catalyst/api/v1/datasets",
        201,
        json={"name": "Unit text"},
    )
    preparation = call(
        client,
        "POST",
        f"http://catalyst/api/v1/datasets/{dataset['id']}/preparations",
        201,
        params={"name": "Instruction samples", "filename": "samples.jsonl"},
        content=b'{"instruction":"Hello","input":"","output":"Hi"}\n',
        headers={"Content-Type": "application/octet-stream"},
    )
    call(
        client,
        "PATCH",
        f"http://catalyst/api/v1/preparations/{preparation['id']}/mapping",
        json={
            "mapping": {
                "mode": "instruction",
                "instruction": {"field": "instruction"},
                "input": {"field": "input"},
                "output": {"field": "output"},
            },
            "normalization": {},
        },
    )
    version = publish_preparation(client, preparation["id"])
    handoff = call(
        client,
        "POST",
        f"http://catalyst/api/v1/dataset-versions/{version['id']}/actions/send-to-yield",
    )
    draft_id = handoff["targetResource"]["id"]
    assert handoff["status"] == "DRAFT" and not executor.launches
    assert (
        call(
            client,
            "POST",
            f"http://catalyst/api/v1/dataset-versions/{version['id']}/actions/send-to-yield",
        )
        == handoff
    )
    base = tmp_path / "base"
    base.mkdir()
    (base / "config.json").write_text('{"model_type":"llama"}')
    weights(base / "model.safetensors")
    base_ref = artifacts.publish_portable_directory(base, kind=MODEL_ARTIFACT_KIND)
    call(
        client,
        "PATCH",
        f"http://yield/api/v1/training-drafts/{draft_id}",
        json={
            "baseModel": {
                "artifact": base_ref.to_dict(),
                "source": {"repository": "example/base", "revision": "a" * 40},
            },
            "parameters": {"maxSteps": 1},
        },
    )
    run = call(
        client,
        "POST",
        f"http://yield/api/v1/training-drafts/{draft_id}/actions/start",
        202,
    )
    for _ in range(14):
        apps["yield"].state.yield_service.advance()
    run = call(client, "GET", f"http://yield/api/v1/training-runs/{run['id']}")
    assert run["state"] == "COMPLETED", run
    result = run["result"]
    handoff = call(
        client,
        "POST",
        f"http://yield/api/v1/training-results/{result['id']}/actions/send-to-reactor",
    )
    draft = call(client, "GET", handoff["openIn"])
    assert draft["state"] == "DRAFT"
    assert draft["modelVersion"] == result["modelVersion"]
    assert apps["reactor"].state.reactor_store.list_deployments() == []
    assert str(tmp_path) not in json.dumps(result)

    # The completed text events are fixtures. No inference or GPU acceptance is claimed.
    # 中文:完成的文本事件仅为 fixture;这里不声称已完成推理或 GPU 验收。
    # 中文:这些已完成的 text events 是测试夹具,不代表执行过推理或通过了 GPU 验收。
    nav = "http://navigator/api/v1/harness/workspaces/unit-workspace/sessions"
    auth = {"Authorization": "Bearer unit-principal"}
    handle = call(
        client,
        "POST",
        nav,
        headers=auth,
        json={
            "header": {
                "version": 2,
                "id": "unit-session",
                "createdAt": 1000,
                "isSeeded": False,
            },
            "inheritedEventCount": 0,
            "clientId": "unit-client",
        },
    )
    events = [
        {"type": "turn/start", "data": {"turn": 1}},
        {
            "type": "user/message",
            "data": {"content": [{"type": "text", "text": "Hello"}]},
        },
        {
            "type": "assistant/message",
            "data": {
                "turn": 1,
                "message": {
                    "content": [{"type": "text", "text": "Wrong"}],
                    "source": {"model": "unit-route"},
                },
                "usage": {"inputTokens": 1, "outputTokens": 1},
            },
        },
        {"type": "turn/end", "data": {"turn": 1, "reason": {"kind": "completed"}}},
    ]
    for index, event in enumerate(events):
        event.update(seq=index, time=1001 + index)
    call(
        client,
        "POST",
        nav + "/unit-session/append",
        headers=auth,
        json={
            "writerToken": handle["writerToken"],
            "epoch": handle["epoch"],
            "batchId": "unit-batch",
            "events": events,
        },
    )
    snapshot = call(client, "GET", nav + "/unit-session", headers=auth)
    send = {
        "expectedRevision": snapshot["revision"],
        "selectedEventSeqs": [1, 2],
        "provenanceRefs": [
            result["modelVersion"]["id"],
            f"cyrene://catalyst/dataset-versions/{version['id']}",
        ],
    }
    echo_receipt = call(
        client,
        "POST",
        nav + "/unit-session/actions/send-to-echo",
        201,
        headers=auth,
        json=send,
    )
    assert echo_receipt["status"] == "DRAFT"
    assert (
        call(
            client,
            "POST",
            nav + "/unit-session/actions/send-to-echo",
            201,
            headers=auth,
            json=send,
        )
        == echo_receipt
    )
    input_id = echo_receipt["targetResource"]["id"]
    imported = call(client, "GET", echo_receipt["openIn"])
    assert "evaluationRun" not in imported
    suite = call(
        client,
        "POST",
        "http://echo/api/v1/evaluation-suites",
        201,
        json={
            "name": "User reference",
            "evaluator": "exact_match.v1",
            "expectedField": "expected",
            "actualField": "output",
            "threshold": 1,
        },
    )
    evaluation = call(
        client,
        "POST",
        f"http://echo/api/v1/evaluation-inputs/{input_id}/actions/evaluate",
        201,
        json={
            "suiteId": suite["id"],
            "engineBindingId": "exact-match-plugin",
            "referenceAnswers": {"1": "Hi"},
        },
    )
    assert evaluation["state"] == "SUCCEEDED"
    annotation = call(
        client,
        "POST",
        f"http://echo/api/v1/evaluation-runs/{evaluation['id']}/annotations",
        201,
        json={
            "sampleIndex": 1,
            "reviewer": "unit-reviewer",
            "manualScore": 0,
            "correctedOutput": "Hi",
        },
    )
    feedback = call(
        client,
        "POST",
        "http://echo/api/v1/feedback-sets",
        201,
        json={
            "name": "Selected correction",
            "runId": evaluation["id"],
            "sampleIndexes": [1],
            "annotationIds": [annotation["id"]],
        },
    )
    returned = call(
        client,
        "POST",
        f"http://echo/api/v1/feedback-sets/{feedback['id']}/actions/send-to-catalyst",
        json={"datasetId": dataset["id"]},
    )
    assert returned["status"] == "DRAFT"
    assert (
        call(
            client,
            "POST",
            f"http://echo/api/v1/feedback-sets/{feedback['id']}/actions/send-to-catalyst",
            json={"datasetId": dataset["id"]},
        )
        == returned
    )
    feedback_preparation = call(client, "GET", returned["openIn"])
    assert feedback_preparation["state"] == "MAPPED"
    assert feedback_preparation.get("publishedVersionId") is None
    second = publish_preparation(client, returned["targetResource"]["id"])
    assert second["datasetId"] == dataset["id"] and second["id"] != version["id"]
    assert f"cyrene://echo/feedback-sets/{feedback['id']}" in second["sourceRefs"]
    from cy_artifacts import ArtifactRef

    raw = artifacts.resolve(ArtifactRef.from_dict(second["source"])).location.read_text()
    row = json.loads(raw)
    assert row["modelOutput"] == "Wrong" and row["output"] == "Hi"
    assert row["humanAnnotation"]["id"] == annotation["id"] and row["evaluationScore"] == 0
    assert row["originalSample"]["provenanceRefs"] == send["provenanceRefs"]
    assert len(executor.launches) == 2
    assert call(client, "GET", nav + "/unit-session", headers=auth) == snapshot
