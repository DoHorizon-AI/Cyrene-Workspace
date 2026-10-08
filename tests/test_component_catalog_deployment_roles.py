"""C12 workspace deployment-role catalog and schema regressions."""

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
SCHEMA_ROOT = WORKSPACE_ROOT / "governance"


def _catalog() -> dict[str, Any]:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _metadata_module() -> ModuleType:
    module_name = "cyrene_catalog_metadata_roles_test"
    existing_module = sys.modules.get(module_name)
    if isinstance(existing_module, ModuleType):
        return existing_module
    path = WORKSPACE_ROOT / "packaging" / "catalog_metadata.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _validate(catalog: dict[str, Any]) -> dict[str, Any]:
    return _metadata_module()._validate_catalog(json.dumps(catalog).encode("utf-8"), SCHEMA_ROOT)


def _group(catalog: dict[str, Any], group_id: str) -> dict[str, Any]:
    return next(group for group in catalog["compatibilityGroups"] if group["groupId"] == group_id)


def test_c12_workspace_product_deployment_roles_match_the_catalog_members() -> None:
    catalog = _validate(_catalog())
    group = _group(catalog, "workspace-product-v2")
    members = {member["componentId"]: member for member in group["members"]}

    assert catalog["generation"] == 13
    assert group["groupVersion"] == "2"
    assert group["contractApiVersion"] == "0.1.0"
    assert group["wireApiVersion"] == "cyrene.workspace.product.v2"
    assert group["contractLock"] == {
        "repository": "DoHorizon-AI/Cyrene-Workspace",
        "commit": "c7dea28958a97ccab3a9cc3199faf0ac5819a2a5",
        "path": "governance/workspace-connection-protocols-v2.lock.json",
        "sha256": "sha256:00fd59fb76d7144b6e1b554feadf035328b216231e0a323178836abf2b8bdd5a",
    }
    assert {
        component_id: member["requiredForAdoption"] for component_id, member in members.items()
    } == {
        "cy-workspace-relay": True,
        "cy-workspace-connector": True,
        "cy-workspace-web-bff": True,
        "cy-workspace-sidecar": False,
        "cy-workspace-frontend-bridge": True,
        "cy-workspace-authority-host": True,
        "cyrene-product-contract-bundle": False,
    }
    assert group["deploymentRoles"] == [
        {
            "roleId": "control-host",
            "requiredMembers": [
                "cy-workspace-relay",
                "cy-workspace-web-bff",
                "cy-workspace-frontend-bridge",
                "cy-workspace-authority-host",
                "cyrene-product-contract-bundle",
            ],
            "allowedMembers": [
                "cy-workspace-relay",
                "cy-workspace-web-bff",
                "cy-workspace-frontend-bridge",
                "cy-workspace-authority-host",
                "cyrene-product-contract-bundle",
            ],
        },
        {
            "roleId": "connector-host",
            "requiredMembers": ["cy-workspace-connector"],
            "allowedMembers": ["cy-workspace-connector", "cy-workspace-sidecar"],
        },
    ]

    for role in group["deploymentRoles"]:
        required = role["requiredMembers"]
        allowed = role["allowedMembers"]
        assert len(required) == len(set(required))
        assert len(allowed) == len(set(allowed))
        assert set(required) <= set(allowed) <= set(members)

    assert members["cy-workspace-sidecar"]["requiredForAdoption"] is False
    assert "deploymentRoles" not in _group(catalog, "package-runtime-native-v1")


def test_generation11_catalog_without_roles_remains_schema_valid() -> None:
    catalog = _catalog()
    catalog["generation"] = 11
    del _group(catalog, "workspace-product-v2")["deploymentRoles"]

    assert _validate(catalog)["generation"] == 11


def test_c12_catalog_mirrors_and_compiled_digest_pins_match() -> None:
    catalog_bytes = CATALOG_PATH.read_bytes()
    expected_digest = "sha256:" + hashlib.sha256(catalog_bytes).hexdigest()
    mirror_path = WORKSPACE_ROOT / "packaging" / "component-catalog-bootstrap-v1.json"

    assert mirror_path.read_bytes() == catalog_bytes
    for module_name, relative_path, attribute in (
        (
            "cyrene_native_component_bootstrap_catalog_test",
            "packaging/native_component_bootstrap.py",
            "COMPILED_CATALOG_DIGEST",
        ),
        (
            "cyrene_control_initialize_catalog_test",
            "tooling/acceptance/native-components-v2/control_initialize.py",
            "TRUSTED_CATALOG_DIGEST",
        ),
    ):
        path = WORKSPACE_ROOT / relative_path
        spec = importlib.util.spec_from_file_location(module_name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        assert getattr(module, attribute) == expected_digest


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_required_member",
        "duplicate_allowed_member",
        "unknown_required_member",
        "unknown_allowed_member",
        "required_not_allowed",
        "duplicate_role",
        "missing_role",
        "role_on_other_group",
    ],
)
def test_invalid_c12_deployment_roles_are_rejected(mutation: str) -> None:
    catalog = _catalog()
    workspace_group = _group(catalog, "workspace-product-v2")
    roles = workspace_group["deploymentRoles"]

    if mutation == "duplicate_required_member":
        roles[0]["requiredMembers"].append(roles[0]["requiredMembers"][0])
    elif mutation == "duplicate_allowed_member":
        roles[0]["allowedMembers"].append(roles[0]["allowedMembers"][0])
    elif mutation == "unknown_required_member":
        roles[0]["requiredMembers"].append("cy-workspace-unknown")
    elif mutation == "unknown_allowed_member":
        roles[0]["allowedMembers"].append("cy-workspace-unknown")
    elif mutation == "required_not_allowed":
        roles[1]["requiredMembers"] = ["cy-workspace-sidecar"]
        roles[1]["allowedMembers"] = ["cy-workspace-connector"]
    elif mutation == "duplicate_role":
        roles[1]["roleId"] = "control-host"
    elif mutation == "missing_role":
        roles.pop()
    else:
        other_group = _group(catalog, "package-runtime-native-v1")
        other_group["deploymentRoles"] = deepcopy(roles)

    with pytest.raises(_metadata_module().CatalogMetadataError, match="schema violation"):
        _validate(catalog)
