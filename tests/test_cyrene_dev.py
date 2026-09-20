"""cyrene-dev orchestrator boundary tests. | 开发编排器边界测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "cyrene-dev.py"
    spec = importlib.util.spec_from_file_location("cyrene_dev", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_worktrees_derives_only_real_sibling_checkouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    platform = tmp_path / "Cyrene-Platform"
    trainer = tmp_path / "Cyrene-Services" / "Cyrene-Yield"
    (platform / ".git").mkdir(parents=True)
    trainer.mkdir(parents=True)
    monkeypatch.setattr(
        module,
        "DEFAULT_WORKTREES",
        {
            "CYRENE_PLATFORM_WORKTREE": platform,
            "CYRENE_YIELD_WORKTREE": trainer,
        },
    )
    for variable in module.DEFAULT_WORKTREES:
        monkeypatch.delenv(variable, raising=False)

    derived = module.default_worktrees()

    assert derived == {"CYRENE_PLATFORM_WORKTREE": str(platform)}


def test_default_worktrees_never_overrides_an_explicit_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    platform = tmp_path / "Cyrene-Platform"
    (platform / ".git").mkdir(parents=True)
    monkeypatch.setattr(module, "DEFAULT_WORKTREES", {"CYRENE_PLATFORM_WORKTREE": platform})
    monkeypatch.setenv("CYRENE_PLATFORM_WORKTREE", "/srv/cyrene/platform")

    assert module.default_worktrees() == {}


def test_guard_home_refuses_destructive_roots(tmp_path: Path) -> None:
    module = _module()
    for unsafe in (Path("/"), Path.home()):
        with pytest.raises(ValueError):
            module.guard_home(unsafe)

    guarded = module.guard_home(tmp_path / "dev")
    module.prepare_directories(guarded)

    assert guarded == (tmp_path / "dev").resolve()
    assert (guarded / "services").is_dir()
    assert (guarded / "artifacts").is_dir()
    assert guarded.stat().st_mode & 0o077 == 0
