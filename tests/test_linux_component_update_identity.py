"""Release identity regressions for the Linux component updater."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
UPDATES_PATH = WORKSPACE_ROOT / "packaging" / "component_updates.py"
BUNDLE_PATH = WORKSPACE_ROOT / "packaging" / "service_bundle.py"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


updates = _load_module("cyrene_component_updates_identity_test", UPDATES_PATH)
bundle = _load_module("cyrene_service_bundle_identity_test", BUNDLE_PATH)


def _updater(tmp_path: Path) -> Any:
    catalog_path = tmp_path / "component-catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "defaultChannel": "stable",
                "channels": {"stable": {}, "preview": {}},
                "components": [],
                "targets": [],
                "publishers": [],
            }
        ),
        encoding="utf-8",
    )
    return updates.ComponentUpdater(
        catalog_path=catalog_path,
        activity_catalog_path=tmp_path / "activity-sources.json",
        state_root=tmp_path / "state",
        install_root=tmp_path / "install",
        broker_path=tmp_path / "missing-broker",
        trusted_catalog_digest=None,
    )


def _candidate(
    component: dict[str, Any], *, version: str, manifest_digest: str, artifact_digest: str
) -> Any:
    artifact_kind = component.get("artifactKind", "python-bundle")
    manifest = {
        "version": version,
        "manifestDigest": manifest_digest,
        "artifact": {"kind": artifact_kind, "sha256": artifact_digest},
        "dependencies": [],
    }
    return updates.Candidate(
        component=component,
        manifest=manifest,
        manifest_digest=manifest_digest,
        artifact_digest=artifact_digest,
        manifest_uri="https://updates.invalid/manifest.json",
        index={},
        index_uri="https://updates.invalid/index.json",
    )


def _check_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidate: Any,
    installed: dict[str, Any],
) -> dict[str, Any]:
    updater = _updater(tmp_path)
    component = candidate.component
    component_id = component["componentId"]
    updater.components[component_id] = component
    monkeypatch.setattr(
        updater,
        "_target_for",
        lambda value: {
            "target": "linux-x86_64",
            "artifactKind": candidate.manifest["artifact"]["kind"],
        },
    )
    monkeypatch.setattr(updater, "_candidate", lambda *args, **kwargs: candidate)
    monkeypatch.setattr(updater, "_installed", lambda value: installed)
    return updater.check([component_id], channel="stable", include_readiness=False)


def test_python_check_treats_verified_outer_and_inner_identity_as_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exact payload/manifest identity should not create a redundant plan."""

    component = {
        "componentId": "cyrene-test-product",
        "pythonBundleService": "navigator",
        "restart": {"group": "single-service", "unit": "cyrene-test-product.service"},
    }
    manifest_digest = "sha256:" + "1" * 64
    artifact_digest = "sha256:" + "2" * 64
    candidate = _candidate(
        component,
        version="0.4.0",
        manifest_digest=manifest_digest,
        artifact_digest=artifact_digest,
    )
    installed = {
        "activeVersion": "0.4.0",
        "manifest": {"manifestDigest": manifest_digest},
        "releaseIdentity": manifest_digest,
        "manifestDigest": manifest_digest,
        "artifactDigest": artifact_digest,
        "bundleIdentity": "3" * 64,
        "identityAttested": True,
        "active": True,
    }

    result = _check_identity(tmp_path, monkeypatch, candidate=candidate, installed=installed)

    assert result["plans"] == []
    assert result["components"][0]["phase"] == "current"
    assert result["components"][0]["updateAvailable"] is False


def test_python_check_detects_new_outer_artifact_identity_without_version_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    component = {
        "componentId": "cyrene-test-product",
        "pythonBundleService": "navigator",
        "restart": {"group": "single-service", "unit": "cyrene-test-product.service"},
    }
    old_manifest_digest = "sha256:" + "4" * 64
    old_artifact_digest = "sha256:" + "5" * 64
    new_manifest_digest = "sha256:" + "6" * 64
    new_artifact_digest = "sha256:" + "7" * 64
    candidate = _candidate(
        component,
        version="0.4.0",
        manifest_digest=new_manifest_digest,
        artifact_digest=new_artifact_digest,
    )
    installed = {
        "activeVersion": "0.4.0",
        "manifest": {"manifestDigest": old_manifest_digest},
        "releaseIdentity": old_manifest_digest,
        "manifestDigest": old_manifest_digest,
        "artifactDigest": old_artifact_digest,
        "bundleIdentity": "3" * 64,
        "identityAttested": True,
        "active": True,
    }

    result = _check_identity(tmp_path, monkeypatch, candidate=candidate, installed=installed)

    assert result["plans"]
    assert result["components"][0]["activeVersion"] == "0.4.0"
    assert result["components"][0]["availableVersion"] == "0.4.0"
    assert result["components"][0]["updateAvailable"] is True


def _native_payload_bytes(marker: str) -> bytes:
    return f"#!/bin/sh\nprintf '%s\\n' '{marker}'\n".encode()


def _native_manifest(component_id: str, version: str, payload_marker: str) -> dict[str, Any]:
    manifest = {
        "componentId": component_id,
        "version": version,
        "artifact": {
            "kind": "native-binary",
            "entrypoint": "bin/tool",
            "sha256": "sha256:" + hashlib.sha256(_native_payload_bytes(payload_marker)).hexdigest(),
        },
    }
    manifest["manifestDigest"] = updates._digest_json(manifest, "manifestDigest")
    return manifest


def _native_payload(path: Path, marker: str) -> None:
    path.mkdir()
    binary = path / "bin" / "tool"
    binary.parent.mkdir()
    binary.write_bytes(_native_payload_bytes(marker))
    binary.chmod(0o755)


def _active_manifest(updater: Any, component: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    installed = updater._installed(component)
    assert installed["active"] is True
    release_identity = installed.get("releaseIdentity") or installed["manifest"]["manifestDigest"]
    return release_identity, installed["manifest"]


def test_native_same_version_releases_have_distinct_identity_and_exact_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _updater(tmp_path)
    component_id = "cyrene-test-native"
    component = {
        "componentId": component_id,
        "restart": {"group": "single-service", "unit": "cyrene-test-native.service"},
    }
    updater.components[component_id] = component
    version = "1.2.3"
    old_manifest = _native_manifest(component_id, version, "old")
    new_manifest = _native_manifest(component_id, version, "new")
    old_payload = tmp_path / "old-payload"
    new_payload = tmp_path / "new-payload"
    _native_payload(old_payload, "old")
    _native_payload(new_payload, "new")
    old_candidate = SimpleNamespace(
        component=component, manifest=old_manifest, manifest_digest=old_manifest["manifestDigest"]
    )
    new_candidate = SimpleNamespace(
        component=component, manifest=new_manifest, manifest_digest=new_manifest["manifestDigest"]
    )

    old_release = updater._install_native_release(old_candidate, old_payload)
    new_release = updater._install_native_release(new_candidate, new_payload)

    assert old_release != new_release
    assert old_release.name.startswith(version + "--")
    assert new_release.name.startswith(version + "--")
    assert old_release.name.endswith(old_manifest["manifestDigest"].split(":", 1)[1])
    assert new_release.name.endswith(new_manifest["manifestDigest"].split(":", 1)[1])
    component_root = tmp_path / "install" / "components" / component_id
    active = component_root / "active"
    active.symlink_to(os.path.relpath(old_release, component_root))
    real_lstat = Path.lstat

    def root_owned_active(path: Path):
        info = real_lstat(path)
        if path == active:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0)
        return info

    monkeypatch.setattr(Path, "lstat", root_owned_active)
    old_pointer_target = os.readlink(active)
    installed_old = updater._installed(component)
    old_identity, active_manifest = _active_manifest(updater, component)
    assert installed_old["identityAttested"] is True
    assert installed_old["pointerIdentity"] == old_release.name
    assert installed_old["artifactDigest"] == old_manifest["artifact"]["sha256"]
    assert old_identity == old_manifest["manifestDigest"]
    assert active_manifest["manifestDigest"] == old_manifest["manifestDigest"]

    previous = updater._capture_active_versions([{"componentId": component_id}])
    transaction = {
        "targetKind": "PACKAGE_ONLY",
        "components": [
            {
                "componentId": component_id,
                "version": version,
                "manifestDigest": new_manifest["manifestDigest"],
                "artifactDigest": new_manifest["artifact"]["sha256"],
                "releasePath": str(new_release),
                "manifest": new_manifest,
            }
        ],
        "previous": previous,
    }
    monkeypatch.setattr(updater, "_restart_transaction", lambda value: None)
    monkeypatch.setattr(updater, "_health_transaction", lambda value: None)
    updater._activate_transaction(transaction)
    assert _active_manifest(updater, component)[0] == new_manifest["manifestDigest"]

    healthy, _ = updater._rollback_transaction(transaction)

    assert healthy is True
    assert os.readlink(active) == old_pointer_target
    restored_identity, restored_manifest = _active_manifest(updater, component)
    assert restored_identity == old_manifest["manifestDigest"]
    assert restored_manifest["manifestDigest"] == old_manifest["manifestDigest"]


def _legacy_v1_bundle(root: Path, marker: str = "legacy") -> dict[str, Any]:
    root.mkdir(parents=True)
    entrypoint = root / "run-service"
    entrypoint.write_text(f"#!/bin/sh\nprintf '%s\\n' '{marker}'\n", encoding="utf-8")
    entrypoint.chmod(0o755)
    lock = root / "requirements.lock"
    lock.write_text("cyrene-navigator==1.2.3 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
    source = {
        "schema_version": 1,
        "service": "navigator",
        "source_repository": "Cyrene-Navigator",
        "source_commit": "c" * 40,
        "requirements_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
    }
    (root / "source.json").write_text(json.dumps(source, sort_keys=True), encoding="utf-8")
    files = {
        "requirements.lock": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "run-service": hashlib.sha256(entrypoint.read_bytes()).hexdigest(),
        "source.json": hashlib.sha256((root / "source.json").read_bytes()).hexdigest(),
    }
    manifest = {
        "schema_version": 1,
        "service": "navigator",
        "version": "",
        "entrypoint": "run-service",
        "health": {"path": bundle.HEALTH_PATHS["navigator"]},
        "files": files,
        "source_repository": "Cyrene-Navigator",
        "source_commit": "c" * 40,
        "dependencies": {
            "lock_file": "requirements.lock",
            "lock_sha256": files["requirements.lock"],
        },
        "target": {"debian_arch": "amd64", "python": "3.12"},
        "artifact_digest": "",
    }
    digest = bundle._artifact_digest(manifest)
    manifest["version"] = digest
    manifest["artifact_digest"] = digest
    (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return bundle.validate_bundle(root, expected_service="navigator")


def _v2_bundle(root: Path, marker: str) -> dict[str, Any]:
    root.mkdir(parents=True)
    entrypoint = root / "run-service"
    entrypoint.write_text(f"#!/bin/sh\nprintf '%s\\n' '{marker}'\n", encoding="utf-8")
    entrypoint.chmod(0o755)
    wheel_hash = "d" * 64
    lock = root / "requirements.lock"
    lock.write_text(
        "cyrene-runtime-maintenance==0.1.0 \\\n    --hash=sha256:" + wheel_hash + "\n",
        encoding="utf-8",
    )
    lock_digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    runtime = [
        {
            "component_id": bundle.RUNTIME_SDK_COMPONENT_ID,
            "manifest_digest": "sha256:" + "e" * 64,
            "artifact_digest": "sha256:" + "f" * 64,
            "distribution": bundle.RUNTIME_SDK_DISTRIBUTION,
            "version": "0.1.0",
            "wheel_sha256": wheel_hash,
        }
    ]
    source = {
        "schema_version": 2,
        "service": "navigator",
        "source_repository": "Cyrene-Navigator",
        "source_commit": "c" * 40,
        "requirements_lock_sha256": lock_digest,
        "runtime_dependencies": runtime,
    }
    (root / "source.json").write_text(json.dumps(source, sort_keys=True), encoding="utf-8")
    bundle._write_bundle_manifest(
        root,
        service="navigator",
        source_commit="c" * 40,
        source_repository="Cyrene-Navigator",
        debian_arch=bundle._debian_arch_for_host(),
        python_version="3.12",
        runtime_dependencies=runtime,
    )
    return bundle.validate_bundle(root, expected_service="navigator")


def _use_fixture_bundle_paths(
    updater: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bypass host ownership checks for isolated /tmp release-path fixtures."""

    def ensure_directory(path: Path, *, create: bool) -> Path:
        path = Path(path)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        elif path.is_symlink() or not path.is_dir():
            raise bundle.ServiceBundleError(
                f"fixture service directory is missing or unsafe: {path}"
            )
        return path

    monkeypatch.setattr(bundle, "_ensure_secure_directory", ensure_directory)
    monkeypatch.setattr(bundle, "_assert_secure_tree", lambda root: None)
    real_lstat = Path.lstat

    def root_owned_active(path: Path):
        info = real_lstat(path)
        if path.name == "active" and os.path.islink(path):
            return SimpleNamespace(st_mode=info.st_mode, st_uid=0)
        return info

    monkeypatch.setattr(Path, "lstat", root_owned_active)
    updater._load_service_bundle = lambda: bundle


def _write_python_receipt(
    updater: Any,
    component_id: str,
    outer_manifest: dict[str, Any],
    bundle_identity: str,
) -> None:
    installed_root = updater.state_root / "installed" / component_id
    releases = installed_root / "releases"
    releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    updater.state_root.chmod(0o700)
    (updater.state_root / "installed").chmod(0o700)
    installed_root.chmod(0o700)
    releases.chmod(0o700)
    release_identity = outer_manifest["manifestDigest"]
    receipt = {
        "schemaVersion": 1,
        "componentId": component_id,
        "releaseIdentity": release_identity,
        "manifestDigest": release_identity,
        "artifactDigest": outer_manifest["artifact"]["sha256"],
        "version": outer_manifest["version"],
        "bundleIdentity": bundle_identity,
        "manifest": outer_manifest,
    }
    receipt_path = releases / (release_identity.removeprefix("sha256:") + ".json")
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    receipt_path.chmod(0o600)
    active_path = installed_root / "active.json"
    active_path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "componentId": component_id,
                "releaseIdentity": release_identity,
                "bundleIdentity": bundle_identity,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    active_path.chmod(0o600)


def _outer_manifest(component_id: str, version: str, artifact_digest: str) -> dict[str, Any]:
    manifest = {
        "componentId": component_id,
        "version": version,
        "artifact": {"kind": "python-bundle", "sha256": artifact_digest},
    }
    manifest["manifestDigest"] = updates._digest_json(manifest, "manifestDigest")
    return manifest


def test_python_receipt_is_bound_to_the_verified_inner_active_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _updater(tmp_path)
    component_id = "cyrene-test-product"
    component = {
        "componentId": component_id,
        "pythonBundleService": "navigator",
        "restart": {"group": "single-service", "unit": "cyrene-test-product.service"},
    }
    updater.components[component_id] = component
    _use_fixture_bundle_paths(updater, monkeypatch)
    install_root = tmp_path / "install"
    first_manifest = _v2_bundle(tmp_path / "bundle-a", "first")
    second_manifest = _v2_bundle(tmp_path / "bundle-b", "second")
    first_release = bundle.stage_release(tmp_path / "bundle-a", install_root=install_root)
    second_release = bundle.stage_release(tmp_path / "bundle-b", install_root=install_root)
    bundle.activate_release("navigator", first_manifest["version"], install_root=install_root)
    outer_manifest = _outer_manifest(
        component_id,
        "0.4.0",
        "sha256:" + "9" * 64,
    )
    _write_python_receipt(updater, component_id, outer_manifest, first_manifest["artifact_digest"])

    installed = updater._installed(component)

    assert installed["identityAttested"] is True
    assert installed["releaseIdentity"] == outer_manifest["manifestDigest"]
    assert installed["manifest"]["manifestDigest"] == outer_manifest["manifestDigest"]
    assert installed["bundleIdentity"] == first_manifest["artifact_digest"]
    assert first_release != second_release

    # A valid outer receipt cannot attest a different inner bundle selected by
    # the active symlink, even when both bundles are independently valid.
    bundle.activate_release(
        "navigator",
        second_manifest["version"],
        install_root=install_root,
        expected_current_version=first_manifest["version"],
    )
    with pytest.raises(updates.UpdateError) as error:
        updater._installed(component)
    assert error.value.code == "INVALID_INSTALLED_RELEASE"


def test_legacy_python_v1_is_readable_for_rollback_but_never_attested_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _updater(tmp_path)
    component_id = "cyrene-test-product"
    component = {
        "componentId": component_id,
        "pythonBundleService": "navigator",
        "restart": {"group": "single-service", "unit": "cyrene-test-product.service"},
    }
    updater.components[component_id] = component
    _use_fixture_bundle_paths(updater, monkeypatch)
    install_root = tmp_path / "install"
    legacy = tmp_path / "legacy"
    legacy_manifest = _legacy_v1_bundle(legacy)
    service_root = tmp_path / "install" / "services" / "navigator"
    release = service_root / "releases" / legacy_manifest["version"]
    release.parent.mkdir(parents=True)
    os.replace(legacy, release)
    service_root.mkdir(parents=True, exist_ok=True)
    (service_root / "active").symlink_to(f"releases/{legacy_manifest['version']}")

    installed = updater._installed(component)

    assert installed["activeVersion"] == legacy_manifest["version"]
    assert installed.get("identityAttested") is False
    assert installed.get("manifest") is not None
    assert installed["manifest"]["schema_version"] == 1

    # Existing content-addressed v1 releases remain usable by the updater's
    # rollback path even though they have no trusted outer receipt.
    previous = updater._capture_active_versions([{"componentId": component_id}])
    later = _legacy_v1_bundle(tmp_path / "later-legacy", "later")
    later_release = service_root / "releases" / later["version"]
    os.replace(tmp_path / "later-legacy", later_release)
    bundle.activate_release(
        "navigator",
        later["version"],
        install_root=install_root,
        expected_current_version=legacy_manifest["version"],
    )
    transaction = {
        "targetKind": "PACKAGE_ONLY",
        "previous": previous,
        "components": [
            {
                "componentId": component_id,
                "manifest": later,
                "bundleIdentity": later["artifact_digest"],
                "pointerIdentity": later["version"],
            }
        ],
    }
    monkeypatch.setattr(updater, "_restart_transaction", lambda value: None)
    monkeypatch.setattr(updater, "_health_transaction", lambda value: None)
    healthy, _ = updater._rollback_transaction(transaction)

    assert healthy is True
    assert (
        bundle.resolve_active_release("navigator", install_root=install_root)[1]
        == legacy_manifest["version"]
    )
    assert updater._installed(component).get("identityAttested") is False


def test_recovery_compares_exact_native_identity_even_when_version_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _updater(tmp_path)
    component_id = "cyrene-test-native"
    plan_id = "plan-" + "a" * 32
    plan_digest = "sha256:" + "b" * 64
    expected = {
        "componentId": component_id,
        "version": "1.2.3",
        "releaseIdentity": "sha256:" + "c" * 64,
        "pointerIdentity": "1.2.3--" + "c" * 64,
        "manifestDigest": "sha256:" + "c" * 64,
        "artifactDigest": "sha256:" + "d" * 64,
        "bundleIdentity": None,
        "identityAttested": True,
    }
    changed = {
        **expected,
        "releaseIdentity": "sha256:" + "e" * 64,
        "pointerIdentity": "1.2.3--" + "e" * 64,
        "manifestDigest": "sha256:" + "e" * 64,
    }
    transaction = {
        "schemaVersion": 2,
        "planId": plan_id,
        "planDigest": plan_digest,
        "phase": "success_end_pending",
        "targetKind": "PACKAGE_ONLY",
        "components": [
            {
                "componentId": component_id,
                "version": expected["version"],
                "releaseIdentity": expected["releaseIdentity"],
                "pointerIdentity": expected["pointerIdentity"],
                "manifestDigest": expected["manifestDigest"],
                "artifactDigest": expected["artifactDigest"],
            }
        ],
        "previous": [],
    }
    transaction_path = tmp_path / "transaction.json"
    transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
    monkeypatch.setattr(
        updater, "_readiness_for", lambda *args, **kwargs: {"status": "MAINTENANCE_ACTIVE"}
    )
    monkeypatch.setattr(updater, "_capture_active_versions", lambda components: [changed])
    monkeypatch.setattr(updater, "_health_transaction", lambda value: None)
    monkeypatch.setattr(
        updater,
        "_end_maintenance",
        lambda *args, **kwargs: pytest.fail("must keep gate held on identity mismatch"),
    )

    with pytest.raises(updates.UpdateError) as error:
        updater._recover_end_pending(transaction, transaction_path)

    assert error.value.code == "TRANSACTION_STATE_MISMATCH"
    assert (
        json.loads(transaction_path.read_text(encoding="utf-8"))["phase"] == "success_end_pending"
    )


def test_recovery_accepts_the_exact_captured_release_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _updater(tmp_path)
    component_id = "cyrene-test-native"
    plan_id = "plan-" + "a" * 32
    plan_digest = "sha256:" + "b" * 64
    active = {
        "componentId": component_id,
        "version": "1.2.3",
        "releaseIdentity": "sha256:" + "c" * 64,
        "pointerIdentity": "1.2.3--" + "c" * 64,
        "manifestDigest": "sha256:" + "c" * 64,
        "artifactDigest": "sha256:" + "d" * 64,
        "bundleIdentity": None,
        "identityAttested": True,
    }
    transaction = {
        "schemaVersion": 2,
        "planId": plan_id,
        "planDigest": plan_digest,
        "phase": "success_end_pending",
        "targetKind": "PACKAGE_ONLY",
        "components": [{key: value for key, value in active.items() if key != "identityAttested"}],
        "previous": [],
    }
    transaction_path = tmp_path / "transaction.json"
    transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
    monkeypatch.setattr(
        updater, "_readiness_for", lambda *args, **kwargs: {"status": "MAINTENANCE_ACTIVE"}
    )
    monkeypatch.setattr(updater, "_capture_active_versions", lambda components: [active])
    monkeypatch.setattr(updater, "_health_transaction", lambda value: None)
    monkeypatch.setattr(updater, "_end_maintenance", lambda *args, **kwargs: None)
    monkeypatch.setattr(updater, "_applied_result", lambda value: {"status": "applied"})

    result = updater._recover_end_pending(transaction, transaction_path)

    assert result == {"status": "applied"}
    assert json.loads(transaction_path.read_text(encoding="utf-8"))["phase"] == "succeeded"


def test_recovery_refuses_legacy_journal_without_captured_release_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _updater(tmp_path)
    plan_id = "plan-" + "f" * 32
    plan_digest = "sha256:" + "1" * 64
    transaction = {
        "schemaVersion": 1,
        "planId": plan_id,
        "planDigest": plan_digest,
        "phase": "applying",
        "targetKind": "PACKAGE_ONLY",
        "components": [],
        "previous": [],
    }
    transaction_path = tmp_path / "legacy-transaction.json"
    transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
    monkeypatch.setattr(
        updater,
        "_rollback_transaction",
        lambda value: pytest.fail("legacy identity must fail held before rollback"),
    )
    confirmation = {"planId": plan_id, "planDigest": plan_digest}

    with pytest.raises(updates.UpdateError):
        updater._recover_transaction(
            transaction,
            transaction_path,
            tmp_path / "stage.json",
            confirmation,
        )

    assert json.loads(transaction_path.read_text(encoding="utf-8")) == transaction
