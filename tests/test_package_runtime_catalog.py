"""Native Package Runtime catalog contract regressions. | 原生包运行时目录契约回归。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = WORKSPACE_ROOT / "governance" / "component-catalog-v1.json"
BOOTSTRAP_CATALOG_PATH = WORKSPACE_ROOT / "packaging" / "component-catalog-bootstrap-v1.json"
PACKAGE_RUNTIME_LOCK_PATH = WORKSPACE_ROOT / "governance" / "package-runtime-protocols-v1.lock.json"
PACKAGE_RUNTIME_GROUP_ID = "package-runtime-native-v1"
PACKAGE_RUNTIME_PROTOCOL = "cy-package-runtime.control.v1"
MAINTENANCE_PROTOCOL = "cyrene.runtime-maintenance.broker.v1"
KERNEL_STATE_PROTOCOL = "cyrene.runtime-maintenance.state.v2"
WIRE_PROTOCOL = "cyrene.runtime-maintenance.binding-operations.v1"
CONTRACT_LOCK_COMMIT = "83e9a8e0a6db5ac8fef9fc9472e47f6ee9321bd8"
CONTRACT_LOCK_SHA256 = "fcfb13fe19fa2db8055c318c903f00e4f65a1d2e4c33700e44b08e694612a267"


def _catalog() -> dict[str, Any]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _component_map(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {component["componentId"]: component for component in catalog["components"]}


def _catalog_metadata_module() -> ModuleType:
    path = WORKSPACE_ROOT / "packaging" / "catalog_metadata.py"
    spec = importlib.util.spec_from_file_location("cyrene_package_runtime_catalog_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _expected_group_members() -> list[dict[str, Any]]:
    return [
        {
            "componentId": "cyrene-runtime-maintenance",
            "requiredForAdoption": True,
            "protocolVersion": MAINTENANCE_PROTOCOL,
        },
        {
            "componentId": "cyrene-kernel",
            "requiredForAdoption": True,
            "protocolVersion": KERNEL_STATE_PROTOCOL,
        },
        {
            "componentId": "cy-package-runtime",
            "requiredForAdoption": True,
            "protocolVersion": PACKAGE_RUNTIME_PROTOCOL,
        },
    ]


def test_package_runtime_catalog_validates_and_adds_only_supported_native_targets() -> None:
    catalog = _catalog_metadata_module()._validate_catalog(
        CATALOG_PATH.read_bytes(), WORKSPACE_ROOT / "governance"
    )
    components = _component_map(catalog)
    targets = {target["id"]: target["target"] for target in catalog["targets"]}
    runtime = components["cy-package-runtime"]
    maintenance = components["cyrene-runtime-maintenance"]
    kernel = components["cyrene-kernel"]

    assert catalog["generation"] == 13
    assert runtime["kind"] == "native-binary"
    assert runtime["publisher"] == "DoHorizon-AI/Cyrene-Platform"
    assert runtime["artifactKinds"] == ["native-binary"]
    assert runtime["targets"] == [
        {
            "targetId": "linux-ubuntu-24.04-x86_64-systemd",
            "artifactKind": "native-binary",
            "support": "supported",
        },
        {
            "targetId": "linux-ubuntu-22.04-x86_64-systemd",
            "artifactKind": "native-binary",
            "support": "supported",
        },
    ]
    assert targets["linux-ubuntu-24.04-x86_64-systemd"] == {
        "os": "linux",
        "osVersion": "24.04",
        "distribution": "ubuntu",
        "distributionVersion": "24.04",
        "architecture": "x86_64",
        "abi": "glibc-2.39",
        "runtime": "systemd",
    }
    assert targets["linux-ubuntu-22.04-x86_64-systemd"] == {
        "os": "linux",
        "osVersion": "22.04",
        "distribution": "ubuntu",
        "distributionVersion": "22.04",
        "architecture": "x86_64",
        "abi": "glibc-2.35",
        "runtime": "systemd",
    }
    assert runtime["restart"] == {
        "group": "single-service",
        "unit": "cyrene-package-runtime.service",
    }
    assert runtime["systemdUnit"] == "cyrene-package-runtime.service"
    assert runtime["dependencies"] == [
        {
            "componentId": "cyrene-runtime-maintenance",
            "versionRange": ">=0.1.0, <0.2.0",
        }
    ]
    assert runtime["compatibilityGroup"] == PACKAGE_RUNTIME_GROUP_ID
    assert runtime["protocolVersion"] == PACKAGE_RUNTIME_PROTOCOL
    assert maintenance["compatibilityGroup"] == PACKAGE_RUNTIME_GROUP_ID
    assert maintenance["protocolVersion"] == MAINTENANCE_PROTOCOL
    assert kernel["compatibilityGroup"] == PACKAGE_RUNTIME_GROUP_ID
    assert kernel["protocolVersion"] == KERNEL_STATE_PROTOCOL
    assert kernel["restart"]["group"] == "core-runtime"


def test_workspace_components_have_no_false_kernel_release_dependencies() -> None:
    components = _component_map(_catalog())

    for component_id in (
        "cy-workspace-relay",
        "cy-workspace-connector",
        "cy-workspace-web-bff",
    ):
        assert components[component_id]["dependencies"] == []


def test_package_runtime_group_matches_the_merged_protocol_contract_lock() -> None:
    catalog = _catalog()
    groups = {group["groupId"]: group for group in catalog["compatibilityGroups"]}
    group = groups[PACKAGE_RUNTIME_GROUP_ID]
    lock_bytes = PACKAGE_RUNTIME_LOCK_PATH.read_bytes()
    lock = json.loads(lock_bytes)
    expected_members = _expected_group_members()

    expected_contract_lock = {
        "repository": "DoHorizon-AI/Cyrene-Workspace",
        "commit": CONTRACT_LOCK_COMMIT,
        "path": "governance/package-runtime-protocols-v1.lock.json",
        "sha256": f"sha256:{CONTRACT_LOCK_SHA256}",
    }
    assert group == {
        "groupId": PACKAGE_RUNTIME_GROUP_ID,
        "groupVersion": "2",
        "contractApiVersion": "0.1.0",
        "wireApiVersion": WIRE_PROTOCOL,
        "contractLock": expected_contract_lock,
        "members": expected_members,
    }
    assert hashlib.sha256(lock_bytes).hexdigest() == CONTRACT_LOCK_SHA256
    assert lock["lockId"] == "package-runtime-protocols-v1"
    assert lock["compatibilityGroup"] == {
        "groupId": PACKAGE_RUNTIME_GROUP_ID,
        "groupVersion": "2",
        "contractApiVersion": "0.1.0",
        "wireApiVersion": WIRE_PROTOCOL,
        "members": expected_members,
    }
    assert lock["protocols"]["packageRuntimeControl"]["protocolVersion"] == (
        PACKAGE_RUNTIME_PROTOCOL
    )
    assert lock["protocols"]["maintenanceBroker"]["protocolVersion"] == MAINTENANCE_PROTOCOL
    assert lock["protocols"]["bindingOperations"]["protocolVersion"] == WIRE_PROTOCOL


def test_catalog_bootstrap_copy_and_release_lock_pin_are_exact() -> None:
    catalog_bytes = CATALOG_PATH.read_bytes()
    release_lock = json.loads((WORKSPACE_ROOT / "release-lock.json").read_text(encoding="utf-8"))
    expected_repositories = {
        "Cyrene-Platform": "ba253cff6f865c288e18af28b8cba64d91977261",
        "Cyrene-Plugins-Official": "96733779d187b13efcadcde95a6b6737fe102d25",
        "Cyrene-Workspace": "83e9a8e0a6db5ac8fef9fc9472e47f6ee9321bd8",
        "Cyrene-Reactor": "49b560975c10e815ad5aac42a6c224dd4d39a8e8",
        "Cyrene-Yield": "65b5e77682d59b05e23f3d248765fb4986499a8e",
        "Cyrene-Exchange": "7f1823f6eabe9956ff40cc2e4aa9035197b51e83",
        "Cyrene-Catalyst": "6f999daacf8a2afd76033bfc5d0d7bcf68202de1",
        "Cyrene-Echo": "7480d72c3d2520ccf7684ae43e2f2558c5c6e828",
        "Cyrene-Navigator": "04e84f7b7943bbe0d60604ff65194904b37ad821",
        "Cyrene-Client": "9c51a27540951589157926d1ee717d912ed22fc2",
    }
    expected_connection_protocol_hashes = {
        "cyrene.workspace.authority.v2": (
            "d8d1f7e9abff39877f55521a3a8f3fc56e950aca55472cc12a2b0fcbcd889d0b"
        ),
        "cyrene.workspace.tunnel.v1": (
            "65e7cee26af09ab3bfa81b90a74fa6cb719bc520ca89d7c36fda224f3afd15dc"
        ),
        "cyrene.workspace.local.v2": (
            "c8da97793c6aee44816202102c269b338701c7171b2298fc875859776c8c2036"
        ),
        "cyrene.workspace.product.v2": (
            "2a5adf164ecf26d02e728ac16d7bde6f227c286b0d1e51342472df29d0785441"
        ),
    }
    protocol_lock = json.loads(
        (WORKSPACE_ROOT / "governance" / "workspace-connection-protocols-v2.lock.json").read_text(
            encoding="utf-8"
        )
    )
    protocol_hashes = {
        protocol["apiVersion"]: protocol["definition"]["sha256"]
        for protocol in protocol_lock["protocols"]
    }

    assert BOOTSTRAP_CATALOG_PATH.read_bytes() == catalog_bytes
    assert release_lock["repositories"] == expected_repositories
    assert protocol_hashes == expected_connection_protocol_hashes
