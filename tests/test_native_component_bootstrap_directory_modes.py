"""Regression tests for public directory modes used by the native broker runner."""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = ROOT / "packaging" / "native_component_bootstrap.py"


def _load_bootstrap() -> Any:
    spec = importlib.util.spec_from_file_location(
        "native_component_bootstrap_directory_modes_test", BOOTSTRAP_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bootstrap = _load_bootstrap()


def _create_umask_restricted_component_tree(
    install_root: Path,
) -> tuple[Path, tuple[Path, ...]]:
    install_root.mkdir()
    install_root.chmod(0o755)
    component_id = bootstrap.BOOTSTRAP_COMPONENT_ID
    component_root = install_root / "components" / component_id
    releases = component_root / "releases"
    release = releases / ("1.2.3--" + "a" * 64)

    previous_umask = os.umask(0o077)
    try:
        component_root.mkdir(parents=True, mode=0o755)
        releases.mkdir(mode=0o755)
        release.mkdir(mode=0o755)
    finally:
        os.umask(previous_umask)

    public_directories = (
        install_root,
        install_root / "components",
        component_root,
        releases,
        release,
    )
    return release, public_directories


def _mock_root_ownership(
    monkeypatch: pytest.MonkeyPatch,
    directories: tuple[Path, ...],
    *,
    wrong_group: Path | None = None,
) -> None:
    original_lstat = Path.lstat
    public_paths = set(directories)

    def root_owned_lstat(path: Path) -> os.stat_result:
        info = original_lstat(path)
        if path not in public_paths:
            return info
        group_id = 42 if path == wrong_group else 0
        return SimpleNamespace(st_mode=info.st_mode, st_uid=0, st_gid=group_id)  # type: ignore[return-value]

    monkeypatch.setattr(Path, "lstat", root_owned_lstat)


def test_confirmed_activation_normalizes_umask_restricted_component_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Set runner-visible modes during activation, before its pointer is exposed."""

    install_root = tmp_path / "usr-lib-cyrene"
    install_root.mkdir()
    install_root.chmod(0o755)
    component_root = install_root / "components" / bootstrap.BOOTSTRAP_COMPONENT_ID
    releases = component_root / "releases"
    release = releases / ("1.2.3--" + "a" * 64)
    private_state = tmp_path / "var-lib-cyrene-updates"
    private_state.mkdir(mode=0o700)
    public_directories = (
        install_root,
        install_root / "components",
        component_root,
        releases,
        release,
    )
    _mock_root_ownership(monkeypatch, public_directories)

    def install_with_restrictive_umask(_updater: Any, _candidate: Any, _payload_root: Path) -> Path:
        previous_umask = os.umask(0o077)
        try:
            component_root.mkdir(parents=True, mode=0o755)
            releases.mkdir(mode=0o755)
            release.mkdir(mode=0o755)
            assert all(
                stat.S_IMODE(path.lstat().st_mode) == 0o700 for path in public_directories[1:]
            )
        finally:
            os.umask(previous_umask)
        return release

    monkeypatch.setattr(bootstrap, "_read_journal", lambda _path: None)
    monkeypatch.setattr(bootstrap, "_assert_fresh_broker", lambda _updater: None)
    monkeypatch.setattr(bootstrap, "_persist_journal", lambda *_args: None)
    monkeypatch.setattr(bootstrap, "_install_or_repair_release", install_with_restrictive_umask)

    def activate_native(
        component_id: str, release_name: str, *, expected_current: str | None
    ) -> None:
        assert expected_current is None
        (install_root / "components" / component_id / "active").symlink_to(
            f"releases/{release_name}"
        )

    updater = SimpleNamespace(
        install_root=install_root,
        _active_native_pointer_identity=lambda _component_id: None,
        _activate_native=activate_native,
        _read_active_receipt=lambda _component_id: None,
        _write_active_receipt=lambda _item: None,
    )
    candidate = SimpleNamespace(
        component={"componentId": bootstrap.BOOTSTRAP_COMPONENT_ID},
        manifest={"version": "1.2.3", "manifestDigest": "sha256:" + "a" * 64},
        artifact_digest="sha256:" + "b" * 64,
    )

    result = bootstrap._activate_confirmed(
        updater,
        candidate=candidate,
        identity={"componentId": bootstrap.BOOTSTRAP_COMPONENT_ID},
        plan_digest="sha256:" + "c" * 64,
        journal_path=tmp_path / "journal.json",
        payload_root=tmp_path / "unused-payload",
    )

    assert result["status"] == "activated"
    assert all(stat.S_IMODE(path.lstat().st_mode) == 0o755 for path in public_directories)
    assert stat.S_IMODE(private_state.lstat().st_mode) == 0o700


@pytest.mark.parametrize("unsafe_path", ["group-writable", "wrong-group", "symlink"])
def test_unsafe_public_component_directory_is_rejected_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe_path: str
) -> None:
    """Refuse untrusted ownership, writable ancestors, and linked release paths."""

    install_root = tmp_path / "usr-lib-cyrene"
    release, public_directories = _create_umask_restricted_component_tree(install_root)
    target = install_root / "components"
    wrong_group = None
    if unsafe_path == "group-writable":
        target.chmod(0o775)
    elif unsafe_path == "wrong-group":
        target = install_root / "components" / bootstrap.BOOTSTRAP_COMPONENT_ID
        wrong_group = target
    else:
        release.rmdir()
        release.symlink_to(tmp_path / "outside", target_is_directory=True)
    _mock_root_ownership(monkeypatch, public_directories, wrong_group=wrong_group)

    with pytest.raises(ValueError, match="unsafe|outside its fixed"):
        bootstrap._ensure_public_component_directories(
            SimpleNamespace(install_root=install_root), bootstrap.BOOTSTRAP_COMPONENT_ID, release
        )

    if unsafe_path == "group-writable":
        assert stat.S_IMODE(target.lstat().st_mode) == 0o775
    assert (
        stat.S_IMODE(
            (install_root / "components" / bootstrap.BOOTSTRAP_COMPONENT_ID).lstat().st_mode
        )
        == 0o700
    )
