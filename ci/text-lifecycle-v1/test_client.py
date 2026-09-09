"""Exercise explicit client actions against owning Product APIs. | 验证逐步客户端的真实接口。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from cy_artifacts import ArtifactKind, LocalArtifactProvider
from test_handoffs import catalyst_app, echo_app, http_bus, yield_app

DATASET_ARTIFACT_KIND = ArtifactKind("dataset")


def test_client_prepares_both_dataset_versions_without_manual_resource_ids(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "lifecycle_client", Path(__file__).parents[2] / "scripts/text-lifecycle-v1.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "artifacts"
    provider = LocalArtifactProvider(root)
    apps = {
        "catalyst": catalyst_app(
            database_path=tmp_path / "catalyst.db",
            artifact_root=root,
            platform_artifacts=True,
            yield_url="http://yield",
        ),
        "yield": yield_app(state_directory=tmp_path / "yield", artifact_root=root),
        "echo": echo_app(
            database_path=tmp_path / "echo.db",
            artifact_root=root,
            platform_artifacts=True,
            catalyst_url="http://catalyst",
        ),
    }
    bus = http_bus(apps)
    apps["catalyst"].state.lifecycle_actions.client = bus
    apps["echo"].state.echo_lifecycle.client = bus

    class Client(module.ProductClient):
        def request(self, product, method, path, *, body=None, data=None, key=None):
            headers = {"Idempotency-Key": key} if key else {}
            if data is not None:
                headers["Content-Type"] = "application/octet-stream"
            response = bus.request(
                method,
                "http://" + product + path,
                json=body,
                content=data,
                headers=headers,
            )
            assert response.status_code < 300, response.text
            return response.json()

    state = {}
    client = Client()

    def action(*values):
        return module.perform(module.parser().parse_args(values), state, client)

    source = tmp_path / "instruction.jsonl"
    source.write_text('{"instruction":"q","input":"","output":"a"}\n')
    action("dataset-import", str(source), "--name", "selected text")
    action("dataset-map")
    action("dataset-split")
    action("dataset-confirm")
    first = action("dataset-publish")
    assert action("send-to-yield")["status"] == "DRAFT"
    assert state["trainingDraft"]["datasetVersion"]["id"] == first["id"]
    snapshot = provider.publish_bytes(
        b'{"instruction":"q","output":"wrong"}\n', kind=DATASET_ARTIFACT_KIND
    )
    state["evaluationInput"] = client.request(
        "echo",
        "POST",
        "/api/v1/evaluation-inputs",
        body={
            "sourceRef": {
                "uri": "cyrene://navigator/sessions/selected",
                "id": "selected",
                "resourceVersion": 2,
            },
            "artifact": snapshot.to_dict(),
            "format": "NAVIGATOR_TEXT_JSONL_V1",
            "contentRefs": ["cyrene://navigator/sessions/selected/events/1"],
        },
    )
    assert action("evaluation-input")["total"] == 1
    assert action("evaluate", "--reference-answer", "corrected")["state"] == "SUCCEEDED"
    action(
        "annotate",
        "--sample",
        "1",
        "--correction",
        "corrected",
        "--score",
        "1",
        "--reviewer",
        "reviewer",
    )
    action("feedback-create", "--sample", "1", "--name", "selected correction")
    assert action("send-to-catalyst")["status"] == "DRAFT"
    assert state["preparation"]["state"] == "MAPPED"
    action("dataset-split")
    action("dataset-confirm")
    second = action("dataset-publish")
    assert second["datasetId"] == first["datasetId"] and second["id"] != first["id"]
