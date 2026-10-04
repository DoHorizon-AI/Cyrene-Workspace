"""Offline first-install tests for the native maintenance broker bootstrap."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
import tarfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


updates = _load("native_bootstrap_updates_test", ROOT / "packaging" / "component_updates.py")
bootstrap = _load(
    "native_component_bootstrap_test", ROOT / "packaging" / "native_component_bootstrap.py"
)


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _fixture(tmp_path: Path) -> tuple[Any, dict[str, bytes], str]:
    updater = updates.ComponentUpdater(
        catalog_path=ROOT / "packaging" / "component-catalog-bootstrap-v1.json",
        activity_catalog_path=tmp_path / "activity-sources.json",
        socket_path=tmp_path / "runtime-maintenance.sock",
        broker_path=tmp_path / "missing-broker",
        install_root=tmp_path / "usr-lib-cyrene",
        state_root=tmp_path / "update-state",
        release_lock_path=tmp_path / "missing-release-lock.json",
        trusted_catalog_digest=updates.TRUSTED_CATALOG_DIGEST,
        systemd_unit_dirs=(tmp_path / "systemd",),
        load_active_catalog=False,
    )
    target_id = "linux-ubuntu-24.04-x86_64-systemd"
    target = updater.targets[target_id]
    unit = (
        b"[Unit]\nDescription=Cyrene Runtime Maintenance\n"
        b"[Service]\nExecStart=/usr/bin/cyrene component-run cyrene-runtime-maintenance\n"
    )
    if tmp_path.name == "unit-mismatch":
        unit = b"[Service]\nExecStart=/usr/bin/cyrene component-run cyrene-kernel\n"
    binary = b"fake broker executable"
    artifact_bytes = _archive(
        {
            "bin/cyrene-runtime-maintenance": binary,
            "systemd/cyrene-runtime-maintenance.service": unit,
        }
    )
    source = {
        "repository": "https://github.com/DoHorizon-AI/Cyrene-Platform",
        "ref": "refs/heads/main",
        "commit": "a" * 40,
    }
    artifact = {
        "kind": "native-binary",
        "uri": "https://github.com/DoHorizon-AI/Cyrene-Platform/releases/download/stable-cyrene-runtime-maintenance-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/broker.tar.gz",
        "sha256": _digest(artifact_bytes),
        "sizeBytes": len(artifact_bytes),
        "entrypoint": "bin/cyrene-runtime-maintenance",
        "files": {
            "bin/cyrene-runtime-maintenance": _digest(binary),
            "systemd/cyrene-runtime-maintenance.service": _digest(unit),
        },
    }
    manifest = {
        "schemaVersion": 1,
        "releaseId": "broker-test-release",
        "componentId": "cyrene-runtime-maintenance",
        "version": "1.2.3",
        "channel": "stable",
        "target": target["target"],
        "artifact": artifact,
        "dependencies": [],
        "restart": {"group": "single-service", "unit": "cyrene-runtime-maintenance.service"},
        "source": source,
        "provenance": {
            "attestation": {
                "kind": "github-artifact-attestation",
                "repository": "DoHorizon-AI/Cyrene-Platform",
                "workflow": "DoHorizon-AI/Cyrene-Platform/.github/workflows/component-release.yml",
                "predicateType": "https://slsa.dev/provenance/v1",
                "subjectName": "broker.tar.gz",
                "run": {
                    "id": "123",
                    "attempt": 1,
                    "url": "https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/123/attempts/1",
                },
            }
        },
    }
    manifest["manifestDigest"] = updates._digest_json(manifest, "manifestDigest")
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    entry = {
        "componentId": "cyrene-runtime-maintenance",
        "version": "1.2.3",
        "target": target["target"],
        "manifestUri": "https://github.com/DoHorizon-AI/Cyrene-Platform/releases/download/stable-cyrene-runtime-maintenance-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/manifest.json",
        "manifestDigest": manifest["manifestDigest"],
    }
    index = {
        "schemaVersion": 1,
        "repository": "DoHorizon-AI/Cyrene-Platform",
        "channel": "stable",
        "generatedAt": "2026-10-04T00:00:00Z",
        "source": source,
        "provenance": {
            "attestation": {
                "kind": "github-artifact-attestation",
                "repository": "DoHorizon-AI/Cyrene-Platform",
                "workflow": "DoHorizon-AI/Cyrene-Platform/.github/workflows/component-release.yml",
                "predicateType": "https://slsa.dev/provenance/v1",
                "subjectName": "component-release-index-v1.json",
                "run": {
                    "id": "123",
                    "attempt": 1,
                    "url": "https://github.com/DoHorizon-AI/Cyrene-Platform/actions/runs/123/attempts/1",
                },
            }
        },
        "releases": [entry],
        "compatibilityGroups": [],
    }
    index["indexDigest"] = updates._digest_json(index, "indexDigest")
    index_bytes = json.dumps(index, sort_keys=True, separators=(",", ":")).encode()
    updater._target_for = lambda component: {
        **target,
        "id": target_id,
        "artifactKind": "native-binary",
    }
    updater._verify_attestation = lambda payload, **kwargs: None
    bootstrap._effective_uid = lambda: 0
    bootstrap.DEFAULT_BROKER_EXECUTABLE = tmp_path / "broker-executable"
    bootstrap.DEFAULT_PROC_ROOT = tmp_path / "empty-proc"
    bootstrap.DEFAULT_PROC_ROOT.mkdir(parents=True, exist_ok=True)
    return (
        updater,
        {
            "index_bytes": index_bytes,
            "index_attestation_bytes": b"index-bundle",
            "manifest_bytes": manifest_bytes,
            "artifact_bytes": artifact_bytes,
            "artifact_attestation_bytes": b"artifact-bundle",
        },
        target_id,
    )


def _call(updater: Any, values: dict[str, bytes], target_id: str, **kwargs: Any) -> dict[str, Any]:
    return bootstrap.bootstrap_verified_runtime_maintenance(
        updater,
        **values,
        channel="stable",
        target_id=target_id,
        **kwargs,
    )


def test_digest_and_detached_attestation_mismatches_are_rejected(tmp_path: Path) -> None:
    updater, values, target_id = _fixture(tmp_path)
    values["artifact_bytes"] += b"tampered"
    with pytest.raises(ValueError, match="artifact bytes do not match"):
        _call(updater, values, target_id)

    updater, values, target_id = _fixture(tmp_path / "signature")
    calls = 0

    def reject_signature(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return
        raise RuntimeError("attestation rejected")

    updater._verify_attestation = reject_signature
    with pytest.raises(RuntimeError, match="attestation rejected"):
        _call(updater, values, target_id)

    updater, values, target_id = _fixture(tmp_path / "index-digest")
    values["index_bytes"] = values["index_bytes"].replace(b"2026-10-04", b"2026-10-03")
    with pytest.raises(updates.UpdateError, match="index digest"):
        _call(updater, values, target_id)


def test_target_mismatch_existing_pointer_and_unit_refuse_bootstrap(tmp_path: Path) -> None:
    updater, values, target_id = _fixture(tmp_path / "target")
    with pytest.raises(ValueError, match="supported native host target"):
        _call(updater, values, "linux-ubuntu-24.04-aarch64-systemd")

    updater, values, target_id = _fixture(tmp_path / "pointer")
    broker_root = updater.install_root / "components" / "cyrene-runtime-maintenance"
    broker_root.mkdir(parents=True)
    (broker_root / "active").symlink_to("releases/1.0.0--" + "1" * 64)
    with pytest.raises(ValueError, match="already active"):
        _call(updater, values, target_id)

    updater, values, target_id = _fixture(tmp_path / "unit")
    unit_dir = updater.systemd_unit_dirs[0]
    unit_dir.mkdir(parents=True)
    (unit_dir / "cyrene-runtime-maintenance.service").write_text("existing")
    with pytest.raises(ValueError, match="unit already exists"):
        _call(updater, values, target_id)

    updater, values, target_id = _fixture(tmp_path / "binary")
    broker_path = bootstrap.DEFAULT_BROKER_EXECUTABLE
    broker_path.parent.mkdir(parents=True, exist_ok=True)
    broker_path.write_text("old broker")
    with pytest.raises(ValueError, match="executable already exists"):
        _call(updater, values, target_id)


def test_first_bootstrap_activates_only_broker_and_recovers_interruption(tmp_path: Path) -> None:
    updater, values, target_id = _fixture(tmp_path)
    kernel_root = updater.install_root / "components" / "cyrene-kernel"
    kernel_root.mkdir(parents=True)
    kernel_pointer = "releases/2.0.0--" + "2" * 64
    (kernel_root / "active").symlink_to(kernel_pointer)
    lock_entries: list[str] = []
    original_lock = updater._exclusive_update_lock
    lock_held = False
    expect_locked_checks = False
    original_active_pointer = updater._active_native_pointer_identity

    @contextmanager
    def record_lock():
        nonlocal lock_held
        with original_lock():
            lock_held = True
            lock_entries.append("entered")
            try:
                yield
            finally:
                lock_held = False

    def check_active_pointer(component_id: str) -> str | None:
        if expect_locked_checks:
            assert lock_held
        return original_active_pointer(component_id)

    updater._exclusive_update_lock = record_lock
    updater._active_native_pointer_identity = check_active_pointer
    plan = _call(updater, values, target_id)
    assert plan["status"] == "confirmation_required"
    assert lock_entries == []
    assert not (updater.install_root / "components" / "cyrene-runtime-maintenance").exists()
    with pytest.raises(ValueError, match="Confirmation digest"):
        _call(updater, values, target_id, confirm_plan_digest="sha256:" + "0" * 64)

    original_write = updater._write_active_receipt
    fail_once = True

    def interrupted(item: dict[str, Any]) -> None:
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("simulated interruption after pointer switch")
        original_write(item)

    updater._write_active_receipt = interrupted
    expect_locked_checks = True
    with pytest.raises(OSError, match="simulated interruption"):
        _call(updater, values, target_id, confirm_plan_digest=plan["planDigest"])
    assert lock_entries == ["entered"]
    assert (
        updater.install_root / "components" / "cyrene-runtime-maintenance" / "active"
    ).is_symlink()
    assert os.readlink(kernel_root / "active") == kernel_pointer

    updater._write_active_receipt = original_write
    recovered = _call(updater, values, target_id, confirm_plan_digest=plan["planDigest"])
    assert lock_entries == ["entered", "entered"]
    assert recovered["status"] == "activated"
    assert recovered["componentId"] == "cyrene-runtime-maintenance"
    assert recovered["manifestDigest"] == plan["manifestDigest"]
    assert recovered["artifactDigest"] == plan["artifactDigest"]
    assert recovered["activePointerTarget"].startswith("releases/1.2.3--")
    assert os.readlink(kernel_root / "active") == kernel_pointer


def test_manifest_jcs_and_source_mismatch_rejected_by_component_updater(tmp_path: Path) -> None:
    updater, values, target_id = _fixture(tmp_path / "jcs")
    manifest = json.loads(values["manifest_bytes"])
    manifest["artifact"]["entrypoint"] = "bin/other"
    values["manifest_bytes"] = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(updates.UpdateError, match="Manifest JCS digest is invalid"):
        _call(updater, values, target_id)

    updater, values, target_id = _fixture(tmp_path / "source")
    manifest = json.loads(values["manifest_bytes"])
    manifest["source"]["commit"] = "b" * 40
    manifest["manifestDigest"] = updates._digest_json(manifest, "manifestDigest")
    values["manifest_bytes"] = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    index = json.loads(values["index_bytes"])
    index["releases"][0]["manifestDigest"] = manifest["manifestDigest"]
    index["indexDigest"] = updates._digest_json(index, "indexDigest")
    values["index_bytes"] = json.dumps(index, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(updates.UpdateError, match="not pinned by its attested index"):
        _call(updater, values, target_id)


@pytest.mark.parametrize("failure_point", ["manifest", "receipt"])
def test_incomplete_release_metadata_is_repaired_and_component_run_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    updater, values, target_id = _fixture(tmp_path / failure_point)
    plan = _call(updater, values, target_id)
    original_write = updater._write_release_receipt
    original_atomic_json = updater._atomic_json_file
    fail_once = True

    def fail_before_receipt(item: dict[str, Any]) -> None:
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("simulated interruption before release receipt")
        original_write(item)

    def fail_before_manifest(path: Path, value: dict[str, Any], *, mode: int) -> None:
        nonlocal fail_once
        if fail_once and path.name == "component-manifest.json":
            fail_once = False
            raise OSError("simulated interruption before component manifest")
        original_atomic_json(path, value, mode=mode)

    if failure_point == "receipt":
        updater._write_release_receipt = fail_before_receipt
        expected_error = "before release receipt"
    else:
        updater._atomic_json_file = fail_before_manifest
        expected_error = "before component manifest"
    with pytest.raises(OSError, match=expected_error):
        _call(updater, values, target_id, confirm_plan_digest=plan["planDigest"])
    manifest = json.loads(values["manifest_bytes"])
    release = (
        updater.install_root
        / "components"
        / "cyrene-runtime-maintenance"
        / "releases"
        / f"1.2.3--{manifest['manifestDigest'].removeprefix('sha256:')}"
    )
    assert (release / "component-manifest.json").is_file() is (failure_point == "receipt")
    assert (
        updater._read_release_receipt("cyrene-runtime-maintenance", manifest["manifestDigest"])
        is None
    )

    updater._write_release_receipt = original_write
    updater._atomic_json_file = original_atomic_json
    recovered = _call(updater, values, target_id, confirm_plan_digest=plan["planDigest"])
    assert recovered["status"] == "activated"
    receipt = updater._read_active_receipt("cyrene-runtime-maintenance")
    assert receipt is not None and receipt["manifest"] == manifest
    for directory in (
        updater.install_root,
        updater.install_root / "components",
        updater.install_root / "components/cyrene-runtime-maintenance",
        updater.install_root / "components/cyrene-runtime-maintenance/releases",
    ):
        directory.chmod(0o755)

    original_stat = updates.os.stat
    calls: dict[str, Any] = {}

    def root_owned_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        value = original_stat(path, *args, **kwargs)
        fields = list(value)
        fields[4] = 0
        return os.stat_result(fields)

    class ExecReached(Exception):
        pass

    def capture_exec(path: str, arguments: list[str], environment: dict[str, str]) -> None:
        calls.update(path=path, arguments=arguments, environment=environment)
        raise ExecReached

    monkeypatch.setattr(updates.os, "stat", root_owned_stat)
    monkeypatch.setattr(updates.os, "execve", capture_exec)
    with pytest.raises(ExecReached):
        updates.run_component("cyrene-runtime-maintenance", install_root=updater.install_root)
    assert Path(calls["path"]) == release / "bin/cyrene-runtime-maintenance"
    assert calls["arguments"] == [str(release / "bin/cyrene-runtime-maintenance")]


def test_unit_mismatch_does_not_create_a_release_or_pointer(tmp_path: Path) -> None:
    updater, values, target_id = _fixture(tmp_path / "unit-mismatch")
    with pytest.raises(ValueError, match="component-run entrypoint"):
        _call(updater, values, target_id)
    assert not (updater.install_root / "components" / "cyrene-runtime-maintenance").exists()


def test_process_table_unknown_fails_closed_and_kernel_threads_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing-proc"
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        bootstrap._broker_process_exists(missing)

    unreadable = tmp_path / "unreadable-proc"
    unreadable.mkdir()
    original_iterdir = Path.iterdir

    def deny_process_listing(path: Path):
        if path == unreadable:
            raise PermissionError("mock inaccessible procfs")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", deny_process_listing)
    with pytest.raises(RuntimeError, match="unreadable"):
        bootstrap._broker_process_exists(unreadable)
    monkeypatch.setattr(Path, "iterdir", original_iterdir)

    vanished_root = tmp_path / "vanished-proc"
    vanished_root.mkdir()
    vanished = vanished_root / "99"
    original_iterdir = Path.iterdir

    def snapshot_vanished_pid(path: Path):
        if path == vanished_root:
            yield vanished
            return
        yield from original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", snapshot_vanished_pid)
    assert bootstrap._broker_process_exists(vanished_root) is False
    monkeypatch.setattr(Path, "iterdir", original_iterdir)

    proc_root = tmp_path / "proc"
    kernel_thread = proc_root / "2"
    kernel_thread.mkdir(parents=True)
    (kernel_thread / "stat").write_text("2 (kthreadd) S 0 0\n")
    (kernel_thread / "cmdline").write_bytes(b"")
    monkeypatch.setattr(bootstrap, "DEFAULT_PROC_ROOT", proc_root)
    monkeypatch.setattr(
        bootstrap, "DEFAULT_BROKER_EXECUTABLE", Path("/usr/bin/cyrene-runtime-maintenance")
    )
    assert bootstrap._broker_process_exists(proc_root) is False

    process = proc_root / "3"
    process.mkdir()
    (process / "stat").write_text("3 (maintenance) S 0 0\n")
    (process / "cmdline").write_bytes(b"cyrene-runtime-maintenance\0")
    executable = proc_root / "cyrene-runtime-maintenance"
    executable.write_text("mock executable")
    (process / "exe").symlink_to(executable)
    assert bootstrap._broker_process_exists(proc_root) is True

    original_read_text = Path.read_text

    def deny_process_stat(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == process / "stat":
            raise PermissionError("mock unreadable process stat")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_process_stat)
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        bootstrap._broker_process_exists(proc_root)
