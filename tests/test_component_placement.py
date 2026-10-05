"""Offline tests for signed host-role placement and fixed peer evidence."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "packaging/component_placement.py"
SPEC = importlib.util.spec_from_file_location("component_placement_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
placement = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = placement
SPEC.loader.exec_module(placement)

CATALOG_DIGEST = "sha256:" + "c" * 64
LOCK = {
    "repository": "DoHorizon-AI/Cyrene-Workspace",
    "commit": "a" * 40,
    "path": "governance/workspace-connection-protocols-v2.lock.json",
    "sha256": "sha256:" + "b" * 64,
}
CONTROL = frozenset(
    {
        "cy-workspace-relay",
        "cy-workspace-web-bff",
        "cy-workspace-frontend-bridge",
        "cy-workspace-authority-host",
        "cyrene-product-contract-bundle",
    }
)
CONNECTOR = frozenset({"cy-workspace-connector"})
SIDECAR = "cy-workspace-sidecar"
ALL_MEMBERS = CONTROL | CONNECTOR | {SIDECAR}
WORKSPACE_IDENTITY = {
    "organizationId": "org-cyrene-test",
    "workspaceId": "workspace-cyrene-test",
    "authorityInstanceId": "authority-cyrene-test",
}


def _group() -> dict[str, object]:
    return {
        "groupId": "workspace-product-v2",
        "groupVersion": "2",
        "contractLock": LOCK,
        "members": [{"componentId": item} for item in sorted(ALL_MEMBERS)],
        "deploymentRoles": [
            {
                "roleId": "control-host",
                "requiredMembers": sorted(CONTROL),
                "allowedMembers": sorted(CONTROL),
            },
            {
                "roleId": "connector-host",
                "requiredMembers": sorted(CONNECTOR),
                "allowedMembers": sorted(CONNECTOR | {SIDECAR}),
            },
        ],
    }


def _fingerprint(key: bytes) -> str:
    encoded = base64.b64encode(hashlib.sha256(key).digest()).decode("ascii").rstrip("=")
    return f"SHA256:{encoded}"


def _write(path: Path, data: bytes, mode: int) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return path


@pytest.fixture
def secure_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep fixture directories strict while avoiding /tmp's intentional sticky bit."""

    actual = placement._verify_path_chain
    root = tmp_path.resolve()

    def scoped_path_check(path: Path, *, expected_uid: int) -> None:
        absolute = path.absolute()
        if absolute != root and root not in absolute.parents:
            actual(absolute, expected_uid=expected_uid)
            return
        current = absolute
        while current != root.parent:
            info = os.lstat(current)
            assert not placement.stat.S_ISLNK(info.st_mode)
            assert placement.stat.S_ISDIR(info.st_mode)
            assert info.st_uid in {0, expected_uid}
            assert placement.stat.S_IMODE(info.st_mode) & 0o022 == 0
            if current == root:
                break
            current = current.parent

    monkeypatch.setattr(placement, "_verify_path_chain", scoped_path_check)
    return root


def _host_config(root: Path, *, local_role: str) -> tuple[Path, dict[str, object]]:
    control_key = b"control public key fixture"
    connector_key = b"connector public key fixture"
    if local_role == "control-host":
        local_id, local_name, local_fp = "control-node", "10.40.0.24", _fingerprint(control_key)
        peer_id, peer_role, peer_name, peer_fp, peer_key = (
            "connector-node",
            "connector-host",
            "10.40.0.22",
            _fingerprint(connector_key),
            connector_key,
        )
    else:
        local_id, local_name, local_fp = "connector-node", "10.40.0.22", _fingerprint(connector_key)
        peer_id, peer_role, peer_name, peer_fp, peer_key = (
            "control-node",
            "control-host",
            "10.40.0.24",
            _fingerprint(control_key),
            control_key,
        )
    directory = root / local_role
    directory.mkdir(mode=0o700)
    identity = _write(directory / "peer-key", b"private key bytes are never read", 0o600)
    known_hosts = _write(
        directory / "known_hosts",
        f"{peer_id} ssh-ed25519 {base64.b64encode(peer_key).decode()}\n".encode(),
        0o644,
    )
    config: dict[str, object] = {
        "schemaVersion": 1,
        "deploymentId": "deployment-2026-10-05",
        "hostId": local_id,
        "hostName": local_name,
        "hostKeyFingerprint": local_fp,
        "sshPort": 22,
        "roleId": local_role,
        "catalogDigest": CATALOG_DIGEST,
        "groupId": "workspace-product-v2",
        "workspaceIdentity": dict(WORKSPACE_IDENTITY),
        "peer": {
            "hostId": peer_id,
            "roleId": peer_role,
            "hostName": peer_name,
            "sshUser": "ruanyun",
            "sshPort": 22,
            "identityFile": str(identity),
            "knownHostsFile": str(known_hosts),
            "knownHostsSha256": hashlib.sha256(known_hosts.read_bytes()).hexdigest(),
            "hostKeyFingerprint": peer_fp,
        },
    }
    config_path = _write(
        directory / "component-deployment.json",
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode(),
        0o600,
    )
    return config_path, config


@pytest.fixture
def contexts(secure_tree: Path) -> tuple[placement.PlacementContext, placement.PlacementContext]:
    group = _group()
    control_path, _ = _host_config(secure_tree, local_role="control-host")
    connector_path, _ = _host_config(secure_tree, local_role="connector-host")
    control = placement.load_placement(
        group,
        trusted_catalog_digest=CATALOG_DIGEST,
        config_path=control_path,
        expected_uid=os.geteuid(),
    )
    connector = placement.load_placement(
        group,
        trusted_catalog_digest=CATALOG_DIGEST,
        config_path=connector_path,
        expected_uid=os.geteuid(),
    )
    assert control is not None and connector is not None
    return control, connector


def _component(component_id: str) -> dict[str, object]:
    digest = "sha256:" + "d" * 64
    return {
        "componentId": component_id,
        "manifestDigest": digest,
        "artifactDigest": "sha256:" + "e" * 64,
        "targetId": "ubuntu-24-04-x86-64",
        "releaseIdentity": digest,
        "identityAttested": True,
    }


def _desired(component_id: str) -> dict[str, str]:
    return {
        "componentId": component_id,
        "version": "1.0.0",
        "manifestDigest": "sha256:" + "d" * 64,
        "artifactDigest": "sha256:" + "e" * 64,
        "targetId": "ubuntu-24-04-x86-64",
        "releaseId": "release-" + "1" * 16,
        "sourceCommit": "a" * 40,
        "sourceRepository": "DoHorizon-AI/Cyrene-Workspace",
        "channel": "preview",
    }


def _service_expectations(
    context: placement.PlacementContext,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    ids = sorted(
        item
        for item in context.peer_role.required_members
        if item != "cyrene-product-contract-bundle"
    )
    return (
        {item: f"{item}.service" for item in ids},
        {item: f"/usr/lib/cyrene/{item}" for item in ids},
        {item: "sha256:" + "3" * 64 for item in ids},
    )


def _service_rows(context: placement.PlacementContext, *, active: bool) -> list[dict[str, object]]:
    units, paths, digests = _service_expectations(context)
    return [
        {
            "componentId": component_id,
            "unit": units[component_id],
            "activeState": "active" if active else "inactive",
            "subState": "running" if active else "dead",
            "mainPid": 2345 if active else 0,
            "executablePath": paths[component_id],
            "executableDigest": digests[component_id],
            "processExecutableDigest": digests[component_id] if active else None,
            "identityAttested": True,
            "health": "healthy" if active else None,
        }
        for component_id in sorted(units)
    ]


def _admission_hold(context: placement.PlacementContext, phase: str) -> dict[str, object]:
    plan_id = "plan-" + "1" * 32
    plan_digest = "sha256:" + "2" * 64
    if context.peer.role_id == "control-host":
        return {
            "requestId": "request-2026-10-05",
            **context.workspace_identity,
            "phase": phase,
            "planDigest": plan_digest,
        }
    digests = {
        item["componentId"]: item["artifactDigest"]
        for item in map(_desired, sorted(context.peer_role.required_members))
    }
    return {
        "kind": "CORE_RUNTIME",
        "requestId": "request-2026-10-05",
        "planId": plan_id,
        "planDigest": plan_digest,
        "componentArtifactDigests": digests,
        "gateGeneration": 4,
        "catalogGeneration": 5,
    }


def _business_admission(
    context: placement.PlacementContext,
    phase: str,
    hold: dict[str, object] | None,
) -> dict[str, object]:
    if phase in {"ABSENT", "STAGED"}:
        return {
            "source": "unavailable",
            "scope": placement._role_admission_scope(context.peer.role_id),
            "state": "unavailable",
        }
    if context.peer.role_id == "control-host":
        gate_hold = hold or _admission_hold(context, "ACTIVE") if phase == "ACTIVE" else hold
        state = "open" if phase == "ACTIVE" else "closed"
        api_ready = phase in {"ACTIVE_HELD", "ACTIVE"}
        return {
            "status": "ready" if api_ready else "not_ready",
            "apiReady": api_ready,
            "executionReady": phase == "ACTIVE",
            "businessReady": phase == "ACTIVE",
            "gate": {
                "source": "cy-workspace-control-plane.deployment-admission.v1",
                "scope": "workspace-product-v2/control-host",
                "state": state,
                "readerGid": 1000,
                "generation": 7,
                "catalogDigest": context.catalog_digest,
                "topologyDigest": context.topology_digest,
                "planDigest": "sha256:" + "2" * 64,
                "adoptionHold": gate_hold,
            },
        }
    if phase in {"PREPARED_HELD", "ACTIVE_HELD"}:
        proofs = []
        for component_id, artifact_digest in hold["componentArtifactDigests"].items():
            proofs.append(
                {
                    "valid": True,
                    "request_id": hold["requestId"],
                    "target_kind": "CORE_RUNTIME",
                    "plan": {
                        "plan_id": hold["planId"],
                        "plan_digest": hold["planDigest"],
                        "component_artifact_digests": hold["componentArtifactDigests"],
                    },
                    "component_id": component_id,
                    "artifact_digest": artifact_digest,
                    "gate_generation": hold["gateGeneration"],
                    "catalog_generation": hold["catalogGeneration"],
                }
            )
        return {
            "source": "runtime-maintenance.ValidateMaintenanceHold",
            "scope": "kernel-task-and-runtime-admission",
            "state": "closed",
            "proofs": proofs,
        }
    return {
        "source": "runtime-maintenance.GetUpdateReadiness",
        "scope": "kernel-task-and-runtime-admission",
        "state": "open",
        "gateGeneration": 4,
        "catalogGeneration": 5,
        "readiness": {
            "status": "READY",
            "gate_generation": 4,
            "install_catalog_generation": 5,
            "active_task_count": 0,
            "active_tasks": [],
            "inflight_runtime_admission_count": 0,
            "active_binding_operation_count": 0,
            "active_binding_operations": [],
            "unknown_activity_sources": [],
            "active_worker_count": 0,
            "active_allocation_count": 0,
            "blocker_codes": [],
            "requires_restart_confirmation": False,
        },
    }


def _receipt(
    context: placement.PlacementContext,
    phase: str,
    *,
    now: int = 100,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schemaVersion": 1,
        "deploymentId": context.deployment_id,
        "workspaceIdentity": context.workspace_identity,
        "topologyDigest": context.topology_digest,
        "hostConfigDigest": "sha256:" + "f" * 64,
        "catalogDigest": context.catalog_digest,
        "groupId": context.group_id,
        "groupVersion": context.group_version,
        "contractLock": context.contract_lock,
        "requesterHostId": context.host_id,
        "requesterRoleId": context.role_id,
        "hostId": context.peer.host_id,
        "roleId": context.peer.role_id,
        "phase": phase,
        "challengeNonce": "a" * 64,
        "issuedAt": now,
        "expiresAt": now + 30,
        "planId": None,
        "planDigest": None,
        "desiredComponents": [],
        "activePointer": {"present": False, "digest": None, "components": []},
        "stagedComponents": [],
        "activeComponents": [],
        "transaction": {"present": False, "phase": "none"},
        "admissionHold": None,
        "businessAdmission": (
            _business_admission(context, phase, None) if phase in {"ABSENT", "STAGED"} else None
        ),
        "services": [],
    }
    if phase in {"STAGED", "PREPARED_HELD"}:
        desired = [_desired(item) for item in sorted(context.peer_role.required_members)]
        hold = _admission_hold(context, phase) if phase == "PREPARED_HELD" else None
        value.update(
            {
                "planId": "plan-" + "1" * 32,
                "planDigest": "sha256:" + "2" * 64,
                "desiredComponents": desired,
                "stagedComponents": [
                    _component(item) for item in sorted(context.peer_role.required_members)
                ],
                "admissionHold": hold,
                "businessAdmission": _business_admission(context, phase, hold),
            }
        )
        if phase == "PREPARED_HELD":
            value["services"] = _service_rows(context, active=False)
    elif phase in {"ACTIVE_HELD", "ACTIVE"}:
        active = sorted(context.peer_role.required_members)
        desired = [_desired(item) for item in active]
        hold = _admission_hold(context, phase) if phase == "ACTIVE_HELD" else None
        value.update(
            {
                "planId": "plan-" + "1" * 32 if phase == "ACTIVE_HELD" else None,
                "planDigest": "sha256:" + "2" * 64 if phase == "ACTIVE_HELD" else None,
                "desiredComponents": desired,
                "activeComponents": [_component(item) for item in active],
                "activePointer": {
                    "present": True,
                    "digest": "sha256:" + "4" * 64,
                    "components": [_component(item) for item in active],
                },
                "services": _service_rows(context, active=True),
                "admissionHold": hold,
                "businessAdmission": _business_admission(context, phase, hold),
            }
        )
    return value


def _validate(context, receipt, **kwargs):
    units, paths, digests = _service_expectations(context)
    return placement.validate_peer_receipt(
        context,
        receipt,
        required_service_ids=frozenset(units),
        required_service_units=units,
        required_service_executable_paths=paths,
        required_service_executable_digests=digests,
        **kwargs,
    )


def test_legacy_groups_and_absent_host_config_keep_existing_behavior(secure_tree: Path) -> None:
    assert (
        placement.load_placement(
            {"groupId": "legacy", "members": []},
            trusted_catalog_digest=CATALOG_DIGEST,
            config_path=secure_tree / "missing.json",
            expected_uid=os.geteuid(),
        )
        is None
    )
    assert (
        placement.load_placement(
            _group(),
            trusted_catalog_digest=CATALOG_DIGEST,
            config_path=secure_tree / "missing.json",
            expected_uid=os.geteuid(),
        )
        is None
    )


def test_signed_role_policy_is_checked_even_when_host_config_is_absent(secure_tree: Path) -> None:
    group = _group()
    group["deploymentRoles"] = []
    with pytest.raises(placement.PlacementError):
        placement.load_placement(
            group,
            trusted_catalog_digest=CATALOG_DIGEST,
            config_path=secure_tree / "missing.json",
            expected_uid=os.geteuid(),
        )


def test_topology_digest_is_symmetric_and_local_config_is_endpoint_bound(contexts) -> None:
    control, connector = contexts
    assert control.topology_digest == connector.topology_digest
    assert control.local_config_digest != connector.local_config_digest
    assert placement.plan_binding(control)["localConfigDigest"] == control.local_config_digest
    assert placement.plan_binding(connector)["topologyDigest"] == connector.topology_digest


def test_local_config_digest_changes_without_changing_shared_topology(secure_tree: Path) -> None:
    config_path, config = _host_config(secure_tree, local_role="control-host")
    group = _group()
    before = placement.load_placement(
        group,
        trusted_catalog_digest=CATALOG_DIGEST,
        config_path=config_path,
        expected_uid=os.geteuid(),
    )
    assert before is not None
    config["peer"]["sshUser"] = "operator"
    config_path.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
    config_path.chmod(0o600)
    after = placement.load_placement(
        group,
        trusted_catalog_digest=CATALOG_DIGEST,
        config_path=config_path,
        expected_uid=os.geteuid(),
    )
    assert after is not None
    assert after.topology_digest == before.topology_digest
    assert after.local_config_digest != before.local_config_digest


def test_workspace_identity_is_required_and_part_of_shared_topology(secure_tree: Path) -> None:
    config_path, config = _host_config(secure_tree, local_role="control-host")
    first = placement.load_placement(
        _group(),
        trusted_catalog_digest=CATALOG_DIGEST,
        config_path=config_path,
        expected_uid=os.geteuid(),
    )
    assert first is not None

    config["workspaceIdentity"] = {**WORKSPACE_IDENTITY, "workspaceId": "workspace-other"}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    config_path.chmod(0o600)
    second = placement.load_placement(
        _group(),
        trusted_catalog_digest=CATALOG_DIGEST,
        config_path=config_path,
        expected_uid=os.geteuid(),
    )
    assert second is not None
    assert second.topology_digest != first.topology_digest

    for invalid_identity in (
        {},
        {**WORKSPACE_IDENTITY, "extra": "not-allowed"},
        {**WORKSPACE_IDENTITY, "authorityInstanceId": " bad"},
        {**WORKSPACE_IDENTITY, "organizationId": "bad\u0085id"},
    ):
        config["workspaceIdentity"] = invalid_identity
        config_path.write_text(json.dumps(config), encoding="utf-8")
        config_path.chmod(0o600)
        with pytest.raises(placement.PlacementError, match="Workspace identity"):
            placement.load_placement(
                _group(),
                trusted_catalog_digest=CATALOG_DIGEST,
                config_path=config_path,
                expected_uid=os.geteuid(),
            )


def test_role_selection_keeps_connector_off_control_host(contexts) -> None:
    control, connector = contexts
    group = _group()
    selected = placement.filter_group_members(
        control,
        group,
        set(CONTROL),
        set(),
    )
    assert selected is not None
    assert set(selected.component_ids) == CONTROL
    assert set(selected.excluded_other_role_ids) == CONNECTOR | {SIDECAR}

    selected_connector = placement.filter_group_members(
        connector,
        group,
        set(CONNECTOR | {SIDECAR}),
        {SIDECAR},
    )
    assert selected_connector is not None
    assert set(selected_connector.component_ids) == CONNECTOR | {SIDECAR}


def test_role_selection_rejects_foreign_active_or_missing_required_target(contexts) -> None:
    control, _connector = contexts
    group = _group()
    with pytest.raises(placement.PlacementError, match="another host role"):
        placement.filter_group_members(
            control, group, set(CONTROL | CONNECTOR), {next(iter(CONNECTOR))}
        )
    with pytest.raises(placement.PlacementError, match="required local"):
        placement.filter_group_members(control, group, set(CONTROL - {"cy-workspace-relay"}), set())


def test_validated_phases_allow_check_stage_but_apply_requires_hold_or_active(contexts) -> None:
    control, connector = contexts
    absent = _validate(connector, _receipt(connector, "ABSENT"), now=100)
    placement.authorize_peer_operation(connector, absent, "check")
    placement.authorize_peer_operation(connector, absent, "stage")
    with pytest.raises(placement.PlacementError, match="does not permit"):
        placement.authorize_peer_operation(connector, absent, "apply")

    staged = _validate(control, _receipt(control, "STAGED"), now=100)
    placement.authorize_peer_operation(control, staged, "check")
    placement.authorize_peer_operation(control, staged, "stage")
    with pytest.raises(placement.PlacementError, match="does not permit"):
        placement.authorize_peer_operation(control, staged, "apply")

    prepared = _validate(control, _receipt(control, "PREPARED_HELD"), now=100)
    placement.authorize_peer_operation(control, prepared, "apply")
    placement.authorize_peer_operation(connector, prepared, "apply")

    active_receipt = _receipt(connector, "ACTIVE")
    active = _validate(connector, active_receipt, now=100)
    placement.authorize_peer_operation(connector, active, "apply")
    assert active.payload["businessAdmission"]["gate"]["state"] == "open"


def test_active_busy_and_unknown_peer_remain_checkable_but_not_applicable(contexts) -> None:
    control, _connector = contexts
    busy_receipt = _receipt(control, "ACTIVE", now=100)
    busy = busy_receipt["businessAdmission"]["readiness"]
    busy["status"] = "ACTIVE_TASKS"
    busy["active_task_count"] = 1
    busy["active_tasks"] = [{"source_id": "product", "task_id": "task-1", "state": "active"}]
    busy_evidence = _validate(control, busy_receipt, now=100)
    assert busy_evidence.phase == "ACTIVE"
    placement.authorize_peer_operation(control, busy_evidence, "check")
    placement.authorize_peer_operation(control, busy_evidence, "stage")
    with pytest.raises(placement.PlacementError, match="idle and READY"):
        placement.authorize_peer_operation(control, busy_evidence, "apply")

    unknown_receipt = _receipt(control, "ACTIVE", now=100)
    unknown = unknown_receipt["businessAdmission"]["readiness"]
    unknown["status"] = "UNKNOWN"
    unknown["blocker_codes"] = ["GATE_UNKNOWN"]
    unknown_evidence = _validate(control, unknown_receipt, now=100)
    placement.authorize_peer_operation(control, unknown_evidence, "check")
    placement.authorize_peer_operation(control, unknown_evidence, "stage")
    with pytest.raises(placement.PlacementError, match="idle and READY"):
        placement.authorize_peer_operation(control, unknown_evidence, "apply")


def test_receipts_reject_stale_plan_lock_open_admission_and_unknown_transaction(contexts) -> None:
    control, _connector = contexts
    receipt = _receipt(control, "PREPARED_HELD")
    with pytest.raises(placement.PlacementError, match="plan changed"):
        _validate(
            control,
            receipt,
            plan={
                "planId": "plan-" + "9" * 32,
                "planDigest": receipt["planDigest"],
                "desiredComponents": receipt["desiredComponents"],
            },
            now=100,
        )
    with pytest.raises(placement.PlacementError, match="freshness"):
        _validate(
            control,
            receipt,
            expected_nonce="b" * 64,
            now=100,
        )

    for mutate in (
        lambda item: item.update({"contractLock": {**LOCK, "sha256": "sha256:" + "9" * 64}}),
        lambda item: item.update({"businessAdmission": {"state": "open"}}),
        lambda item: item.update({"transaction": {"present": True, "phase": "applying"}}),
        lambda item: item["admissionHold"].update({"phase": "ACTIVE"}),
        lambda item: item["activePointer"].update({"present": True}),
        lambda item: item.update({"expiresAt": 90}),
        lambda item: item.update({"challengeNonce": "not-a-nonce"}),
    ):
        bad = _receipt(control, "PREPARED_HELD")
        mutate(bad)
        with pytest.raises(placement.PlacementError):
            _validate(control, bad, now=100)


def test_active_requires_live_signed_service_identity(contexts) -> None:
    _control, connector = contexts
    bad = _receipt(connector, "ACTIVE")
    bad["services"] = []
    with pytest.raises(placement.PlacementError, match="omits a required unit"):
        _validate(connector, bad, now=100)
    bad = _receipt(connector, "ACTIVE")
    bad["services"][0]["processExecutableDigest"] = "sha256:" + "9" * 64
    with pytest.raises(placement.PlacementError, match="identity or health"):
        _validate(connector, bad, now=100)


def test_connector_held_state_requires_real_per_component_hold_proofs(contexts) -> None:
    control, _connector = contexts
    receipt = _receipt(control, "PREPARED_HELD")
    evidence = _validate(control, receipt, now=100)
    placement.authorize_peer_operation(control, evidence, "apply")

    bad = _receipt(control, "PREPARED_HELD")
    bad["businessAdmission"]["proofs"][0]["valid"] = False
    with pytest.raises(placement.PlacementError, match="does not match the plan"):
        _validate(control, bad, now=100)

    bad = _receipt(control, "PREPARED_HELD")
    bad["admissionHold"]["componentArtifactDigests"].clear()
    with pytest.raises(placement.PlacementError, match="protected plan"):
        _validate(control, bad, now=100)


def test_stable_evidence_digest_excludes_only_nonce_and_time(contexts) -> None:
    control, _connector = contexts
    first = _receipt(control, "PREPARED_HELD", now=100)
    second = {**first, "challengeNonce": "b" * 64, "issuedAt": 105, "expiresAt": 135}
    first_evidence = _validate(control, first, now=100)
    second_evidence = _validate(control, second, now=105)
    assert first_evidence.stable_digest == second_evidence.stable_digest
    assert first_evidence.response_digest != second_evidence.response_digest
    second["hostConfigDigest"] = "sha256:" + "0" * 64
    changed = _validate(control, second, now=105)
    assert changed.stable_digest != first_evidence.stable_digest


def test_stable_binding_survives_prepare_hold_activation_and_release(contexts) -> None:
    control, _connector = contexts
    phases = ["STAGED", "PREPARED_HELD", "ACTIVE_HELD", "ACTIVE"]
    evidence = [
        _validate(control, _receipt(control, phase, now=100 + index), now=100 + index)
        for index, phase in enumerate(phases)
    ]
    assert len({item.stable_digest for item in evidence}) == 1
    changed = _receipt(control, "STAGED", now=110)
    changed["desiredComponents"][0]["artifactDigest"] = "sha256:" + "9" * 64
    changed["stagedComponents"][0]["artifactDigest"] = "sha256:" + "9" * 64
    changed["stagedComponents"][0]["manifestDigest"] = "sha256:" + "d" * 64
    changed_evidence = _validate(control, changed, now=110)
    assert changed_evidence.stable_digest != evidence[0].stable_digest


def test_peer_receipt_and_control_hold_bind_workspace_identity(contexts) -> None:
    control, connector = contexts
    receipt = _receipt(control, "STAGED", now=100)
    receipt["workspaceIdentity"] = {**control.workspace_identity, "workspaceId": "other"}
    with pytest.raises(placement.PlacementError, match="identity, phase, or freshness"):
        _validate(control, receipt, now=100)

    receipt = _receipt(connector, "PREPARED_HELD", now=100)
    receipt["businessAdmission"]["gate"]["adoptionHold"]["authorityInstanceId"] = "other"
    with pytest.raises(placement.PlacementError, match="adoption hold identity"):
        _validate(connector, receipt, now=100)


def test_staged_update_can_partition_verified_desired_and_existing_active(contexts) -> None:
    _control, connector = contexts
    receipt = _receipt(connector, "STAGED", now=100)
    active_component = receipt["stagedComponents"].pop()
    receipt["activeComponents"] = [active_component]
    receipt["activePointer"] = {
        "present": True,
        "digest": "sha256:" + "4" * 64,
        "components": [active_component],
    }
    evidence = _validate(connector, receipt, now=100)
    assert evidence.phase == "STAGED"
    assert receipt["businessAdmission"]["state"] == "unavailable"


def test_plan_only_response_is_nonce_bound_and_contains_no_readiness_authority(contexts) -> None:
    control, _connector = contexts
    nonce = "a" * 64
    value = {
        "schemaVersion": 1,
        "deploymentId": control.deployment_id,
        "workspaceIdentity": control.workspace_identity,
        "topologyDigest": control.topology_digest,
        "hostConfigDigest": "sha256:" + "f" * 64,
        "catalogDigest": control.catalog_digest,
        "groupId": control.group_id,
        "groupVersion": control.group_version,
        "contractLock": control.contract_lock,
        "requesterHostId": control.host_id,
        "requesterRoleId": control.role_id,
        "hostId": control.peer.host_id,
        "roleId": control.peer.role_id,
        "challengeNonce": nonce,
        "issuedAt": 100,
        "expiresAt": 130,
        "planId": "plan-" + "1" * 32,
        "planDigest": "sha256:" + "2" * 64,
        "desiredComponents": [
            _desired(item) for item in sorted(control.peer_role.required_members)
        ],
    }
    evidence = placement._validate_plan_response(control, value, nonce=nonce, now=100)
    assert evidence.payload["desiredComponents"] == value["desiredComponents"]
    assert not hasattr(evidence, "phase")
    with pytest.raises(placement.PlacementError, match="fields are invalid"):
        placement._validate_plan_response(
            control, {**value, "businessReady": True}, nonce=nonce, now=100
        )

    with pytest.raises(placement.PlacementError, match="identity or freshness"):
        placement._validate_plan_response(
            control,
            {
                **value,
                "workspaceIdentity": {**control.workspace_identity, "organizationId": "other"},
            },
            nonce=nonce,
            now=100,
        )


def test_second_peer_read_must_match_verified_persisted_plan(contexts) -> None:
    control, _connector = contexts
    now = 100
    nonce = "a" * 64
    desired = [_desired(item) for item in sorted(control.peer_role.required_members)]
    plan_payload = {
        "schemaVersion": 1,
        "deploymentId": control.deployment_id,
        "workspaceIdentity": control.workspace_identity,
        "topologyDigest": control.topology_digest,
        "hostConfigDigest": "sha256:" + "f" * 64,
        "catalogDigest": control.catalog_digest,
        "groupId": control.group_id,
        "groupVersion": control.group_version,
        "contractLock": control.contract_lock,
        "requesterHostId": control.host_id,
        "requesterRoleId": control.role_id,
        "hostId": control.peer.host_id,
        "roleId": control.peer.role_id,
        "challengeNonce": nonce,
        "issuedAt": now,
        "expiresAt": now + 30,
        "planId": "plan-" + "1" * 32,
        "planDigest": "sha256:" + "2" * 64,
        "desiredComponents": desired,
    }
    verified_plan = placement._validate_plan_response(control, plan_payload, nonce=nonce, now=now)
    receipt = _receipt(control, "STAGED", now=now)
    assert _validate(control, receipt, plan=verified_plan.payload, now=now).phase == "STAGED"

    changed_plan = {**verified_plan.payload, "desiredComponents": [dict(item) for item in desired]}
    changed_plan["desiredComponents"][0]["artifactDigest"] = "sha256:" + "9" * 64
    with pytest.raises(placement.PlacementError, match="persisted plan changed"):
        _validate(control, receipt, plan=changed_plan, now=now)


def test_fixed_peer_transport_uses_no_user_ssh_config_or_proxy(contexts, monkeypatch) -> None:
    _control, connector = contexts
    captured: list[str] = []

    class FakeProcess:
        def __init__(self, argv: list[str]) -> None:
            self.argv = argv

    def fake_popen(argv, **kwargs):
        captured.extend(argv)
        return FakeProcess(argv)

    def fake_collect(process, *, timeout):
        nonce = process.argv[process.argv.index("--nonce") + 1]
        receipt = _receipt(connector, "ABSENT", now=int(placement.time.time()))
        receipt["challengeNonce"] = nonce
        return json.dumps(receipt).encode(), b""

    monkeypatch.setattr(placement.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(placement, "_collect_bounded", fake_collect)
    evidence = placement.fetch_peer_receipt(connector, expected_phase="ABSENT")
    assert evidence.phase == "ABSENT"
    assert captured[:3] == [placement.SSH_EXECUTABLE, "-F", "/dev/null"]
    assert "HostName=10.40.0.24" in captured
    assert "HostKeyAlias=control-node" in captured
    assert "ProxyCommand=none" in captured
    assert "ProxyJump=none" in captured
    assert "IdentityAgent=none" in captured
    assert "ForwardAgent=no" in captured
    assert "ControlMaster=no" in captured
    assert "PermitLocalCommand=no" in captured
    assert "placement-peer-receipt" in captured
    assert "--nonce" in captured
    assert "shell" not in captured


def test_plan_transport_uses_fixed_read_only_operation(contexts, monkeypatch) -> None:
    _control, connector = contexts
    captured: list[str] = []

    class FakeProcess:
        def __init__(self, argv: list[str]) -> None:
            self.argv = argv

    def fake_popen(argv, **kwargs):
        captured.extend(argv)
        return FakeProcess(argv)

    def fake_collect(process, *, timeout):
        nonce = process.argv[process.argv.index("--nonce") + 1]
        payload = {
            "schemaVersion": 1,
            "deploymentId": connector.deployment_id,
            "workspaceIdentity": connector.workspace_identity,
            "topologyDigest": connector.topology_digest,
            "hostConfigDigest": "sha256:" + "f" * 64,
            "catalogDigest": connector.catalog_digest,
            "groupId": connector.group_id,
            "groupVersion": connector.group_version,
            "contractLock": connector.contract_lock,
            "requesterHostId": connector.host_id,
            "requesterRoleId": connector.role_id,
            "hostId": connector.peer.host_id,
            "roleId": connector.peer.role_id,
            "challengeNonce": nonce,
            "issuedAt": int(placement.time.time()),
            "expiresAt": int(placement.time.time()) + 30,
            "planId": None,
            "planDigest": None,
            "desiredComponents": [],
        }
        return json.dumps(payload).encode(), b""

    monkeypatch.setattr(placement.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(placement, "_collect_bounded", fake_collect)
    plan = placement.fetch_peer_plan(connector)
    assert plan.payload["desiredComponents"] == []
    assert "placement-peer-plan" in captured
    assert "placement-peer-receipt" not in captured
    assert "--nonce" in captured
    assert "--phase" not in captured


def test_peer_transport_rejects_output_over_limit() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 70000)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    with pytest.raises(placement.PlacementError, match="size limit"):
        placement._collect_bounded(process, timeout=3)


def test_present_unsafe_host_config_and_known_hosts_are_rejected(secure_tree: Path) -> None:
    config_path, config = _host_config(secure_tree, local_role="control-host")
    config["peer"]["knownHostsSha256"] = "0" * 64
    config_path.write_text(json.dumps(config), encoding="utf-8")
    config_path.chmod(0o600)
    with pytest.raises(placement.PlacementError, match="known_hosts digest"):
        placement.load_placement(
            _group(),
            trusted_catalog_digest=CATALOG_DIGEST,
            config_path=config_path,
            expected_uid=os.geteuid(),
        )

    config_path.unlink()
    config_path.symlink_to(secure_tree / "missing-target")
    with pytest.raises(placement.PlacementError):
        placement.load_placement(
            _group(),
            trusted_catalog_digest=CATALOG_DIGEST,
            config_path=config_path,
            expected_uid=os.geteuid(),
        )


def test_present_wrong_known_host_fingerprint_is_rejected(secure_tree: Path) -> None:
    config_path, config = _host_config(secure_tree, local_role="control-host")
    config["peer"]["hostKeyFingerprint"] = _fingerprint(b"different peer key")
    config_path.write_text(json.dumps(config), encoding="utf-8")
    config_path.chmod(0o600)
    with pytest.raises(placement.PlacementError, match="fingerprint"):
        placement.load_placement(
            _group(),
            trusted_catalog_digest=CATALOG_DIGEST,
            config_path=config_path,
            expected_uid=os.geteuid(),
        )


def test_actual_path_chain_refuses_world_writable_ancestors(tmp_path: Path) -> None:
    with pytest.raises(placement.PlacementError, match="ownership or mode"):
        placement._verify_path_chain(tmp_path, expected_uid=os.geteuid())
