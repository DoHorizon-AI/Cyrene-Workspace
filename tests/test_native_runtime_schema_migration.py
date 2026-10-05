"""Focused contract tests for schema-1 profile detection and migration proof binding."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = WORKSPACE_ROOT / "packaging" / "native_runtime_schema_migration.py"
spec = importlib.util.spec_from_file_location("native_runtime_schema_migration_test", MODULE_PATH)
assert spec is not None and spec.loader is not None
migration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = migration
spec.loader.exec_module(migration)

UPDATES_PATH = WORKSPACE_ROOT / "packaging" / "component_updates.py"
updates_spec = importlib.util.spec_from_file_location("native_schema_updates_test", UPDATES_PATH)
assert updates_spec is not None and updates_spec.loader is not None
updates = importlib.util.module_from_spec(updates_spec)
sys.modules[updates_spec.name] = updates
updates_spec.loader.exec_module(updates)


def _state(*, experimental: bool = False, sequence: int = 0) -> dict:
    state = {
        "schema_version": 1,
        "journal_sequence": sequence,
        "gate_generation": 7,
        "install_catalog_generation": 11,
        "maintenance": None,
        "completed_maintenances": {},
        "tasks": {},
        "runtime_admissions": {},
        "sources": {},
    }
    if experimental:
        state["binding_operations"] = {}
        state["completed_binding_operations"] = {}
    return state


def _journal(*states: dict) -> bytes:
    entries = [
        {
            "sequence": state["journal_sequence"],
            "event": {"event": "state_checkpoint", "state": state},
        }
        for state in states
    ]
    return b"".join(json.dumps(entry).encode() + b"\n" for entry in entries)


@pytest.mark.parametrize(
    ("experimental", "expected"),
    [
        (False, "released-v1-no-binding-admissions"),
        (True, "experimental-v1-binding-admissions"),
    ],
)
def test_profile_is_derived_from_snapshot_and_matching_checkpoints(experimental, expected):
    snapshot = _state(experimental=experimental, sequence=2)
    journal = _journal(_state(experimental=experimental, sequence=1), snapshot)

    assert migration.derive_schema1_profile(json.dumps(snapshot).encode(), journal) == expected


def test_profile_rejects_checkpoint_disagreement():
    snapshot = _state(experimental=False, sequence=1)

    with pytest.raises(migration.MigrationError, match="disagree"):
        migration.derive_schema1_profile(
            json.dumps(snapshot).encode(), _journal(_state(experimental=True, sequence=1))
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: state.update(extra=True),
        lambda state: state.update(schema_version=True),
        lambda state: state.update(binding_operations={}),
        lambda state: state.update(journal_sequence=True),
    ],
)
def test_profile_rejects_unknown_partial_or_wrongly_typed_state(mutation):
    state = _state()
    mutation(state)

    with pytest.raises(migration.MigrationError):
        migration.derive_schema1_profile(json.dumps(state).encode(), b"")


@pytest.mark.parametrize(
    "journal",
    [
        b'{"sequence":1,"sequence":2,"event":{}}\n',
        b'{"sequence":true,"event":{}}\n',
        b'{"sequence":1,"event":{"event":"state_checkpoint","state":{}}}\n',
        b'{"sequence":1,"event":{"event":"not_a_broker_event"}}\n',
        b"not-json\n",
    ],
)
def test_profile_rejects_malformed_journal_envelopes(journal):
    with pytest.raises(migration.MigrationError):
        migration.derive_schema1_profile(json.dumps(_state()).encode(), journal)


def test_released_profile_rejects_binding_operation_journal_event():
    journal = b'{"sequence":1,"event":{"event":"binding_operation_admitted","record":{}}}\n'

    with pytest.raises(migration.MigrationError, match="binding-operation"):
        migration.derive_schema1_profile(json.dumps(_state()).encode(), journal)


def test_profile_rejects_snapshot_newer_than_journal():
    snapshot = _state(sequence=2)

    with pytest.raises(migration.MigrationError, match="truncated"):
        migration.derive_schema1_profile(
            json.dumps(snapshot).encode(), _journal(_state(sequence=1))
        )


def test_migration_proof_binds_exact_confirmed_complete_group_plan():
    transaction = {
        "targetKind": "CORE_RUNTIME",
        "requiresRestart": True,
        "userConfirmedRestart": True,
        "maintenanceToken": "private-token",
        "maintenanceGateGeneration": 8,
        "requestId": "update-1",
        "planId": "plan-1",
        "planDigest": "sha256:" + "a" * 64,
        "componentArtifactDigests": {
            "cyrene-kernel": "sha256:" + "b" * 64,
            "cyrene-runtime-maintenance": "sha256:" + "c" * 64,
            "cy-package-runtime": "sha256:" + "d" * 64,
        },
        "expectedGateGeneration": 7,
        "expectedCatalogGeneration": 11,
    }

    proof = migration.create_migration_proof(transaction, "released-v1-no-binding-admissions")

    assert proof == {
        "schema1_profile": "released-v1-no-binding-admissions",
        "request_id": "update-1",
        "maintenance_token": "private-token",
        "target_kind": "CORE_RUNTIME",
        "plan_id": "plan-1",
        "plan_digest": "sha256:" + "a" * 64,
        "component_artifact_digests": transaction["componentArtifactDigests"],
        "expected_gate_generation": 8,
        "expected_catalog_generation": 11,
    }


def test_migration_proof_rejects_incomplete_plan_or_unconfirmed_restart():
    transaction = {
        "targetKind": "CORE_RUNTIME",
        "requiresRestart": True,
        "userConfirmedRestart": False,
        "maintenanceToken": "private-token",
        "maintenanceGateGeneration": 8,
        "requestId": "update-1",
        "planId": "plan-1",
        "planDigest": "sha256:" + "a" * 64,
        "componentArtifactDigests": {
            "cyrene-kernel": "sha256:" + "b" * 64,
            "cyrene-runtime-maintenance": "sha256:" + "c" * 64,
        },
        "expectedGateGeneration": 7,
        "expectedCatalogGeneration": 11,
    }

    with pytest.raises(migration.MigrationError):
        migration.create_migration_proof(transaction, "released-v1-no-binding-admissions")


def _active_payload_fixture(tmp_path: Path) -> tuple[SimpleNamespace, Path, bytes]:
    component_id = "cyrene-kernel"
    install_root = tmp_path / "opt" / "cyrene"
    component_root = install_root / "components" / component_id
    pointer = "1.0.0--" + "a" * 64
    release = component_root / "releases" / pointer
    release.mkdir(parents=True)
    for directory in (
        install_root,
        install_root / "components",
        component_root,
        component_root / "releases",
        release,
    ):
        directory.chmod(0o755)
    payload = b"verified executable bytes\n"
    (release / "bin").mkdir()
    (release / "bin").chmod(0o755)
    executable = release / "bin" / component_id
    executable.write_bytes(payload)
    executable.chmod(0o755)
    (component_root / "active").symlink_to(f"releases/{pointer}")
    manifest = {
        "componentId": component_id,
        "version": "1.0.0",
        "manifestDigest": "sha256:" + "b" * 64,
        "artifact": {
            "kind": "native-binary",
            "entrypoint": "bin/" + component_id,
            "files": {"bin/" + component_id: "sha256:" + hashlib.sha256(payload).hexdigest()},
        },
    }
    installed = {
        "active": True,
        "identityAttested": True,
        "manifest": manifest,
        "manifestDigest": manifest["manifestDigest"],
        "activeVersion": manifest["version"],
        "pointerIdentity": pointer,
    }
    updater = SimpleNamespace(
        components={component_id: {"componentId": component_id}},
        install_root=install_root,
        _installed=lambda _component: installed,
    )
    return updater, executable, payload


def test_active_native_payload_requires_and_accepts_exact_root_owned_file_map(
    tmp_path, monkeypatch
):
    updater, _executable, _payload = _active_payload_fixture(tmp_path)
    _mock_root_owned_metadata(monkeypatch)

    installed = migration.verify_active_native_payload(updater, "cyrene-kernel")

    assert installed["identityAttested"] is True


@pytest.mark.parametrize("mutation", ["payload", "pointer", "mode", "symlink"])
def test_active_native_payload_rejects_local_identity_or_file_drift(
    tmp_path, monkeypatch, mutation
):
    updater, executable, _payload = _active_payload_fixture(tmp_path)
    _mock_root_owned_metadata(monkeypatch)
    component_root = updater.install_root / "components" / "cyrene-kernel"
    if mutation == "payload":
        executable.write_bytes(b"changed bytes\n")
    elif mutation == "pointer":
        (component_root / "active").unlink()
        (component_root / "active").symlink_to("releases/other")
    elif mutation == "mode":
        executable.chmod(stat.S_IMODE(executable.stat().st_mode) | 0o022)
    else:
        replacement = executable.with_suffix(".real")
        executable.rename(replacement)
        executable.symlink_to(replacement.name)

    with pytest.raises(migration.MigrationError):
        migration.verify_active_native_payload(updater, "cyrene-kernel")


def test_active_release_requires_exact_immutable_attested_index_and_manifest(tmp_path, monkeypatch):
    updater, _executable, _payload = _active_payload_fixture(tmp_path)
    _mock_root_owned_metadata(monkeypatch)
    installed = updater._installed(updater.components["cyrene-kernel"])
    manifest = installed["manifest"]
    commit = "c" * 40
    run = {"id": "1234", "attempt": 1}
    manifest.update(
        {
            "channel": "preview",
            "target": "ubuntu-22.04-x86_64",
            "source": {
                "repository": "https://github.com/Example/Platform",
                "ref": "refs/heads/develop",
                "commit": commit,
            },
            "provenance": {
                "attestation": {
                    "kind": "github-artifact-attestation",
                    "repository": "Example/Platform",
                    "workflow": "release.yml",
                    "predicateType": "https://slsa.dev/provenance/v1",
                    "subjectName": "kernel-manifest.json",
                    "run": run,
                }
            },
        }
    )
    component = updater.components["cyrene-kernel"]
    component.update({"publisher": "Example/Platform"})
    publisher = {
        "repository": "Example/Platform",
        "workflow": "release.yml",
        "releaseDiscovery": {"indexAssetName": "component-index.json"},
    }
    index_uri = (
        "https://github.com/Example/Platform/releases/download/preview-"
        + commit
        + "/component-index.json"
    )
    manifest_uri = (
        "https://github.com/Example/Platform/releases/download/preview-"
        + commit
        + "/kernel-manifest.json"
    )
    index = {
        "schemaVersion": 1,
        "repository": "Example/Platform",
        "channel": "preview",
        "source": {
            "repository": "https://github.com/Example/Platform",
            "ref": "refs/heads/develop",
            "commit": commit,
        },
        "provenance": {"attestation": {"subjectName": "component-index.json", "run": run}},
        "releases": [
            {
                "componentId": "cyrene-kernel",
                "version": manifest["version"],
                "target": manifest["target"],
                "manifestUri": manifest_uri,
                "manifestDigest": manifest["manifestDigest"],
            }
        ],
        "compatibilityGroups": [],
        "indexDigest": "sha256:" + "d" * 64,
    }
    index_bytes = json.dumps(index, separators=(",", ":")).encode()
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode()
    release = {
        "tag_name": "preview-" + commit,
        "draft": False,
        "prerelease": True,
        "immutable": True,
        "assets": [{"name": "component-index.json", "browser_download_url": index_uri}],
    }
    updater.publishers = {"Example/Platform": publisher}
    updater.catalog = {"channels": {"preview": {"sourceRefs": ["refs/heads/develop"]}}}
    updater._component_release_tag_prefix = lambda _component, _channel: None
    updater._get_json = lambda uri: (
        release if uri.endswith("/releases/tags/preview-" + commit) else None
    )
    updater._get_bytes = lambda uri: {index_uri: index_bytes, manifest_uri: manifest_bytes}[uri]
    updater._require_github_asset_uri = lambda uri, _repository: uri
    updater._validate_index = lambda *_args: None
    updater._verify_attestation = lambda *_args, **_kwargs: None
    updater._target_for = lambda _component: {"target": "ubuntu-22.04-x86_64"}
    updater._validate_manifest_digest = lambda value, expected: (
        None if value.get("manifestDigest") == expected else pytest.fail()
    )

    verified = migration.verify_active_release_attested(updater, "cyrene-kernel")

    assert verified is installed


def test_active_release_rejects_mutable_legacy_tag(tmp_path, monkeypatch):
    updater, _executable, _payload = _active_payload_fixture(tmp_path)
    _mock_root_owned_metadata(monkeypatch)
    installed = updater._installed(updater.components["cyrene-kernel"])
    manifest = installed["manifest"]
    manifest.update(
        {
            "channel": "preview",
            "source": {
                "repository": "https://github.com/Example/Platform",
                "ref": "refs/heads/develop",
                "commit": "c" * 40,
            },
        }
    )
    updater.components["cyrene-kernel"]["publisher"] = "Example/Platform"
    updater.publishers = {
        "Example/Platform": {
            "repository": "Example/Platform",
            "workflow": "release.yml",
            "releaseDiscovery": {"indexAssetName": "component-index.json"},
        }
    }
    updater.catalog = {"channels": {"preview": {"sourceRefs": ["refs/heads/develop"]}}}
    updater._component_release_tag_prefix = lambda _component, _channel: None
    updater._get_json = lambda _uri: {
        "tag_name": "preview-" + "c" * 40,
        "draft": False,
        "prerelease": True,
        "immutable": False,
        "assets": [],
    }

    with pytest.raises(migration.MigrationError, match="immutable"):
        migration.verify_active_release_attested(updater, "cyrene-kernel")


def _schema_adoption_fixture(*, staged_ids=None, old_versions=(1, 1)):
    group = {
        "groupId": migration.NATIVE_GROUP_ID,
        "groupVersion": "2",
        "contractApiVersion": "0.1.0",
        "wireApiVersion": "cyrene.runtime-maintenance.binding-operations.v1",
        "contractLock": {
            "repository": "DoHorizon-AI/Cyrene-Workspace",
            "commit": "a" * 40,
            "path": "governance/package-runtime-protocols-v1.lock.json",
            "sha256": "sha256:" + "b" * 64,
        },
        "members": [
            {
                "componentId": component_id,
                "requiredForAdoption": True,
                "protocolVersion": protocol,
            }
            for component_id, protocol in migration.NATIVE_GROUP_MEMBERS.items()
        ],
    }
    components = {
        component_id: {
            "componentId": component_id,
            "compatibilityGroup": migration.NATIVE_GROUP_ID,
            "protocolVersion": protocol,
        }
        for component_id, protocol in migration.NATIVE_GROUP_MEMBERS.items()
    }
    staged = []
    for component_id, protocol in migration.NATIVE_GROUP_MEMBERS.items():
        if staged_ids is not None and component_id not in staged_ids:
            continue
        staged.append(
            {
                "componentId": component_id,
                "manifest": {
                    "schemaVersion": 2,
                    "componentId": component_id,
                    "protocolVersion": protocol,
                    "compatibility": {
                        "groupId": migration.NATIVE_GROUP_ID,
                        "groupVersion": "2",
                        "contractApiVersion": "0.1.0",
                        "wireApiVersion": "cyrene.runtime-maintenance.binding-operations.v1",
                        "contractLock": group["contractLock"],
                    },
                },
            }
        )
    old_by_id = {
        "cyrene-runtime-maintenance": old_versions[0],
        "cyrene-kernel": old_versions[1],
    }

    def installed(component):
        component_id = component["componentId"]
        if component_id == "cy-package-runtime":
            return {"active": False}
        version = old_by_id[component_id]
        manifest = {"schemaVersion": version}
        if version == 2:
            manifest["compatibility"] = {"groupId": migration.NATIVE_GROUP_ID}
        return {"active": True, "identityAttested": True, "manifest": manifest}

    updater = SimpleNamespace(
        catalog={"compatibilityGroups": [group]},
        components=components,
        _installed=installed,
    )
    return updater, {"components": staged}


def test_schema_adoption_requires_complete_c10_group_and_signed_c9_pair(monkeypatch):
    updater, staged = _schema_adoption_fixture()
    monkeypatch.setattr(
        migration, "verify_active_release_attested", lambda _updater, _component_id: None
    )

    assert migration.is_schema1_group_adoption(updater, staged) is True


def test_schema_adoption_rejects_partial_or_mixed_old_core():
    partial_updater, partial = _schema_adoption_fixture(
        staged_ids={"cyrene-kernel", "cyrene-runtime-maintenance"}
    )
    assert migration.is_schema1_group_adoption(partial_updater, partial) is False

    mixed_updater, full = _schema_adoption_fixture(old_versions=(1, 2))
    with pytest.raises(migration.MigrationError, match="mixed"):
        migration.is_schema1_group_adoption(mixed_updater, full)


def test_post_end_client_restore_retry_keeps_healthy_c10_core(tmp_path, monkeypatch):
    updater = updates.ComponentUpdater(
        catalog_path=WORKSPACE_ROOT / "packaging" / "component-catalog-bootstrap-v1.json",
        activity_catalog_path=tmp_path / "activity-sources.json",
        state_root=tmp_path / "update-state",
        install_root=tmp_path / "install",
        release_lock_path=WORKSPACE_ROOT / "release-lock.json",
        trusted_catalog_digest=updates.TRUSTED_CATALOG_DIGEST,
        load_active_catalog=False,
    )
    transaction_path = tmp_path / "update-state" / "transactions" / "plan.json"
    transaction_path.parent.mkdir(parents=True, mode=0o700)
    transaction_path.parent.chmod(0o700)
    transaction = {
        "phase": "success_end_pending",
        "migrationPhase": "success_end_pending",
        "planId": "plan-" + "a" * 32,
        "planDigest": "sha256:" + "b" * 64,
        "targetKind": "CORE_RUNTIME",
        "maintenanceToken": "private-token",
        "expectedCatalogGeneration": 10,
        "maintenanceGateGeneration": 8,
        "components": [],
    }
    current = [
        {
            "componentId": component_id,
            "version": "1.0.0",
            "releaseIdentity": "sha256:" + digest,
            "manifestDigest": "sha256:" + digest,
            "artifactDigest": "sha256:" + hashlib.sha256(component_id.encode()).hexdigest(),
            "pointerIdentity": "1.0.0--" + digest,
            "bundleIdentity": None,
            "identityAttested": True,
        }
        for component_id, digest in (
            ("cyrene-runtime-maintenance", "c" * 64),
            ("cyrene-kernel", "d" * 64),
            ("cy-package-runtime", "e" * 64),
        )
    ]
    transaction["components"] = [dict(item) for item in current]
    calls = {"health": 0, "end": 0, "restore": 0, "begin": 0, "rollback": 0, "restart": 0}
    monkeypatch.setattr(
        updater,
        "_validate_schema2_core_health",
        lambda _tx: calls.__setitem__("health", calls["health"] + 1),
    )
    monkeypatch.setattr(updater, "_capture_active_versions", lambda _components: current)
    monkeypatch.setattr(
        updater,
        "_readiness_for",
        lambda *_args, **_kwargs: {
            "status": "MAINTENANCE_ACTIVE" if calls["end"] == 0 else "READY"
        },
    )
    monkeypatch.setattr(
        updater,
        "_end_maintenance",
        lambda *_args, **_kwargs: calls.__setitem__("end", calls["end"] + 1),
    )

    def restore_clients(_transaction, *, core_only):
        assert core_only is False
        calls["restore"] += 1
        if calls["restore"] == 1:
            raise updates.UpdateError("CLIENT_RESTORE_PENDING", "simulated client start failure")

    monkeypatch.setattr(updater, "_restore_previously_active_clients", restore_clients)
    monkeypatch.setattr(updater, "_applied_result", lambda _tx: {"status": "applied"})
    updates._atomic_json(transaction_path, transaction)

    with pytest.raises(updates.UpdateError, match="simulated client start failure"):
        updater._complete_schema_adoption(transaction, transaction_path)
    assert transaction["migrationPhase"] == "clients_restore_pending"

    result = updater._complete_schema_adoption(transaction, transaction_path)

    assert result == {"status": "applied"}
    assert calls == {"health": 2, "end": 1, "restore": 2, "begin": 0, "rollback": 0, "restart": 0}
    assert transaction["migrationPhase"] == "complete"


def _shared_state_fixture(tmp_path: Path, *, experimental: bool = False):
    root = tmp_path / "runtime"
    root.mkdir()
    root.chmod(0o2770)
    snapshot = json.dumps(_state(experimental=experimental)).encode() + b"\n"
    (root / "maintenance-state.json").write_bytes(snapshot)
    (root / "maintenance-state.json").chmod(0o660)
    (root / "maintenance-journal.jsonl").write_bytes(b"")
    (root / "maintenance-journal.jsonl").chmod(0o660)
    return root


def _mock_runtime_root_metadata(monkeypatch, root: Path):
    original_lstat = Path.lstat
    original_fstat = os.fstat

    def root_owned(info, *, group=False):
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_uid=0,
            st_gid=info.st_gid if group else 77,
            st_nlink=info.st_nlink,
            st_dev=info.st_dev,
            st_ino=info.st_ino,
            st_size=info.st_size,
        )

    def fake_lstat(path):
        info = original_lstat(path)
        if path == root:
            return root_owned(info, group=True)
        if path.parent == root and path.name in {
            "maintenance-state.json",
            "maintenance-journal.jsonl",
        }:
            return root_owned(info, group=True)
        return info

    def fake_fstat(descriptor):
        info = original_fstat(descriptor)
        return root_owned(info, group=True)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    monkeypatch.setattr(migration.os, "fstat", fake_fstat)


def test_schema1_profile_reads_fixed_root_owned_shared_state_files(tmp_path, monkeypatch):
    root = _shared_state_fixture(tmp_path, experimental=True)
    _mock_runtime_root_metadata(monkeypatch, root)

    assert migration.read_schema1_profile(root) == "experimental-v1-binding-admissions"


@pytest.mark.parametrize("unsafe", ["symlink", "mode", "hardlink", "fifo"])
def test_schema1_profile_rejects_unsafe_shared_state_files(tmp_path, monkeypatch, unsafe):
    root = _shared_state_fixture(tmp_path)
    snapshot = root / "maintenance-state.json"
    if unsafe == "symlink":
        snapshot.unlink()
        snapshot.symlink_to(root / "maintenance-journal.jsonl")
    elif unsafe == "mode":
        snapshot.chmod(0o640)
    elif unsafe == "hardlink":
        os.link(snapshot, root / "state-alias")
    else:
        snapshot.unlink()
        os.mkfifo(snapshot)
    _mock_runtime_root_metadata(monkeypatch, root)

    with pytest.raises(migration.MigrationError):
        migration.read_schema1_profile(root)


def _unit_inventory_updater(unit_output: str, states: dict[str, tuple[str, int]]):
    components = {
        component_id: {
            "componentId": component_id,
            "systemdUnit": unit,
            "restart": {"unit": unit, "group": group, "order": order},
        }
        for component_id, unit, group, order in (
            ("cy-package-runtime", "cyrene-package-runtime.service", "single-service", 0),
            ("cyrene-yield", "cyrene-yield.service", "single-service", 0),
            ("cyrene-linux-sys-adapter", "cyrene-linux-sys-adapter.service", "core-runtime", 10),
            ("cyrene-sandboxd", "cyrene-sandboxd.service", "core-runtime", 20),
            ("cyrene-kernel", "cyrene-kernel.service", "core-runtime", 30),
            (
                "cyrene-runtime-maintenance",
                "cyrene-runtime-maintenance.service",
                "single-service",
                0,
            ),
        )
    }

    def runner(command, **_kwargs):
        if command[1] == "list-unit-files":
            return SimpleNamespace(returncode=0, stdout=unit_output, stderr="")
        unit = command[2]
        state, pid = states[unit]
        return SimpleNamespace(returncode=0, stdout=f"{state}\n{pid}\n", stderr="")

    updater = SimpleNamespace(
        components=components,
        runner=runner,
        _catalog_matched_unit=lambda component: component.get("systemdUnit"),
        _installed=lambda _component: {"active": True, "identityAttested": True},
        _unit_exists=lambda _component: True,
    )
    return updater


def test_unit_inventory_captures_only_catalogued_active_units_and_orders_quiesce():
    unit_output = "\n".join(
        f"{unit} enabled enabled"
        for unit in (
            "cyrene-package-runtime.service",
            "cyrene-yield.service",
            "cyrene-linux-sys-adapter.service",
            "cyrene-sandboxd.service",
            "cyrene-kernel.service",
            "cyrene-runtime-maintenance.service",
        )
    )
    states = {
        unit: ("active", pid)
        for unit, pid in zip(
            (line.split()[0] for line in unit_output.splitlines()),
            range(100, 106),
            strict=True,
        )
    }
    updater = _unit_inventory_updater(unit_output, states)

    inventory = migration.capture_unit_inventory(updater, {"cy-package-runtime"})

    assert migration.stop_unit_order(updater, inventory) == [
        "cyrene-package-runtime.service",
        "cyrene-yield.service",
        "cyrene-kernel.service",
        "cyrene-sandboxd.service",
        "cyrene-linux-sys-adapter.service",
        "cyrene-runtime-maintenance.service",
    ]
    assert migration.start_core_unit_order(updater, inventory) == [
        "cyrene-linux-sys-adapter.service",
        "cyrene-sandboxd.service",
        "cyrene-runtime-maintenance.service",
        "cyrene-kernel.service",
        "cyrene-package-runtime.service",
    ]


@pytest.mark.parametrize(
    "unit_line",
    [
        "cyrene-unowned.service enabled enabled",
        "cyrene-unknown.timer enabled enabled",
    ],
)
def test_unit_inventory_rejects_unclassified_cyrene_units(unit_line):
    updater = _unit_inventory_updater(unit_line, {})

    with pytest.raises(migration.MigrationError, match="Unclassified"):
        migration.capture_unit_inventory(updater, set())


def test_unit_inventory_rejects_transitional_state():
    unit = "cyrene-kernel.service"
    updater = _unit_inventory_updater(f"{unit} enabled enabled", {unit: ("activating", 123)})

    with pytest.raises(migration.MigrationError, match="transitional"):
        migration.capture_unit_inventory(updater, set())


def _mock_root_owned_metadata(monkeypatch):
    lstat = migration._lstat

    def root_owned(path, description):
        info = lstat(path, description)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_nlink=info.st_nlink)

    monkeypatch.setattr(migration, "_lstat", root_owned)
