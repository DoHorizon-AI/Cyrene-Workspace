"""
┌─────────────────────────────────────────────────────────────────────┐
│ Module: tests.test_native_admin_authorization                        │
│ Role: Guard the optional, fixed-helper native operator authorization.│
│                                                                      │
│ 模块职责：验证可选普通运维账号授权只允许固定更新 helper。              │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
SPEC = importlib.util.spec_from_file_location("native_operator_admin_test_module", ADMIN_PATH)
assert SPEC is not None and SPEC.loader is not None
admin_initialize = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = admin_initialize
SPEC.loader.exec_module(admin_initialize)

HELPER_BYTES = b"#!/bin/sh\nexec /usr/bin/cyrene update --json\n"


@pytest.fixture(autouse=True)
def _simulate_root_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model root ownership metadata without running tests as root."""

    monkeypatch.setattr(admin_initialize, "ADMIN_ROOT_UID", os.getuid())
    monkeypatch.setattr(admin_initialize, "ADMIN_ROOT_GID", os.getgid())
    monkeypatch.setattr(admin_initialize.os, "chown", lambda *_args: None)
    monkeypatch.setattr(admin_initialize.os, "fchown", lambda *_args: None)


def _root_file(path: Path, content: bytes, mode: int) -> None:
    """Create a root-owned package fixture with the exact expected mode."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.chmod(path, mode)
    os.chown(path, 0, 0)


def _lookup(name: str) -> Any:
    """Resolve only the fixed test account through a deterministic NSS stub."""

    if name == "ruanyun":
        return SimpleNamespace(pw_name="ruanyun", pw_uid=1200)
    if name == "root":
        return SimpleNamespace(pw_name="root", pw_uid=0)
    raise KeyError(name)


def _runner(*, fail: bool = False):
    """Return a visudo-only mock runner that never invokes host privilege tools."""

    calls: list[list[str]] = []

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        assert arguments[:3] == ["/usr/sbin/visudo", "-c", "-f"]
        assert Path(arguments[3]).is_file()
        if fail:
            return subprocess.CompletedProcess(arguments, 1, "", "mock syntax rejection")
        return subprocess.CompletedProcess(arguments, 0, "parsed OK", "")

    return run, calls


@pytest.fixture
def authorization_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Prepare isolated root-owned helper, include, and private staging paths."""

    helper = tmp_path / "usr/libexec/cyrene-component-update-helper"
    _root_file(helper, HELPER_BYTES, 0o755)
    sudoers = tmp_path / "etc/sudoers.d"
    sudoers.mkdir(parents=True)
    os.chmod(sudoers, 0o755)
    os.chown(sudoers, 0, 0)
    staging = tmp_path / "var/lib/cyrene/native-initialization/operator-private"
    return helper, sudoers, staging


def _authorize(
    username: str,
    helper: Path,
    sudoers: Path,
    staging: Path,
    *,
    runner=None,
) -> dict[str, Any]:
    """Invoke the authorization seam with fully mocked account/tool lookups."""

    selected_runner, _calls = _runner() if runner is None else (runner, [])
    return admin_initialize._authorize_component_update_operator(
        username,
        HELPER_BYTES,
        sudoers_directory=sudoers,
        helper_path=helper,
        staging_parent=staging,
        lookup=_lookup,
        runner=selected_runner,
    )


def test_operator_rule_grants_only_fixed_no_argument_helper(
    authorization_paths: tuple[Path, Path, Path],
) -> None:
    """The generated rule is narrow, root-owned, syntax checked, and receipted."""

    helper, sudoers, staging = authorization_paths
    runner, calls = _runner()
    receipt = _authorize("ruanyun", helper, sudoers, staging, runner=runner)
    rule_path = sudoers / "cyrene-component-update-ruanyun"

    assert receipt["username"] == "ruanyun"
    assert receipt["uid"] == 1200
    assert receipt["helper"] == str(helper)
    assert receipt["sudoersSha256"].startswith("sha256:")
    assert receipt["status"] == "installed"
    assert rule_path.read_bytes() == (
        b'ruanyun ALL=(root) NOPASSWD: /usr/libexec/cyrene-component-update-helper ""\n'
    )
    info = rule_path.stat()
    assert (info.st_uid, info.st_gid, info.st_mode & 0o777) == (
        os.getuid(),
        os.getgid(),
        0o440,
    )
    assert len(calls) == 1
    assert not list(staging.iterdir())


@pytest.mark.parametrize("username", ["root", "", "bad/name", "-unsafe", "missing"])
def test_operator_rejects_root_or_invalid_accounts(
    authorization_paths: tuple[Path, Path, Path], username: str
) -> None:
    """Root, malformed names, and absent NSS accounts never create a rule."""

    helper, sudoers, staging = authorization_paths
    with pytest.raises(admin_initialize.AdminInitializationError):
        _authorize(username, helper, sudoers, staging)
    assert not (sudoers / f"cyrene-component-update-{username}").exists()


def test_operator_rejects_helper_symlink_and_tampered_bytes(
    authorization_paths: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """The installed helper must be a real root-owned file matching signed bytes."""

    helper, sudoers, staging = authorization_paths
    saved = tmp_path / "saved-helper"
    helper.rename(saved)
    helper.symlink_to(saved)
    with pytest.raises(admin_initialize.AdminInitializationError, match="metadata is unsafe"):
        _authorize("ruanyun", helper, sudoers, staging)
    helper.unlink()
    _root_file(helper, b"tampered helper", 0o755)
    with pytest.raises(
        admin_initialize.AdminInitializationError, match="differs from verified DEB"
    ):
        _authorize("ruanyun", helper, sudoers, staging)
    assert not (sudoers / "cyrene-component-update-ruanyun").exists()


def test_existing_nonmatching_rule_is_never_overwritten(
    authorization_paths: tuple[Path, Path, Path],
) -> None:
    """An existing sudoers entry is refused without changing its bytes."""

    helper, sudoers, staging = authorization_paths
    destination = sudoers / "cyrene-component-update-ruanyun"
    _root_file(destination, b"ruanyun ALL=(root) NOPASSWD: ALL\n", 0o440)
    before = destination.read_bytes()
    with pytest.raises(admin_initialize.AdminInitializationError, match="refusing overwrite"):
        _authorize("ruanyun", helper, sudoers, staging)
    assert destination.read_bytes() == before


def test_visudo_failure_leaves_no_rule_and_matching_rule_is_idempotent(
    authorization_paths: tuple[Path, Path, Path],
) -> None:
    """Syntax failure rolls back staging, and exact existing rules are read-only."""

    helper, sudoers, staging = authorization_paths
    failing_runner, calls = _runner(fail=True)
    with pytest.raises(admin_initialize.AdminInitializationError, match="visudo failed"):
        _authorize("ruanyun", helper, sudoers, staging, runner=failing_runner)
    assert calls and not (sudoers / "cyrene-component-update-ruanyun").exists()
    assert not staging.exists() or not list(staging.iterdir())

    _authorize("ruanyun", helper, sudoers, staging)
    existing = sudoers / "cyrene-component-update-ruanyun"
    before = existing.read_bytes()
    receipt = _authorize("ruanyun", helper, sudoers, staging)
    assert receipt["status"] == "verified-existing"
    assert existing.read_bytes() == before


def test_post_install_readback_failure_removes_only_new_rule(
    authorization_paths: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed post-install check removes the just-created matching rule."""

    helper, sudoers, staging = authorization_paths
    original_read_bytes = Path.read_bytes

    def fail_rule_readback(path: Path) -> bytes:
        if path == sudoers / "cyrene-component-update-ruanyun" and path.exists():
            raise OSError("mock readback failure")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_rule_readback)
    with pytest.raises(OSError, match="mock readback failure"):
        _authorize("ruanyun", helper, sudoers, staging)
    assert not (sudoers / "cyrene-component-update-ruanyun").exists()


def test_operator_argument_is_explicitly_optional() -> None:
    """Ordinary initialization does not select or authorize an operator account."""

    parser = admin_initialize.build_parser()
    args = parser.parse_args(
        [
            "--release-directory",
            "/release",
            "--expected-source-ref",
            "refs/heads/main",
            "--expected-source-commit",
            "a" * 40,
            "--index",
            "/index",
            "--index-attestation",
            "/index.sig",
            "--manifest",
            "/manifest",
            "--artifact",
            "/artifact",
            "--artifact-attestation",
            "/artifact.sig",
            "--channel",
            "stable",
        ]
    )
    assert args.operator_user is None
