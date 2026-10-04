"""
┌─────────────────────────────────────────────────────────────────────┐
│ Module: tests.test_native_admin_host_identity                       │
│ Role: Verify strict Ubuntu host identity file handling.             │
│                                                                      │
│ 模块职责：验证 Ubuntu 主机身份文件的严格读取边界。                    │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ADMIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "tooling"
    / "acceptance"
    / "native-components-v2"
    / "admin_initialize.py"
)
NATIVE_PATH = ADMIN_PATH.with_name("native_acceptance.py")
NATIVE_SPEC = importlib.util.spec_from_file_location("native_acceptance", NATIVE_PATH)
assert NATIVE_SPEC is not None and NATIVE_SPEC.loader is not None
native_acceptance = importlib.util.module_from_spec(NATIVE_SPEC)
sys.modules[NATIVE_SPEC.name] = native_acceptance
NATIVE_SPEC.loader.exec_module(native_acceptance)
ADMIN_SPEC = importlib.util.spec_from_file_location("native_admin_host_identity_test", ADMIN_PATH)
assert ADMIN_SPEC is not None and ADMIN_SPEC.loader is not None
admin_initialize = importlib.util.module_from_spec(ADMIN_SPEC)
sys.modules[ADMIN_SPEC.name] = admin_initialize
ADMIN_SPEC.loader.exec_module(admin_initialize)


@pytest.fixture
def release_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, int]]:
    """Create isolated OS-release paths and mock root-owned file metadata."""

    root = tmp_path / "host-root"
    (root / "etc").mkdir(parents=True)
    (root / "usr/lib").mkdir(parents=True)
    metadata = {"link_uid": 0, "file_uid": 0, "file_mode": 0o644}
    real_lstat = os.lstat
    real_fstat = os.fstat
    link_path = root / "etc/os-release"

    def mocked_lstat(path: str | bytes | os.PathLike[str] | os.PathLike[bytes]):
        result = real_lstat(path)
        if Path(path) == link_path and stat.S_ISLNK(result.st_mode):
            return SimpleNamespace(st_mode=result.st_mode, st_uid=metadata["link_uid"])
        return result

    def mocked_fstat(descriptor: int):
        result = real_fstat(descriptor)
        candidate_paths = (link_path, root / "usr/lib/os-release")
        for candidate in candidate_paths:
            try:
                candidate_info = real_lstat(candidate)
            except OSError:
                continue
            if (candidate_info.st_dev, candidate_info.st_ino) == (result.st_dev, result.st_ino):
                break
        else:
            return result
        return SimpleNamespace(
            st_mode=stat.S_IFMT(result.st_mode) | metadata["file_mode"],
            st_uid=metadata["file_uid"],
        )

    monkeypatch.setattr(admin_initialize.os, "lstat", mocked_lstat)
    monkeypatch.setattr(admin_initialize.os, "fstat", mocked_fstat)
    return root, metadata


def _write_release(path: Path, *, distro: str = "ubuntu", version: str = "22.04") -> None:
    """Write the minimal release identity fields used by the target selector."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'ID={distro}\nVERSION_ID="{version}"\n', encoding="utf-8")


@pytest.mark.parametrize("version", ["22.04", "24.04"])
@pytest.mark.parametrize("use_standard_link", [False, True])
def test_accepts_supported_ubuntu_release_as_regular_file_or_standard_link(
    release_root: tuple[Path, dict[str, int]],
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    use_standard_link: bool,
) -> None:
    """Accept regular release files and Ubuntu's exact relative symlink."""

    root, _metadata = release_root
    monkeypatch.setattr(admin_initialize.platform, "machine", lambda: "x86_64")
    if use_standard_link:
        _write_release(root / "usr/lib/os-release", version=version)
        (root / "etc/os-release").symlink_to("../usr/lib/os-release")
    else:
        _write_release(root / "etc/os-release", version=version)

    assert admin_initialize._host_ubuntu_version(root) == version


@pytest.mark.parametrize(
    ("link_target", "create_target"),
    [
        ("../../outside/os-release", True),
        ("../usr/lib/os-release", False),
    ],
)
def test_rejects_external_or_dangling_os_release_links(
    release_root: tuple[Path, dict[str, int]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    link_target: str,
    create_target: bool,
) -> None:
    """Reject links that escape the fixed target or do not resolve."""

    root, _metadata = release_root
    monkeypatch.setattr(admin_initialize.platform, "machine", lambda: "x86_64")
    if create_target:
        _write_release(tmp_path / "outside/os-release")
    if link_target == "../usr/lib/os-release" and create_target:
        _write_release(root / "usr/lib/os-release")
    (root / "etc/os-release").symlink_to(link_target)

    with pytest.raises(
        admin_initialize.AdminInitializationError,
        match="Host OS release identity is unavailable",
    ):
        admin_initialize._host_ubuntu_version(root)


def test_rejects_unowned_standard_link_and_target(
    release_root: tuple[Path, dict[str, int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Require root ownership for both the link and its target file."""

    root, metadata = release_root
    monkeypatch.setattr(admin_initialize.platform, "machine", lambda: "x86_64")
    _write_release(root / "usr/lib/os-release")
    (root / "etc/os-release").symlink_to("../usr/lib/os-release")

    metadata["link_uid"] = 1000
    with pytest.raises(admin_initialize.AdminInitializationError, match="identity is unavailable"):
        admin_initialize._host_ubuntu_version(root)

    metadata["link_uid"] = 0
    metadata["file_uid"] = 1000
    with pytest.raises(admin_initialize.AdminInitializationError, match="identity is unavailable"):
        admin_initialize._host_ubuntu_version(root)


@pytest.mark.parametrize("mode", [0o664, 0o646])
def test_rejects_group_or_world_writable_release_file(
    release_root: tuple[Path, dict[str, int]],
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
) -> None:
    """Reject release files writable by group or other users."""

    root, metadata = release_root
    monkeypatch.setattr(admin_initialize.platform, "machine", lambda: "x86_64")
    _write_release(root / "etc/os-release")
    metadata["file_mode"] = mode

    with pytest.raises(admin_initialize.AdminInitializationError, match="identity is unavailable"):
        admin_initialize._host_ubuntu_version(root)


@pytest.mark.parametrize("kind", ["directory", "fifo", "second_symlink"])
def test_rejects_nonregular_release_target(
    release_root: tuple[Path, dict[str, int]],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Reject directories, FIFOs, and a second symlink at the fixed target path."""

    root, _metadata = release_root
    monkeypatch.setattr(admin_initialize.platform, "machine", lambda: "x86_64")
    if kind == "directory":
        (root / "etc/os-release").mkdir()
    elif kind == "fifo":
        os.mkfifo(root / "etc/os-release")
    else:
        target = root / "usr/lib/os-release"
        _write_release(root / "usr/lib/os-release-real")
        target.symlink_to("os-release-real")
        (root / "etc/os-release").symlink_to("../usr/lib/os-release")

    with pytest.raises(admin_initialize.AdminInitializationError, match="identity is unavailable"):
        admin_initialize._host_ubuntu_version(root)


def test_rejects_parent_symlink_that_redirects_the_fixed_target(
    release_root: tuple[Path, dict[str, int]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reject parent-directory links even when the os-release link text is canonical."""

    root, _metadata = release_root
    monkeypatch.setattr(admin_initialize.platform, "machine", lambda: "x86_64")
    outside = tmp_path / "outside"
    _write_release(outside / "os-release")
    (root / "usr/lib").rmdir()
    (root / "usr/lib").symlink_to(outside, target_is_directory=True)
    (root / "etc/os-release").symlink_to("../usr/lib/os-release")

    with pytest.raises(admin_initialize.AdminInitializationError, match="identity is unavailable"):
        admin_initialize._host_ubuntu_version(root)


@pytest.mark.parametrize(
    ("contents", "machine", "message"),
    [
        ('ID=fedora\nVERSION_ID="22.04"\n', "x86_64", "only Ubuntu x86_64"),
        ('ID=ubuntu\nVERSION_ID="20.04"\n', "x86_64", "outside the signed DEB targets"),
        ('ID=ubuntu\nVERSION_ID="22.04"\n', "aarch64", "only Ubuntu x86_64"),
    ],
)
def test_preserves_distribution_version_and_architecture_gates(
    release_root: tuple[Path, dict[str, int]],
    monkeypatch: pytest.MonkeyPatch,
    contents: str,
    machine: str,
    message: str,
) -> None:
    """Keep the release identity limited to the signed Ubuntu x86_64 targets."""

    root, _metadata = release_root
    monkeypatch.setattr(admin_initialize.platform, "machine", lambda: machine)
    release_path = root / "etc/os-release"
    release_path.write_text(contents, encoding="utf-8")

    with pytest.raises(admin_initialize.AdminInitializationError, match=message):
        admin_initialize._host_ubuntu_version(root)
