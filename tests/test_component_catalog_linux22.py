"""Ubuntu 22.04 component catalog support regressions. | Ubuntu 22.04 组件目录支持回归。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = WORKSPACE_ROOT / "governance" / "component-catalog-v1.json"
TARGET_ID = "linux-ubuntu-22.04-x86_64-python-3.12"
PRODUCT_IDS = {
    "cyrene-catalyst",
    "cyrene-exchange",
    "cyrene-navigator",
    "cyrene-reactor",
    "cyrene-yield",
}


def _catalog() -> dict[str, Any]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _component_map(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {component["componentId"]: component for component in catalog["components"]}


def _catalog_metadata_module() -> ModuleType:
    path = WORKSPACE_ROOT / "packaging" / "catalog_metadata.py"
    spec = importlib.util.spec_from_file_location("cyrene_catalog_metadata_linux22_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_catalog_validates_against_catalog_and_manifest_schemas() -> None:
    validated = _catalog_metadata_module()._validate_catalog(
        CATALOG_PATH.read_bytes(), WORKSPACE_ROOT / "governance"
    )

    assert validated["generation"] == 10


def test_generation9_schema_requires_the_ubuntu22_target_for_each_product() -> None:
    metadata = _catalog_metadata_module()
    catalog = deepcopy(_catalog())
    catalyst = next(
        component
        for component in catalog["components"]
        if component["componentId"] == "cyrene-catalyst"
    )
    catalyst["targets"] = [row for row in catalyst["targets"] if row["targetId"] != TARGET_ID]

    with pytest.raises(metadata.CatalogMetadataError, match="schema violation"):
        metadata._validate_catalog(
            json.dumps(catalog).encode("utf-8"), WORKSPACE_ROOT / "governance"
        )


def test_generation9_schema_rejects_ubuntu22_python_target_for_echo() -> None:
    metadata = _catalog_metadata_module()
    catalog = deepcopy(_catalog())
    echo = next(
        component
        for component in catalog["components"]
        if component["componentId"] == "cyrene-echo"
    )
    echo["targets"].append(
        {
            "targetId": TARGET_ID,
            "artifactKind": "python-bundle",
            "support": "supported",
        }
    )

    with pytest.raises(metadata.CatalogMetadataError, match="schema violation"):
        metadata._validate_catalog(
            json.dumps(catalog).encode("utf-8"), WORKSPACE_ROOT / "governance"
        )


def test_generation9_schema_keeps_previous_catalog_generation_readable() -> None:
    metadata = _catalog_metadata_module()
    catalog = deepcopy(_catalog())
    catalog["generation"] = 8
    catalog["targets"] = [target for target in catalog["targets"] if target["id"] != TARGET_ID]
    for component in catalog["components"]:
        component["targets"] = [row for row in component["targets"] if row["targetId"] != TARGET_ID]

    validated = metadata._validate_catalog(
        json.dumps(catalog).encode("utf-8"), WORKSPACE_ROOT / "governance"
    )

    assert validated["generation"] == 8


def test_ubuntu22_python_target_is_supported_only_by_the_five_products() -> None:
    catalog = _catalog()
    components = _component_map(catalog)
    targets = {target["id"]: target for target in catalog["targets"]}

    assert catalog["generation"] == 10
    assert targets[TARGET_ID] == {
        "id": TARGET_ID,
        "target": {
            "os": "linux",
            "osVersion": "22.04",
            "distribution": "ubuntu",
            "distributionVersion": "22.04",
            "architecture": "x86_64",
            "abi": "glibc-2.35",
            "runtime": "python:3.12",
        },
        "hostSupport": "supported",
    }
    assert "linux-ubuntu-24.04-x86_64-python-3.12" in targets

    for component_id, component in components.items():
        rows = [row for row in component["targets"] if row["targetId"] == TARGET_ID]
        if component_id in PRODUCT_IDS:
            assert rows == [
                {
                    "targetId": TARGET_ID,
                    "artifactKind": "python-bundle",
                    "support": "supported",
                }
            ]
        else:
            assert rows == []


def test_existing_python_oci_core_targets_and_sdk_locks_are_preserved() -> None:
    catalog = _catalog()
    components = _component_map(catalog)

    for component_id in PRODUCT_IDS:
        component = components[component_id]
        rows = {row["targetId"]: row for row in component["targets"]}
        assert rows["linux-ubuntu-24.04-x86_64-python-3.12"]["support"] == "supported"
        if component_id == "cyrene-navigator":
            assert "oci-image" not in component["artifactKinds"]
        else:
            assert rows["windows-10.0-x86_64-docker-linux"]["artifactKind"] == "oci-image"

        dependencies = {
            item["componentId"]: item.get("versionRange") for item in component["dependencies"]
        }
        assert dependencies["cyrene-runtime-maintenance-sdk"] == "=0.1.0"
        if component_id != "cyrene-navigator":
            assert dependencies["cyrene-runtime-maintenance"] == ">=0.1.0, <0.2.0"

    compatibility_group = next(
        group
        for group in catalog["compatibilityGroups"]
        if group["groupId"] == "workspace-product-v2"
    )
    assert compatibility_group["contractLock"] == {
        "repository": "DoHorizon-AI/Cyrene-Workspace",
        "commit": "c7dea28958a97ccab3a9cc3199faf0ac5819a2a5",
        "path": "governance/workspace-connection-protocols-v2.lock.json",
        "sha256": "sha256:00fd59fb76d7144b6e1b554feadf035328b216231e0a323178836abf2b8bdd5a",
    }

    echo_rows = components["cyrene-echo"]["targets"]
    assert not any(row["targetId"] == TARGET_ID for row in echo_rows)
    assert (
        next(
            row for row in echo_rows if row["targetId"] == "linux-ubuntu-24.04-x86_64-python-3.12"
        )["support"]
        == "unsupported"
    )
    assert (
        next(row for row in echo_rows if row["targetId"] == "windows-10.0-x86_64-docker-linux")[
            "support"
        ]
        == "supported"
    )

    sdk = components["cyrene-runtime-maintenance-sdk"]
    assert sdk["role"] == "build-dependency"
    assert sdk["restart"]["group"] == "none"
    assert "systemdUnit" not in sdk
    assert sdk["targets"] == [
        {
            "targetId": "linux-ubuntu-24.04-x86_64-python-3.12-library",
            "artifactKind": "python-bundle",
            "support": "contract-only",
        }
    ]

    for component_id in (
        "cyrene-linux-sys-adapter",
        "cyrene-nvidia-adapter",
        "cyrene-sandboxd",
        "cyrene-kernel",
        "cy-node-agent",
        "cy-runtime-agent",
    ):
        targets = {row["targetId"] for row in components[component_id]["targets"]}
        assert "linux-ubuntu-22.04-x86_64-systemd" in targets
        assert "linux-ubuntu-24.04-x86_64-systemd" in targets


def test_release_lock_and_bootstrap_copy_match_the_catalog_targets() -> None:
    catalog = _catalog()
    profiles = json.loads((WORKSPACE_ROOT / "release-lock.json").read_text(encoding="utf-8"))[
        "nativePythonProfiles"
    ]
    bootstrap_path = WORKSPACE_ROOT / "packaging" / "component-catalog-bootstrap-v1.json"
    catalog_digest = hashlib.sha256(CATALOG_PATH.read_bytes()).hexdigest()

    assert hashlib.sha256(bootstrap_path.read_bytes()).hexdigest() == catalog_digest
    assert json.loads(bootstrap_path.read_text(encoding="utf-8")) == catalog
    for target_id, profile in profiles.items():
        target = next(item for item in catalog["targets"] if item["id"] == target_id)
        for field in (
            "os",
            "osVersion",
            "distribution",
            "distributionVersion",
            "architecture",
            "abi",
            "runtime",
        ):
            assert profile[field] == target["target"][field]
