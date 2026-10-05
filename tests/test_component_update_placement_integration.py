"""Updater integration tests for signed two-host component placement."""

from __future__ import annotations

import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
UPDATES_PATH = ROOT / "packaging/component_updates.py"
PLACEMENT_PATH = ROOT / "packaging/component_placement.py"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


updates = _load_module("component_updates_placement_integration", UPDATES_PATH)
placement = _load_module("component_placement_integration", PLACEMENT_PATH)


def _load_cyrene_cli() -> Any:
    from importlib.machinery import SourceFileLoader

    path = ROOT / "cyrene"
    loader = SourceFileLoader("cyrene_placement_cli", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _updater(tmp_path: Path) -> Any:
    return updates.ComponentUpdater(
        catalog_path=updates.DEFAULT_CATALOG,
        activity_catalog_path=tmp_path / "activity-sources.json",
        state_root=tmp_path / "state",
        install_root=tmp_path / "install",
        release_lock_path=tmp_path / "missing-release-lock.json",
        broker_path=tmp_path / "missing-broker",
        load_active_catalog=False,
    )


def _context(role_id: str) -> Any:
    group = next(
        item for item in placement_group_catalog() if item["groupId"] == "workspace-product-v2"
    )
    roles = {
        role["roleId"]: placement.DeploymentRole(
            role_id=role["roleId"],
            required_members=frozenset(role["requiredMembers"]),
            allowed_members=frozenset(role["allowedMembers"]),
        )
        for role in group["deploymentRoles"]
    }
    peer_role_id = "connector-host" if role_id == "control-host" else "control-host"
    peer = SimpleNamespace(host_id=f"{peer_role_id}-node", role_id=peer_role_id)
    return (
        SimpleNamespace(
            host_id=f"{role_id}-node",
            role_id=role_id,
            peer=peer,
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
        ),
        group,
    )


def placement_group_catalog() -> list[dict[str, Any]]:
    catalog = __import__("json").loads(updates.DEFAULT_CATALOG.read_text(encoding="utf-8"))
    return catalog["compatibilityGroups"]


def _candidate(component: dict[str, Any], group: dict[str, Any]) -> updates.Candidate:
    member = next(
        item for item in group["members"] if item["componentId"] == component["componentId"]
    )
    compatibility = {
        "groupId": group["groupId"],
        "groupVersion": group["groupVersion"],
        "contractApiVersion": group["contractApiVersion"],
        "wireApiVersion": group["wireApiVersion"],
        "contractLock": group["contractLock"],
    }
    manifest = {
        "schemaVersion": 2,
        "version": "0.1.0",
        "protocolVersion": member["protocolVersion"],
        "compatibility": compatibility,
        "artifact": {"digest": "sha256:" + "c" * 64},
    }
    return updates.Candidate(
        component=component,
        manifest=manifest,
        manifest_digest="sha256:" + "d" * 64,
        artifact_digest="sha256:" + "c" * 64,
        manifest_uri="https://example.invalid/manifest.json",
        index={},
        index_uri="https://example.invalid/index.json",
    )


@pytest.mark.parametrize(
    ("role_id", "requested", "active", "expected"),
    [
        (
            "control-host",
            {"cy-workspace-relay"},
            set(),
            {
                "cy-workspace-relay",
                "cy-workspace-web-bff",
                "cy-workspace-frontend-bridge",
                "cy-workspace-authority-host",
                "cyrene-product-contract-bundle",
            },
        ),
        (
            "connector-host",
            {"cy-workspace-connector"},
            {"cy-workspace-sidecar"},
            {"cy-workspace-connector", "cy-workspace-sidecar"},
        ),
    ],
)
def test_group_expansion_stays_inside_the_configured_host_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role_id: str,
    requested: set[str],
    active: set[str],
    expected: set[str],
) -> None:
    updater = _updater(tmp_path)
    context, group = _context(role_id)
    group_members = {member["componentId"] for member in group["members"]}
    monkeypatch.setattr(
        updater,
        "_placement_module_for_group",
        lambda _group: (placement, context),
    )
    monkeypatch.setattr(updater, "_target_for", lambda _component: {"id": "linux-test"})
    monkeypatch.setattr(
        updater,
        "_installed",
        lambda component: {"active": component["componentId"] in active},
    )
    selected_candidate = _candidate(updater.components[next(iter(requested))], group)
    monkeypatch.setattr(
        updater,
        "_candidate",
        lambda component, _target, _channel: _candidate(component, group),
    )

    expanded = updater._expand_compatibility_groups(
        {selected_candidate.component["componentId"]: selected_candidate}, "preview"
    )

    assert set(expanded) == expected
    if role_id == "connector-host":
        assert "cyrene-product-contract-bundle" not in expanded
        assert not (set(expanded) & (group_members - context.local_role.allowed_members))


def test_legacy_group_without_deployment_roles_keeps_whole_local_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _updater(tmp_path)
    group = next(
        item for item in placement_group_catalog() if item["groupId"] == "workspace-product-v2"
    )
    legacy_group = {key: value for key, value in group.items() if key != "deploymentRoles"}
    monkeypatch.setattr(updater, "_placement_module_for_group", lambda _group: None)
    monkeypatch.setattr(updater, "_target_for", lambda _component: {"id": "linux-test"})
    monkeypatch.setattr(updater, "_installed", lambda _component: {"active": False})
    monkeypatch.setattr(
        updater,
        "_candidate",
        lambda component, _target, _channel: _candidate(component, legacy_group),
    )
    selected = _candidate(updater.components["cy-workspace-connector"], legacy_group)

    expanded = updater._expand_compatibility_groups({"cy-workspace-connector": selected}, "preview")

    assert set(expanded) == {
        member["componentId"]
        for member in group["members"]
        if member.get("requiredForAdoption")
        or updater.components[member["componentId"]].get("kind") == "data-bundle"
    }


def test_default_and_explicit_selection_respect_the_local_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _updater(tmp_path)
    context, group = _context("control-host")
    monkeypatch.setattr(
        updater,
        "_placement_module_for_group",
        lambda candidate_group: (
            (placement, context) if candidate_group["groupId"] == group["groupId"] else None
        ),
    )
    monkeypatch.setattr(updater, "_target_for", lambda _component: {"id": "linux-test"})
    monkeypatch.setattr(updater, "_installed", lambda _component: {"active": False})

    selected = set(updater._default_placement_selection())

    assert "cy-workspace-relay" in selected
    assert "cy-workspace-connector" not in selected
    assert "cy-workspace-sidecar" not in selected
    with pytest.raises(updates.UpdateError, match="violates host placement"):
        updater._validate_requested_placement({"cy-workspace-connector"})


def test_plan_binding_uses_peer_role_service_units_and_revalidates_fresh_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater = _updater(tmp_path)
    context, _group = _context("control-host")
    monkeypatch.setattr(
        updater,
        "_placement_module_for_group",
        lambda _group: (placement, context),
    )
    monkeypatch.setattr(updater, "_target_for", lambda _component: {"id": "linux-test"})
    monkeypatch.setattr(updater, "_installed", lambda _component: {"active": False})
    observed: list[dict[str, Any]] = []
    operations: list[str] = []
    evidence_digest = "sha256:" + "e" * 64
    evidence = SimpleNamespace(
        payload={
            "planId": "plan-" + "1" * 32,
            "planDigest": "sha256:" + "2" * 64,
            "desiredComponents": [],
        },
        stable_digest=evidence_digest,
        phase="PREPARED",
    )
    peer_plan = SimpleNamespace(payload=evidence.payload, stable_digest=evidence_digest)
    monkeypatch.setattr(placement, "fetch_peer_plan", lambda _context: peer_plan)
    monkeypatch.setattr(
        updater,
        "_placement_desired_components",
        lambda _group, selected_ids, **_kwargs: [
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
            for component_id in selected_ids
        ],
    )
    service_units = {"cy-workspace-connector": "cy-workspace-connector.service"}
    service_paths = {
        "cy-workspace-connector": "/usr/lib/cyrene/components/cy-workspace-connector/bin/connector"
    }
    service_digests = {"cy-workspace-connector": "sha256:" + "f" * 64}
    monkeypatch.setattr(
        updater,
        "_verify_peer_placement_plan",
        lambda _context, _plan: ({}, service_units, service_paths, service_digests),
    )
    monkeypatch.setattr(
        placement,
        "fetch_peer_receipt",
        lambda _context, **kwargs: observed.append(kwargs) or evidence,
    )
    monkeypatch.setattr(
        placement,
        "authorize_peer_operation",
        lambda _context, _evidence, operation: operations.append(operation),
    )

    bindings = updater._placement_bindings_for_ids({"cy-workspace-relay"}, operation="check")
    assert observed == [
        {
            "plan": evidence.payload,
            "required_service_ids": frozenset({"cy-workspace-connector"}),
            "required_service_units": service_units,
            "required_service_executable_paths": service_paths,
            "required_service_executable_digests": service_digests,
        }
    ]
    plan = {"components": [{"componentId": "cy-workspace-relay"}], "deploymentPlacement": bindings}
    updater._revalidate_plan_placement(plan, operation="stage")
    updater._revalidate_plan_placement(plan, operation="apply")
    assert operations == ["check", "stage", "apply"]

    evidence.stable_digest = "sha256:" + "f" * 64
    with pytest.raises(updates.UpdateError, match="Trusted peer placement proof failed"):
        updater._revalidate_plan_placement(plan, operation="apply")


def test_peer_plan_derives_signed_service_identities_before_fresh_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater = _updater(tmp_path)
    context, _group = _context("control-host")
    monkeypatch.setattr(
        updater,
        "_placement_module_for_group",
        lambda _group: (placement, context),
    )
    monkeypatch.setattr(updater, "_target_for", lambda _component: {"id": "linux-test"})
    monkeypatch.setattr(updater, "_installed", lambda _component: {"active": False})
    observed: dict[str, Any] = {}
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
        for component_id in context.peer_role.required_members
    ]
    peer_plan = SimpleNamespace(
        payload={
            "planId": "plan-" + "1" * 32,
            "planDigest": "sha256:" + "2" * 64,
            "desiredComponents": desired,
        },
        stable_digest="sha256:" + "e" * 64,
    )
    monkeypatch.setattr(placement, "fetch_peer_plan", lambda _context: peer_plan)
    monkeypatch.setattr(
        updater,
        "_placement_desired_components",
        lambda _group, selected_ids, **_kwargs: [
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
            for component_id in selected_ids
        ],
    )
    units = {"cy-workspace-connector": "cy-workspace-connector.service"}
    paths = {"cy-workspace-connector": "/usr/lib/cyrene/components/connector/bin/connector"}
    digests = {"cy-workspace-connector": "sha256:" + "f" * 64}
    monkeypatch.setattr(
        updater,
        "_verify_peer_placement_plan",
        lambda _context, _plan: ({}, units, paths, digests),
    )

    def fetch(_context: Any, **kwargs: Any) -> Any:
        observed.update(kwargs)
        return SimpleNamespace(
            payload={"planId": "plan-" + "1" * 32, "planDigest": "sha256:" + "2" * 64},
            stable_digest="sha256:" + "e" * 64,
            phase="ACTIVE",
        )

    monkeypatch.setattr(placement, "fetch_peer_receipt", fetch)
    monkeypatch.setattr(placement, "authorize_peer_operation", lambda *_args: None)
    updater._placement_bindings_for_ids({"cy-workspace-relay"}, operation="check")
    assert observed["required_service_units"] == units
    assert observed["required_service_executable_paths"] == paths
    assert observed["required_service_executable_digests"] == digests


def test_peer_evidence_cli_accepts_only_fixed_readonly_request_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_cyrene_cli()
    args = cli.build_parser().parse_args(
        [
            "placement-peer-evidence",
            "placement-peer-receipt",
            "--deployment-id",
            "deployment-fixture",
            "--requester-host-id",
            "connector-node",
            "--requester-role-id",
            "connector-host",
            "--group-id",
            "workspace-product-v2",
            "--nonce",
            "a" * 64,
            "--phase",
            "STAGED",
        ]
    )
    assert args.func == cli.cmd_placement_peer_evidence
    assert args.operation == "placement-peer-receipt"
    assert args.phase == "STAGED"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            [
                "placement-peer-evidence",
                "placement-peer-release",
                "--deployment-id",
                "deployment-fixture",
                "--requester-host-id",
                "connector-node",
                "--requester-role-id",
                "connector-host",
                "--group-id",
                "workspace-product-v2",
                "--nonce",
                "a" * 64,
            ]
        )


def test_peer_evidence_cli_runs_fixed_updater_and_emits_single_json_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _load_cyrene_cli()
    response = {"phase": "ABSENT", "challengeNonce": "a" * 64}

    class FakeUpdater:
        def placement_peer_response(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs == {
                "operation": "placement-peer-plan",
                "deployment_id": "deployment-fixture",
                "requester_host_id": "connector-node",
                "requester_role_id": "connector-host",
                "group_id": "workspace-product-v2",
                "nonce": "a" * 64,
                "expected_phase": None,
            }
            return response

    fake_module = SimpleNamespace(ComponentUpdater=lambda: FakeUpdater())
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "_load_component_updates_module", lambda: fake_module)
    args = Namespace(
        operation="placement-peer-plan",
        deployment_id="deployment-fixture",
        requester_host_id="connector-node",
        requester_role_id="connector-host",
        group_id="workspace-product-v2",
        nonce="a" * 64,
        phase=None,
    )

    assert cli.cmd_placement_peer_evidence(args) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == json.dumps(response, separators=(",", ":")) + "\n"


def test_peer_evidence_cli_rejects_nonroot_without_loading_updater(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _load_cyrene_cli()
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        cli,
        "_load_component_updates_module",
        lambda: pytest.fail("nonroot peer request must not load privileged updater"),
    )
    args = Namespace(
        operation="placement-peer-plan",
        deployment_id="deployment-fixture",
        requester_host_id="connector-node",
        requester_role_id="connector-host",
        group_id="workspace-product-v2",
        nonce="a" * 64,
        phase=None,
    )

    assert cli.cmd_placement_peer_evidence(args) == 1
    assert "requires the root helper" in capsys.readouterr().err


def test_peer_snapshot_rejects_active_component_owned_by_other_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater = _updater(tmp_path)
    _context_value, group = _context("control-host")
    control_role = next(
        role for role in group["deploymentRoles"] if role["roleId"] == "control-host"
    )
    monkeypatch.setattr(
        updater,
        "_installed",
        lambda component: {"active": component["componentId"] == "cy-workspace-connector"},
    )

    with pytest.raises(updates.UpdateError, match="belongs to the other host role"):
        updater._placement_active_components(
            group,
            frozenset(control_role["allowedMembers"]),
        )


def test_peer_catalog_read_verification_never_repairs_or_advances_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater = _updater(tmp_path)
    metadata = {"catalogSha256": updater.catalog_digest, "generation": updater.catalog_generation}
    updater.catalog_source = metadata
    monkeypatch.setattr(updater, "_read_active_catalog", lambda: (b"catalog", metadata))
    monkeypatch.setattr(updater, "_read_catalog_floor", lambda: metadata)
    monkeypatch.setattr(
        updater,
        "_write_catalog_floor",
        lambda _value: pytest.fail("peer evidence reads must not mutate the catalog receipt"),
    )

    updater._verify_placement_catalog_readonly()

    monkeypatch.setattr(
        updater,
        "_read_catalog_floor",
        lambda: {"catalogSha256": "sha256:" + "f" * 64, "generation": 99},
    )
    with pytest.raises(updates.UpdateError, match="active signed catalog or its monotonic receipt"):
        updater._verify_placement_catalog_readonly()


def test_control_adoption_prepare_apply_held_release_is_one_persisted_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater = _updater(tmp_path)
    context, group = _context("control-host")
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
    plan = {"planId": plan_id, "planDigest": plan_digest, "channel": "preview"}
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
    peer_evidence = SimpleNamespace(phase="STAGED")
    peer_binding = {"peerEvidenceDigest": "sha256:" + "e" * 64}
    state: dict[str, Any] = {}
    lifecycle_calls: list[str] = []

    monkeypatch.setattr(updater, "_require_authorized_process", lambda: None)
    monkeypatch.setattr(updater, "_reload_catalog_for_operation", lambda: None)
    monkeypatch.setattr(
        updater,
        "_staged_placement_record",
        lambda *_args: (record, tmp_path / "stage.json", plan, placement, context, group, desired),
    )
    monkeypatch.setattr(updater, "_revalidate_plan_placement", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        updater,
        "_placement_peer_evidence",
        lambda *_args, **_kwargs: (placement, context, peer_evidence, peer_binding),
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
    monkeypatch.setattr(
        updater, "_control_admission_identity", lambda _context: (10001, context.workspace_identity)
    )

    def write_gate(_context: Any, *, plan_id: str, plan_digest: str, phase: str) -> dict[str, Any]:
        lifecycle_calls.append(f"gate:{phase}")
        state.update(
            {
                "planDigest": plan_digest,
                "state": "open" if phase == "ACTIVE" else "closed",
                "generation": state.get("generation", 0) + 1,
                "adoptionHold": {"phase": phase},
            }
        )
        return dict(state)

    monkeypatch.setattr(updater, "_write_control_admission_state", write_gate)
    monkeypatch.setattr(updater, "_read_control_admission_state", lambda _context: dict(state))
    monkeypatch.setattr(updater, "_validate_control_readyz", lambda *_args: None)
    monkeypatch.setattr(updater, "_control_readyz", lambda: {"apiReady": True})
    monkeypatch.setattr(updater, "_validate_local_placement_adoption", lambda *_args: None)
    monkeypatch.setattr(updater, "_verify_local_active_held", lambda *_args, **_kwargs: None)

    confirmation = {"planId": plan_id, "planDigest": plan_digest, "confirmed": True}
    prepared = updater.prepare_placement_adoption(plan_id, plan_digest, confirmation)
    assert prepared["status"] == "prepared_held"
    adoption = updater._read_placement_adoption(required=True)
    assert adoption is not None and adoption["phase"] == "PREPARED_HELD"

    monkeypatch.setattr(updates, "_running_as_root", lambda: True)
    monkeypatch.setattr(updater, "_placement_for_plan", lambda _plan: (placement, context, group))
    monkeypatch.setattr(updater, "_require_managed_services", lambda _components: None)
    monkeypatch.setattr(updater, "_capture_active_versions", lambda _components: {})
    monkeypatch.setattr(updater, "_prepare_authority_activation", lambda *_args: {})

    def mark(name: str) -> Any:
        def call(*_args: Any, **_kwargs: Any) -> None:
            lifecycle_calls.append(name)

        return call

    monkeypatch.setattr(updater, "_activate_transaction", mark("activate"))
    monkeypatch.setattr(updater, "_restart_transaction", mark("restart"))
    monkeypatch.setattr(updater, "_health_transaction", mark("health"))
    monkeypatch.setattr(updater, "_activate_authority_bundle", mark("authority"))
    monkeypatch.setattr(updater, "_applied_result", lambda _transaction: {"status": "applied"})
    transaction_path = updater._private_state_directory("transactions") / f"{plan_id}.json"
    applied = updater._apply_placement_held(
        record,
        transaction_path,
        None,
        adoption,
        confirmation,
    )
    assert applied == {"status": "applied", "adoptionPhase": "ACTIVE_HELD"}
    assert updater._read_placement_adoption(required=True)["phase"] == "ACTIVE_HELD"
    assert json.loads(transaction_path.read_text(encoding="utf-8"))["phase"] == "succeeded-held"

    monkeypatch.setattr(
        updater,
        "_placement_local_snapshot",
        lambda *_args: {
            "phase": "ACTIVE_HELD",
            "activeComponents": [{"componentId": item["componentId"]} for item in desired],
        },
    )
    peer_evidence.phase = "ACTIVE_HELD"
    released = updater.release_placement_adoption(plan_id, plan_digest, confirmation)
    assert released["status"] == "active"
    assert updater._read_placement_adoption(required=True)["phase"] == "ACTIVE"
    assert lifecycle_calls == [
        "gate:PREPARED_HELD",
        "activate",
        "restart",
        "health",
        "authority",
        "gate:ACTIVE_HELD",
        "gate:ACTIVE_HELD",
        "gate:ACTIVE",
    ]
    assert "begin" not in lifecycle_calls and "end" not in lifecycle_calls


@pytest.mark.parametrize("peer_state", ["busy", "unknown"])
def test_held_apply_rejects_busy_or_unknown_peer_without_mutating_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    peer_state: str,
) -> None:
    updater = _updater(tmp_path)
    context, group = _context("control-host")
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
    plan = {"planId": plan_id, "planDigest": plan_digest, "channel": "preview"}
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
    adoption = {
        "schemaVersion": 1,
        "groupId": "workspace-product-v2",
        "deploymentId": context.deployment_id,
        "roleId": context.role_id,
        "topologyDigest": context.topology_digest,
        "localConfigDigest": context.local_config_digest,
        "catalogDigest": context.catalog_digest,
        "planId": plan_id,
        "planDigest": plan_digest,
        "desiredComponents": desired,
        "componentArtifactDigests": {
            item["componentId"]: item["artifactDigest"] for item in desired
        },
        "requestId": "cyrene-update-" + plan_id,
        "targetKind": "PACKAGE_ONLY",
        "phase": "PREPARED_HELD",
    }
    updater._write_placement_adoption(adoption)
    monkeypatch.setattr(updates, "_running_as_root", lambda: True)
    monkeypatch.setattr(updater, "_placement_for_plan", lambda _plan: (placement, context, group))
    monkeypatch.setattr(updater, "_require_managed_services", lambda _components: None)
    monkeypatch.setattr(updater, "_validate_local_placement_adoption", lambda *_args: None)
    monkeypatch.setattr(updater, "_revalidate_plan_placement", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        updater,
        "_placement_peer_evidence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            updates.UpdateError(
                "PLACEMENT_PEER_BUSY" if peer_state == "busy" else "PLACEMENT_ADMISSION_UNKNOWN",
                f"peer state is {peer_state}",
                retryable=True,
            )
        ),
    )
    monkeypatch.setattr(
        updater,
        "_activate_transaction",
        lambda *_args: pytest.fail("peer must be safe before local activation"),
    )
    transaction_path = updater._private_state_directory("transactions") / f"{plan_id}.json"

    with pytest.raises(updates.UpdateError):
        updater._apply_placement_held(
            record,
            transaction_path,
            None,
            adoption,
            {"planId": plan_id, "planDigest": plan_digest, "confirmed": True},
        )

    assert updater._read_placement_adoption(required=True)["phase"] == "PREPARED_HELD"
    assert not transaction_path.exists()


def test_release_pending_reconciles_open_gate_without_reacquiring_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater = _updater(tmp_path)
    context, group = _context("control-host")
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
    plan = {"planId": plan_id, "planDigest": plan_digest}
    record = {"plan": plan, "channel": "preview", "components": []}
    adoption = {
        "schemaVersion": 1,
        "groupId": "workspace-product-v2",
        "deploymentId": context.deployment_id,
        "roleId": context.role_id,
        "topologyDigest": context.topology_digest,
        "localConfigDigest": context.local_config_digest,
        "catalogDigest": context.catalog_digest,
        "planId": plan_id,
        "planDigest": plan_digest,
        "desiredComponents": desired,
        "componentArtifactDigests": {
            item["componentId"]: item["artifactDigest"] for item in desired
        },
        "requestId": "cyrene-update-" + plan_id,
        "targetKind": "PACKAGE_ONLY",
        "phase": "RELEASE_PENDING",
    }
    updater._write_placement_adoption(adoption)
    monkeypatch.setattr(updater, "_require_authorized_process", lambda: None)
    monkeypatch.setattr(updater, "_reload_catalog_for_operation", lambda: None)
    monkeypatch.setattr(
        updater,
        "_staged_placement_record",
        lambda *_args: (record, tmp_path / "stage.json", plan, placement, context, group, desired),
    )
    monkeypatch.setattr(updater, "_placement_for_plan", lambda _plan: (placement, context, group))
    monkeypatch.setattr(updater, "_verify_local_active_held", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(updater, "_validate_local_placement_adoption", lambda *_args: None)
    monkeypatch.setattr(
        updater,
        "_placement_peer_evidence",
        lambda *_args, **_kwargs: (placement, context, SimpleNamespace(phase="ACTIVE_HELD"), {}),
    )
    monkeypatch.setattr(
        updater,
        "_read_control_admission_state",
        lambda _context: {
            "state": "open",
            "planDigest": plan_digest,
            "adoptionHold": {"phase": "ACTIVE"},
        },
    )
    monkeypatch.setattr(updater, "_validate_control_readyz", lambda *_args: None)
    monkeypatch.setattr(updater, "_control_readyz", lambda: {"apiReady": True})
    monkeypatch.setattr(
        updater,
        "_write_control_admission_state",
        lambda *_args, **_kwargs: pytest.fail("already-open gate must not be rewritten on retry"),
    )

    result = updater.release_placement_adoption(
        plan_id,
        plan_digest,
        {"planId": plan_id, "planDigest": plan_digest, "confirmed": True},
    )

    assert result["status"] == "active"
    assert updater._read_placement_adoption(required=True)["phase"] == "ACTIVE"
