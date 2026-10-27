"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 test_deployment_probe.py                                              │
│  Module: tooling.deploy.teams_cloud.tests.test_deployment_probe          │
│  Role: Verify deployment probe contracts without VM/provider access.      │
│                                                                          │
│  模块职责：用契约形状与临时 SQLite 验证部署探测，不模拟外部成功。        │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import ast
import base64
import importlib
import inspect
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
deployment_probe = importlib.import_module("deployment_probe")
preflight = importlib.import_module("preflight")
runtime_update = importlib.import_module("runtime_update")
stage_transition = importlib.import_module("stage_transition")


def _uuid(number: int) -> str:
    """Return a deterministic canonical UUID for a schema-only fixture."""

    return f"00000000-0000-4000-8000-{number:012x}"


def test_private_module_references_exist_and_match_live_signatures() -> None:
    """Catch private API typos/signature drift against real modules, not patched fakes."""

    module_by_name = {
        "preflight": preflight,
        "runtime_update": runtime_update,
        "stage_transition": stage_transition,
    }
    tree = ast.parse(Path(deployment_probe.__file__).read_text(encoding="utf-8"))
    references = 0
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Attribute)
            or not isinstance(node.value, ast.Name)
            or node.value.id not in module_by_name
        ):
            continue
        module = module_by_name[node.value.id]
        assert hasattr(module, node.attr), f"{node.value.id}.{node.attr} is missing"
        if not isinstance(node.ctx, ast.Load):
            continue
        parent_calls = [
            candidate
            for candidate in ast.walk(tree)
            if isinstance(candidate, ast.Call) and candidate.func is node
        ]
        for call in parent_calls:
            target = getattr(module, node.attr)
            assert callable(target), f"{node.value.id}.{node.attr} is not callable"
            assert not any(isinstance(argument, ast.Starred) for argument in call.args)
            assert all(keyword.arg is not None for keyword in call.keywords)
            inspect.signature(target).bind(
                *[object() for _ in call.args],
                **{keyword.arg: object() for keyword in call.keywords},
            )
            references += 1
    assert references > 0


def test_locked_image_mapping_uses_the_live_runtime_update_api() -> None:
    """Exercise the exact private image-lock API used before the Exchange probe."""

    image_ref = "registry.example.invalid/cyrene/exchange:fixture"
    image_id = "sha256:" + "a" * 64
    image_ids = runtime_update._locked_image_ids(
        {"images": [{"image": image_ref, "imageId": image_id}], "externalImages": {}}
    )
    assert image_ids == {image_ref: image_id}
    assert stage_transition._service_image(
        {"services": {"exchange": {"image": image_ref}}}, image_ids, "exchange"
    ) == {"imageRef": image_ref, "imageId": image_id}


def test_exchange_image_guard_joins_live_inventory_to_rendered_compose_lock() -> None:
    """Compare the real inventory fields without accepting an alias-only image match."""

    stage_source = ast.parse(Path(stage_transition.__file__).read_text(encoding="utf-8"))
    inventory_function = next(
        node
        for node in stage_source.body
        if isinstance(node, ast.FunctionDef) and node.name == "_live_service_inventory"
    )
    producer_fields = {
        key.value
        for node in ast.walk(inventory_function)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and target.value.id == "by_service"
            for target in node.targets
        )
        and isinstance(node.value, ast.Dict)
        for key in node.value.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    image_ref = "registry.example.invalid/cyrene/exchange:recovered-alias"
    image_id = "sha256:" + "a" * 64
    exchange_service = {
        "containerId": "b" * 64,
        "imageRef": image_ref,
        "imageId": image_id,
        "health": "healthy",
        "networkNames": ["cyrene-net"],
        "mounts": [],
    }
    assert producer_fields == set(exchange_service)
    active = {
        "services": {"exchange": exchange_service},
        "compose": {"services": {"exchange": {"image": image_ref}}},
        "imageLock": {
            "images": [{"image": image_ref, "imageId": image_id}],
            "externalImages": {},
        },
    }
    container_id, expected = deployment_probe._verify_active_exchange_image(active)
    assert container_id == exchange_service["containerId"]
    assert expected == {"imageRef": image_ref, "imageId": image_id}

    changed = json.loads(json.dumps(active))
    changed["services"]["exchange"]["imageId"] = "sha256:" + "c" * 64
    with pytest.raises(deployment_probe.DeploymentProbeError, match="image lock"):
        deployment_probe._verify_active_exchange_image(changed)

    unresolved = json.loads(json.dumps(active))
    unresolved["compose"]["services"]["exchange"]["image"] = (
        "registry.example.invalid/cyrene/exchange:unlocked-alias"
    )
    with pytest.raises(stage_transition.StageTransitionError, match="does not resolve"):
        deployment_probe._verify_active_exchange_image(unresolved)


def _policy() -> dict[str, Any]:
    """Build a private-policy DTO fixture without a real owner or VM path."""

    return {
        "schemaVersion": deployment_probe.POLICY_SCHEMA,
        "owner": {
            "issuer": "https://login.microsoftonline.com/test-tenant/v2.0",
            "subject": _uuid(1),
            "organizationId": _uuid(2),
            "workspaceId": _uuid(3),
        },
        "runtime": {
            "selectionPath": "/private/selection.json",
            "journalDirectory": "/private/journals",
            "outputDirectory": "/private/.artifacts/probe-run",
        },
        "probeTargets": {
            "huggingface": {
                "targetAlias": "hf-read",
                "repoType": "model",
                "repoId": "owner/model",
            },
            "azure": {
                "targetAlias": "azure-resource-group",
                "target": {"resource_group": "resource-group-a"},
            },
            "gcp": {
                "targetAlias": "gcp-zone",
                "target": {"zone": "region-a"},
            },
        },
        "notificationProvenanceRequired": True,
    }


def _ready_snapshot() -> dict[str, Any]:
    """Build the credential-free Exchange readiness DTO shape for unit tests."""

    connections = [
        {
            "connectionRef": _uuid(10),
            "providerId": "huggingface",
            "profileRef": "hf-profile",
            "profileAlias": "hf",
            "accessMode": "workspace_operator",
            "readOnly": True,
            "targetAlias": "hf-read",
            "targetScope": {
                "repoType": "model",
                "repoId": "owner/model",
                "permissions": ["read"],
            },
        },
        {
            "connectionRef": _uuid(11),
            "providerId": "azure",
            "profileRef": "azure-profile",
            "profileAlias": "azure",
            "accessMode": "workspace_operator",
            "readOnly": False,
            "targetAlias": "azure-resource-group",
            "targetScope": {
                "targetAlias": "azure-resource-group",
                "targetRef": "target-ref-a",
            },
        },
        {
            "connectionRef": _uuid(12),
            "providerId": "gcp",
            "profileRef": "gcp-profile",
            "profileAlias": "gcp",
            "accessMode": "workspace_operator",
            "readOnly": False,
            "targetAlias": "gcp-zone",
            "targetScope": {"targetAlias": "gcp-zone", "targetRef": "target-ref-b"},
        },
    ]
    profiles = [
        {
            "providerId": "huggingface",
            "profileRef": "hf-profile",
            "authMode": "device_oauth",
            "targetScope": {"repositories": []},
        },
        {
            "providerId": "azure",
            "profileRef": "azure-profile",
            "authMode": "managed_identity",
            "readOnly": False,
            "targetScope": {"targets": {"azure-resource-group": {"targetRef": "target-ref-a"}}},
        },
        {
            "providerId": "gcp",
            "profileRef": "gcp-profile",
            "authMode": "workload_identity_federation",
            "readOnly": False,
            "targetScope": {"targets": {"gcp-zone": {"targetRef": "target-ref-b"}}},
        },
    ]
    operation_ids = {
        "huggingface": ["huggingface.repo.get"],
        "azure": ["cloud.resources.list", "cloud.provider.health.read"],
        "gcp": ["cloud.resources.list", "cloud.provider.health.read"],
    }
    bindings = []
    catalog_items = []
    for index, connection in enumerate(connections, start=20):
        provider = connection["providerId"]
        binding_id = _uuid(index)
        tools = [
            {"operationId": operation_id, "mutating": False}
            for operation_id in operation_ids[provider]
        ]
        bindings.append(
            {
                "bindingId": binding_id,
                "connectionRef": connection["connectionRef"],
                "providerId": provider,
                "profileRef": connection["profileRef"],
                "tools": tools,
            }
        )
        catalog_items.append(
            {
                "bindingId": binding_id,
                "tools": [
                    {
                        "id": preflight.CONNECTOR_OPERATION_POLICIES[operation_id][0],
                        "readOnly": True,
                    }
                    for operation_id in operation_ids[provider]
                ],
            }
        )
    return {
        "schemaVersion": 1,
        "actor": _policy()["owner"],
        "profiles": profiles,
        "connections": connections,
        "bindings": bindings,
        "catalog": {"schemaVersion": 1, "items": catalog_items},
    }


def _compose_scope() -> dict[str, Any]:
    """Build rendered Compose profile scopes matching the readiness fixture."""

    cloud = {
        "profiles": [
            {
                "provider": "azure",
                "profile_ref": "azure-profile",
                "identity_ref": "azure-identity",
                "read_only": False,
                "scope": {
                    "subscription_id": "subscription-a",
                    "resource_groups": ["resource-group-a"],
                },
            },
            {
                "provider": "gcp",
                "profile_ref": "gcp-profile",
                "identity_ref": "gcp-identity",
                "read_only": False,
                "scope": {
                    "project_id": "project-a",
                    "zones": ["region-a"],
                    "instance_name_prefixes": ["instance-"],
                },
            },
        ]
    }
    return {
        "services": {
            "cloud-management-phase2": {
                "environment": {"CYRENE_CAPABILITY_CONFIGURATION_JSON": json.dumps(cloud)}
            }
        }
    }


def _github_readiness_snapshot() -> dict[str, Any]:
    """Build the ten-repository GitHub readiness shape without live identifiers."""

    profile_ref = "github-profile-fixture"
    repositories = []
    connections = []
    bindings = []
    catalog_items = []
    schema = {
        "type": "object",
        "properties": {
            "repository_id": {"type": "string", "pattern": "^[0-9]+$"},
            "repository": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"},
        },
        "required": ["repository_id", "repository"],
        "additionalProperties": False,
    }
    for index in range(10):
        repository_id = str(9000 + index)
        owner = f"owner{index}"
        name = f"repo{index}"
        repository = f"{owner}/{name}"
        repositories.append({"repositoryId": repository_id, "owner": owner, "name": name})
        connection_ref = _uuid(4000 + index)
        binding_id = _uuid(5000 + index)
        connections.append(
            {
                "connectionRef": connection_ref,
                "providerId": "github",
                "profileRef": profile_ref,
                "readOnly": False,
                "targetScope": {"repositoryId": repository_id, "repository": repository},
            }
        )
        bindings.append(
            {
                "bindingId": binding_id,
                "connectionRef": connection_ref,
                "providerId": "github",
                "profileRef": profile_ref,
                "tools": [
                    {
                        "toolId": "github.repository.get",
                        "operationId": "github.repository.get",
                        "resourceProjection": "github_repository",
                        "mutating": False,
                    }
                ],
            }
        )
        catalog_items.append(
            {
                "bindingId": binding_id,
                "tools": [
                    {
                        "id": "github.repository.get",
                        "readOnly": True,
                        "inputSchema": schema,
                    }
                ],
            }
        )
    return {
        "connections": connections,
        "profiles": [
            {
                "providerId": "github",
                "profileRef": profile_ref,
                "authMode": "github_app",
                "authStatus": "healthy",
                "accessMode": "workspace_operator",
                "targetScope": {"repositories": repositories},
            }
        ],
        "bindings": bindings,
        "catalog": {"items": catalog_items},
    }


def test_policy_schema_is_closed_and_never_accepts_operations_from_input() -> None:
    """Keep owner targets fixed while the operation allowlist remains code-owned."""

    document = _policy()
    assert deployment_probe._validate_policy_document(document) is document

    expanded = json.loads(json.dumps(document))
    expanded["probeTargets"]["azure"]["operationId"] = "cloud.resource.create"
    with pytest.raises(deployment_probe.DeploymentProbeError):
        deployment_probe._validate_policy_document(expanded)

    expanded = json.loads(json.dumps(document))
    expanded["probeTargets"]["runpod"] = {"target": {"pod_id": "pod-a"}}
    with pytest.raises(deployment_probe.DeploymentProbeError):
        deployment_probe._validate_policy_document(expanded)


def test_probe_plan_uses_exact_five_read_only_operations_and_scoped_arguments() -> None:
    """Join the root policy targets to the live readiness and rendered cloud scopes."""

    operations, auth_modes = deployment_probe._build_probe_operations(
        snapshot=_ready_snapshot(),
        compose=_compose_scope(),
        targets=_policy()["probeTargets"],
    )
    assert len(operations) == 5
    assert {(row["providerId"], row["operationId"]) for row in operations} == {
        ("huggingface", "huggingface.repo.get"),
        ("azure", "cloud.resources.list"),
        ("azure", "cloud.provider.health.read"),
        ("gcp", "cloud.resources.list"),
        ("gcp", "cloud.provider.health.read"),
    }
    azure_list = next(
        row
        for row in operations
        if row["providerId"] == "azure" and row["operationId"] == "cloud.resources.list"
    )
    assert azure_list["arguments"]["target"] == {"resource_group": "resource-group-a"}
    gcp_list = next(
        row
        for row in operations
        if row["providerId"] == "gcp" and row["operationId"] == "cloud.resources.list"
    )
    assert gcp_list["arguments"]["target"] == {"zone": "region-a"}
    assert auth_modes == {
        "huggingface": "device_oauth",
        "azure": "managed_identity",
        "gcp": "workload_identity_federation",
    }


def test_probe_plan_blocks_target_or_permission_drift_before_provider_calls() -> None:
    """Reject a scope mismatch and any write-enabled requested operation."""

    policy_targets = json.loads(json.dumps(_policy()["probeTargets"]))
    policy_targets["azure"]["target"]["resource_group"] = "resource-group-outside-scope"
    with pytest.raises(deployment_probe.DeploymentProbeError, match="outside the active"):
        deployment_probe._build_probe_operations(
            snapshot=_ready_snapshot(),
            compose=_compose_scope(),
            targets=policy_targets,
        )

    snapshot = _ready_snapshot()
    azure = next(row for row in snapshot["bindings"] if row["providerId"] == "azure")
    azure["tools"][0]["mutating"] = True
    with pytest.raises(deployment_probe.DeploymentProbeError, match="read-only"):
        deployment_probe._build_probe_operations(
            snapshot=snapshot,
            compose=_compose_scope(),
            targets=_policy()["probeTargets"],
        )

    snapshot = _ready_snapshot()
    azure_catalog = next(
        row
        for row in snapshot["catalog"]["items"]
        if row["bindingId"]
        == next(
            binding["bindingId"]
            for binding in snapshot["bindings"]
            if binding["providerId"] == "azure"
        )
    )
    azure_read_tool_id = preflight.CONNECTOR_OPERATION_POLICIES["cloud.resources.list"][0]
    azure_catalog_tool = next(
        row for row in azure_catalog["tools"] if row["id"] == azure_read_tool_id
    )
    azure_catalog_tool["readOnly"] = False
    with pytest.raises(deployment_probe.DeploymentProbeError, match="catalog"):
        deployment_probe._build_probe_operations(
            snapshot=snapshot,
            compose=_compose_scope(),
            targets=_policy()["probeTargets"],
        )


def test_evidence_builder_emits_only_the_preflight_exchange_api_dto() -> None:
    """Build a schema fixture only; this test performs no external connector call."""

    observed_at = deployment_probe._utc_now()
    digests = {
        provider: f"{index:064x}"
        for index, provider in enumerate(("huggingface", "azure", "gcp"), start=1)
    }
    rows = [
        {
            "providerId": provider,
            "operationId": operation_id,
            "outcome": "SUCCEEDED",
            "observedAt": observed_at,
            "resultDigestSha256": "a" * 64,
        }
        for provider, operation_ids in deployment_probe.PROBE_OPERATIONS.items()
        for operation_id in operation_ids
    ]
    evidence = deployment_probe._make_evidence(
        task_id="deployment-probe-test-run",
        owner=_policy()["owner"]["subject"],
        verified_at=observed_at,
        scope_digests=digests,
        auth_modes={
            "huggingface": "device_oauth",
            "azure": "managed_identity",
            "gcp": "workload_identity_federation",
        },
        rows=rows,
    )
    assert set(evidence) == {
        "schemaVersion",
        "status",
        "evidenceClass",
        "executionPath",
        "taskId",
        "owner",
        "verifiedAt",
        "providers",
    }
    assert evidence["executionPath"] == "exchange_connector_api"
    assert {
        provider["providerId"]: {
            operation["operationId"] for operation in provider["readOperations"]
        }
        for provider in evidence["providers"]
    } == {
        provider: set(operations)
        for provider, operations in deployment_probe.PROBE_OPERATIONS.items()
    }


def _binding(provider: str, number: int) -> dict[str, Any]:
    """Build one exact frozen binding row for health-window set tests."""

    return {
        "bindingId": _uuid(1000 + number),
        "connectionRef": _uuid(2000 + number),
        "providerId": provider,
        "profileRef": f"profile-{provider}",
        "tools": [
            {
                "toolId": "read-tool",
                "operationId": "read.operation",
                "resourceProjection": "profile",
                "mutating": False,
            }
        ],
    }


def test_health_window_expands_the_real_fourteen_binding_provider_shape() -> None:
    """Keep unrelated computer bindings while selecting 10 GitHub plus HF/cloud refs."""

    bindings = [_binding("github", index) for index in range(10)]
    bindings.extend(
        [
            _binding("huggingface", 20),
            _binding("azure", 21),
            _binding("gcp", 22),
            _binding("computer", 23),
        ]
    )
    assert len(bindings) == 14
    selected, refs = deployment_probe._select_health_connections(
        bindings,
        ["github-issues", "huggingface", "cloud-management-phase2"],
    )
    assert len(selected) == 13
    assert len(refs) == 13
    assert {row["providerId"] for row in selected} == {
        "github",
        "huggingface",
        "azure",
        "gcp",
    }
    assert "computer" not in {row["providerId"] for row in selected}
    with pytest.raises(deployment_probe.DeploymentProbeError):
        deployment_probe._select_health_connections(bindings, ["runpod"])
    with pytest.raises(deployment_probe.DeploymentProbeError, match="RunPod"):
        deployment_probe._select_health_connections(
            [*bindings, _binding("runpod", 24)], ["github-issues"]
        )
    with pytest.raises(deployment_probe.DeploymentProbeError):
        deployment_probe._select_health_connections(bindings, ["github-issues", "github-issues"])


def test_readonly_health_window_returns_bounded_state_for_service_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate the wrapper DTO without claiming a real Exchange health result."""

    owner = _policy()["owner"]
    bindings = [_binding("github", index) for index in range(10)]
    bindings.extend(
        [
            _binding("huggingface", 20),
            _binding("azure", 21),
            _binding("gcp", 22),
        ]
    )
    active = {
        "compose": {},
        "services": {"exchange": {"containerId": "a" * 64}},
        "selector": {"configDir": "/private/config"},
        "actor": owner,
    }
    monkeypatch.setattr(deployment_probe.os, "geteuid", lambda: 0)
    monkeypatch.setattr(deployment_probe, "_load_policy", lambda _path: (_policy(), "a" * 64))
    monkeypatch.setattr(deployment_probe, "_load_active_runtime", lambda _policy: active)
    monkeypatch.setattr(
        stage_transition,
        "_candidate_config_json",
        lambda _compose, _name: {"bindings": bindings},
    )
    monkeypatch.setattr(deployment_probe, "_assert_active_runtime_unchanged", lambda *_args: None)

    def checked_stdout(command: list[str], *, label: str, timeout_seconds: int) -> str:
        assert label == "read-only connector authorization health window"
        assert timeout_seconds == 15
        script_index = command.index("-c")
        script = command[script_index + 1]
        assert "PRAGMA query_only=ON" in script
        arguments = command[script_index + 2 :]
        assert arguments[-1] == "900"
        selected = json.loads(arguments[1])
        snapshot = datetime.now(UTC)
        next_check = snapshot + timedelta(minutes=30)
        return json.dumps(
            {
                "selectedConnectionCount": len(selected),
                "monitorStateCount": len(selected),
                "activeMonitorLeaseCount": 0,
                "inFlightConnectorCallCount": 0,
                "nextCheckAt": next_check.isoformat().replace("+00:00", "Z"),
                "snapshotAtUtc": snapshot.isoformat().replace("+00:00", "Z"),
                "nextCheckSecondsRemaining": 1800,
                "minimumLeadSeconds": 900,
            }
        )

    monkeypatch.setattr(runtime_update, "_checked_stdout", checked_stdout)
    receipt = deployment_probe.readonly_health_window(
        Path("/private/deployment-probe-policy.json"),
        targets=["github-issues", "huggingface", "cloud-management-phase2"],
        minimum_lead_seconds=900,
    )
    assert receipt["ready"] is True
    assert receipt["busy"] is False
    assert receipt["selectedConnectionCount"] == 13
    assert receipt["targets"] == ["cloud-management-phase2", "github-issues", "huggingface"]
    assert len(receipt["connectionRefsSha256"]) == 64
    receipt_text = json.dumps(receipt)
    assert all(
        _uuid(number) not in receipt_text for number in [*range(2000, 2010), 2020, 2021, 2022]
    )


def test_notification_provenance_rebinds_current_workspace_source(
    tmp_path: Path,
) -> None:
    """Preserve accepted owner/task lineage while hashing current active config."""

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    notification = {
        "schemaVersion": 1,
        "organizationId": _uuid(2),
        "workspaceId": _uuid(3),
        "owner": {"subject": _uuid(1)},
        "destination": {"channelId": "channel-a", "rootThreadId": "thread-a"},
        "exchange": {"profileRef": "teams-notification-profile"},
    }
    notification_path = config_dir / "teams-credential-notifications.json"
    notification_path.write_text(json.dumps(notification), encoding="utf-8")
    anchor_path = tmp_path / "accepted-anchor.json"
    anchor_path.write_text(json.dumps({"taskId": "accepted-source-task"}), encoding="utf-8")
    exchange_document = {
        "connectorHost": {"teamsMessages": {"connectionRef": "teams-notification-connection"}}
    }
    active = {
        "selector": {"configDir": str(config_dir)},
        "compose": {
            "services": {
                "exchange": {
                    "environment": {
                        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON": json.dumps(
                            exchange_document
                        )
                    }
                }
            }
        },
    }
    current_commit = "b" * 40
    provenance = deployment_probe._build_notification_provenance(
        active=active,
        anchor_path=anchor_path,
        anchor={"taskId": "accepted-source-task"},
        old_provenance={
            "sourceTaskId": "accepted-source-task",
            "sourceWorkspaceCommit": "a" * 40,
        },
        workspace_source_commit=current_commit,
    )
    assert provenance["sourceTaskId"] == "accepted-source-task"
    assert provenance["sourceWorkspaceCommit"] == current_commit
    assert provenance["sourceAnchorSha256"] == deployment_probe._sha256_bytes(
        anchor_path.read_bytes()
    )
    assert provenance["candidateConfigSha256"] == deployment_probe._sha256_bytes(
        notification_path.read_bytes()
    )
    assert provenance["exchangeConnectionRef"] == "teams-notification-connection"
    assert provenance["exchangeProfileRef"] == "teams-notification-profile"


def test_probe_receipt_exposes_only_the_accepted_notification_anchor_path() -> None:
    """Keep the canonical plan anchor available to the subsequent plan builder."""

    anchor_path = Path("/private/.artifacts/accepted-anchor.json")
    active = {
        "selector": {
            "operationId": "00000000-0000-4000-8000-000000000001",
            "imageLock": {"sha256": "a" * 64},
        },
        "scopeDigests": {provider: "b" * 64 for provider in ("huggingface", "azure", "gcp")},
    }
    receipt = deployment_probe._public_summary(
        status="REAL_NATIVE_EXCHANGE_READONLY_PROBE_PASS",
        task_id="deployment-probe-test-run",
        policy_sha256="c" * 64,
        active=active,
        readiness_path=Path("/private/.artifacts/readiness.json"),
        readiness_sha256="d" * 64,
        responses_path=Path("/private/.artifacts/responses.json"),
        responses_sha256="e" * 64,
        evidence_path=Path("/private/.artifacts/evidence.json"),
        evidence_sha256="f" * 64,
        provenance_path=Path("/private/.artifacts/provenance.json"),
        provenance_sha256="0" * 64,
        summary_path=Path("/private/.artifacts/summary.json"),
        summary_sha256="1" * 64,
        notification_anchor_path=anchor_path,
    )
    assert receipt["notificationAnchorPath"] == str(anchor_path)
    assert receipt["taskId"] == "deployment-probe-test-run"
    assert receipt["modelInvoked"] is False
    assert receipt["connectorWritesInvoked"] is False


def test_github_acceptance_joins_all_ten_connections_to_readonly_catalog() -> None:
    """Derive one exact repository.get call from each active GitHub connection."""

    operations = deployment_probe._build_github_read_operations(_github_readiness_snapshot())
    assert len(operations) == 10
    assert len({row["connectionRef"] for row in operations}) == 10
    assert len({row["arguments"]["repository_id"] for row in operations}) == 10
    assert all(row["providerId"] == "github" for row in operations)
    assert all(row["operationId"] == "github.repository.get" for row in operations)
    assert all(row["targetProjection"] == row["arguments"] for row in operations)

    snapshot = _github_readiness_snapshot()
    snapshot["bindings"][0]["tools"][0]["mutating"] = True
    with pytest.raises(deployment_probe.DeploymentProbeError, match="read-only"):
        deployment_probe._build_github_read_operations(snapshot)


def _github_results(operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Build response fixtures whose repo IDs and slugs match their exact bound targets."""

    return {
        "status": "PASS",
        "results": [
            {
                "connectionRef": operation["connectionRef"],
                "providerId": "github",
                "operationId": "github.repository.get",
                "outcome": "verified",
                "observedAt": deployment_probe._utc_now(),
                "result": {
                    **operation["arguments"],
                    "private": True,
                    "default_branch": "main",
                    "html_url": f"https://github.com/{operation['arguments']['repository']}",
                },
            }
            for operation in operations
        ],
    }


def test_github_acceptance_checks_ten_identity_results_by_connection_ref() -> None:
    """Require exactly one matching repository projection for every connection."""

    operations = deployment_probe._build_github_read_operations(_github_readiness_snapshot())
    success, rows, _verified_at = deployment_probe._validate_github_native_results(
        _github_results(operations), operations
    )
    assert success is True
    assert len(rows) == 10
    assert all(row["outcome"] == "SUCCEEDED" for row in rows)
    assert all("connectionRef" not in row for row in rows)

    duplicate = _github_results(operations)
    duplicate["results"][-1]["connectionRef"] = duplicate["results"][0]["connectionRef"]
    with pytest.raises(deployment_probe.DeploymentProbeError, match="uniquely joined"):
        deployment_probe._validate_github_native_results(duplicate, operations)


def test_github_acceptance_rejects_repository_identity_mismatch() -> None:
    """A successful provider envelope is insufficient when its repo projection drifts."""

    operations = deployment_probe._build_github_read_operations(_github_readiness_snapshot())
    envelope = _github_results(operations)
    envelope["results"][0]["result"]["repository_id"] = "999999"
    success, rows, _verified_at = deployment_probe._validate_github_native_results(
        envelope, operations
    )
    assert success is False
    failed = [row for row in rows if row["outcome"] == "FAILED"]
    assert len(failed) == 1
    assert failed[0]["failureCode"] == "REPOSITORY_SCOPE_MISMATCH"


def test_github_repository_result_parser_accepts_only_exact_plugin_projection() -> None:
    """Read the official ToolJsonContent projection from direct or canonical text form."""

    projection = {
        "repository_id": "12345",
        "repository": "owner/project",
        "private": True,
        "default_branch": "main",
        "html_url": "https://github.com/owner/project",
    }
    assert deployment_probe._extract_github_repository_projection(projection) == projection
    assert (
        deployment_probe._extract_github_repository_projection(
            {"isError": False, "content": [{"type": "text", "text": json.dumps(projection)}]}
        )
        == projection
    )
    invalid = {**projection, "repository": "owner/other"}
    with pytest.raises(deployment_probe.DeploymentProbeError):
        deployment_probe._extract_github_repository_projection(
            {
                "structuredContent": projection,
                "content": [{"type": "text", "text": json.dumps(invalid)}],
            }
        )


def test_github_acceptance_api_writes_private_evidence_without_stdout_identifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise receipt wiring with a contract fixture, not an external acceptance claim."""

    policy = _policy()
    policy["runtime"]["outputDirectory"] = str(tmp_path)
    active = {
        "selector": {
            "operationId": _uuid(7000),
            "imageLock": {"sha256": "f" * 64},
        },
        "services": {
            "navigator": {"containerId": "a" * 64},
            "exchange": {"containerId": "b" * 64, "imageId": "sha256:" + "c" * 64},
        },
        "actor": policy["owner"],
    }
    readiness = _github_readiness_snapshot()
    native_input: dict[str, Any] = {}

    def write_private(path: Path, document: dict[str, Any]) -> str:
        raw = deployment_probe._canonical_json(document) + b"\n"
        path.write_bytes(raw)
        path.chmod(0o600)
        return deployment_probe._sha256_bytes(raw)

    def execute_native(
        _container_id: str,
        operations: list[dict[str, Any]],
        _actor: dict[str, str],
        *,
        include_connection_ref: bool,
        timeout_seconds: int,
    ) -> tuple[dict[str, Any], bytes]:
        native_input["operations"] = operations
        assert include_connection_ref is True
        assert timeout_seconds == deployment_probe.MAX_GITHUB_ACCEPTANCE_SECONDS
        envelope = _github_results(operations)
        for row, operation in zip(envelope["results"], operations, strict=True):
            row["connectionRef"] = operation["connectionRef"]
        raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        return envelope, raw

    monkeypatch.setattr(deployment_probe.os, "geteuid", lambda: 0)
    monkeypatch.setattr(deployment_probe, "_load_policy", lambda _path: (policy, "d" * 64))
    monkeypatch.setattr(deployment_probe, "_validate_private_directory", lambda path, **_kw: path)
    monkeypatch.setattr(deployment_probe, "_write_private_json", write_private)
    monkeypatch.setattr(deployment_probe, "_load_active_runtime", lambda _policy: active)
    monkeypatch.setattr(
        deployment_probe, "_read_exchange_readiness", lambda _active: (readiness, "e" * 64)
    )
    monkeypatch.setattr(deployment_probe, "_check_policy_unchanged", lambda *_args: None)
    monkeypatch.setattr(deployment_probe, "_execute_native_probe", execute_native)
    monkeypatch.setattr(deployment_probe, "_assert_active_runtime_unchanged", lambda *_args: None)

    receipt = deployment_probe.run_github_read_acceptance(
        tmp_path / "policy.json", run_id="fixture-run"
    )
    assert receipt["status"] == "REAL_NATIVE_GITHUB_READONLY_PROBE_PASS"
    assert receipt["connectionCount"] == 10
    assert receipt["activeOperationId"] == _uuid(7000)
    public_text = json.dumps(receipt)
    assert "owner0/repo0" not in public_text
    assert _uuid(4000) not in public_text
    evidence = json.loads(Path(receipt["evidencePath"]).read_text())
    assert evidence["connectionCount"] == 10
    assert len(evidence["results"]) == 10
    responses = json.loads(Path(receipt["responsesPath"]).read_text())
    raw_output = base64.b64decode(responses["rawResponseBase64"])
    assert deployment_probe._sha256_bytes(raw_output) == receipt["rawResponseSha256"]
    assert b"owner0/repo0" in raw_output
    assert len(native_input["operations"]) == 10


def test_health_window_sql_is_read_only_and_checks_all_selected_providers(
    tmp_path: Path,
) -> None:
    """Run the exact metadata query on a temporary DB and verify it changes nothing."""

    database = tmp_path / "integrations.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE integration_connections (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            actor_issuer TEXT NOT NULL,
            actor_subject TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            document TEXT NOT NULL
        );
        CREATE TABLE authorization_monitor_state (
            connection_id TEXT PRIMARY KEY,
            next_check_at TEXT NOT NULL,
            claim_owner TEXT,
            lease_expires_at TEXT
        );
        CREATE TABLE connector_invocations (
            organization_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            actor_issuer TEXT NOT NULL,
            actor_subject TEXT NOT NULL,
            connection_ref TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            state TEXT NOT NULL
        );
        """
    )
    actor = _policy()["owner"]
    provider_names = {
        "github": "github",
        "huggingface": "hf",
        "azure": "azure",
        "gcp": "gcp",
    }
    selected = []
    future = (datetime.now(UTC) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    for number, provider in enumerate(provider_names, start=1):
        reference = _uuid(3000 + number)
        selected.append({"providerId": provider, "connectionRef": reference})
        connection.execute(
            "INSERT INTO integration_connections VALUES(?,?,?,?,?,?,?)",
            (
                reference,
                actor["organizationId"],
                actor["workspaceId"],
                actor["issuer"],
                actor["subject"],
                1,
                json.dumps(
                    {
                        "id": reference,
                        "provider": provider_names[provider],
                        "enabled": True,
                        "organizationId": actor["organizationId"],
                        "workspaceId": actor["workspaceId"],
                        "actorIssuer": actor["issuer"],
                        "actorSubject": actor["subject"],
                    }
                ),
            ),
        )
        connection.execute(
            "INSERT INTO authorization_monitor_state VALUES(?,?,?,?)",
            (reference, future, None, None),
        )
    connection.commit()
    before = connection.execute(
        "SELECT connection_id,next_check_at,claim_owner,lease_expires_at "
        "FROM authorization_monitor_state ORDER BY connection_id"
    ).fetchall()
    connection.close()

    command = [
        sys.executable,
        "-c",
        deployment_probe._health_window_sql_script(),
        str(database),
        json.dumps(selected, sort_keys=True, separators=(",", ":")),
        actor["issuer"],
        actor["subject"],
        actor["organizationId"],
        actor["workspaceId"],
        "900",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["selectedConnectionCount"] == 4
    assert report["monitorStateCount"] == 4
    assert report["activeMonitorLeaseCount"] == 0
    assert report["inFlightConnectorCallCount"] == 0
    assert report["minimumLeadSeconds"] == 900
    assert report["nextCheckSecondsRemaining"] > 900
    assert "connectionRef" not in result.stdout

    connection = sqlite3.connect(database)
    after = connection.execute(
        "SELECT connection_id,next_check_at,claim_owner,lease_expires_at "
        "FROM authorization_monitor_state ORDER BY connection_id"
    ).fetchall()
    connection.close()
    assert after == before
