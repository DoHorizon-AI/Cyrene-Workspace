"""Tests for exact C9 Health decoding and signed schema-1 rollback identity."""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
UPDATES_PATH = ROOT / "packaging" / "component_updates.py"
_SPEC = importlib.util.spec_from_file_location(
    "native_legacy_rollback_component_updates", UPDATES_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
updates = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = updates
_SPEC.loader.exec_module(updates)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _identity(component_id: str, *, version: str, digest: str) -> dict[str, Any]:
    return {
        "componentId": component_id,
        "version": version,
        "releaseIdentity": digest,
        "manifestDigest": digest,
        "artifactDigest": _digest("f" if digest != _digest("f") else "e"),
        "pointerIdentity": f"{version}--{digest.removeprefix('sha256:')}",
        "bundleIdentity": None,
        "identityAttested": True,
    }


def _legacy_manifest(
    updater: Any,
    component_id: str,
    identity: dict[str, Any],
    *,
    source_repository: str = "https://github.com/DoHorizon-AI/Cyrene-Platform",
    workflow: str = ("DoHorizon-AI/Cyrene-Platform/.github/workflows/component-release.yml"),
) -> dict[str, Any]:
    component = updater.components[component_id]
    publisher_repository = component["publisher"]
    executable_digest = _digest("a" if component_id == updates.BROKER_COMPONENT_ID else "b")
    artifact = {
        "kind": "native-binary",
        "sha256": identity["artifactDigest"],
        "sizeBytes": 12,
        "entrypoint": "bin/cyrene-runtime-maintenance"
        if component_id == updates.BROKER_COMPONENT_ID
        else f"bin/{component_id}",
        "files": {
            "bin/cyrene-runtime-maintenance"
            if component_id == updates.BROKER_COMPONENT_ID
            else f"bin/{component_id}": executable_digest
        },
    }
    target = updater._target_for(component)
    return {
        "schemaVersion": 1,
        "componentId": component_id,
        "version": identity["version"],
        "manifestDigest": identity["manifestDigest"],
        "target": target["target"],
        "channel": "stable",
        "source": {
            "repository": source_repository,
            "ref": "refs/heads/main",
            "commit": "c" * 40,
        },
        "artifact": artifact,
        "provenance": {
            "attestation": {
                "kind": "github-artifact-attestation",
                "repository": publisher_repository,
                "workflow": workflow,
                "predicateType": "https://slsa.dev/provenance/v1",
                "run": {"id": "123456789", "attempt": 1},
            }
        },
    }


def _rollback_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, dict[str, Any]]:
    updater = updates.ComponentUpdater(
        catalog_path=ROOT / "packaging" / "component-catalog-bootstrap-v1.json",
        activity_catalog_path=tmp_path / "activity-sources.json",
        state_root=tmp_path / "update-state",
        install_root=tmp_path / "install-root",
        release_lock_path=tmp_path / "missing-release-lock.json",
        trusted_catalog_digest=updates.TRUSTED_CATALOG_DIGEST,
        load_active_catalog=False,
    )
    helper = updater._load_native_runtime_schema_migration()
    group = next(
        item
        for item in updater.catalog["compatibilityGroups"]
        if item["groupId"] == helper.NATIVE_GROUP_ID
    )
    broker_id = updates.BROKER_COMPONENT_ID
    component_ids = tuple(helper.NATIVE_GROUP_MEMBERS)
    old_identities = {
        component_id: _identity(
            component_id,
            version="1.0.0",
            digest=_digest(character),
        )
        for component_id, character in zip(component_ids[:2], ("1", "2"), strict=True)
    }
    old_identities["cy-package-runtime"] = {
        "componentId": "cy-package-runtime",
        "version": None,
        "releaseIdentity": None,
        "manifestDigest": None,
        "artifactDigest": None,
        "pointerIdentity": None,
        "bundleIdentity": None,
        "identityAttested": False,
    }
    current = [dict(old_identities[item]) for item in component_ids]
    candidate_rows = []
    for component_id, protocol in helper.NATIVE_GROUP_MEMBERS.items():
        candidate_digest = _digest(
            "d" if component_id == broker_id else "e" if component_id == "cyrene-kernel" else "9"
        )
        candidate_compatibility = {
            "groupId": group["groupId"],
            "groupVersion": group["groupVersion"],
            "contractApiVersion": group["contractApiVersion"],
            "wireApiVersion": group["wireApiVersion"],
            "contractLock": group["contractLock"],
        }
        manifest = {
            "schemaVersion": 2,
            "componentId": component_id,
            "protocolVersion": protocol,
            "compatibility": candidate_compatibility,
        }
        candidate_rows.append(
            {
                "componentId": component_id,
                "version": "2.0.0",
                "releaseIdentity": candidate_digest,
                "manifestDigest": candidate_digest,
                "artifactDigest": _digest(
                    "8"
                    if component_id == broker_id
                    else "7"
                    if component_id == "cyrene-kernel"
                    else "6"
                ),
                "pointerIdentity": f"2.0.0--{candidate_digest.removeprefix('sha256:')}",
                "bundleIdentity": None,
                "manifest": manifest,
            }
        )
    pre_active_units = [
        {
            "unit": updater._catalog_matched_unit(updater.components[component_id]),
            "componentId": component_id,
            "unitFileState": "enabled",
            "active": component_id == broker_id,
        }
        for component_id in component_ids
    ]
    previous = [old_identities[item] for item in component_ids]
    transaction = {
        "schemaVersion": 2,
        "phase": "rollback_end_pending",
        "migrationPhase": "rollback_end_pending",
        "planId": "plan-" + "a" * 32,
        "planDigest": _digest("c"),
        "targetKind": "CORE_RUNTIME",
        "schemaAdoption": True,
        "schema1Profile": "released-v1-no-binding-admissions",
        "maintenanceToken": "maintenance-token-" + "a" * 40,
        "expectedCatalogGeneration": 12,
        "maintenanceGateGeneration": 9,
        "components": candidate_rows,
        "previous": previous,
        "preActiveUnits": pre_active_units,
    }
    target = {"target": "ubuntu-24.04-x86_64"}
    monkeypatch.setattr(updater, "_target_for", lambda _component: target)

    installed: dict[str, dict[str, Any]] = {}
    for component_id in component_ids[:2]:
        identity = old_identities[component_id]
        installed[component_id] = {
            "activeVersion": identity["version"],
            "manifest": _legacy_manifest(updater, component_id, identity),
            "active": True,
            "identityAttested": True,
            "releaseIdentity": identity["releaseIdentity"],
            "manifestDigest": identity["manifestDigest"],
            "artifactDigest": identity["artifactDigest"],
            "pointerIdentity": identity["pointerIdentity"],
            "bundleIdentity": None,
        }
    monkeypatch.setattr(
        updater, "_installed", lambda component: installed[component["componentId"]]
    )
    migration = SimpleNamespace(
        SCHEMA1_PROFILES=helper.SCHEMA1_PROFILES,
        NATIVE_GROUP_MEMBERS=helper.NATIVE_GROUP_MEMBERS,
        verify_active_native_payload=lambda *_args: None,
    )
    monkeypatch.setattr(updater, "_load_native_runtime_schema_migration", lambda: migration)
    monkeypatch.setattr(updater, "_capture_active_versions", lambda _components: current)
    monkeypatch.setattr(updater, "_unit_exists", lambda _component: True)
    monkeypatch.setattr(updater, "_resolve_installed_broker", lambda: tmp_path / "broker")
    monkeypatch.setattr(updater, "_validate_active_legacy_broker_process", lambda *_args: None)
    return updater, transaction


def test_exact_signed_schema1_health_without_protocol_version_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater, transaction = _rollback_fixture(tmp_path, monkeypatch)
    response = {
        "status": "SERVING",
        "core_bootstrap_eligible": False,
        "catalog_generation": transaction["expectedCatalogGeneration"],
        "gate_generation": transaction["maintenanceGateGeneration"],
    }
    monkeypatch.setattr(updater, "_broker_request", lambda method, _params: response)

    updater._validate_legacy_core_health(transaction)


@pytest.mark.parametrize(
    "response",
    [
        {
            "status": "SERVING",
            "core_bootstrap_eligible": False,
            "catalog_generation": 12,
            "gate_generation": 9,
            "protocol_version": "cyrene.runtime-maintenance.broker.v1",
        },
        {
            "status": "SERVING",
            "core_bootstrap_eligible": 0,
            "catalog_generation": 12,
            "gate_generation": 9,
        },
        {
            "status": "SERVING",
            "core_bootstrap_eligible": False,
            "catalog_generation": 11,
            "gate_generation": 9,
        },
        {
            "status": "SERVING",
            "core_bootstrap_eligible": False,
            "catalog_generation": 12,
            "gate_generation": 8,
        },
        {
            "status": "UNKNOWN",
            "core_bootstrap_eligible": False,
            "catalog_generation": 12,
            "gate_generation": 9,
        },
    ],
)
def test_schema1_health_with_extra_missing_or_wrong_state_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, Any],
) -> None:
    updater, transaction = _rollback_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(updater, "_broker_request", lambda _method, _params: response)

    with pytest.raises(updates.UpdateError, match="held generations"):
        updater._validate_legacy_core_health(transaction)


@pytest.mark.parametrize(
    "mutation",
    ["source", "attestation", "broker_identity", "unattested_receipt", "profile"],
)
def test_wrong_legacy_source_signature_identity_or_profile_is_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    updater, transaction = _rollback_fixture(tmp_path, monkeypatch)
    installed = updater._installed(updater.components[updates.BROKER_COMPONENT_ID])
    if mutation == "source":
        installed["manifest"]["source"]["repository"] = "https://example.invalid/other"
    elif mutation == "attestation":
        installed["manifest"]["provenance"]["attestation"]["workflow"] = "wrong.yml"
    elif mutation == "broker_identity":
        transaction["previous"][0]["manifestDigest"] = _digest("a")
        transaction["previous"][0]["releaseIdentity"] = _digest("a")
        transaction["previous"][0]["pointerIdentity"] = f"1.0.0--{'a' * 64}"
    elif mutation == "unattested_receipt":
        transaction["previous"][0]["identityAttested"] = False
    else:
        transaction["schema1Profile"] = "unknown-profile"

    with pytest.raises(updates.UpdateError):
        updater._validate_restored_legacy_core_identity(transaction)


def test_schema2_health_remains_strict_about_protocol_and_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater, transaction = _rollback_fixture(tmp_path, monkeypatch)
    old_health = {
        "status": "SERVING",
        "core_bootstrap_eligible": False,
        "catalog_generation": transaction["expectedCatalogGeneration"],
        "gate_generation": transaction["maintenanceGateGeneration"],
    }
    monkeypatch.setattr(updater, "_broker_request", lambda _method, _params: old_health)

    with pytest.raises(updates.UpdateError) as error:
        updater._validate_schema2_core_health(transaction)

    assert error.value.code == "SCHEMA_CORE_HEALTH_UNKNOWN"


def test_restored_broker_pid_must_match_the_verified_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updater, _transaction = _rollback_fixture(tmp_path, monkeypatch)
    executable = tmp_path / "signed-broker"
    manifest = {
        "artifact": {
            "entrypoint": "bin/cyrene-runtime-maintenance",
            "files": {"bin/cyrene-runtime-maintenance": _digest("a")},
        }
    }
    monkeypatch.setattr(
        updater,
        "runner",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="ActiveState=active\nSubState=running\nMainPID=4321\n",
            stderr="",
        ),
    )
    original_stat = Path.stat

    def fake_readlink(path: str | os.PathLike[str]) -> str:
        if os.fspath(path) == "/proc/4321/exe":
            return str(executable)
        raise OSError("unexpected executable path")

    def fake_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result | SimpleNamespace:
        if os.fspath(path) == "/proc/4321/exe":
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o755)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(updates, "os", SimpleNamespace(readlink=fake_readlink))
    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(updates, "_file_digest", lambda _path: _digest("a"))

    updates.ComponentUpdater._validate_active_legacy_broker_process(
        updater, "cyrene-runtime-maintenance.service", executable, manifest
    )

    seen: list[str] = []

    def unsigned_readlink(path: str | os.PathLike[str]) -> str:
        seen.append(os.fspath(path))
        if os.fspath(path) == "/proc/4321/exe":
            return str(tmp_path / "unsigned-broker")
        raise OSError("unexpected executable path")

    monkeypatch.setattr(updates, "os", SimpleNamespace(readlink=unsigned_readlink))
    with pytest.raises(updates.UpdateError, match="receipt-pinned executable"):
        updates.ComponentUpdater._validate_active_legacy_broker_process(
            updater, "cyrene-runtime-maintenance.service", executable, manifest
        )
    assert "/proc/4321/exe" in seen
