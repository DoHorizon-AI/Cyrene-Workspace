"""Focused updater journal and gate sequence for Package Runtime admission."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
UPDATES_PATH = WORKSPACE_ROOT / "packaging" / "component_updates.py"
SPEC = importlib.util.spec_from_file_location("package_runtime_transaction_test", UPDATES_PATH)
assert SPEC is not None and SPEC.loader is not None
updates = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = updates
SPEC.loader.exec_module(updates)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    catalog_path = tmp_path / "component-catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "generation": 1,
                "defaultChannel": "stable",
                "channels": {"stable": {}, "preview": {}},
                "components": [],
                "targets": [],
                "publishers": [],
            }
        ),
        encoding="utf-8",
    )
    updater = updates.ComponentUpdater(
        catalog_path=catalog_path,
        activity_catalog_path=tmp_path / "activity-sources.json",
        state_root=tmp_path / "updater-state",
        install_root=tmp_path / "install",
        broker_path=tmp_path / "missing-broker",
        trusted_catalog_digest=None,
        load_active_catalog=False,
    )
    updater.activity_catalog_path.write_text("{}", encoding="utf-8")
    source = {
        "source_id": "cyrene-yield",
        "uid": 1000,
        "gid": 998,
        "source_token_sha256": "a" * 64,
    }
    initial_catalog = {"schema_version": 1, "generation": 7, "sources": [source]}
    scope = {
        "binding_id": "yield.llama-factory.primary",
        "package_id": "cyrene.training.llama-factory",
        "installation_ids": ["installation-" + "1" * 32],
        "operations": ["activate", "deactivate", "recover"],
    }
    updated_catalog = {
        "schema_version": 1,
        "generation": 8,
        "sources": [{**source, "binding_scopes": [scope]}],
    }
    record = {
        "record_version": 1,
        "installation_id": "installation-" + "1" * 32,
        "package_id": "cyrene.training.llama-factory",
        "package_version": "0.1.0",
        "artifact_digest": "sha256:" + "b" * 64,
        "archive_digest": "sha256:" + "c" * 64,
        "capabilities": ["training.llama-factory.v1"],
        "state": "INSTALLED",
        "verification": {},
        "dependencies": {},
        "installed_at_unix_ms": 123,
    }
    candidate = SimpleNamespace(
        package_id=record["package_id"],
        artifact_digest=record["artifact_digest"],
        attestation_bundle_digests={"signed-subject": "sha256:" + "9" * 64},
    )
    plan = SimpleNamespace(
        plan_id="plan-" + "d" * 32,
        plan_digest="sha256:" + "e" * 64,
        catalog_generation=7,
        gate_generation=10,
        component_artifact_digests={candidate.package_id: candidate.artifact_digest},
        material={"package_id": candidate.package_id},
    )
    update = SimpleNamespace(
        source_arguments=("--source", "cyrene-yield=1000:998"),
        binding_scopes={"cyrene-yield": [scope]},
        source_identity={
            "cyrene-yield": {
                "uid": 1000,
                "gid": 998,
                "source_token_sha256": "a" * 64,
            }
        },
        expected_generation=8,
        changed=True,
    )
    helper = SimpleNamespace(
        verify_cached_package_candidate=lambda: candidate,
        build_package_bootstrap_plan=lambda *_args, **_kwargs: plan,
        validate_package_bootstrap_confirmation=lambda *_args: None,
        build_offline_install_input=lambda *_args, **_kwargs: {},
        stage_offline_install_request=lambda *_args, **_kwargs: (
            tmp_path / "stage",
            tmp_path / "request.json",
        ),
        run_offline_install=lambda *_args, **_kwargs: record,
        build_activity_catalog_update=lambda *_args, **_kwargs: update,
        stage_catalog_commit_inputs=lambda *_args, **_kwargs: (
            tmp_path / "proof.json",
            tmp_path / "scopes.json",
        ),
        run_activity_catalog_update=lambda *_args, **_kwargs: {
            "schema_version": 1,
            "generation": 8,
            "sources": [
                {
                    "source_id": "cyrene-yield",
                    "token_file": "/etc/cyrene/runtime-activity-source-tokens/cyrene-yield.token",
                    "binding_scope_count": 1,
                }
            ],
        },
        validate_activity_catalog_readback=lambda catalog, *_args: catalog,
        build_runtime_source_policy=lambda *_args: {"schema_version": 1},
        write_runtime_source_policy=lambda _policy: "sha256:" + "f" * 64,
        read_runtime_source_policy=lambda: {"schema_version": 1},
        validate_runtime_source_policy=lambda policy, *_args: policy,
        probe_runtime_authority=lambda *_args, **_kwargs: {
            "authority": "platform_package_runtime",
            "protocol_version": "cy-package-runtime.control.v1",
            "catalog_generation": 8,
            "capabilities": ["cy-package-runtime.binding-operation-admission.v1"],
        },
    )
    monkeypatch.setattr(updater, "_load_native_package_runtime_bootstrap", lambda: helper)
    monkeypatch.setattr(updater, "_require_authorized_process", lambda: None)
    monkeypatch.setattr(updates, "_running_as_root", lambda: True)
    monkeypatch.setattr(updates, "_verify_private_directory", lambda *_args: None)
    monkeypatch.setattr(updater, "_assert_no_pending_catalog_intent", lambda: None)
    systemd_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        updater,
        "_run_systemctl",
        lambda operation, unit: systemd_calls.append((operation, unit)),
    )
    monkeypatch.setattr(
        updater, "_wait_unit_active", lambda unit: systemd_calls.append(("active", unit))
    )
    monkeypatch.setattr(
        updater,
        "_readiness_for",
        lambda *_args, **_kwargs: {
            "status": "READY",
            "gate_generation": 10,
            "install_catalog_generation": 7,
            "active_task_count": 0,
            "active_tasks": [],
            "active_worker_count": 0,
            "active_allocation_count": 0,
            "inflight_runtime_admission_count": 0,
            "unknown_activity_sources": [],
            "requires_restart_confirmation": False,
        },
    )
    calls: list[tuple[str, dict[str, Any]]] = []
    hold_result: dict[str, Any] = {}

    def broker_request(method: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        calls.append((method, params))
        if method == "BeginMaintenance":
            return {
                "status": "MAINTENANCE_ACTIVE",
                "maintenance_token": "t" * 40,
                "gate_generation": 11,
            }
        if method == "ValidateMaintenanceHold":
            return hold_result or {
                "valid": True,
                "request_id": params["request_id"],
                "target_kind": params["target_kind"],
                "plan_id": params["plan_id"],
                "plan_digest": params["plan_digest"],
                "component_artifact_digests": params["component_artifact_digests"],
                "component_id": params["component_id"],
                "artifact_digest": params["artifact_digest"],
                "gate_generation": params["expected_gate_generation"],
                "catalog_generation": params["expected_catalog_generation"],
            }
        raise AssertionError(method)

    monkeypatch.setattr(updater, "_broker_request", broker_request)
    catalog_reads = 0

    def activity_catalog() -> tuple[dict[str, Any], list[str]]:
        nonlocal catalog_reads
        catalog_reads += 1
        if catalog_reads <= 2:
            return initial_catalog, ["cyrene-yield"]
        updater.activity_catalog_path.write_text(json.dumps(updated_catalog), encoding="utf-8")
        return updated_catalog, ["cyrene-yield"]

    monkeypatch.setattr(updater, "_activity_catalog", activity_catalog)
    ended: list[dict[str, Any]] = []
    monkeypatch.setattr(
        updater, "_end_maintenance", lambda transaction, **_kwargs: ended.append(transaction)
    )
    return {
        "updater": updater,
        "candidate": candidate,
        "plan": plan,
        "calls": calls,
        "ended": ended,
        "hold_result": hold_result,
        "state_root": tmp_path / "updater-state",
        "systemd_calls": systemd_calls,
        "helper": helper,
    }


def test_package_bootstrap_persists_each_verified_phase_and_ends_exact_hold(
    harness: dict[str, Any],
) -> None:
    plan = harness["plan"]
    result = harness["updater"].bootstrap_plugin_package_runtime(
        plan_id=plan.plan_id,
        plan_digest=plan.plan_digest,
        confirmation=True,
    )

    journal = json.loads(
        (harness["state_root"] / "transactions" / f"{plan.plan_id}.json").read_text()
    )
    assert result["status"] == "installed"
    assert result["runtimeActivated"] is False
    assert journal["phase"] == "succeeded"
    assert "maintenanceToken" not in journal
    assert [method for method, _params in harness["calls"]] == [
        "BeginMaintenance",
        "ValidateMaintenanceHold",
    ]
    assert harness["systemd_calls"] == [
        ("start", "cyrene-package-runtime.service"),
        ("active", "cyrene-package-runtime.service"),
    ]
    assert journal["runtimeAuthority"]["catalog_generation"] == 8
    assert len(harness["ended"]) == 1


def test_package_bootstrap_leaves_unknown_hold_validation_pending(
    harness: dict[str, Any],
) -> None:
    plan = harness["plan"]
    harness["hold_result"].update({"valid": False})
    with pytest.raises(updates.UpdateError, match="maintenance remains held"):
        harness["updater"].bootstrap_plugin_package_runtime(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            confirmation=True,
        )

    journal = json.loads(
        (harness["state_root"] / "transactions" / f"{plan.plan_id}.json").read_text()
    )
    assert journal["phase"] == "hold_validation_unknown"
    assert journal["maintenanceToken"] == "t" * 40
    assert harness["ended"] == []


def test_package_bootstrap_keeps_hold_when_runtime_authority_is_unknown(
    harness: dict[str, Any],
) -> None:
    plan = harness["plan"]

    def reject_runtime_authority(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("authority generation/capability mismatch")

    harness["helper"].probe_runtime_authority = reject_runtime_authority
    with pytest.raises(updates.UpdateError, match="maintenance remains held"):
        harness["updater"].bootstrap_plugin_package_runtime(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            confirmation=True,
        )

    journal = json.loads(
        (harness["state_root"] / "transactions" / f"{plan.plan_id}.json").read_text()
    )
    assert journal["phase"] == "runtime_health_unknown"
    assert journal["maintenanceToken"] == "t" * 40
    assert harness["ended"] == []
    assert [method for method, _params in harness["calls"]] == ["BeginMaintenance"]


def test_package_bootstrap_keeps_hold_when_runtime_service_cannot_start(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = harness["plan"]

    def reject_start(*_args: Any) -> None:
        raise updates.UpdateError("SYSTEMD_FAILED", "service start failed")

    monkeypatch.setattr(harness["updater"], "_run_systemctl", reject_start)
    with pytest.raises(updates.UpdateError, match="maintenance remains held"):
        harness["updater"].bootstrap_plugin_package_runtime(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            confirmation=True,
        )

    journal = json.loads(
        (harness["state_root"] / "transactions" / f"{plan.plan_id}.json").read_text()
    )
    assert journal["phase"] == "runtime_health_unknown"
    assert journal["maintenanceToken"] == "t" * 40
    assert harness["ended"] == []
    assert [method for method, _params in harness["calls"]] == ["BeginMaintenance"]


def test_package_bootstrap_operations_expose_no_candidate_paths_or_tokens(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = harness["updater"]
    monkeypatch.setattr(updater, "inspect_plugin_package_plan", lambda: {"planId": "plan-x"})
    request = {"protocolVersion": updates.PROTOCOL_VERSION, "operation": "check-plugin-package"}
    assert updater.handle(request)["result"] == {"planId": "plan-x"}
    rejected = updater.handle({**request, "releasePath": "/tmp/untrusted"})
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "INVALID_REQUEST"
    captured: dict[str, Any] = {}

    def install(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "pending"}

    monkeypatch.setattr(updater, "bootstrap_plugin_package_runtime", install)
    install_request = {
        "protocolVersion": updates.PROTOCOL_VERSION,
        "operation": "install-plugin-package",
        "planId": "plan-id",
        "planDigest": "sha256:" + "a" * 64,
        "confirmation": True,
    }
    assert updater.handle(install_request)["result"] == {"status": "pending"}
    assert captured == {
        "plan_id": "plan-id",
        "plan_digest": "sha256:" + "a" * 64,
        "confirmation": True,
    }
    rejected = updater.handle({**install_request, "candidate": {"path": "/tmp/untrusted"}})
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "INVALID_REQUEST"
