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
PACKAGE_BOOTSTRAP_PATH = WORKSPACE_ROOT / "packaging" / "native_package_runtime_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("package_runtime_transaction_test", UPDATES_PATH)
assert SPEC is not None and SPEC.loader is not None
updates = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = updates
SPEC.loader.exec_module(updates)
PACKAGE_SPEC = importlib.util.spec_from_file_location(
    "package_runtime_plan_transaction_test", PACKAGE_BOOTSTRAP_PATH
)
assert PACKAGE_SPEC is not None and PACKAGE_SPEC.loader is not None
package_bootstrap = importlib.util.module_from_spec(PACKAGE_SPEC)
sys.modules[PACKAGE_SPEC.name] = package_bootstrap
PACKAGE_SPEC.loader.exec_module(package_bootstrap)


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
    cohort_sources = [
        {
            "source_id": f"cyrene-{service}",
            "uid": 1000,
            "gid": 998,
            "source_token_sha256": f"{index:x}" * 64,
        }
        for index, service in enumerate(
            ("navigator", "yield", "reactor", "exchange", "catalyst"), start=1
        )
    ]
    cohort_sources.sort(key=lambda item: item["source_id"])
    initial_catalog["sources"] = cohort_sources
    scope = {
        "binding_id": "yield.llama-factory.primary",
        "package_id": "cyrene.training.llama-factory",
        "installation_ids": ["installation-" + "1" * 32],
        "operations": ["activate", "deactivate", "recover"],
    }
    updated_catalog = {
        "schema_version": 1,
        "generation": 8,
        "sources": [
            {**item, "binding_scopes": [scope] if item["source_id"] == "cyrene-yield" else []}
            for item in cohort_sources
        ],
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
    environment_projection_inputs: list[dict[str, Any]] = []

    def project_environment(
        projected_candidate: Any,
        installation_record: Any,
        projected_catalog: dict[str, Any],
        source_policy: dict[str, Any],
        *,
        previous_catalog_generation: int,
    ) -> Any:
        environment_projection_inputs.append(
            {
                "candidate": projected_candidate,
                "installation": installation_record,
                "catalog_generation": projected_catalog["generation"],
                "policy": source_policy,
                "previous_catalog_generation": previous_catalog_generation,
            }
        )
        return SimpleNamespace(changed=True)

    plan = SimpleNamespace(
        plan_id="plan-" + "d" * 32,
        plan_digest="sha256:" + "e" * 64,
        catalog_generation=7,
        gate_generation=10,
        component_artifact_digests={candidate.package_id: candidate.artifact_digest},
        material={"package_id": candidate.package_id},
    )
    update = SimpleNamespace(
        source_arguments=tuple(
            argument
            for item in cohort_sources
            for argument in ("--source", f"{item['source_id']}={item['uid']}:{item['gid']}")
        ),
        binding_scopes={"cyrene-yield": [scope]},
        source_identity={
            item["source_id"]: {
                "uid": item["uid"],
                "gid": item["gid"],
                "source_token_sha256": item["source_token_sha256"],
            }
            for item in cohort_sources
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
        validate_installation_record=lambda _candidate, value: value,
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
        ensure_yield_product_package_environment=project_environment,
    )
    monkeypatch.setattr(updater, "_load_native_package_runtime_bootstrap", lambda: helper)
    monkeypatch.setattr(updater, "_require_authorized_process", lambda: None)
    monkeypatch.setattr(updates, "_running_as_root", lambda: True)
    monkeypatch.setattr(updates, "_verify_private_directory", lambda *_args: None)
    monkeypatch.setattr(updater, "_assert_no_pending_catalog_intent", lambda: None)
    product_units = [
        {
            "source_id": f"cyrene-{service}",
            "component_id": f"cyrene-{service}",
            "unit": f"cyrene-{service}.service",
            "service": service,
            "uid": 1000,
            "gid": 998,
            "source_token_sha256": f"{index:x}" * 64,
        }
        for index, service in enumerate(
            ("navigator", "yield", "reactor", "exchange", "catalyst"), start=1
        )
    ]
    verified_products = [
        {"service": row["service"], "componentId": row["component_id"]} for row in product_units
    ]
    active_services = {"navigator", "yield", "catalyst"}
    active_state = {
        row["unit"]: ("active" if row["service"] in active_services else "inactive")
        for row in product_units
    }
    live_checks: list[str] = []
    first_products = SimpleNamespace(
        _live_product=lambda _updater, product: live_checks.append(product["service"]),
        _main_pid=lambda _updater, unit: 101 if active_state[unit] == "active" else 0,
    )
    monkeypatch.setattr(
        updater,
        "_package_bootstrap_product_units",
        lambda _catalog, _helper: (first_products, product_units, verified_products),
    )
    monkeypatch.setattr(
        updater,
        "_package_product_unit_state",
        lambda unit: active_state[unit],
    )
    monkeypatch.setattr(
        updater,
        "_capture_package_product_activity",
        lambda *_args: [
            {"source_id": row["source_id"], "unit": row["unit"]}
            for row in product_units
            if row["service"] in active_services
        ],
    )
    monkeypatch.setattr(
        updater,
        "_wait_package_products_quiesced",
        lambda *_args: None,
    )
    monkeypatch.setattr(updater, "_verify_package_activity_generation", lambda _generation: None)
    systemd_calls: list[tuple[str, str]] = []

    def run_systemctl(operation: str, unit: str) -> None:
        systemd_calls.append((operation, unit))
        if unit in active_state and operation == "stop":
            active_state[unit] = "inactive"
        elif unit in active_state and operation == "start":
            active_state[unit] = "active"

    monkeypatch.setattr(updater, "_run_systemctl", run_systemctl)
    monkeypatch.setattr(
        updater, "_wait_unit_active", lambda unit: systemd_calls.append(("active", unit))
    )
    gate_status = ["READY"]

    def readiness(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "status": gate_status[0],
            "gate_generation": 10,
            "install_catalog_generation": 8 if catalog_reads > 2 else 7,
            "active_task_count": 0,
            "active_tasks": [],
            "active_worker_count": 0,
            "active_allocation_count": 0,
            "inflight_runtime_admission_count": 0,
            "unknown_activity_sources": [],
            "requires_restart_confirmation": False,
        }

    monkeypatch.setattr(updater, "_readiness_for", readiness)
    calls: list[tuple[str, dict[str, Any]]] = []
    hold_result: dict[str, Any] = {}

    def broker_request(method: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        calls.append((method, params))
        if method == "BeginMaintenance":
            gate_status[0] = "MAINTENANCE_ACTIVE"
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

    def end_maintenance(transaction: dict[str, Any], **_kwargs: Any) -> None:
        assert all(
            active_state[row["unit"]]
            == ("active" if row["service"] in active_services else "inactive")
            for row in product_units
        )
        ended.append(transaction)
        gate_status[0] = "READY"

    monkeypatch.setattr(updater, "_end_maintenance", end_maintenance)
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
        "product_units": product_units,
        "active_state": active_state,
        "live_checks": live_checks,
        "active_services": active_services,
        "gate_status": gate_status,
        "environment_projection_inputs": environment_projection_inputs,
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
    assert [item["unit"] for item in journal["priorActiveProducts"]] == [
        "cyrene-navigator.service",
        "cyrene-yield.service",
        "cyrene-catalyst.service",
    ]
    assert [method for method, _params in harness["calls"]] == [
        "BeginMaintenance",
        "ValidateMaintenanceHold",
    ]
    assert harness["systemd_calls"] == [
        ("stop", "cyrene-navigator.service"),
        ("stop", "cyrene-yield.service"),
        ("stop", "cyrene-catalyst.service"),
        ("start", "cyrene-package-runtime.service"),
        ("active", "cyrene-package-runtime.service"),
        ("start", "cyrene-navigator.service"),
        ("active", "cyrene-navigator.service"),
        ("start", "cyrene-yield.service"),
        ("active", "cyrene-yield.service"),
        ("start", "cyrene-catalyst.service"),
        ("active", "cyrene-catalyst.service"),
    ]
    assert harness["active_state"]["cyrene-reactor.service"] == "inactive"
    assert harness["active_state"]["cyrene-exchange.service"] == "inactive"
    assert journal["productEnvironmentGeneration"] == 8
    assert harness["environment_projection_inputs"][0]["previous_catalog_generation"] == 7
    assert harness["environment_projection_inputs"][0]["catalog_generation"] == 8
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

    def reject_start(operation: str, unit: str) -> None:
        if operation == "start" and unit == "cyrene-package-runtime.service":
            raise updates.UpdateError("SYSTEMD_FAILED", "service start failed")
        if unit in harness["active_state"] and operation == "stop":
            harness["active_state"][unit] = "inactive"

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


def test_package_bootstrap_keeps_hold_when_shared_generation_projection_fails(
    harness: dict[str, Any],
) -> None:
    plan = harness["plan"]

    def reject_projection(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("projection readback mismatch")

    harness["helper"].ensure_yield_product_package_environment = reject_projection
    with pytest.raises(updates.UpdateError, match="maintenance remains held"):
        harness["updater"].bootstrap_plugin_package_runtime(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            confirmation=True,
        )

    journal = json.loads(
        (harness["state_root"] / "transactions" / f"{plan.plan_id}.json").read_text()
    )
    assert journal["phase"] == "product_environment_unknown"
    assert harness["ended"] == []
    assert [method for method, _params in harness["calls"]] == ["BeginMaintenance"]
    assert set(harness["active_state"].values()) == {"inactive"}


def test_package_bootstrap_keeps_hold_when_restored_product_health_fails(
    harness: dict[str, Any],
) -> None:
    plan = harness["plan"]
    first_products, _units, _verified = harness["updater"]._package_bootstrap_product_units(
        {}, harness["helper"]
    )

    def fail_health(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("signed Product API health failed")

    first_products._live_product = fail_health
    with pytest.raises(updates.UpdateError, match="maintenance remains held"):
        harness["updater"].bootstrap_plugin_package_runtime(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            confirmation=True,
        )

    journal = json.loads(
        (harness["state_root"] / "transactions" / f"{plan.plan_id}.json").read_text()
    )
    assert journal["phase"] == "product_health_pending"
    assert harness["ended"] == []
    assert [method for method, _params in harness["calls"]] == ["BeginMaintenance"]


def test_package_bootstrap_resumes_pending_clients_without_reinstall_or_new_hold(
    harness: dict[str, Any],
) -> None:
    plan = harness["plan"]
    first_products, _units, _verified = harness["updater"]._package_bootstrap_product_units(
        {}, harness["helper"]
    )
    fail_once = [True]

    def health(_updater: Any, product: dict[str, Any]) -> None:
        if fail_once[0]:
            fail_once[0] = False
            raise RuntimeError("temporary signed Product API health failure")
        harness["live_checks"].append(product["service"])

    first_products._live_product = health
    with pytest.raises(updates.UpdateError, match="maintenance remains held"):
        harness["updater"].bootstrap_plugin_package_runtime(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            confirmation=True,
        )
    journal_path = harness["state_root"] / "transactions" / f"{plan.plan_id}.json"
    assert json.loads(journal_path.read_text())["phase"] == "product_health_pending"

    result = harness["updater"].bootstrap_plugin_package_runtime(
        plan_id=plan.plan_id,
        plan_digest=plan.plan_digest,
        confirmation=True,
    )
    journal = json.loads(journal_path.read_text())
    assert result["status"] == "installed"
    assert journal["phase"] == "succeeded"
    assert [method for method, _params in harness["calls"]] == [
        "BeginMaintenance",
        "ValidateMaintenanceHold",
    ]
    assert harness["ended"]
    assert harness["helper"]


def test_package_bootstrap_end_success_retry_is_restore_only(
    harness: dict[str, Any],
) -> None:
    updater = harness["updater"]
    plan = harness["plan"]
    original_restore = updater._restore_package_product_activity
    restore_calls = [0]

    def fail_once_after_end(*args: Any, **kwargs: Any) -> None:
        restore_calls[0] += 1
        if restore_calls[0] == 2:
            raise RuntimeError("client exited during End transition")
        original_restore(*args, **kwargs)

    updater._restore_package_product_activity = fail_once_after_end
    with pytest.raises(updates.UpdateError, match="package state is committed"):
        updater.bootstrap_plugin_package_runtime(
            plan_id=plan.plan_id,
            plan_digest=plan.plan_digest,
            confirmation=True,
        )

    journal_path = harness["state_root"] / "transactions" / f"{plan.plan_id}.json"
    assert json.loads(journal_path.read_text())["phase"] == "clients_restore_pending"
    assert len(harness["ended"]) == 1
    prior_systemd_call_count = len(harness["systemd_calls"])

    result = updater.bootstrap_plugin_package_runtime(
        plan_id=plan.plan_id,
        plan_digest=plan.plan_digest,
        confirmation=True,
    )
    journal = json.loads(journal_path.read_text())
    assert result["status"] == "installed"
    assert journal["phase"] == "succeeded"
    assert len(harness["ended"]) == 1
    assert [method for method, _params in harness["calls"]].count("BeginMaintenance") == 1
    assert len(harness["systemd_calls"]) == prior_systemd_call_count


def test_package_bootstrap_confirmation_binds_exact_five_product_sources_and_units() -> None:
    candidate = SimpleNamespace(
        package_id="cyrene.training.llama-factory",
        package_version="0.1.0",
        capability="training.llama-factory.v1",
        interface_version="1",
        source_ref="refs/heads/develop",
        source_commit="7b8315d2f88cc7147cb9f58d48c89d0dabe7bb36",
        release_digest="sha256:" + "1" * 64,
        descriptor_digest="sha256:" + "2" * 64,
        manifest_digest="sha256:" + "3" * 64,
        artifact_digest="sha256:" + "4" * 64,
        archive_digest="sha256:" + "5" * 64,
        dependency_lock_digest="sha256:" + "6" * 64,
        preparer_wheel_digest="sha256:" + "7" * 64,
        attestation_bundle_digests={"archive": "sha256:" + "8" * 64},
    )
    rows = [
        {
            "source_id": source_id,
            "component_id": source_id,
            "unit": unit,
            "service": service,
            "uid": 999,
            "gid": 998,
            "source_token_sha256": f"{index:x}" * 64,
        }
        for index, (source_id, unit, service) in enumerate(
            package_bootstrap.PACKAGE_BOOTSTRAP_PRODUCT_SOURCES, start=1
        )
    ]
    reviewed = package_bootstrap.build_package_bootstrap_plan(
        candidate,
        catalog_generation=7,
        gate_generation=10,
        affected_product_units=rows,
    )
    changed = [dict(row) for row in rows]
    changed[-1]["source_token_sha256"] = "f" * 64
    reapplied = package_bootstrap.build_package_bootstrap_plan(
        candidate,
        catalog_generation=7,
        gate_generation=10,
        affected_product_units=changed,
    )
    assert reviewed.plan_digest != reapplied.plan_digest
    with pytest.raises(package_bootstrap.PackageRuntimeBootstrapError):
        package_bootstrap.validate_package_bootstrap_confirmation(
            reviewed,
            {
                "plan_id": reapplied.plan_id,
                "plan_digest": reapplied.plan_digest,
                "confirmed": True,
            },
        )
    foreign = [dict(row) for row in rows]
    foreign[0]["unit"] = "foreign.service"
    with pytest.raises(package_bootstrap.PackageRuntimeBootstrapError):
        package_bootstrap.build_package_bootstrap_plan(
            candidate,
            catalog_generation=7,
            gate_generation=10,
            affected_product_units=foreign,
        )


def test_package_bootstrap_derives_sources_from_signed_products_and_rejects_catalog_drift(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = harness["updater"]
    helper = package_bootstrap
    rows = [
        {
            "source_id": source_id,
            "uid": 999,
            "gid": 998,
            "source_token_sha256": f"{index:x}" * 64,
        }
        for index, (source_id, _unit, _service) in enumerate(
            helper.PACKAGE_BOOTSTRAP_PRODUCT_SOURCES, start=1
        )
    ]
    products = [
        {"componentId": source_id, "service": service}
        for source_id, _unit, service in helper.PACKAGE_BOOTSTRAP_PRODUCT_SOURCES
    ]
    components = {
        source_id: {"systemdUnit": unit}
        for source_id, unit, _service in helper.PACKAGE_BOOTSTRAP_PRODUCT_SOURCES
    }
    updater.components = components
    signed = SimpleNamespace(
        RECEIPT_PATH=Path("/fixed/receipt.json"),
        _read_receipt=lambda: {"installer": {"targetId": "linux-ubuntu-22.04-x86_64-systemd"}},
        _verified_products=lambda *_args: (object(), products),
        _unit_path=lambda _updater, component, service: (Path(service), b"trusted-unit"),
    )
    monkeypatch.setattr(updater, "_load_native_first_products_bootstrap", lambda: signed)
    catalog = {"sources": sorted(rows, key=lambda item: item["source_id"])}
    _module, product_rows, verified = updates.ComponentUpdater._package_bootstrap_product_units(
        updater, catalog, helper
    )
    assert [row["unit"] for row in product_rows] == [
        unit for _source, unit, _service in helper.PACKAGE_BOOTSTRAP_PRODUCT_SOURCES
    ]
    assert len(verified) == 5

    with pytest.raises(updates.UpdateError, match="exactly match"):
        updates.ComponentUpdater._package_bootstrap_product_units(
            updater, {"sources": catalog["sources"][:-1]}, helper
        )
    with pytest.raises(updates.UpdateError, match="exactly match"):
        updates.ComponentUpdater._package_bootstrap_product_units(
            updater,
            {"sources": [*catalog["sources"], {**rows[0], "source_id": "foreign-source"}]},
            helper,
        )


def test_shared_activity_environment_readback_requires_exact_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "runtime-activity-sources.env"
    path.write_text("CYRENE_RUNTIME_ACTIVITY_CATALOG_GENERATION=8\n", encoding="ascii")
    real_info = path.lstat()
    current_content = [b"CYRENE_RUNTIME_ACTIVITY_CATALOG_GENERATION=8\n"]
    monkeypatch.setattr(updates, "DEFAULT_PACKAGE_ACTIVITY_ENVIRONMENT", path)
    monkeypatch.setattr(updates.os, "open", lambda *_args, **_kwargs: 8192)
    monkeypatch.setattr(
        updates.os,
        "fstat",
        lambda _descriptor: SimpleNamespace(
            st_mode=updates.stat.S_IFREG | 0o644,
            st_uid=0,
            st_gid=0,
            st_nlink=1,
            st_dev=real_info.st_dev,
            st_ino=real_info.st_ino,
            st_size=len(current_content[0]),
        ),
    )
    monkeypatch.setattr(updates.os, "read", lambda _descriptor, _limit: current_content[0])
    monkeypatch.setattr(updates.os, "close", lambda _descriptor: None)

    updates.ComponentUpdater._verify_package_activity_generation(8)
    current_content[0] = b"CYRENE_RUNTIME_ACTIVITY_CATALOG_GENERATION=7\n"
    with pytest.raises(updates.UpdateError, match="does not match the committed catalog"):
        updates.ComponentUpdater._verify_package_activity_generation(8)


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
