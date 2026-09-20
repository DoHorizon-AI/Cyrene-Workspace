"""Release-lock validation tests. | 发布锁校验测试。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

WORKSPACE_ROOT = Path(__file__).parents[1]


def _module() -> ModuleType:
    path = WORKSPACE_ROOT / "scripts" / "validate_release_lock.py"
    spec = importlib.util.spec_from_file_location("validate_release_lock", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _document() -> dict:
    return json.loads((WORKSPACE_ROOT / "release-lock.json").read_text(encoding="utf-8"))


def test_repository_lock_is_structurally_valid() -> None:
    errors, _ = _module().validate(_document())
    assert errors == []


def test_repository_lock_names_current_blockers() -> None:
    _, blockers = _module().validate(_document())
    assert any("training.llama-factory.v1" in blocker for blocker in blockers)
    assert any("execution.engine.v1" in blocker for blocker in blockers)


def test_lock_rejects_an_unpinned_acceptance_model() -> None:
    document = _document()
    document["acceptanceModel"]["revision"] = "main"
    errors, _ = _module().validate(document)
    assert any("acceptanceModel.revision" in error for error in errors)


def test_lock_rejects_missing_repository_revisions() -> None:
    document = _document()
    del document["repositories"]["Cyrene-Reactor"]
    errors, _ = _module().validate(document)
    assert any("Cyrene-Reactor" in error for error in errors)