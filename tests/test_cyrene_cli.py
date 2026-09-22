"""Unified Cyrene CLI data-safety tests. | Cyrene 统一 CLI 数据安全测试。"""

from __future__ import annotations

import importlib.util
import io
import tarfile
from argparse import Namespace
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pytest

WORKSPACE_ROOT = Path(__file__).parents[1]


def _module() -> ModuleType:
    path = WORKSPACE_ROOT / "cyrene"
    loader = SourceFileLoader("cyrene_cli", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _set_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "state" / "cyrene" / "dev"
    home.mkdir(parents=True)
    monkeypatch.setenv("CYRENE_DEV_HOME", str(home))
    return home


def test_backup_rejects_destination_inside_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    home = _set_home(tmp_path, monkeypatch)
    (home / "artifact.txt").write_text("important", encoding="utf-8")
    target = home / "backups" / "unsafe.tar.gz"

    assert module.cmd_backup(Namespace(dest=str(target))) == 1
    assert not target.exists()
    assert not target.parent.exists()


def test_backup_is_created_with_canonical_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    home = _set_home(tmp_path, monkeypatch)
    (home / "artifact.txt").write_text("important", encoding="utf-8")
    target = tmp_path / "safe.tar.gz"

    assert module.cmd_backup(Namespace(dest=str(target))) == 0

    with tarfile.open(target, "r:gz") as archive:
        names = archive.getnames()
    assert names[0] == "cyrene-dev"
    assert "cyrene-dev/artifact.txt" in names
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_restore_rejects_path_traversal_and_preserves_existing_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    home = _set_home(tmp_path, monkeypatch)
    existing = home / "existing.txt"
    existing.write_text("preserve", encoding="utf-8")
    archive_path = tmp_path / "malicious.tar.gz"
    payload = b"escaped"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo("cyrene-dev/../../escaped.txt")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))

    assert module.cmd_restore(Namespace(src=str(archive_path))) == 1
    assert existing.read_text(encoding="utf-8") == "preserve"
    assert not (tmp_path / "escaped.txt").exists()


def test_restore_atomically_replaces_stale_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    home = _set_home(tmp_path, monkeypatch)
    (home / "stale.txt").write_text("old", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "restored.txt").write_text("new", encoding="utf-8")
    archive_path = tmp_path / "valid.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source, arcname="cyrene-dev")

    assert module.cmd_restore(Namespace(src=str(archive_path))) == 0
    assert (home / "restored.txt").read_text(encoding="utf-8") == "new"
    assert not (home / "stale.txt").exists()
    assert home.stat().st_mode & 0o077 == 0


def test_restore_rejects_related_directory_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    home = _set_home(tmp_path, monkeypatch)
    existing = home / "existing.txt"
    existing.write_text("preserve", encoding="utf-8")

    assert module.cmd_restore(Namespace(src=str(home.parent))) == 1
    assert existing.read_text(encoding="utf-8") == "preserve"
