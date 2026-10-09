"""Offline first-install tests for the native maintenance broker bootstrap."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import sys
import tarfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
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
binding_helper = _load(
    "_cyrene_bootstrap_catalog_binding", ROOT / "packaging" / "bootstrap_catalog_binding.py"
)


@pytest.fixture(autouse=True)
def _simulate_root_owned_test_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Model the root-owned filesystem inside this test's isolated temporary tree."""

    previous_umask = os.umask(0o077)
    original_lstat = Path.lstat
    test_root = tmp_path.resolve()

    def root_owned_install_lstat(path: Path) -> os.stat_result:
        metadata = original_lstat(path)
        if not path.absolute().is_relative_to(test_root):
            return metadata
        fields = list(metadata)
        fields[4] = 0
        fields[5] = 0
        return os.stat_result(fields)

    original_fstat = os.fstat

    def root_owned_test_fstat(descriptor: int) -> os.stat_result:
        metadata = original_fstat(descriptor)
        try:
            descriptor_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            return metadata
        if not descriptor_path.absolute().is_relative_to(test_root):
            return metadata
        fields = list(metadata)
        fields[4] = 0
        fields[5] = 0
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "lstat", root_owned_install_lstat)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "fstat", root_owned_test_fstat)
    try:
        yield
    finally:
        os.umask(previous_umask)


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


def _completed_broker_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, dict[str, bytes], str, dict[str, Any], Path, Path, Path]:
    """Create a completed broker and its exact installed unit for replay checks."""

    updater, values, target_id = _fixture(tmp_path)
    monkeypatch.setattr(bootstrap, "RUNTIME_SYSTEMD_UNIT_DIRECTORY", tmp_path / "runtime-systemd")
    plan = _call(updater, values, target_id)
    assert plan["status"] == "confirmation_required"
    _call(updater, values, target_id, confirm_plan_digest=plan["planDigest"])

    unit_name = updater.components[bootstrap.BOOTSTRAP_COMPONENT_ID]["systemdUnit"]
    with tarfile.open(fileobj=io.BytesIO(values["artifact_bytes"]), mode="r:gz") as archive:
        unit_bytes = archive.extractfile(f"systemd/{unit_name}")
        assert unit_bytes is not None
        unit_payload = unit_bytes.read()
    unit_path = updater.systemd_unit_dirs[0] / unit_name
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_bytes(unit_payload)
    unit_path.chmod(0o644)

    active_path = updater.install_root / "components" / bootstrap.BOOTSTRAP_COMPONENT_ID / "active"
    release = active_path.parent / "releases" / os.readlink(active_path).removeprefix("releases/")
    updater.runner = lambda argv, **_kwargs: SimpleNamespace(
        returncode=0,
        stdout={"ActiveState": "inactive\n", "MainPID": "0\n"}[argv[2].removeprefix("--property=")],
        stderr="",
    )
    return (
        updater,
        values,
        target_id,
        plan,
        bootstrap._journal_path(updater),
        active_path,
        release,
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


def test_confirmed_bootstrap_reuses_only_a_live_outer_lock_lease(tmp_path: Path) -> None:
    updater, values, target_id = _fixture(tmp_path)
    plan = _call(updater, values, target_id)
    lock_entries: list[str] = []
    verified_subjects: list[str] = []
    original_lock = updater._exclusive_update_lock
    updater._verify_attestation = lambda _payload, **kwargs: verified_subjects.append(
        kwargs["subject_name"]
    )

    @contextmanager
    def record_lock():
        with original_lock():
            lock_entries.append("entered")
            yield

    updater._exclusive_update_lock = record_lock
    with bootstrap.exclusive_update_lock(updater) as lock_lease:
        result = bootstrap.bootstrap_verified_runtime_maintenance_under_lock(
            updater,
            **values,
            channel="stable",
            target_id=target_id,
            confirm_plan_digest=plan["planDigest"],
            lock_lease=lock_lease,
        )
        with bootstrap.exclusive_update_lock(updater) as nested_lease:
            assert nested_lease is lock_lease
        with pytest.raises(PermissionError, match="active updater lock lease"):
            bootstrap.bootstrap_verified_runtime_maintenance_under_lock(
                object(),
                lock_lease=lock_lease,
            )

    assert lock_entries == ["entered"]
    assert verified_subjects == ["component-release-index-v1.json", "broker.tar.gz"]
    assert result["status"] == "activated"
    with pytest.raises(PermissionError, match="active updater lock lease"):
        bootstrap.bootstrap_verified_runtime_maintenance_under_lock(
            updater,
            lock_lease=lock_lease,
        )
    with pytest.raises(TypeError):
        bootstrap.bootstrap_verified_runtime_maintenance(
            updater,
            **values,
            channel="stable",
            target_id=target_id,
            confirm_plan_digest=plan["planDigest"],
            _lock_lease=lock_lease,
        )


def test_first_core_bootstrap_binds_active_v2_to_compiled_v1_broker_authority(
    tmp_path: Path,
) -> None:
    updater, values, target_id = _fixture(tmp_path)
    plan = _call(updater, values, target_id)
    assert plan["status"] == "confirmation_required"

    active_bytes = (ROOT / "governance" / "component-catalog-v2.json").read_bytes()
    active = json.loads(active_bytes)
    active_digest = _digest(active_bytes)
    metadata = {
        "schemaVersion": 1,
        "repository": "DoHorizon-AI/Cyrene-Workspace",
        "workflow": "DoHorizon-AI/Cyrene-Workspace/.github/workflows/component-catalog-release.yml",
        "channel": "stable",
        "releaseId": "catalog-stable-" + "b" * 40,
        "sourceCommit": "b" * 40,
        "sourceRef": "refs/heads/main",
        "catalogSha256": active_digest,
        "generation": 15,
        "subjectName": "component-catalog-v2.json",
        "attestationAssetName": "component-catalog-v2.json.attestation.jsonl",
    }
    updater._read_active_catalog = lambda: (active_bytes, metadata)
    updater.catalog = active
    updater.catalog_bytes = active_bytes
    updater.catalog_digest = active_digest
    updater.catalog_generation = 15
    updater.catalog_source = metadata
    updater.components = {row["componentId"]: row for row in active["components"]}
    updater.targets = {row["id"]: row for row in active["targets"]}
    updater.publishers = {row["repository"]: row for row in active["publishers"]}

    assert updater.bootstrap_catalog_digest == bootstrap.COMPILED_CATALOG_DIGEST
    assert updater.bootstrap_catalog_digest != updater.catalog_digest
    assert updater.catalog_generation == 15
    assert (
        updater.bootstrap_catalog_bytes
        == (ROOT / "packaging" / "component-catalog-bootstrap-v1.json").read_bytes()
    )

    with pytest.raises(ValueError, match="compiled trusted catalog"):
        _call(
            updater,
            values,
            target_id,
            confirm_plan_digest=plan["planDigest"],
        )

    with bootstrap.exclusive_update_lock(updater) as lock_lease:
        result = bootstrap.bootstrap_verified_runtime_maintenance_under_lock(
            updater,
            **values,
            channel="stable",
            target_id=target_id,
            confirm_plan_digest=plan["planDigest"],
            lock_lease=lock_lease,
            active_catalog_digest=active_digest,
        )

    assert result["status"] == "activated"
    assert result["componentId"] == "cyrene-runtime-maintenance"
    assert result["manifestDigest"] == plan["manifestDigest"]
    assert result["artifactDigest"] == plan["artifactDigest"]


def test_packaged_v2_bootstrap_binding_uses_bundled_v1_baseline_without_active_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater, values, target_id = _fixture(tmp_path)
    plan = _call(updater, values, target_id)
    assert plan["status"] == "confirmation_required"

    package_root = tmp_path / "usr-share-cyrene"
    package_root.mkdir(mode=0o755)
    v2_catalog_path = package_root / "component-catalog-v2.json"
    binding_path = package_root / "bootstrap-catalog-binding-v1.json"
    baseline_path = package_root / "component-catalog-v1.json"
    active_bytes = (ROOT / "governance" / "component-catalog-v2.json").read_bytes()
    baseline_bytes = (ROOT / "packaging" / "component-catalog-bootstrap-v1.json").read_bytes()
    active = json.loads(active_bytes)
    active_digest = _digest(active_bytes)
    v2_catalog_path.write_bytes(active_bytes)
    v2_catalog_path.chmod(0o644)
    baseline_path.write_bytes(baseline_bytes)
    baseline_path.chmod(0o644)
    commit = "b" * 40
    binding = {
        "schemaVersion": 1,
        "catalog": {
            "repository": "DoHorizon-AI/Cyrene-Workspace",
            "workflow": "DoHorizon-AI/Cyrene-Workspace/.github/workflows/component-catalog-release.yml",
            "releaseId": "catalog-v2-stable-" + commit,
            "source": {"ref": "refs/heads/main", "commit": commit},
            "assetName": "component-catalog-v2.json",
            "sha256": active_digest.removeprefix("sha256:"),
            "attestationBundleSha256": "c" * 64,
            "generation": active["generation"],
        },
    }
    binding_path.write_text(json.dumps(binding, sort_keys=True), encoding="utf-8")
    binding_path.chmod(0o644)

    monkeypatch.setattr(binding_helper, "INSTALLED_BINDING_PATH", binding_path)
    monkeypatch.setattr(binding_helper, "INSTALLED_CATALOG_PATHS", frozenset({v2_catalog_path}))
    # /tmp is intentionally writable on test hosts; retain the real file identity,
    # no-follow, ownership, and mode checks while isolating only the parent-path check.
    monkeypatch.setattr(binding_helper, "_check_root_safe_path", lambda _path: None)
    root_safe_reader = binding_helper._read_regular_file
    helper_source_path = (
        Path(bootstrap.__file__).resolve().with_name("bootstrap_catalog_binding.py")
    )

    def fixture_root_safe_reader(path: Path, label: str, *, require_root: bool) -> bytes:
        assert require_root is True
        if Path(path) == helper_source_path:
            # The source checkout helper is not root-owned; production reads the
            # fixed installed sibling through the original root-safe reader.
            return helper_source_path.read_bytes()
        return root_safe_reader(path, label, require_root=require_root)

    monkeypatch.setattr(binding_helper, "_read_regular_file", fixture_root_safe_reader)
    monkeypatch.setattr(bootstrap, "BUNDLED_V1_CATALOG_PATH", baseline_path)
    validated_binding = binding_helper.load_bootstrap_catalog_binding(
        binding_path, v2_catalog_path, require_root=True
    )
    updater._load_installed_bootstrap_catalog_binding = lambda: (
        binding_helper.load_bootstrap_catalog_binding(
            binding_path, v2_catalog_path, require_root=True
        )
    )
    updater.bootstrap_catalog_binding = validated_binding
    updater.bootstrap_catalog_authorized = True
    updater.bootstrap_catalog_bytes = active_bytes
    updater.bootstrap_catalog_digest = active_digest
    updater.catalog = active
    updater.catalog_bytes = active_bytes
    updater.catalog_digest = active_digest
    updater.catalog_generation = active["generation"]
    updater.catalog_source = None
    updater.components = {row["componentId"]: row for row in active["components"]}
    updater.targets = {row["id"]: row for row in active["targets"]}
    updater.publishers = {row["repository"]: row for row in active["publishers"]}
    updater._read_active_catalog = lambda: None

    assert updater.bootstrap_catalog_digest == active_digest
    assert updater.bootstrap_catalog_digest != bootstrap.COMPILED_CATALOG_DIGEST
    assert updater.catalog_generation == 15
    assert json.loads(baseline_bytes)["generation"] == 13

    binding_bytes = binding_path.read_bytes()
    tampered_binding = copy.deepcopy(binding)
    tampered_binding["catalog"]["sha256"] = "0" * 64
    binding_path.write_text(json.dumps(tampered_binding, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError, match="binding could not be revalidated"):
        bootstrap._require_active_v2_catalog_context(updater, active_digest)
    binding_path.write_bytes(binding_bytes)

    baseline_path.write_bytes(active_bytes)
    with pytest.raises(ValueError, match="bundled V1 baseline bytes"):
        bootstrap._require_active_v2_catalog_context(updater, active_digest)
    baseline_path.write_bytes(baseline_bytes)

    with bootstrap.exclusive_update_lock(updater) as lock_lease:
        result = bootstrap.bootstrap_verified_runtime_maintenance_under_lock(
            updater,
            **values,
            channel="stable",
            target_id=target_id,
            confirm_plan_digest=plan["planDigest"],
            lock_lease=lock_lease,
            active_catalog_digest=active_digest,
        )

    assert result["status"] == "activated"
    assert result["componentId"] == "cyrene-runtime-maintenance"
    assert result["manifestDigest"] == plan["manifestDigest"]
    assert result["artifactDigest"] == plan["artifactDigest"]


def test_completed_same_plan_can_be_read_only_reconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (
        updater,
        values,
        target_id,
        original_plan,
        journal_path,
        active_path,
        release,
    ) = _completed_broker_plan(tmp_path, monkeypatch)
    journal_bytes = journal_path.read_bytes()
    pointer_target = os.readlink(active_path)
    release_files = {
        path.relative_to(release).as_posix(): path.read_bytes()
        for path in release.rglob("*")
        if path.is_file()
    }
    lock_entries: list[str] = []
    updater._exclusive_update_lock = lambda: lock_entries.append("unexpected-lock")
    monkeypatch.setattr(bootstrap, "_broker_process_exists", lambda _root: False)

    plan = _call(updater, values, target_id)

    assert plan == original_plan
    assert journal_path.read_bytes() == journal_bytes
    assert os.readlink(active_path) == pointer_target
    assert {
        path.relative_to(release).as_posix(): path.read_bytes()
        for path in release.rglob("*")
        if path.is_file()
    } == release_files
    assert lock_entries == []


def test_completed_broker_plan_accepts_one_unit_reached_through_usrmerge_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    updater, values, target_id, original_plan, *_ = _completed_broker_plan(tmp_path, monkeypatch)
    unit_directory = updater.systemd_unit_dirs[0]
    alias_directory = tmp_path / "lib-systemd-system"
    alias_directory.symlink_to(unit_directory, target_is_directory=True)
    updater.systemd_unit_dirs = (unit_directory, alias_directory)
    monkeypatch.setattr(bootstrap, "_broker_process_exists", lambda _root: False)

    replayed = _call(updater, values, target_id)

    assert replayed == original_plan


@pytest.mark.parametrize("alias_kind", ["distinct_file", "leaf_symlink", "unsafe_owner"])
def test_completed_broker_plan_rejects_nonphysical_or_unsafe_unit_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias_kind: str
) -> None:
    updater, values, target_id, _plan, *_ = _completed_broker_plan(
        tmp_path / alias_kind, monkeypatch
    )
    unit_name = updater.components[bootstrap.BOOTSTRAP_COMPONENT_ID]["systemdUnit"]
    unit_directory = updater.systemd_unit_dirs[0]
    unit_path = unit_directory / unit_name
    if alias_kind == "distinct_file":
        second_directory = tmp_path / alias_kind / "second-systemd"
        second_directory.mkdir()
        (second_directory / unit_name).write_bytes(unit_path.read_bytes())
        (second_directory / unit_name).chmod(0o644)
        updater.systemd_unit_dirs = (unit_directory, second_directory)
        expected_message = "missing or ambiguous"
    elif alias_kind == "leaf_symlink":
        second_directory = tmp_path / alias_kind / "second-systemd"
        second_directory.mkdir()
        (second_directory / unit_name).symlink_to(unit_path)
        updater.systemd_unit_dirs = (unit_directory, second_directory)
        expected_message = "differs from this plan"
    else:
        original_lstat = Path.lstat

        def foreign_owner(path: Path) -> os.stat_result:
            metadata = original_lstat(path)
            if path == unit_path:
                fields = list(metadata)
                fields[4] = os.geteuid() + 1
                return os.stat_result(fields)
            return metadata

        monkeypatch.setattr(Path, "lstat", foreign_owner)
        expected_message = "differs from this plan"
    monkeypatch.setattr(bootstrap, "_broker_process_exists", lambda _root: False)

    with pytest.raises(ValueError, match=expected_message):
        _call(updater, values, target_id)


@pytest.mark.parametrize(
    ("changed_state", "message"),
    [("process", "process is running"), ("unit", "ActiveState")],
)
def test_completed_plan_confirmation_rechecks_live_state_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_state: str,
    message: str,
) -> None:
    (
        updater,
        values,
        target_id,
        _original_plan,
        journal_path,
        active_path,
        release,
    ) = _completed_broker_plan(tmp_path / changed_state, monkeypatch)
    plan = _call(updater, values, target_id)
    before_journal = journal_path.read_bytes()
    before_pointer = os.readlink(active_path)
    before_release = {
        path.relative_to(release).as_posix(): path.read_bytes()
        for path in release.rglob("*")
        if path.is_file()
    }
    if changed_state == "process":
        monkeypatch.setattr(bootstrap, "_broker_process_exists", lambda _root: True)
    else:
        updater.runner = lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="active\n", stderr=""
        )

    with pytest.raises((RuntimeError, ValueError), match=message):
        _call(updater, values, target_id, confirm_plan_digest=plan["planDigest"])

    assert journal_path.read_bytes() == before_journal
    assert os.readlink(active_path) == before_pointer
    assert {
        path.relative_to(release).as_posix(): path.read_bytes()
        for path in release.rglob("*")
        if path.is_file()
    } == before_release


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("identity", "does not match this exact plan"),
        ("pointer", "active pointer differs"),
        ("receipt", "receipt|manifest does not match its receipt"),
        ("payload", "payload digest differs"),
        ("unit", "systemd unit differs"),
        ("running", "process is running"),
        ("process_unknown", "UNKNOWN"),
        ("unit_active", "ActiveState"),
        ("unit_unknown", "MainPID"),
    ],
)
def test_completed_broker_replay_rejects_any_changed_live_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    message: str,
) -> None:
    (
        updater,
        values,
        target_id,
        _plan,
        journal_path,
        active_path,
        release,
    ) = _completed_broker_plan(tmp_path / failure, monkeypatch)
    if failure == "identity":
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["identity"]["targetId"] = "linux-ubuntu-22.04-x86_64-systemd"
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
    elif failure == "pointer":
        active_path.unlink()
        active_path.symlink_to("releases/other--" + "0" * 64)
    elif failure == "receipt":
        receipt_path = (
            updater._installed_component_directory(bootstrap.BOOTSTRAP_COMPONENT_ID)
            / "releases"
            / (release.name.split("--", 1)[1] + ".json")
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["artifactDigest"] = _digest(b"different signed artifact")
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    elif failure == "payload":
        (release / "bin" / "cyrene-runtime-maintenance").write_bytes(b"changed payload")
    elif failure == "unit":
        unit_path = (
            updater.systemd_unit_dirs[0]
            / updater.components[bootstrap.BOOTSTRAP_COMPONENT_ID]["systemdUnit"]
        )
        unit_path.write_text("[Service]\nExecStart=/bin/false\n", encoding="utf-8")
    elif failure == "running":
        monkeypatch.setattr(bootstrap, "_broker_process_exists", lambda _root: True)
    elif failure == "process_unknown":
        monkeypatch.setattr(
            bootstrap,
            "_broker_process_exists",
            lambda _root: (_ for _ in ()).throw(RuntimeError("broker state UNKNOWN")),
        )
    elif failure == "unit_active":
        updater.runner = lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="active\n", stderr=""
        )
    elif failure == "unit_unknown":
        updater.runner = lambda argv, **_kwargs: SimpleNamespace(
            returncode=0 if "ActiveState" in argv[2] else 1,
            stdout="inactive\n" if "ActiveState" in argv[2] else "unknown\n",
            stderr="systemd query failed",
        )

    with pytest.raises((RuntimeError, ValueError, updates.UpdateError), match=message):
        _call(updater, values, target_id)


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


@pytest.mark.parametrize(
    ("executable_name", "expected_broker"),
    [("cyrene-runtime-maintenance", True), ("gvfsd", False)],
)
def test_deleted_running_executable_is_classified_by_inode_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executable_name: str,
    expected_broker: bool,
) -> None:
    proc_root = tmp_path / "proc-deleted-broker"
    process = proc_root / "3"
    process.mkdir(parents=True)
    (process / "stat").write_text("3 (maintenance) S 1 0\n")
    (process / "cmdline").write_bytes(b"renamed-process\0")
    executable = tmp_path / executable_name
    executable.write_text("still mapped executable inode")
    exe_link = process / "exe"
    exe_link.symlink_to(executable)
    descriptor = os.open(executable, os.O_RDONLY)
    executable.unlink()
    original_readlink = os.readlink
    original_stat = os.stat

    def deleted_proc_readlink(path: Any, *args: Any, **kwargs: Any) -> str:
        if Path(path) == exe_link:
            return f"{executable} (deleted)"
        return original_readlink(path, *args, **kwargs)

    def deleted_proc_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path) == exe_link:
            return os.fstat(descriptor)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "readlink", deleted_proc_readlink)
    monkeypatch.setattr(os, "stat", deleted_proc_stat)
    monkeypatch.setattr(
        bootstrap, "DEFAULT_BROKER_EXECUTABLE", Path("/usr/bin/cyrene-runtime-maintenance")
    )
    try:
        assert bootstrap._broker_process_exists(proc_root) is expected_broker
    finally:
        os.close(descriptor)


def test_unreadable_process_executable_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc_root = tmp_path / "proc-exe-unreadable"
    process = proc_root / "3"
    process.mkdir(parents=True)
    (process / "stat").write_text("3 (worker) S 1 0\n")
    (process / "cmdline").write_bytes(b"python\0")
    (process / "exe").symlink_to(tmp_path / "python")
    original_readlink = os.readlink

    def deny_process_executable(path: Any, *args: Any, **kwargs: Any) -> str:
        if Path(path) == process / "exe":
            raise PermissionError("mock unreadable procfs executable")
        return original_readlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "readlink", deny_process_executable)
    with pytest.raises(RuntimeError, match="unreadable|UNKNOWN"):
        bootstrap._broker_process_exists(proc_root)


def test_non_regular_process_executable_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc_root = tmp_path / "proc-non-regular-executable"
    process = proc_root / "3"
    process.mkdir(parents=True)
    (process / "stat").write_text("3 (worker) S 1 0\n")
    (process / "cmdline").write_bytes(b"python\0")
    exe_link = process / "exe"
    exe_link.symlink_to(tmp_path / "python")
    original_stat = os.stat

    def report_directory_for_executable(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path) == exe_link:
            return original_stat(tmp_path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", report_directory_for_executable)
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        bootstrap._broker_process_exists(proc_root)
