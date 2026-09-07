"""Reference runtime coordination tests. | Reference runtime 协调测试。"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "reference-runtime.py"
    spec = importlib.util.spec_from_file_location("reference_runtime", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        command="bootstrap",
        runtime_home=tmp_path / "runtime",
        profile="NATIVE_LINUX_PROFILE",
        no_build=False,
    )


def test_bootstrap_emits_only_component_profiles_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    monkeypatch.setattr(
        module,
        "_commands",
        lambda _args, _home: {
            "platformUp": ["platform-up"],
            "platformDown": ["platform-down"],
            "trainerBootstrap": ["trainer-up"],
            "reactorBootstrap": ["reactor-up"],
        },
    )
    monkeypatch.setattr(
        module,
        "_invoke",
        lambda command, _code: {"profile": command[0], "status": "READY"},
    )
    result = module.run(_args(tmp_path))
    assert result["status"] == "READY"
    assert set(result["components"]) == {"platform", "trainer", "reactor"}
    assert "RuntimeConfig" not in str(result)
    assert (tmp_path / "runtime" / "reference-runtime.json").stat().st_mode & 0o077 == 0


def test_failed_child_bootstrap_tears_down_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    calls: list[str] = []
    monkeypatch.setattr(
        module,
        "_commands",
        lambda _args, _home: {
            "platformUp": ["platform-up"],
            "platformDown": ["platform-down"],
            "trainerBootstrap": ["trainer-up"],
            "reactorBootstrap": ["reactor-up"],
        },
    )

    def invoke(command: list[str], _code: str) -> dict[str, str]:
        calls.append(command[0])
        if command[0] == "trainer-up":
            raise ValueError("TRAINER_FAILED")
        return {"profile": command[0], "status": "READY"}

    monkeypatch.setattr(module, "_invoke", invoke)
    with pytest.raises(ValueError, match="TRAINER_FAILED"):
        module.run(_args(tmp_path))
    assert calls == ["platform-up", "trainer-up", "platform-down"]
