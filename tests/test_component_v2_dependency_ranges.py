"""Strict v2 component dependency range regressions. | v2 组件依赖范围严格回归。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = WORKSPACE_ROOT / "governance" / "component-catalog-v1.json"
UPDATES_PATH = WORKSPACE_ROOT / "packaging" / "component_updates.py"
UPDATES_SPEC = importlib.util.spec_from_file_location(
    "cyrene_component_v2_dependency_range_test", UPDATES_PATH
)
assert UPDATES_SPEC is not None and UPDATES_SPEC.loader is not None
updates = importlib.util.module_from_spec(UPDATES_SPEC)
sys.modules[UPDATES_SPEC.name] = updates
UPDATES_SPEC.loader.exec_module(updates)


def _catalog_components() -> dict[str, dict[str, Any]]:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return {item["componentId"]: item for item in catalog["components"]}


def test_c11_kernel_dependency_is_bounded_and_required_in_v2_manifest() -> None:
    components = _catalog_components()
    kernel = components["cyrene-kernel"]
    dependency = {
        "componentId": "cyrene-sandboxd",
        "versionRange": ">=0.1.0, <0.2.0",
    }
    assert kernel["protocolVersion"] == "cyrene.runtime-maintenance.state.v2"
    assert kernel["dependencies"] == [dependency]

    updater = SimpleNamespace(components=components)
    validate = updates.ComponentUpdater._validate_manifest_dependencies
    validate(updater, [dependency], kernel, require_version_range=True)

    with pytest.raises(updates.UpdateError, match="dependency record is malformed"):
        validate(
            updater,
            [{"componentId": "cyrene-sandboxd"}],
            kernel,
            require_version_range=True,
        )
