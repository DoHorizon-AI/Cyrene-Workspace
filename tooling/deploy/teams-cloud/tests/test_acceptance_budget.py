"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 test_acceptance_budget.py                                            │
│  Module: tooling.deploy.teams_cloud.tests.test_acceptance_budget        │
│  Role: Check cumulative spend and cleanup guards without provider calls. │
│                                                                          │
│  模块职责：不调用供应商，验证累计预算和资源清理门禁。                    │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
budget = importlib.import_module("acceptance_budget")
maintainers = importlib.import_module("maintainer_subjects")


TASK_ID = "cloud-task-20261023"
MARKER = f"cyrene-teams-cloud-{TASK_ID}"


def _action(argv: list[str], timeout: int = 60) -> dict[str, Any]:
    """Return one bounded fake RunPod CLI call."""

    return {"argv": argv, "timeoutSeconds": timeout}


def _list_readback(*, expected: bool = False) -> dict[str, Any]:
    """Return the RunPod list query, which filters the exact owner marker locally."""

    value = {
        "argv": ["runpodctl", "pod", "list", "--all", "--output", "json"],
        "timeoutSeconds": 60,
        "itemsPointer": "",
        "markerPointer": "/name",
        "markerValue": MARKER,
        "referencePointer": "/id",
        "markerInArgv": False,
    }
    if expected:
        value.update({"expectedPointer": "/name", "expectedValue": MARKER})
    return value


def _plan(tmp_path: Path, *, upper_bound: float = 1.0) -> tuple[Path, Path]:
    """Write a complete synthetic RunPod plan and preceding Azure/GCP receipt."""

    readback = _list_readback(expected=True)
    resource = {
        "id": "runpod-pod-001",
        "provider": "runpod",
        "maxLifetimeSeconds": 900,
        "ownership": {"taskId": TASK_ID, "owner": "verified-owner", "marker": MARKER},
        "pricing": {
            "currency": "USD",
            "upperBoundUsd": upper_bound,
            "quoteRef": "https://prices.runpod.io/verified-cpu-pod",
            "quotedAt": datetime.now(UTC).isoformat(),
        },
        "create": _action(
            [
                "runpodctl",
                "pod",
                "create",
                "--name",
                MARKER,
                "--compute-type",
                "cpu",
                "--image",
                "ubuntu:22.04",
            ],
            120,
        ),
        "readback": _list_readback(),
        "exercise": [
            {
                "name": "read_pod_detail",
                "action": _action(["runpodctl", "pod", "get", "{resource_ref}"]),
                "readback": readback,
            }
        ],
        "cleanup": _action(["runpodctl", "pod", "delete", "{resource_ref}"], 120),
        "cleanupReadback": _list_readback(),
    }
    plan_path = tmp_path / "plan.json"
    previous_path = tmp_path / "previous.json"
    plan_path.write_text(
        json.dumps(
            {
                "schemaVersion": "cyrene-cloud-acceptance-plan-v1",
                "taskId": TASK_ID,
                "budgetUsd": 5.0,
                "stage": "runpod",
                "owner": "verified-owner",
                "resources": [resource],
            }
        ),
        encoding="utf-8",
    )
    plan_path.chmod(0o600)
    previous_path.write_text(
        json.dumps(
            {
                "schemaVersion": "cyrene-cloud-acceptance-ledger-v1",
                "evidenceClass": "REAL_VENDOR_CLI",
                "stage": "azure_gcp",
                "status": "VERIFIED",
                "taskId": TASK_ID,
                "owner": "verified-owner",
                "previousEvidenceSha256": "c" * 64,
                "verifiedAt": datetime.now(UTC).isoformat(),
                "cleanupVerifiedAt": datetime.now(UTC).isoformat(),
                "cumulativeReservedUsd": 4.0,
                "resources": {
                    "azure-resource": {
                        "provider": "azure",
                        "state": "DELETED",
                        "ownership": {
                            "taskId": TASK_ID,
                            "owner": "verified-owner",
                            "marker": f"cyrene-teams-cloud-{TASK_ID}-azure",
                        },
                        "events": [
                            {
                                "action": "verify-cleanup-absence",
                                "outcome": "EXITED",
                                "exitCode": 0,
                            }
                        ],
                    },
                    "gcp-resource": {
                        "provider": "gcp",
                        "state": "DELETED",
                        "ownership": {
                            "taskId": TASK_ID,
                            "owner": "verified-owner",
                            "marker": f"cyrene-teams-cloud-{TASK_ID}-gcp",
                        },
                        "events": [
                            {
                                "action": "verify-cleanup-absence",
                                "outcome": "EXITED",
                                "exitCode": 0,
                            }
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    previous_path.chmod(0o600)
    return plan_path, previous_path


def _set_runpod_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the synthetic RunPod plan to one task-prefixed Pod namespace."""

    monkeypatch.setenv(
        "CYRENE_CAPABILITY_CONFIGURATION_JSON",
        json.dumps(
            {
                "profiles": [
                    {
                        "provider": "runpod",
                        "scope": {"pod_name_prefixes": ["cyrene-teams-cloud-"]},
                    }
                ]
            }
        ),
    )


def test_runpod_reservation_carries_previous_stage_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a RunPod plan whose verified Azure/GCP spend would exceed USD 5."""

    plan_path, previous_path = _plan(tmp_path, upper_bound=1.01)
    _set_runpod_profile(monkeypatch)
    monkeypatch.setattr(budget, "_is_private_artifact", lambda _path: True)
    monkeypatch.setattr(budget.shutil, "which", lambda _name: "/usr/bin/runpodctl")

    with pytest.raises(budget.BudgetError, match="cumulative reserved upper bound"):
        budget.validate_plan(
            plan_path,
            previous_evidence=previous_path,
            now=datetime.now(UTC),
        )


def test_current_deployment_task_rejects_cli_resource_mutation_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep all billable lifecycle writes on the single formal DSH API budget path."""

    plan_path, previous_path = _plan(tmp_path)
    document = json.loads(plan_path.read_text(encoding="utf-8"))
    document["taskId"] = "teams-cloud-20261023"
    plan_path.write_text(json.dumps(document), encoding="utf-8")
    plan_path.chmod(0o600)
    monkeypatch.setattr(budget, "_is_private_artifact", lambda _path: True)
    _set_runpod_profile(monkeypatch)
    monkeypatch.setattr(budget.shutil, "which", lambda _name: "/usr/bin/runpodctl")

    with pytest.raises(budget.BudgetError, match="formal DSH API path"):
        budget.validate_plan(
            plan_path,
            previous_evidence=previous_path,
            now=datetime.now(UTC),
        )


def test_runpod_unfiltered_list_requires_local_exact_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allow a vendor list query only when the read-back filters by its exact marker."""

    plan_path, previous_path = _plan(tmp_path)
    _set_runpod_profile(monkeypatch)
    monkeypatch.setattr(budget, "_is_private_artifact", lambda _path: True)
    monkeypatch.setattr(budget.shutil, "which", lambda _name: "/usr/bin/runpodctl")
    plan = budget.validate_plan(
        plan_path,
        previous_evidence=previous_path,
        now=datetime.now(UTC),
    )
    assert plan["stage"] == "runpod"

    document = json.loads(plan_path.read_text(encoding="utf-8"))
    document["resources"][0]["readback"]["markerValue"] = "another-task"
    plan_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(budget.BudgetError, match="must match its owner marker"):
        budget.validate_plan(
            plan_path,
            previous_evidence=previous_path,
            now=datetime.now(UTC),
        )


def test_cleanup_attempts_every_independent_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One failed deletion must not prevent cleanup attempts for other resources."""

    attempted: list[str] = []

    def cleanup_one(resource: dict[str, Any], _ledger: dict[str, Any], _path: Path) -> None:
        attempted.append(resource["id"])
        if resource["id"] == "resource-b":
            raise budget.BudgetError("simulated cleanup failure")

    monkeypatch.setattr(budget, "_cleanup_resource", cleanup_one)
    plan = {"resources": [{"id": "resource-a"}, {"id": "resource-b"}]}
    with pytest.raises(budget.BudgetError, match="resource-b"):
        budget.cleanup_resources(plan, {}, tmp_path / "ledger.json")
    assert attempted == ["resource-b", "resource-a"]


def test_cloud_cli_auth_uses_isolated_profiles_and_file_backed_runpod_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prepare each fixed CLI with workload auth without putting credentials in argv."""

    azure_client_id = "00000000-0000-4000-8000-000000000001"
    subscription_id = "00000000-0000-4000-8000-000000000002"
    gcp_config = tmp_path / "gcp-wif.json"
    gcp_config.write_text("{}", encoding="utf-8")
    gcp_config.chmod(0o400)
    runpod_key = tmp_path / "runpod-api-key"
    runpod_key.write_text("synthetic-runpod-api-key", encoding="utf-8")
    runpod_key.chmod(0o400)
    state_dir = tmp_path / "cloud-state"
    state_dir.mkdir(mode=0o700)
    profiles = {
        "profiles": [
            {
                "provider": "azure",
                "auth": {"mode": "managed_identity", "client_id": azure_client_id},
                "scope": {"subscription_id": subscription_id},
            },
            {
                "provider": "gcp",
                "scope": {"project_id": "cyrene-project", "zones": ["us-central1-a"]},
            },
        ]
    }
    monkeypatch.setenv("CYRENE_CAPABILITY_CONFIGURATION_JSON", json.dumps(profiles))
    monkeypatch.setenv("AZURE_CLIENT_ID", azure_client_id)
    monkeypatch.setenv("CYRENE_CLOUD_MGMT_AZURE_SUBSCRIPTION_ID", subscription_id)
    monkeypatch.setenv("CYRENE_CLOUD_MGMT_GCP_WIF_CONFIG", str(gcp_config))
    monkeypatch.setenv("CYRENE_CLOUD_MGMT_RUNPOD_API_KEY_FILE", str(runpod_key))
    monkeypatch.setenv("CYRENE_CLOUD_MGMT_STATE_DIR", str(state_dir))
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(budget.subprocess, "run", fake_run)
    with budget._cloud_cli_state():
        azure_env = budget._cli_environment("az")
        gcp_env = budget._cli_environment("gcloud")
        runpod_env = budget._cli_environment("runpodctl")

    assert calls[0][:5] == ["az", "login", "--identity", "--client-id", azure_client_id]
    assert calls[1][:4] == ["az", "account", "set", "--subscription"]
    assert calls[1][4] == subscription_id
    assert azure_env["AZURE_CONFIG_DIR"].startswith(str(state_dir))
    assert gcp_env["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"] == str(gcp_config)
    assert gcp_env["CLOUDSDK_CORE_PROJECT"] == "cyrene-project"
    assert runpod_env["RUNPOD_API_KEY"] == "synthetic-runpod-api-key"
    assert all("synthetic-runpod-api-key" not in "\0".join(argv) for argv in calls)


def test_provider_scope_checks_bind_dynamic_resource_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require Azure IDs and GCP names to remain inside the exact host scopes."""

    subscription = "11111111-2222-3333-4444-555555555555"
    marker = "cyrene-teams-cloud-task-123"
    monkeypatch.setenv(
        "CYRENE_CAPABILITY_CONFIGURATION_JSON",
        json.dumps(
            {
                "profiles": [
                    {
                        "provider": "azure",
                        "scope": {
                            "subscription_id": subscription,
                            "resource_groups": ["cyrene-test-rg"],
                        },
                    },
                    {
                        "provider": "gcp",
                        "scope": {
                            "project_id": "cyrene-test-project",
                            "zones": ["us-central1-a"],
                            "instance_name_prefixes": ["cyrene-teams-cloud-"],
                        },
                    },
                ]
            }
        ),
    )
    azure_resource = {
        "id": "azure-test-resource",
        "provider": "azure",
        "ownership": {"marker": marker},
    }
    azure_id = (
        f"/subscriptions/{subscription}/resourceGroups/cyrene-test-rg/"
        "providers/Microsoft.Storage/storageAccounts/cyreneacceptance"
    )
    assert budget._validate_resource_reference(azure_resource, azure_id) == azure_id
    with pytest.raises(budget.BudgetError, match="outside the bound"):
        budget._validate_resource_reference(
            azure_resource,
            "/subscriptions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/resourceGroups/other/"
            "providers/Microsoft.Storage/storageAccounts/unsafe",
        )

    gcp_resource = {"id": "gcp-test-resource", "provider": "gcp", "ownership": {"marker": marker}}
    gcp_create = [
        "gcloud",
        "--project",
        "cyrene-test-project",
        "compute",
        "instances",
        "create",
        marker,
        "--zone",
        "us-central1-a",
        "--labels",
        f"cyrene-task={marker}",
    ]
    budget._validate_cli_scope("gcp", gcp_create, marker)
    assert budget._validate_resource_reference(gcp_resource, marker) == marker
    with pytest.raises(budget.BudgetError, match="exact owner name, label"):
        budget._validate_cli_scope(
            "gcp", gcp_create[:6] + ["unowned-name"] + gcp_create[7:], marker
        )


def test_provider_cli_scope_fails_closed_without_explicit_gcp_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not let gcloud inherit a project outside the plan's explicit scope."""

    monkeypatch.setenv(
        "CYRENE_CAPABILITY_CONFIGURATION_JSON",
        json.dumps(
            {
                "profiles": [
                    {
                        "provider": "gcp",
                        "scope": {
                            "project_id": "cyrene-test-project",
                            "zones": ["us-central1-a"],
                            "instance_name_prefixes": ["cyrene-teams-cloud-"],
                        },
                    }
                ]
            }
        ),
    )
    with pytest.raises(budget.BudgetError, match="bound project"):
        budget._validate_cli_scope(
            "gcp",
            ["gcloud", "compute", "instances", "list", "--format=json"],
            MARKER,
        )


def test_cli_output_is_capped_before_it_can_fill_process_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep a provider's unexpectedly large response within the fixed capture budget."""

    monkeypatch.setattr(budget, "_CLI_CONFIG_ROOT", tmp_path)
    monkeypatch.setattr(budget, "_cli_environment", lambda _executable: {})
    result = budget._execute(
        [
            sys.executable,
            "-c",
            f"import os; os.write(1, b'x' * {budget.MAX_CAPTURE_BYTES + 1024})",
        ],
        timeout=10,
    )

    assert result["outcome"] == "OUTPUT_LIMIT_EXCEEDED"
    assert result["stdoutBytes"] == budget.MAX_CAPTURE_BYTES
    assert len(result["stdout"]) == budget.MAX_CAPTURE_BYTES


def test_owner_can_restore_missing_projection_but_cannot_revoke_self(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permit owner recovery and serialize the normalized identity projection."""

    operator = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    monkeypatch.setenv("CYRENE_WORKSPACE_ID", "workspace-test")
    monkeypatch.setenv("CYRENE_ORGANIZATION_ID", "organization-test")
    monkeypatch.setenv("CYRENE_AUTHORITY_AAD_TENANT_ID", "11111111-2222-4333-8444-555555555555")
    monkeypatch.setenv("CYRENE_WORKSPACE_DIRECTORY_OPERATOR_ID", operator)
    calls: list[list[str]] = []

    def fake_directory(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(maintainers.subprocess, "run", fake_directory)
    path = tmp_path / "teams-actor-subjects.json"
    result = maintainers.apply_change("add", operator.upper(), "restore owner", path)

    assert result["status"] == "UPDATED"
    assert calls[0][calls[0].index("--subject") + 1] == operator
    assert json.loads(path.read_text(encoding="utf-8"))["subjects"] == [operator]
    assert path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "teams-actor-subjects.json.lock").exists()
    with pytest.raises(maintainers.MaintainerError, match="cannot revoke its own"):
        maintainers.apply_change("revoke", operator, "restore owner", path)
