"""Focused checks for the installed broker path and public request flags."""

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

ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ID = "cyrene-runtime-maintenance"
TARGET_ID = "linux-ubuntu-24.04-x86_64-systemd"

_SPEC = importlib.util.spec_from_file_location(
    "component_updates", ROOT / "packaging" / "component_updates.py"
)
assert _SPEC is not None and _SPEC.loader is not None
updates = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = updates
_SPEC.loader.exec_module(updates)


@pytest.fixture(autouse=True)
def _simulate_root_owned_test_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    original_lstat = Path.lstat
    test_root = tmp_path.resolve()

    def root_owned_lstat(path: Path) -> os.stat_result:
        metadata = original_lstat(path)
        if not path.absolute().is_relative_to(test_root):
            return metadata
        fields = list(metadata)
        fields[4] = 0
        fields[5] = 0
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "lstat", root_owned_lstat)
    monkeypatch.setattr(os, "geteuid", lambda: 0)


def _updater(
    tmp_path: Path, *, broker_path: Path | None = None, state_root: Path | None = None
) -> Any:
    return updates.ComponentUpdater(
        catalog_path=ROOT / "packaging" / "component-catalog-bootstrap-v1.json",
        activity_catalog_path=tmp_path / "activity-sources.json",
        socket_path=tmp_path / "runtime-maintenance.sock",
        broker_path=broker_path,
        install_root=tmp_path / "usr-lib-cyrene",
        state_root=state_root or tmp_path / "update-state",
        release_lock_path=tmp_path / "missing-release-lock.json",
        trusted_catalog_digest=updates.TRUSTED_CATALOG_DIGEST,
        systemd_unit_dirs=(tmp_path / "systemd",),
        load_active_catalog=False,
    )


def _install_active_broker(
    updater: Any,
    *,
    version: str = "0.1.0",
    journal: bool = True,
    receipts: bool = True,
) -> Path:
    component = updater.components[COMPONENT_ID]
    target = updater.targets[TARGET_ID]
    binary_bytes = b"signed immutable broker payload"
    artifact_digest = "sha256:" + hashlib.sha256(binary_bytes).hexdigest()
    artifact = {
        "kind": "native-binary",
        "sha256": artifact_digest,
        "sizeBytes": len(binary_bytes),
        "entrypoint": "bin/cyrene-runtime-maintenance",
        "files": {"bin/cyrene-runtime-maintenance": artifact_digest},
    }
    manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "componentId": COMPONENT_ID,
        "version": version,
        "target": target["target"],
        "artifact": artifact,
        "dependencies": [],
        "restart": {"group": "single-service", "unit": component["systemdUnit"]},
    }
    manifest["manifestDigest"] = updates._digest_json(manifest, "manifestDigest")
    pointer_identity = (
        f"{manifest['version']}--{manifest['manifestDigest'].removeprefix('sha256:')}"
    )
    component_root = updater.install_root / "components" / COMPONENT_ID
    release = component_root / "releases" / pointer_identity
    binary = release / "bin" / "cyrene-runtime-maintenance"
    binary.parent.mkdir(parents=True, mode=0o755)
    binary.write_bytes(binary_bytes)
    binary.chmod(0o755)
    manifest_path = release / "component-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o644)
    for directory in (
        updater.install_root,
        updater.install_root / "components",
        component_root,
        component_root / "releases",
        release,
        binary.parent,
    ):
        directory.chmod(0o755)
    (component_root / "active").symlink_to(f"releases/{pointer_identity}")
    active_item = {
        "componentId": COMPONENT_ID,
        "version": manifest["version"],
        "releaseIdentity": manifest["manifestDigest"],
        "manifestDigest": manifest["manifestDigest"],
        "artifactDigest": artifact_digest,
        "bundleIdentity": None,
        "manifest": manifest,
    }
    if receipts:
        updater._write_release_receipt(active_item)
        updater._write_active_receipt(active_item)
    if journal:
        journal_path = updater.state_root / updates.BROKER_BOOTSTRAP_JOURNAL
        updater.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        journal_path.parent.mkdir(parents=True, mode=0o700)
        updater.state_root.chmod(0o700)
        journal_path.parent.chmod(0o700)
        journal_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "phase": "complete",
                    "planDigest": "a" * 64,
                    "releaseIdentity": pointer_identity,
                    "identity": {
                        "componentId": COMPONENT_ID,
                        "targetId": TARGET_ID,
                        "version": manifest["version"],
                        "manifestDigest": manifest["manifestDigest"],
                        "artifactDigest": artifact_digest,
                        "indexDigest": "sha256:" + "b" * 64,
                    },
                }
            ),
            encoding="utf-8",
        )
        journal_path.chmod(0o600)
    return binary


def test_default_resolution_uses_active_manifest_and_trusted_install_receipt(
    tmp_path: Path,
) -> None:
    updater = _updater(tmp_path)
    binary = _install_active_broker(updater)
    assert updater._resolve_installed_broker() == binary


def test_updated_broker_uses_current_receipt_with_stale_bootstrap_journal(tmp_path: Path) -> None:
    updater = _updater(tmp_path)
    _install_active_broker(updater)
    active = updater.install_root / "components" / COMPONENT_ID / "active"
    active.unlink()

    updated_binary = _install_active_broker(updater, version="0.2.0", journal=False)
    assert updater._resolve_installed_broker() == updated_binary


def test_bootstrap_fallback_accepts_root_owned_0755_state_ancestors(tmp_path: Path) -> None:
    state_root = tmp_path / "public-one" / "public-two" / "update-state"
    state_root.parent.parent.mkdir(parents=True)
    state_root.parent.parent.chmod(0o755)
    state_root.parent.mkdir()
    state_root.parent.chmod(0o755)
    tmp_path.chmod(0o755)
    updater = _updater(tmp_path, state_root=state_root)
    binary = _install_active_broker(updater, receipts=False)
    assert updater._resolve_installed_broker() == binary


@pytest.mark.parametrize(
    "unsafe", ["owner", "symlink", "hash", "missing-journal", "missing-active"]
)
def test_default_resolution_fails_closed_for_unsafe_or_unbound_install(
    tmp_path: Path, unsafe: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater = _updater(tmp_path)
    binary = _install_active_broker(
        updater,
        journal=unsafe != "missing-journal",
        receipts=unsafe != "missing-journal",
    )
    if unsafe == "owner":
        original_lstat = Path.lstat

        def untrusted_binary_owner(path: Path) -> os.stat_result:
            metadata = original_lstat(path)
            if path == binary:
                fields = list(metadata)
                fields[4] = 65534
                return os.stat_result(fields)
            return metadata

        monkeypatch.setattr(Path, "lstat", untrusted_binary_owner)
    elif unsafe == "symlink":
        payload = binary.with_name("payload")
        binary.rename(payload)
        binary.symlink_to(payload.name)
    elif unsafe == "hash":
        binary.write_bytes(b"changed broker payload")
    elif unsafe == "missing-active":
        (binary.parents[3] / "active").unlink()

    with pytest.raises(updates.UpdateError):
        updater._resolve_installed_broker()


def test_explicit_legacy_broker_path_keeps_its_override_semantics(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy-broker"
    legacy.write_text("legacy", encoding="utf-8")
    legacy.chmod(0o755)
    updater = _updater(tmp_path, broker_path=legacy)
    updater.runner = lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0,
        stdout='{"request_id":"request-1","result":{}}\n',
        stderr="",
    )
    assert updater._broker_request("Health", {}, request_id="request-1") == {}


def test_health_is_read_only_while_maintenance_keeps_operator_flag(tmp_path: Path) -> None:
    broker = tmp_path / "broker"
    broker.write_text("broker", encoding="utf-8")
    broker.chmod(0o755)
    updater = _updater(tmp_path, broker_path=broker)
    calls: list[list[str]] = []

    def runner(argv: list[str], *, input: str, **_kwargs: Any) -> Any:
        calls.append(argv)
        request_id = json.loads(input)["request_id"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"request_id": request_id, "result": {}}) + "\n",
            stderr="",
        )

    updater.runner = runner
    updater._activity_catalog = lambda: ({"generation": 1}, [])
    updater._broker_request("Health", {}, request_id="health")
    updater._broker_request("BeginMaintenance", {}, request_id="maintenance")
    assert "--operator" not in calls[0]
    assert calls[1][-1] == "--operator"
