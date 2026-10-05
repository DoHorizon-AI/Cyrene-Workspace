"""Focused offline tests for the two-host placement admission lifecycle.

These cases exercise the updater's real lifecycle methods with a temporary
state directory and a deterministic Broker boundary.  They never contact a
host service or mutate machine state.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


updates = _load_module(
    "component_updates_adoption_lifecycle", ROOT / "packaging/component_updates.py"
)
placement = _load_module(
    "component_placement_adoption_lifecycle", ROOT / "packaging/component_placement.py"
)


def _updater(tmp_path: Path) -> Any:
    """Create an updater whose durable state is isolated under ``tmp_path``."""
    return updates.ComponentUpdater(
        catalog_path=updates.DEFAULT_CATALOG,
        activity_catalog_path=tmp_path / "activity-sources.json",
        state_root=tmp_path / "state",
        install_root=tmp_path / "install",
        release_lock_path=tmp_path / "missing-release-lock.json",
        broker_path=tmp_path / "missing-broker",
        load_active_catalog=False,
    )


def _context(role_id: str) -> tuple[Any, dict[str, Any]]:
    """Build the fixed signed role context used by the placement module."""
    catalog = json.loads(updates.DEFAULT_CATALOG.read_text(encoding="utf-8"))
    group = next(
        item for item in catalog["compatibilityGroups"] if item["groupId"] == "workspace-product-v2"
    )
    roles = {
        row["roleId"]: placement.DeploymentRole(
            role_id=row["roleId"],
            required_members=frozenset(row["requiredMembers"]),
            allowed_members=frozenset(row["allowedMembers"]),
        )
        for row in group["deploymentRoles"]
    }
    peer_role_id = "connector-host" if role_id == "control-host" else "control-host"
    context = SimpleNamespace(
        host_id=f"{role_id}-node",
        role_id=role_id,
        peer=SimpleNamespace(host_id=f"{peer_role_id}-node", role_id=peer_role_id),
        peer_role=roles[peer_role_id],
        local_role=roles[role_id],
        deployment_id="deployment-fixture",
        topology_digest="sha256:" + "a" * 64,
        local_config_digest="sha256:" + "b" * 64,
        workspace_identity={
            "organizationId": "org-cyrene-test",
            "workspaceId": "workspace-cyrene-test",
            "authorityInstanceId": "authority-cyrene-test",
        },
        catalog_digest=updates.TRUSTED_CATALOG_DIGEST,
        group_id="workspace-product-v2",
        group_version="2",
        contract_lock=group["contractLock"],
    )
    return context, group


def _fixture(tmp_path: Path, role_id: str) -> dict[str, Any]:
    """Build one exact role-local staged plan and its updater fixture."""
    updater = _updater(tmp_path)
    context, group = _context(role_id)
    plan_id = "plan-" + "1" * 32
    plan_digest = "sha256:" + "2" * 64
    desired = [
        {
            "componentId": component_id,
            "version": "0.1.0",
            "manifestDigest": "sha256:" + "a" * 64,
            "artifactDigest": "sha256:" + "b" * 64,
            "targetId": "linux-test",
            "releaseId": "preview-" + "c" * 40,
            "sourceCommit": "c" * 40,
            "sourceRepository": "DoHorizon-AI/Cyrene-Workspace",
            "channel": "preview",
        }
        for component_id in sorted(context.local_role.required_members)
    ]
    plan = {
        "planId": plan_id,
        "planDigest": plan_digest,
        "channel": "preview",
        "deploymentPlacement": [placement.plan_binding(context)],
    }
    record = {
        "plan": plan,
        "channel": "preview",
        "components": [
            {
                "componentId": item["componentId"],
                "version": item["version"],
                "manifestDigest": item["manifestDigest"],
                "artifactDigest": item["artifactDigest"],
                "restartGroup": "workspace-product-v2",
            }
            for item in desired
        ],
    }
    return {
        "updater": updater,
        "context": context,
        "group": group,
        "planId": plan_id,
        "planDigest": plan_digest,
        "desired": desired,
        "plan": plan,
        "record": record,
        "confirmation": {"planId": plan_id, "planDigest": plan_digest, "confirmed": True},
    }


def _catalog() -> tuple[dict[str, Any], list[str]]:
    """Return a valid one-source catalog and its Broker source set."""
    return (
        {
            "schema_version": 1,
            "generation": 17,
            "sources": [
                {
                    "source_id": "cyrene-yield",
                    "uid": 1001,
                    "gid": 1001,
                    "source_token_sha256": "d" * 64,
                }
            ],
        },
        ["cyrene-yield"],
    )


def _readiness(status: str = "READY") -> dict[str, Any]:
    """Return an idle CORE_RUNTIME readiness snapshot for focused tests."""
    return {
        "status": status,
        "gate_generation": 23,
        "install_catalog_generation": 17,
        "active_task_count": 0,
        "active_tasks": [],
        "inflight_runtime_admission_count": 0,
        "active_binding_operation_count": 0,
        "active_binding_operations": [],
        "unknown_activity_sources": [],
        "active_worker_count": 0,
        "active_allocation_count": 0,
        "blocker_codes": [],
        "requires_restart_confirmation": True,
    }


def _install_prepare_fixture(fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch only external state reads while preserving prepare's real flow."""
    updater = fixture["updater"]
    context = fixture["context"]
    desired = fixture["desired"]
    group = fixture["group"]
    record = fixture["record"]
    plan = fixture["plan"]
    peer = SimpleNamespace(phase="STAGED")
    binding = {"peerEvidenceDigest": "sha256:" + "e" * 64}
    monkeypatch.setattr(updater, "_require_authorized_process", lambda: None)
    monkeypatch.setattr(updater, "_reload_catalog_for_operation", lambda: None)
    monkeypatch.setattr(
        updater,
        "_staged_placement_record",
        lambda *_args: (
            record,
            fixture["updater"].state_root / "stage.json",
            plan,
            placement,
            context,
            group,
            desired,
        ),
    )
    monkeypatch.setattr(updater, "_revalidate_plan_placement", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        updater,
        "_placement_peer_evidence",
        lambda *_args, **_kwargs: (placement, context, peer, binding),
    )
    monkeypatch.setattr(
        updater,
        "_placement_local_snapshot",
        lambda *_args: {
            "phase": "STAGED",
            "activeComponents": [],
            "stagedComponents": [{"componentId": item["componentId"]} for item in desired],
            "transaction": {"present": False, "phase": "none"},
        },
    )
    monkeypatch.setattr(updater, "_placement_service_inventory", lambda *_args, **_kwargs: [])


def _adoption(fixture: dict[str, Any], *, phase: str, token: str = "t" * 48) -> dict[str, Any]:
    """Return a protected connector hold record bound to the fixture plan."""
    context = fixture["context"]
    desired = fixture["desired"]
    return {
        "schemaVersion": 1,
        "groupId": "workspace-product-v2",
        "deploymentId": context.deployment_id,
        "roleId": context.role_id,
        "topologyDigest": context.topology_digest,
        "localConfigDigest": context.local_config_digest,
        "catalogDigest": context.catalog_digest,
        "planId": fixture["planId"],
        "planDigest": fixture["planDigest"],
        "desiredComponents": desired,
        "componentArtifactDigests": {
            item["componentId"]: item["artifactDigest"] for item in desired
        },
        "peerEvidenceDigest": "sha256:" + "e" * 64,
        "peerRoleId": context.peer.role_id,
        "peerHostId": context.peer.host_id,
        "requestId": "cyrene-update-" + fixture["planId"],
        "targetKind": "CORE_RUNTIME",
        "expectedGateGeneration": 23,
        "expectedCatalogGeneration": 17,
        "expectedActivitySources": ["cyrene-yield"],
        "gateGeneration": 23,
        "catalogGeneration": 17,
        "maintenanceToken": token,
        "phase": phase,
    }


def _valid_broker_result(method: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
    """Synthesize the exact response shape expected by the real Broker client path."""
    if method == "BeginMaintenance":
        return {
            "status": "MAINTENANCE_ACTIVE",
            "maintenance_token": "t" * 48,
            "gate_generation": 23,
            "blocker_codes": [],
        }
    if method == "ValidateMaintenanceHold":
        return {
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
    pytest.fail(f"unexpected Broker method {method}")


def test_connector_prepare_begins_exact_core_hold_and_peer_receipt_omits_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Begin and per-component validation share the stable request identity."""
    fixture = _fixture(tmp_path, "connector-host")
    updater = fixture["updater"]
    _install_prepare_fixture(fixture, monkeypatch)
    catalog, source_ids = _catalog()
    monkeypatch.setattr(updates, "_running_as_root", lambda: True)
    monkeypatch.setattr(updater, "_activity_catalog", lambda: (catalog, source_ids))
    monkeypatch.setattr(updater, "_readiness_for", lambda *_args, **_kwargs: _readiness())
    calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def broker(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append((method, dict(params), dict(kwargs)))
        return _valid_broker_result(method, params, **kwargs)

    monkeypatch.setattr(updater, "_broker_request", broker)
    prepared = updater.prepare_placement_adoption(
        fixture["planId"], fixture["planDigest"], fixture["confirmation"]
    )

    assert prepared["status"] == "prepared_held"
    begin_calls = [call for call in calls if call[0] == "BeginMaintenance"]
    validate_calls = [call for call in calls if call[0] == "ValidateMaintenanceHold"]
    assert len(begin_calls) == 1
    assert len(validate_calls) == len(fixture["desired"])
    begin_params = begin_calls[0][1]
    request_id = "cyrene-update-" + fixture["planId"]
    assert begin_params == {
        "request_id": request_id,
        "target_kind": "CORE_RUNTIME",
        "requires_restart": True,
        "expected_catalog_generation": 17,
        "expected_activity_sources": source_ids,
        "expected_gate_generation": 23,
        "user_confirmed_restart": True,
        "plan_id": fixture["planId"],
        "plan_digest": fixture["planDigest"],
        "component_artifact_digests": {
            item["componentId"]: item["artifactDigest"] for item in fixture["desired"]
        },
    }
    assert all(call[1]["request_id"] == request_id for call in validate_calls)
    assert all(call[2]["request_id"] == request_id for call in validate_calls)
    assert {call[1]["component_id"] for call in validate_calls} == {
        item["componentId"] for item in fixture["desired"]
    }
    assert all(call[1]["target_kind"] == "CORE_RUNTIME" for call in validate_calls)

    # Peer evidence proves the live hold but must never serialize the secret token.
    snapshot = {
        "phase": "PREPARED_HELD",
        "planId": fixture["planId"],
        "planDigest": fixture["planDigest"],
        "desiredComponents": fixture["desired"],
        "stagedComponents": [{"componentId": item["componentId"]} for item in fixture["desired"]],
        "activeComponents": [],
        "activePointer": {"present": False, "digest": None, "components": []},
        "transaction": {"present": False, "phase": "none"},
        "services": [],
    }
    monkeypatch.setattr(updater, "_load_component_placement", lambda: placement)
    monkeypatch.setattr(placement, "load_placement", lambda *_args, **_kwargs: fixture["context"])
    monkeypatch.setattr(
        placement, "plan_binding", lambda _context: fixture["plan"]["deploymentPlacement"][0]
    )
    monkeypatch.setattr(updater, "_placement_read_lock", lambda: nullcontext())
    monkeypatch.setattr(updater, "_verify_placement_catalog_readonly", lambda: None)
    monkeypatch.setattr(updater, "_placement_local_snapshot", lambda *_args: snapshot)
    response = updater.placement_peer_response(
        operation="placement-peer-receipt",
        deployment_id=fixture["context"].deployment_id,
        requester_host_id=fixture["context"].peer.host_id,
        requester_role_id=fixture["context"].peer.role_id,
        group_id="workspace-product-v2",
        nonce="f" * 64,
    )
    encoded = json.dumps(response, sort_keys=True)
    assert "maintenanceToken" not in encoded
    assert "t" * 48 not in encoded
    assert response["businessAdmission"]["source"] == "runtime-maintenance.ValidateMaintenanceHold"


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [("ACTIVE_TASKS", "ACTIVE_TASKS"), ("UNKNOWN", "GATE_UNKNOWN")],
)
def test_connector_prepare_refuses_busy_or_unknown_readiness_before_begin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected_code: str,
) -> None:
    """No token or pending journal is created when CORE_RUNTIME is not known idle."""
    fixture = _fixture(tmp_path, "connector-host")
    updater = fixture["updater"]
    _install_prepare_fixture(fixture, monkeypatch)
    catalog, source_ids = _catalog()
    monkeypatch.setattr(updates, "_running_as_root", lambda: True)
    monkeypatch.setattr(updater, "_activity_catalog", lambda: (catalog, source_ids))
    readiness = _readiness(status)
    if status == "ACTIVE_TASKS":
        readiness["active_task_count"] = 1
        readiness["active_tasks"] = [
            {"source_id": "cyrene-yield", "task_id": "task-1", "state": "RUNNING"}
        ]
    monkeypatch.setattr(updater, "_readiness_for", lambda *_args, **_kwargs: readiness)
    monkeypatch.setattr(
        updater,
        "_broker_request",
        lambda *_args, **_kwargs: pytest.fail(
            "Begin must not be called for busy or unknown readiness"
        ),
    )

    with pytest.raises(updates.UpdateError) as raised:
        updater.prepare_placement_adoption(
            fixture["planId"], fixture["planDigest"], fixture["confirmation"]
        )

    assert raised.value.code == expected_code
    assert not updater._placement_adoption_path().exists()


def _prepare_apply_fixture(
    fixture: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, dict[str, Any]]]:
    """Install the deterministic connector hold and held-apply seams."""
    updater = fixture["updater"]
    context = fixture["context"]
    group = fixture["group"]
    record = fixture["record"]
    plan = fixture["plan"]
    adoption = _adoption(fixture, phase="PREPARED_HELD")
    updater._write_placement_adoption(adoption)
    peer = SimpleNamespace(phase="PREPARED_HELD")
    monkeypatch.setattr(updates, "_running_as_root", lambda: True)
    monkeypatch.setattr(updater, "_require_managed_services", lambda _components: None)
    monkeypatch.setattr(updater, "_revalidate_plan_placement", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(updater, "_placement_for_plan", lambda _plan: (placement, context, group))
    monkeypatch.setattr(updater, "_capture_active_versions", lambda _components: [])
    monkeypatch.setattr(updater, "_prepare_authority_activation", lambda *_args: {})
    monkeypatch.setattr(
        updater,
        "_placement_peer_evidence",
        lambda *_args, **_kwargs: (
            placement,
            context,
            peer,
            {"peerEvidenceDigest": "sha256:" + "e" * 64},
        ),
    )
    monkeypatch.setattr(updater, "_activate_transaction", lambda _transaction: None)
    monkeypatch.setattr(updater, "_restart_transaction", lambda _transaction: None)
    monkeypatch.setattr(updater, "_health_transaction", lambda _transaction: None)
    monkeypatch.setattr(updater, "_activate_authority_bundle", lambda _transaction: None)
    monkeypatch.setattr(updater, "_applied_result", lambda _transaction: {"status": "applied"})
    monkeypatch.setattr(updater, "_verify_local_active_held", lambda *_args, **_kwargs: None)
    calls: list[tuple[str, dict[str, Any]]] = []

    def broker(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append((method, dict(params)))
        return _valid_broker_result(method, params, **kwargs)

    monkeypatch.setattr(updater, "_broker_request", broker)
    transaction_path = (
        updater._private_state_directory("transactions") / f"{fixture['planId']}.json"
    )
    fixture["transaction_path"] = transaction_path
    fixture["adoption"] = adoption
    fixture["record"] = record
    fixture["plan"] = plan
    return calls


def test_connector_held_apply_reuses_existing_hold_without_begin_or_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Applying bytes within a prepared adoption keeps the same admission hold."""
    fixture = _fixture(tmp_path, "connector-host")
    updater = fixture["updater"]
    calls = _prepare_apply_fixture(fixture, monkeypatch)

    result = updater._apply_placement_held(
        fixture["record"],
        fixture["transaction_path"],
        None,
        fixture["adoption"],
        fixture["confirmation"],
    )

    assert result == {"status": "applied", "adoptionPhase": "ACTIVE_HELD"}
    assert updater._read_placement_adoption(required=True)["phase"] == "ACTIVE_HELD"
    assert "BeginMaintenance" not in {method for method, _params in calls}
    assert "EndMaintenance" not in {method for method, _params in calls}
    assert {method for method, _params in calls} == {"ValidateMaintenanceHold"}
    assert all(params["request_id"] == fixture["adoption"]["requestId"] for _, params in calls)


def test_failed_connector_held_apply_keeps_token_and_does_not_release_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rolled-back apply returns to PREPARED_HELD without calling End."""
    fixture = _fixture(tmp_path, "connector-host")
    updater = fixture["updater"]
    calls = _prepare_apply_fixture(fixture, monkeypatch)
    monkeypatch.setattr(
        updater,
        "_activate_transaction",
        lambda _transaction: (_ for _ in ()).throw(RuntimeError("activation fixture failure")),
    )
    monkeypatch.setattr(
        updater, "_rollback_transaction", lambda _transaction: (True, "fixture rollback")
    )

    with pytest.raises(updates.UpdateError, match="Placement apply failed"):
        updater._apply_placement_held(
            fixture["record"],
            fixture["transaction_path"],
            None,
            fixture["adoption"],
            fixture["confirmation"],
        )

    held = updater._read_placement_adoption(required=True)
    assert held["phase"] == "PREPARED_HELD"
    assert held["maintenanceToken"] == fixture["adoption"]["maintenanceToken"]
    assert "EndMaintenance" not in {method for method, _params in calls}
    assert (
        json.loads(fixture["transaction_path"].read_text(encoding="utf-8"))["phase"]
        == "rolled_back-held"
    )


def test_control_prepare_uses_control_gate_without_local_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A control-host prepare closes its own dispatch gate without a local Broker."""
    fixture = _fixture(tmp_path, "control-host")
    updater = fixture["updater"]
    _install_prepare_fixture(fixture, monkeypatch)
    state: dict[str, Any] = {}
    monkeypatch.setattr(
        updater,
        "_control_admission_identity",
        lambda _context: (10001, fixture["context"].workspace_identity),
    )
    monkeypatch.setattr(
        updater,
        "_write_control_admission_state",
        lambda _context, *, plan_id, plan_digest, phase: (
            state.update(
                {
                    "state": "closed",
                    "generation": 1,
                    "planDigest": plan_digest,
                    "adoptionHold": {"phase": phase, "planDigest": plan_digest},
                }
            )
            or dict(state)
        ),
    )
    monkeypatch.setattr(updater, "_read_control_admission_state", lambda _context: dict(state))
    monkeypatch.setattr(
        updater,
        "_activity_catalog",
        lambda: pytest.fail("control-host prepare must not read the connector activity catalog"),
    )
    monkeypatch.setattr(
        updater,
        "_broker_request",
        lambda *_args, **_kwargs: pytest.fail("control-host prepare must not call a local Broker"),
    )
    monkeypatch.setattr(
        updater,
        "_begin_maintenance",
        lambda *_args: pytest.fail("control-host prepare must not begin Kernel maintenance"),
    )

    prepared = updater.prepare_placement_adoption(
        fixture["planId"], fixture["planDigest"], fixture["confirmation"]
    )

    assert prepared["status"] == "prepared_held"
    assert state["state"] == "closed"
    assert state["adoptionHold"]["phase"] == "PREPARED_HELD"


@pytest.mark.parametrize("live_status", ["READY", "UNKNOWN"])
def test_connector_release_pending_uses_live_gate_and_never_invents_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_status: str,
) -> None:
    """Retry resolves only from a fresh open readback; unknown remains held and pending."""
    fixture = _fixture(tmp_path, "connector-host")
    updater = fixture["updater"]
    context = fixture["context"]
    catalog, _source_ids = _catalog()
    journal = _adoption(fixture, phase="RELEASE_PENDING")
    updater._write_placement_adoption(journal)
    monkeypatch.setattr(updater, "_require_authorized_process", lambda: None)
    monkeypatch.setattr(updater, "_reload_catalog_for_operation", lambda: None)
    monkeypatch.setattr(
        updater,
        "_staged_placement_record",
        lambda *_args: (
            fixture["record"],
            tmp_path / "stage.json",
            fixture["plan"],
            placement,
            context,
            fixture["group"],
            fixture["desired"],
        ),
    )
    monkeypatch.setattr(updater, "_verify_local_active_held", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        updater,
        "_placement_peer_evidence",
        lambda *_args, **_kwargs: (placement, context, SimpleNamespace(phase="ACTIVE_HELD"), {}),
    )
    readiness = _readiness(live_status)
    if live_status == "UNKNOWN":
        readiness["blocker_codes"] = ["BROKER_UNAVAILABLE"]
    monkeypatch.setattr(updater, "_readiness_for", lambda *_args, **_kwargs: readiness)
    monkeypatch.setattr(updater, "_activity_catalog", lambda: (catalog, ["cyrene-yield"]))
    calls: list[str] = []

    def broker(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append(method)
        if method == "ValidateMaintenanceHold":
            raise updates.UpdateError(
                "GATE_UNKNOWN", "fixture cannot validate unknown gate", retryable=True
            )
        if method == "EndMaintenance":
            pytest.fail("unknown gate state must not be converted into an open state")
        return _valid_broker_result(method, params, **kwargs)

    monkeypatch.setattr(updater, "_broker_request", broker)
    monkeypatch.setattr(
        updater,
        "_end_maintenance",
        lambda *_args, **_kwargs: calls.append("EndMaintenance"),
    )
    confirmation = fixture["confirmation"]

    if live_status == "READY":
        first = updater.release_placement_adoption(
            fixture["planId"], fixture["planDigest"], confirmation
        )
        second = updater.release_placement_adoption(
            fixture["planId"], fixture["planDigest"], confirmation
        )
        assert first["status"] == second["status"] == "active"
        assert calls == []
        stored = updater._read_placement_adoption(required=True)
        assert stored["phase"] == "ACTIVE"
        assert "maintenanceToken" not in stored
    else:
        with pytest.raises(updates.UpdateError):
            updater.release_placement_adoption(
                fixture["planId"], fixture["planDigest"], confirmation
            )
        stored = updater._read_placement_adoption(required=True)
        assert stored["phase"] == "RELEASE_PENDING"
        assert stored["maintenanceToken"] == journal["maintenanceToken"]
        assert "EndMaintenance" not in calls
