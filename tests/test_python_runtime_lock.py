"""Focused validation tests for the isolated DEB Python runtime."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import platform
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = WORKSPACE_ROOT / "packaging" / "python-runtime.lock.json"


def _module() -> ModuleType:
    path = WORKSPACE_ROOT / "packaging" / "python_runtime.py"
    spec = importlib.util.spec_from_file_location("cyrene_python_runtime_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _lock() -> dict:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def test_locked_runtime_uses_real_official_asset_and_separate_archive_digest() -> None:
    module = _module()
    lock = _lock()
    python = lock["python"]
    archive = python["archive"]
    release = archive["release"]

    assert python["version"] == "3.12.14"
    assert python["installRoot"] == "/opt/cyrene/python/3.12.14"
    assert python["executable"] == "/opt/cyrene/python/3.12.14/bin/python3.12"
    assert release["repository"] == "astral-sh/python-build-standalone"
    assert release["tag"] == "20260929"
    assert release["commit"] == "b498734a5791d0e6786695a226fd398a41c6f7f6"
    assert release["assetId"] == 598637702
    assert archive["url"].endswith(
        "cpython-3.12.14%2B20260929-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
    )
    assert archive["sha256"] == "ef605200f8174e87ecfc308e52a88127543f85dd5c940dc5e92cab244b98a003"
    assert lock["buildResolver"]["version"] == "0.12.21"
    assert lock["buildResolver"]["installedPath"] == "/opt/cyrene/uv/0.12.21/uv"
    assert lock["buildResolver"]["usage"] == {
        "build": "resolve-frozen-python-distribution-mapping",
        "runtime": "explicit-trainer-environment-prepare",
        "automaticRuntimeBootstrap": False,
    }
    assert lock["buildResolver"]["distributionMetadata"]["mapping"]["sha256"] == archive["sha256"]
    assert lock["runtimeDependencies"]["systemPackages"] == [
        "ca-certificates",
        "libcrypt1",
        "libgcc-s1",
    ]
    assert lock["payload"]["verificationRecordPath"].endswith(
        "python-runtime-verification.json"
    )

    module.validate_lock(lock, LOCK_PATH)


def test_runtime_lock_rejects_archive_hash_or_uv_mapping_drift() -> None:
    module = _module()
    lock = _lock()
    lock["python"]["archive"]["sha256"] = "0" * 64
    with pytest.raises(module.PythonRuntimeError, match="uv's pinned distribution mapping"):
        module.validate_lock(lock, LOCK_PATH)
    lock = _lock()
    lock["buildResolver"]["distributionMetadata"]["sha256"] = "0" * 64
    with pytest.raises(module.PythonRuntimeError, match="immutable 0.12.21 source"):
        module.validate_lock(lock, LOCK_PATH)


def test_runtime_lock_rejects_unpinned_uv_install_path_or_usage() -> None:
    module = _module()
    lock = _lock()
    lock["buildResolver"]["installedPath"] = "/usr/local/bin/uv"
    with pytest.raises(module.PythonRuntimeError, match="runtime path and explicit-use policy"):
        module.validate_lock(lock, LOCK_PATH)


def test_runtime_stages_the_verified_uv_executable_at_its_locked_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    source = tmp_path / "uv"
    source.write_bytes(b"pinned uv bytes")
    source.chmod(0o755)
    resolver = {
        "version": "0.12.21",
        "installedPath": "/opt/cyrene/uv/0.12.21/uv",
        "binaryArchive": {"executableSha256": hashlib.sha256(source.read_bytes()).hexdigest()},
    }
    monkeypatch.setattr(module, "_uv_version", lambda executable: "0.12.21")

    staged = module._stage_uv_runtime(source, resolver, tmp_path / "stage")

    assert staged == tmp_path / "stage/opt/cyrene/uv/0.12.21/uv"
    assert staged.read_bytes() == source.read_bytes()
    assert staged.stat().st_mode & 0o777 == 0o755
    with pytest.raises(module.PythonRuntimeError, match="already exists"):
        module._stage_uv_runtime(source, resolver, tmp_path / "stage")


def test_runtime_staging_profile_must_match_the_exact_ubuntu_build_host() -> None:
    module = _module()
    lock = _lock()
    host_version = platform.freedesktop_os_release().get("VERSION_ID")
    supported_versions = {"22.04", "24.04"}
    if host_version not in supported_versions:
        pytest.skip("runtime staging requires Ubuntu 22.04 or Ubuntu 24.04")
    target = f"linux-ubuntu-{host_version}-x86_64-python-3.12"

    module._validate_profile(WORKSPACE_ROOT / "release-lock.json", LOCK_PATH, target, lock)
    other_version = "24.04" if host_version == "22.04" else "22.04"
    with pytest.raises(module.PythonRuntimeError, match="does not match"):
        module._validate_profile(
            WORKSPACE_ROOT / "release-lock.json",
            LOCK_PATH,
            f"linux-ubuntu-{other_version}-x86_64-python-3.12",
            lock,
        )


def test_local_archive_must_match_official_size_and_digest(tmp_path: Path) -> None:
    module = _module()
    source = tmp_path / "archive.tar.gz"
    source.write_bytes(b"official bytes")
    destination = tmp_path / "work" / "archive.tar.gz"

    with pytest.raises(module.PythonRuntimeError, match="size or SHA-256 mismatch"):
        module._verified_local_or_download(
            source,
            "https://github.com/astral-sh/python-build-standalone/release/file.tar.gz",
            destination,
            sha256=hashlib.sha256(b"other bytes").hexdigest(),
            size=len(b"official bytes"),
        )
    assert not destination.exists()

    source.unlink()
    source.symlink_to(tmp_path / "untrusted")
    (tmp_path / "untrusted").write_bytes(b"official bytes")
    with pytest.raises(module.PythonRuntimeError, match="regular non-symlink"):
        module._verified_local_or_download(
            source,
            "https://github.com/astral-sh/python-build-standalone/release/file.tar.gz",
            destination,
            sha256=hashlib.sha256(b"official bytes").hexdigest(),
            size=len(b"official bytes"),
        )


def test_private_python_archive_rejects_traversal_and_escaping_symlinks(tmp_path: Path) -> None:
    module = _module()
    archive_path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        content = b"payload"
        member = tarfile.TarInfo("python/../../escape")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))

    with pytest.raises(module.PythonRuntimeError, match="unexpected root|unsafe path"):
        module._validate_archive_paths(archive_path, root_name="python")

    with tarfile.open(archive_path, "w:gz") as archive:
        link = tarfile.TarInfo("python/bin/python3.12")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../../etc/passwd"
        archive.addfile(link)
    with pytest.raises(module.PythonRuntimeError, match="symlink escapes"):
        module._validate_archive_paths(archive_path, root_name="python")
