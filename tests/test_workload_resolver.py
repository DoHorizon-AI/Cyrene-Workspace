"""Contract tests for deterministic workload closure and trust binding."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
CATALOG_DIGEST = "sha256:" + "a" * 64
REPOSITORY = "Example-Corp/Resolver-Releases"
WORKFLOW = "Example-Corp/Resolver-Releases/.github/workflows/publish.yml"
SOURCE_COMMIT = "b" * 40
HOST = {
    "os": "linux",
    "osVersion": "24.04",
    "distribution": "ubuntu",
    "distributionVersion": "24.04",
    "architecture": "x86_64",
    "abi": "glibc-2.39",
}
TARGETS = {
    "linux-u24-python": {**HOST, "runtime": "python:3.12"},
    "linux-u24-systemd": {**HOST, "runtime": "systemd"},
    "linux-u24-data": {**HOST, "runtime": "data-v1"},
    "linux-u24-plugin": {**HOST, "runtime": "python:3.12"},
}

_RESOLVER_PATH = ROOT / "packaging" / "workload_resolver.py"
_RESOLVER_SPEC = importlib.util.spec_from_file_location(
    "cyrene_workload_resolver_test", _RESOLVER_PATH
)
assert _RESOLVER_SPEC is not None and _RESOLVER_SPEC.loader is not None
_RESOLVER_MODULE = importlib.util.module_from_spec(_RESOLVER_SPEC)
sys.modules[_RESOLVER_SPEC.name] = _RESOLVER_MODULE
_RESOLVER_SPEC.loader.exec_module(_RESOLVER_MODULE)
potential_component_ids = _RESOLVER_MODULE.potential_component_ids
resolve_workload = _RESOLVER_MODULE.resolve_workload


def _sha(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _component(
    component_id: str,
    kind: str,
    target_id: str,
    artifact_kind: str,
    *,
    dependencies: list[dict[str, str]] | None = None,
    plugin: dict[str, str] | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "componentId": component_id,
        "kind": kind,
        "publisher": REPOSITORY,
        "publisherId": "resolver-publisher",
        "artifactKinds": [artifact_kind],
        "targets": [{"targetId": target_id, "artifactKind": artifact_kind, "support": "supported"}],
        "restart": {"group": "none"},
        "dependencies": dependencies or [],
        "protocolVersion": "cyrene.test.v1",
    }
    if plugin is not None:
        row["pluginPackage"] = plugin
    if role:
        row["role"] = role
    return row


def _catalog() -> dict[str, Any]:
    components = [
        _component(
            "app",
            "python-bundle",
            "linux-u24-python",
            "python-bundle",
            dependencies=[
                {"componentId": "runtime", "versionRange": ">=1.0.0, <2.0.0"},
                {"componentId": "builder", "versionRange": "=1.0.0"},
            ],
        ),
        _component(
            "runtime",
            "native-binary",
            "linux-u24-systemd",
            "native-binary",
            dependencies=[{"componentId": "shared", "versionRange": "=1.2.0"}],
        ),
        _component("shared", "data-bundle", "linux-u24-data", "data-bundle"),
        _component(
            "builder",
            "python-bundle",
            "linux-u24-python",
            "python-bundle",
            role="build-dependency",
        ),
        _component(
            "plugin-a",
            "plugin-package",
            "linux-u24-plugin",
            "plugin-package",
            plugin={
                "packageId": "cyrene.tools.sample-a",
                "capabilityId": "tools.sample-a.v1",
                "interfaceVersion": "1.0.0",
            },
        ),
        _component(
            "plugin-b",
            "plugin-package",
            "linux-u24-plugin",
            "plugin-package",
            plugin={
                "packageId": "cyrene.tools.sample-b",
                "capabilityId": "tools.sample-b.v1",
                "interfaceVersion": "1.0.0",
            },
        ),
        _component("backend-a", "data-bundle", "linux-u24-data", "data-bundle"),
        _component("backend-b", "data-bundle", "linux-u24-data", "data-bundle"),
    ]
    return {
        "schemaVersion": 2,
        "generation": 1,
        "defaultChannel": "stable",
        "targets": [
            {"id": "linux-u24-host", "target": HOST, "hostSupport": "supported"},
            *[
                {"id": target_id, "target": target, "hostSupport": "supported"}
                for target_id, target in TARGETS.items()
            ],
        ],
        "publishers": [
            {
                "id": "resolver-publisher",
                "repository": REPOSITORY,
                "workflow": WORKFLOW,
                "tagFormat": "component-version-source-sha",
                "releaseDiscovery": {
                    "apiUri": "https://api.github.com/repos/Example-Corp/Resolver-Releases/releases",
                    "indexAssetName": "component-release-index-v1.json",
                },
            }
        ],
        "components": components,
        "workloads": [
            {
                "workloadId": "sample",
                "hostTargets": ["linux-u24-host"],
                "requiredComponents": ["app"],
                "recommendedComponents": ["plugin-a"],
                "optionalComponents": ["plugin-b"],
                "choiceGroups": [],
                "conflicts": [
                    {
                        "componentIds": ["plugin-a", "plugin-b"],
                        "reason": "Only one plugin may be active.",
                    }
                ],
                "targetPreferences": [
                    {"componentId": "app", "targetId": "linux-u24-python"},
                    {"componentId": "plugin-a", "targetId": "linux-u24-plugin"},
                    {"componentId": "plugin-b", "targetId": "linux-u24-plugin"},
                ],
                "sourcePolicy": {
                    "mode": "actualProduct",
                    "productComponentIds": ["app"],
                    "productSources": [{"componentId": "app", "sourceId": "app"}],
                    "operations": [
                        "activate",
                        "recover_binding",
                        "deactivate",
                        "runtime_status",
                        "get_installation",
                    ],
                },
                "bindings": [
                    {"componentId": "plugin-a", "bindingId": "resolver-binding-app-plugin-a"},
                    {"componentId": "plugin-b", "bindingId": "resolver-binding-app-plugin-b"},
                ],
            }
        ],
    }


def _release_envelopes(
    catalog: dict[str, Any],
    component_ids: set[str],
    *,
    versions: dict[str, str] | None = None,
    missing_indexes: set[str] | None = None,
) -> dict[str, Any]:
    versions = versions or {}
    missing_indexes = missing_indexes or set()
    envelopes = []
    for component in catalog["components"]:
        component_id = component["componentId"]
        if component_id not in component_ids or component_id in missing_indexes:
            continue
        target_row = component["targets"][0]
        target = next(
            row["target"] for row in catalog["targets"] if row["id"] == target_row["targetId"]
        )
        version = versions.get(component_id, "1.2.0" if component_id == "shared" else "1.0.0")
        release_id = f"stable-{component_id}-{version}-{SOURCE_COMMIT}"
        manifest_digest = _sha(f"manifest:{component_id}:{version}")
        manifest_asset_digest = _sha(f"manifest-asset:{component_id}:{version}")
        payload_digest = _sha(f"payload:{component_id}:{version}")
        dependencies = copy.deepcopy(component["dependencies"])
        artifact: dict[str, Any] = {"kind": target_row["artifactKind"], "sha256": payload_digest}
        if artifact["kind"] == "plugin-package":
            package = component["pluginPackage"]
            artifact = {
                "kind": "plugin-package",
                "packageId": package["packageId"],
                "capabilityId": package["capabilityId"],
                "interfaceVersion": package["interfaceVersion"],
                "archive": {"sha256": payload_digest},
            }
        manifest = {
            "schemaVersion": 2,
            "releaseId": release_id,
            "componentId": component_id,
            "version": version,
            "channel": "stable",
            "target": target,
            "artifact": artifact,
            "dependencies": dependencies,
            "restart": copy.deepcopy(component["restart"]),
            "source": {
                "repository": f"https://github.com/{REPOSITORY}",
                "commit": SOURCE_COMMIT,
                "ref": f"refs/tags/{release_id}",
            },
            "provenance": {},
            "manifestDigest": manifest_digest,
            "protocolVersion": component["protocolVersion"],
            "contentDigest": payload_digest,
        }
        asset_digest = _sha(f"index-bytes:{component_id}:{version}")
        index_digest = _sha(f"index-content:{component_id}:{version}")
        index = {
            "schemaVersion": 1,
            "repository": REPOSITORY,
            "channel": "stable",
            "source": {
                "repository": f"https://github.com/{REPOSITORY}",
                "ref": "refs/heads/main",
                "commit": SOURCE_COMMIT,
            },
            "releases": [
                {
                    "componentId": component_id,
                    "version": version,
                    "target": target,
                    "manifestUri": f"https://github.com/{REPOSITORY}/releases/download/{release_id}/component-release-manifest-v2.json",
                    "manifestDigest": manifest_digest,
                }
            ],
            "indexDigest": index_digest,
        }
        index_attestation = {
            "repository": REPOSITORY,
            "workflow": WORKFLOW,
            "sourceCommit": SOURCE_COMMIT,
            "sourceRef": "refs/heads/main",
            "subjectName": "component-release-index-v1.json",
            "subjectDigest": asset_digest,
        }
        manifest_attestation = {
            "repository": REPOSITORY,
            "workflow": WORKFLOW,
            "sourceCommit": SOURCE_COMMIT,
            "sourceRef": f"refs/tags/{release_id}",
            "subjectName": "component-release-manifest-v2.json",
            "subjectDigest": manifest_asset_digest,
        }
        envelopes.append(
            {
                "repository": REPOSITORY,
                "assetName": "component-release-index-v1.json",
                "assetUri": f"https://github.com/{REPOSITORY}/releases/download/{release_id}/component-release-index-v1.json",
                "assetDigest": asset_digest,
                "indexDigest": index_digest,
                "channel": "stable",
                "releaseTag": release_id,
                "source": {
                    "repository": f"https://github.com/{REPOSITORY}",
                    "ref": "refs/heads/main",
                    "commit": SOURCE_COMMIT,
                },
                "attestationRef": index_attestation,
                "index": index,
                "manifests": [
                    {
                        "componentId": component_id,
                        "version": version,
                        "target": target,
                        "manifestUri": index["releases"][0]["manifestUri"],
                        "manifestDigest": manifest_digest,
                        "manifestAssetDigest": manifest_asset_digest,
                        "artifactDigest": payload_digest,
                        "manifest": manifest,
                        "releaseTag": release_id,
                        "attestationRef": manifest_attestation,
                    }
                ],
            }
        )
    return {"indexes": envelopes}


def _resolve(catalog: dict[str, Any], indexes: dict[str, Any], **kwargs: Any):
    return resolve_workload(
        catalog,
        CATALOG_DIGEST,
        kwargs.pop("workload_id", "sample"),
        kwargs.pop("target_id", "linux-u24-host"),
        kwargs.pop("selections", {}),
        kwargs.pop("installed_components", {}),
        indexes,
        kwargs.pop("action", "install"),
    ).to_dict()


def _installed_identity(component_id: str, target_id: str) -> dict[str, Any]:
    """Return the local receipt identity required for a one-component uninstall."""

    return {
        "installed": True,
        "version": "1.0.0",
        "releaseId": f"stable-{component_id}-1.0.0-{SOURCE_COMMIT}",
        "manifestDigest": _sha(f"canonical-manifest:{component_id}"),
        "manifestAssetDigest": _sha(f"raw-manifest:{component_id}"),
        "digest": _sha(f"artifact:{component_id}"),
        "installationId": f"installation-{component_id}-001",
        "targetId": target_id,
    }


def test_resolves_transitive_closure_and_ignores_build_dependencies() -> None:
    catalog = _catalog()
    result = _resolve(
        catalog, _release_envelopes(catalog, {"app", "runtime", "shared", "plugin-a"})
    )

    assert result["status"] == "ready"
    assert [row["componentId"] for row in result["selectedComponents"]] == [
        "app",
        "plugin-a",
        "runtime",
        "shared",
    ]
    assert "builder" not in {row["componentId"] for row in result["selectedComponents"]}
    assert any(
        row["componentId"] == "shared" and row["fromComponentId"] == "runtime"
        for row in result["closureReasons"]
    )
    assert all(
        set(row)
        == {
            "componentId",
            "reasonCode",
            "fromComponentId",
            "rootComponentId",
            "versionRange",
            "depth",
        }
        for row in result["closureReasons"]
    )
    plugin = next(row for row in result["selectedComponents"] if row["componentId"] == "plugin-a")
    assert plugin["bindingId"] == "resolver-binding-app-plugin-a"
    assert plugin["sourcePolicy"]["productComponentIds"] == ["app"]
    assert plugin["sourcePolicy"]["productSources"] == [{"componentId": "app", "sourceId": "app"}]


def test_rejects_explicit_unselect_of_required_component() -> None:
    catalog = _catalog()
    result = _resolve(
        catalog,
        _release_envelopes(catalog, {"app", "runtime", "shared", "plugin-a"}),
        selections={"excludeComponentIds": ["app"]},
    )
    assert result["status"] == "blocked"
    assert any(item["code"] == "REQUIRED_COMPONENT_UNSELECTABLE" for item in result["blockers"])


def test_choice_cannot_override_explicit_component_exclusion() -> None:
    catalog = _catalog()
    workload = catalog["workloads"][0]
    workload["choiceGroups"] = [{"choiceId": "backend", "componentIds": ["backend-a", "backend-b"]}]
    workload["targetPreferences"].extend(
        [
            {"componentId": "backend-a", "targetId": "linux-u24-data"},
            {"componentId": "backend-b", "targetId": "linux-u24-data"},
        ]
    )
    indexes = _release_envelopes(catalog, {"app", "runtime", "shared", "plugin-a", "backend-a"})
    result = _resolve(
        catalog,
        indexes,
        selections={
            "choices": {"backend": "backend-a"},
            "excludeComponentIds": ["backend-a"],
        },
    )

    assert result["status"] == "blocked"
    assert "backend-a" not in {row["componentId"] for row in result["selectedComponents"]}
    assert any(
        row["code"] == "SELECTION_CONFLICT"
        and row["componentId"] == "backend-a"
        and row["details"]["choiceId"] == "backend"
        for row in result["blockers"]
    )


def test_rejects_binding_ids_outside_platform_scope_identifier_subset() -> None:
    catalog = _catalog()
    catalog["workloads"][0]["bindings"][0]["bindingId"] = "resolver-binding/app/plugin-a"
    result = _resolve(
        catalog,
        _release_envelopes(catalog, {"app", "runtime", "shared", "plugin-a"}),
    )

    assert result["status"] == "blocked"
    assert any(
        row["code"] == "SOURCE_BINDING_INVALID" and row["componentId"] == "plugin-a"
        for row in result["blockers"]
    )
    plugin = next(row for row in result["selectedComponents"] if row["componentId"] == "plugin-a")
    assert plugin["bindingId"] is None


def test_missing_plugin_capability_is_structured_and_names_exact_capability() -> None:
    catalog = _catalog()
    result = _resolve(catalog, _release_envelopes(catalog, {"app", "runtime", "shared"}))
    blocker = next(item for item in result["blockers"] if item["code"] == "MISSING_CAPABILITY")
    assert blocker["componentId"] == "plugin-a"
    assert blocker["capabilityId"] == "tools.sample-a.v1"


def test_reports_cycle_and_version_conflict() -> None:
    catalog = _catalog()
    runtime = next(row for row in catalog["components"] if row["componentId"] == "runtime")
    runtime["dependencies"] = [
        {"componentId": "app", "versionRange": "=1.0.0"},
        {"componentId": "shared", "versionRange": "=1.2.0"},
    ]
    indexes = _release_envelopes(catalog, {"app", "runtime", "shared", "plugin-a"})
    result = _resolve(catalog, indexes)
    assert any(item["code"] == "DEPENDENCY_CYCLE" for item in result["blockers"])

    catalog = _catalog()
    runtime = next(row for row in catalog["components"] if row["componentId"] == "runtime")
    runtime["dependencies"] = [{"componentId": "shared", "versionRange": "=1.3.0"}]
    result = _resolve(
        catalog, _release_envelopes(catalog, {"app", "runtime", "shared", "plugin-a"})
    )
    assert any(
        item["code"] == "VERSION_CONFLICT" and item["componentId"] == "shared"
        for item in result["blockers"]
    )


def test_target_support_and_installed_recognition() -> None:
    catalog = _catalog()
    app = next(row for row in catalog["components"] if row["componentId"] == "app")
    app["targets"][0]["support"] = "unsupported"
    blocked = _resolve(
        catalog, _release_envelopes(catalog, {"app", "runtime", "shared", "plugin-a"})
    )
    assert any(
        item["code"] == "UNSUPPORTED_TARGET" and item["componentId"] == "app"
        for item in blocked["blockers"]
    )

    catalog = _catalog()
    indexes = _release_envelopes(catalog, {"app", "runtime", "shared", "plugin-a"})
    app_candidate = next(
        item
        for envelope in indexes["indexes"]
        for item in envelope["manifests"]
        if item["componentId"] == "app"
    )
    installed = {
        "app": {"version": "1.0.0", "digest": app_candidate["artifactDigest"], "installed": True}
    }
    result = _resolve(catalog, indexes, installed_components=installed)
    app_row = next(row for row in result["selectedComponents"] if row["componentId"] == "app")
    assert app_row["installed"] is True


def test_optional_and_choice_selection_and_conflict() -> None:
    catalog = _catalog()
    workload = catalog["workloads"][0]
    workload["choiceGroups"] = [{"choiceId": "backend", "componentIds": ["backend-a", "backend-b"]}]
    workload["targetPreferences"].extend(
        [
            {"componentId": "backend-a", "targetId": "linux-u24-data"},
            {"componentId": "backend-b", "targetId": "linux-u24-data"},
        ]
    )
    ids = {"app", "runtime", "shared", "plugin-a", "plugin-b", "backend-a"}
    result = _resolve(
        catalog,
        _release_envelopes(catalog, ids),
        selections={"includeComponentIds": ["plugin-b"], "choices": {"backend": "backend-a"}},
    )
    assert (
        result["status"] == "blocked"
    )  # plugin-a is required indirectly only if selected; choice closure is otherwise ready.
    codes = {item["code"] for item in result["blockers"]}
    assert "COMPONENT_CONFLICT" in codes

    missing_choice = _resolve(
        catalog, _release_envelopes(catalog, ids), selections={"excludeComponentIds": ["plugin-a"]}
    )
    assert any(item["code"] == "CHOICE_REQUIRED" for item in missing_choice["blockers"])


def test_digest_is_reproducible_and_binds_selection_and_index_identity() -> None:
    catalog = _catalog()
    indexes = _release_envelopes(catalog, {"app", "runtime", "shared", "plugin-a", "plugin-b"})
    first = _resolve(catalog, indexes, selections={"includeComponentIds": ["plugin-b"]})
    shuffled = _resolve(
        catalog,
        {"indexes": list(reversed(indexes["indexes"]))},
        selections={"includeComponentIds": ["plugin-b"]},
    )
    changed = _resolve(catalog, indexes, selections={"excludeComponentIds": ["plugin-a"]})
    assert first["planDigest"] == shuffled["planDigest"]
    assert first["planId"] == "plan-" + first["planDigest"].removeprefix("sha256:")[:32]
    assert first["planDigest"] != changed["planDigest"]
    assert first["action"] == "install"
    assert first["planDigestMaterial"]["action"] == "install"
    assert (
        first["planDigest"]
        == "sha256:"
        + hashlib.sha256(
            json.dumps(
                first["planDigestMaterial"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )


def test_manifest_attestation_binds_raw_asset_digest_separately_from_jcs_digest() -> None:
    catalog = _catalog()
    indexes = _release_envelopes(catalog, {"app", "runtime", "shared", "plugin-a"})
    selected = next(
        row
        for envelope in indexes["indexes"]
        for row in envelope["manifests"]
        if row["componentId"] == "app"
    )
    assert selected["manifestAssetDigest"] != selected["manifestDigest"]
    assert selected["attestationRef"]["subjectDigest"] == selected["manifestAssetDigest"]

    original = _resolve(catalog, indexes)
    changed_indexes = copy.deepcopy(indexes)
    changed = next(
        row
        for envelope in changed_indexes["indexes"]
        for row in envelope["manifests"]
        if row["componentId"] == "app"
    )
    changed["manifestAssetDigest"] = _sha("different-raw-manifest")
    changed["attestationRef"]["subjectDigest"] = changed["manifestAssetDigest"]
    changed_result = _resolve(catalog, changed_indexes)
    assert changed_result["status"] == "ready"
    assert changed_result["planDigest"] != original["planDigest"]

    broken_indexes = copy.deepcopy(indexes)
    broken = next(
        row
        for envelope in broken_indexes["indexes"]
        for row in envelope["manifests"]
        if row["componentId"] == "app"
    )
    broken["attestationRef"]["subjectDigest"] = broken["manifestDigest"]
    broken_result = _resolve(catalog, broken_indexes)
    assert any(row["code"] == "TRUSTED_INDEX_BINDING_INVALID" for row in broken_result["blockers"])


def test_explicit_publisher_identity_prevents_same_repository_workflow_ambiguity() -> None:
    catalog = _catalog()
    catalog["publishers"].append(
        {
            "id": "other-publisher",
            "repository": REPOSITORY,
            "workflow": "Example-Corp/Resolver-Releases/.github/workflows/other.yml",
            "tagFormat": "component-version-source-sha",
            "releaseDiscovery": {
                "apiUri": "https://api.github.com/repos/Example-Corp/Resolver-Releases/releases",
                "indexAssetName": "component-release-index-v1.json",
            },
        }
    )
    for component in catalog["components"]:
        component["publisherId"] = "other-publisher"
    indexes = _release_envelopes(catalog, {"app", "runtime", "shared", "plugin-a"})
    result = _resolve(catalog, indexes)
    assert result["status"] == "blocked"
    wrong_workflow = "Example-Corp/Resolver-Releases/.github/workflows/other.yml"
    for envelope in indexes["indexes"]:
        envelope["attestationRef"]["workflow"] = wrong_workflow
        for manifest in envelope["manifests"]:
            manifest["attestationRef"]["workflow"] = wrong_workflow
    result = _resolve(catalog, indexes)
    assert result["status"] == "ready"
    assert all(
        row["indexIdentity"]["repository"] == REPOSITORY for row in result["selectedComponents"]
    )
    assert all(
        row["publisherIdentity"]["id"] == "other-publisher"
        and row["publisherIdentity"]["workflow"] == wrong_workflow
        and row["indexIdentity"]["publisherIdentity"] == row["publisherIdentity"]
        for row in result["selectedComponents"]
    )


def test_component_version_source_sha_tag_format_is_exact() -> None:
    catalog = _catalog()
    indexes = _release_envelopes(catalog, {"app", "runtime", "shared", "plugin-a"})
    result = _resolve(catalog, indexes)
    assert result["status"] == "ready"

    broken_indexes = copy.deepcopy(indexes)
    envelope = broken_indexes["indexes"][0]
    bad_tag = f"stable-{envelope['index']['releases'][0]['componentId']}-{SOURCE_COMMIT}"
    envelope["releaseTag"] = bad_tag
    for release in envelope["index"]["releases"]:
        release["manifestUri"] = release["manifestUri"].replace(
            envelope["index"]["releases"][0]["manifestUri"].rsplit("/", 2)[-2], bad_tag
        )
    for wrapped in envelope["manifests"]:
        wrapped["releaseTag"] = bad_tag
        wrapped["manifest"]["releaseId"] = bad_tag
        wrapped["manifestUri"] = wrapped["manifestUri"].replace(
            wrapped["manifestUri"].rsplit("/", 2)[-2], bad_tag
        )
    blocked = _resolve(catalog, broken_indexes)
    assert blocked["status"] == "blocked"
    assert any(row["code"] == "TRUSTED_INDEX_BINDING_INVALID" for row in blocked["blockers"])


def test_v1_catalog_uses_legacy_single_component_workload_without_schema_changes() -> None:
    catalog = _catalog()
    catalog["schemaVersion"] = 1
    catalog.pop("workloads")
    catalog["targets"] = [
        {"id": target_id, "target": target, "hostSupport": "supported"}
        for target_id, target in TARGETS.items()
    ]
    for component in catalog["components"]:
        component.pop("publisherId", None)
        component.pop("pluginPackage", None)
    catalog["publishers"][0].pop("id", None)
    indexes = _release_envelopes(catalog, {"app", "runtime", "shared"})
    result = _resolve(catalog, indexes, workload_id="app", target_id="linux-u24-python")
    assert result["status"] == "ready"
    assert [row["componentId"] for row in result["selectedComponents"]] == [
        "app",
        "runtime",
        "shared",
    ]
    assert potential_component_ids(catalog, "app") == ("app", "runtime", "shared")


def test_potential_component_ids_excludes_build_only_dependencies() -> None:
    catalog = _catalog()
    assert potential_component_ids(catalog, "sample") == (
        "app",
        "plugin-a",
        "plugin-b",
        "runtime",
        "shared",
    )


def test_catalog_asset_discovery_accepts_exact_v1_or_v2_pair_only() -> None:
    metadata = _catalog_metadata_module()

    def asset(asset_id: int, name: str, tag: str) -> dict[str, Any]:
        return {
            "id": asset_id,
            "name": name,
            "size": 10,
            "state": "uploaded",
            "url": f"https://api.github.com/repos/{metadata.REPOSITORY}/releases/assets/{asset_id}",
            "browser_download_url": metadata._expected_browser_url(tag, name),
        }

    for version in (1, 2):
        tag = ("catalog-v2-preview-" if version == 2 else "catalog-preview-") + "c" * 40
        catalog_asset, proof_asset = metadata.CATALOG_ASSETS[version]
        found, selected_catalog, selected_proof = metadata._find_assets(
            {"assets": [asset(1, catalog_asset, tag), asset(2, proof_asset, tag)]}, tag
        )
        assert set(found) == {catalog_asset, proof_asset}
        assert selected_catalog == catalog_asset
        assert selected_proof == proof_asset
    with pytest.raises(metadata.CatalogMetadataError):
        tag = "catalog-v2-preview-" + "c" * 40
        metadata._find_assets(
            {
                "assets": [
                    asset(1, metadata.CATALOG_ASSETS[1][0], tag),
                    asset(2, metadata.CATALOG_ASSETS[2][1], tag),
                ]
            },
            tag,
        )


def _catalog_metadata_module():
    path = ROOT / "packaging" / "catalog_metadata.py"
    spec = importlib.util.spec_from_file_location("cyrene_catalog_metadata_resolver_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_existing_v1_and_new_v2_catalogs_validate_without_mutating_v1_schema() -> None:
    metadata = _catalog_metadata_module()
    governance = ROOT / "governance"
    v1 = metadata._validate_catalog(
        (governance / "component-catalog-v1.json").read_bytes(), governance
    )
    v2 = metadata._validate_catalog(
        (governance / "component-catalog-v2.json").read_bytes(), governance
    )
    assert v1["schemaVersion"] == 1
    assert v2["schemaVersion"] == 2
    assert v2["generation"] == 15
    invalid_binding = copy.deepcopy(v2)
    invalid_binding["workloads"][0]["bindings"][0]["bindingId"] = "invalid/scope-id"
    with pytest.raises(metadata.CatalogMetadataError):
        metadata._validate_catalog(json.dumps(invalid_binding).encode("utf-8"), governance)
    target_id = "linux-ubuntu-24.04-x86_64-oci"
    assert any(row["id"] == target_id for row in v1["targets"])
    legacy_oci_target = next(row for row in v1["targets"] if row["id"] == target_id)
    assert legacy_oci_target["target"] == {
        "os": "linux",
        "osVersion": "24.04",
        "distribution": "ubuntu",
        "distributionVersion": "24.04",
        "architecture": "x86_64",
        "runtime": "oci",
    }
    echo = next(row for row in v2["components"] if row["componentId"] == "cyrene-echo")
    assert any(
        row["targetId"] == target_id
        and row["artifactKind"] == "oci-image"
        and row["support"] == "supported"
        for row in echo["targets"]
    )
    echo_workload = next(row for row in v2["workloads"] if row["workloadId"] == "echo")
    assert "cyrene-evaluation-exact-match" in echo_workload["recommendedComponents"]
    assert "cyrene-evaluation-exact-match" not in echo_workload["optionalComponents"]
    assert echo_workload["sourcePolicy"]["productSources"] == [
        {"componentId": "cyrene-echo", "sourceId": "cyrene-echo"}
    ]
    plugin_components = [
        row
        for row in v2["components"]
        if row["componentId"].startswith("cyrene-tools-")
        or row["componentId"] == "cyrene-evaluation-exact-match"
    ]
    assert len(plugin_components) == 5
    assert all(row["protocolVersion"] == "cyrene.plugin.runtime.v1" for row in plugin_components)
    client = next(
        row for row in v2["components"] if row["componentId"] == "cyrene-client-workspace-web"
    )
    assert client["protocolVersion"] == "cyrene.static-web.v1"
    assert (
        next(
            row for row in echo_workload["targetPreferences"] if row["componentId"] == "cyrene-echo"
        )["targetId"]
        == target_id
    )
    sdk = next(
        row for row in v2["components"] if row["componentId"] == "cyrene-runtime-maintenance-sdk"
    )
    assert sdk["role"] == "build-dependency"
    assert sdk["targets"] == [
        {
            "targetId": "linux-ubuntu-24.04-x86_64-python-3.12-library",
            "artifactKind": "python-bundle",
            "support": "supported",
        }
    ]
    assert all(
        "cyrene-runtime-maintenance-sdk" in row["requiredComponents"] for row in v2["workloads"]
    )
    plugins = next(row for row in v2["workloads"] if row["workloadId"] == "plugins")
    assert plugins["minimumExplicitOptionalSelections"] == 1


def test_catalyst_requires_workspace_control_without_changing_echo() -> None:
    metadata = _catalog_metadata_module()
    governance = ROOT / "governance"
    catalog = metadata._validate_catalog(
        (governance / "component-catalog-v2.json").read_bytes(), governance
    )
    target_id = "linux-ubuntu-24.04-x86_64-node-24"
    expected_target = {
        "os": "linux",
        "osVersion": "24.04",
        "distribution": "ubuntu",
        "distributionVersion": "24.04",
        "architecture": "x86_64",
        "abi": "glibc-2.39",
        "runtime": "node:24",
    }
    target = next(row for row in catalog["targets"] if row["id"] == target_id)
    assert target["target"] == expected_target
    control = next(
        row
        for row in catalog["components"]
        if row["componentId"] == "cyrene-client-workspace-control"
    )
    publisher = next(
        row for row in catalog["publishers"] if row.get("id") == "official-client-workspace-control"
    )
    assert publisher["repository"] == "DoHorizon-AI/Cyrene-Client"
    assert (
        publisher["workflow"]
        == "DoHorizon-AI/Cyrene-Client/.github/workflows/workspace-control-release.yml"
    )
    assert publisher["tagFormat"] == "component-source-sha"
    assert _RESOLVER_MODULE._expected_release_tag(
        control, publisher, "preview", f"0.0.1+sha.{SOURCE_COMMIT}", SOURCE_COMMIT
    ) == ("component-source-sha", f"preview-cyrene-client-workspace-control-{SOURCE_COMMIT}")
    assert control["kind"] == "native-binary"
    assert control["role"] == "service"
    assert control["publisherId"] == "official-client-workspace-control"
    assert control["protocolVersion"] == "cyrene.client.studio-control.v1"
    assert control["systemdUnit"] == "cyrene-client-workspace-control.service"
    assert control["restart"] == {
        "group": "single-service",
        "unit": "cyrene-client-workspace-control.service",
    }
    assert control["dependencies"] == []
    assert control["targets"] == [
        {"targetId": target_id, "artifactKind": "native-binary", "support": "supported"}
    ]

    catalyst = next(row for row in catalog["workloads"] if row["workloadId"] == "catalyst")
    assert "cyrene-client-workspace-control" in catalyst["requiredComponents"]
    assert {
        "componentId": "cyrene-client-workspace-control",
        "targetId": target_id,
    } in catalyst["targetPreferences"]
    selected_target, target_error = _RESOLVER_MODULE._component_target(
        catalog, control, catalyst, "linux-ubuntu-24.04-x86_64"
    )
    assert target_error is None
    assert selected_target is not None and selected_target["targetId"] == target_id
    catalyst_fetches = potential_component_ids(catalog, "catalyst")
    assert "cyrene-client-workspace-control" in catalyst_fetches
    assert "cyrene-echo" not in catalyst_fetches
    echo = next(row for row in catalog["workloads"] if row["workloadId"] == "echo")
    assert "cyrene-client-workspace-control" not in echo["requiredComponents"]
    assert "cyrene-client-workspace-web" not in echo["requiredComponents"]
    assert "cyrene-client-workspace-control" not in potential_component_ids(catalog, "echo")
    web = next(
        row for row in catalog["components"] if row["componentId"] == "cyrene-client-workspace-web"
    )
    assert web["dependencies"] == []


def test_semver_build_metadata_accepts_only_official_source_sha_suffix() -> None:
    version = f"0.0.1+sha.{SOURCE_COMMIT}"
    assert _RESOLVER_MODULE._parse_semver(version) == (0, 0, 1)
    assert _RESOLVER_MODULE._range_matches(version, ">=0.0.1, <0.0.2")
    invalid_version = "0.0.1+sha.not-a-source-sha"
    assert _RESOLVER_MODULE._parse_semver(invalid_version) is None
    assert not _RESOLVER_MODULE._range_matches(invalid_version, "*")

    catalog = _catalog()
    valid = _resolve(
        catalog,
        _release_envelopes(
            catalog,
            {"app", "runtime", "shared", "plugin-a", "plugin-b"},
            versions={"app": version},
        ),
    )
    assert valid["status"] == "ready"
    selected_app = next(row for row in valid["selectedComponents"] if row["componentId"] == "app")
    assert selected_app["version"] == version

    invalid = _resolve(
        catalog,
        _release_envelopes(
            catalog,
            {"app", "runtime", "shared", "plugin-a", "plugin-b"},
            versions={"app": invalid_version},
        ),
    )
    assert invalid["status"] == "blocked"
    assert any(row["code"] == "VERSION_CONFLICT" for row in invalid["blockers"])


def test_plugins_workload_requires_one_explicit_package_in_addition_to_sdk() -> None:
    metadata = _catalog_metadata_module()
    catalog = metadata._validate_catalog(
        (ROOT / "governance" / "component-catalog-v2.json").read_bytes(),
        ROOT / "governance",
    )
    no_plugin = resolve_workload(
        catalog,
        _sha("catalog-v2"),
        "plugins",
        "linux-ubuntu-24.04-x86_64",
        {},
        {},
        {"indexes": []},
    ).to_dict()
    assert any(row["code"] == "MINIMUM_SELECTION_NOT_MET" for row in no_plugin["blockers"])

    selected_plugin = resolve_workload(
        catalog,
        _sha("catalog-v2"),
        "plugins",
        "linux-ubuntu-24.04-x86_64",
        {"includeComponentIds": ["cyrene-tools-dataset-preparation"]},
        {},
        {"indexes": []},
    ).to_dict()
    assert not any(
        row["code"] == "MINIMUM_SELECTION_NOT_MET" for row in selected_plugin["blockers"]
    )


def test_catalog_v2_rejects_publisher_repository_mismatch() -> None:
    metadata = _catalog_metadata_module()
    catalog = json.loads(
        (ROOT / "governance" / "component-catalog-v2.json").read_text(encoding="utf-8")
    )
    component = next(
        row for row in catalog["components"] if row["componentId"] == "cyrene-client-workspace-web"
    )
    component["publisherId"] = "official-data-tools-plugins"
    with pytest.raises(metadata.CatalogMetadataError, match="exact publisher identity"):
        metadata._validate_catalog_v2_relationships(catalog)


def test_catalog_v2_rejects_inferred_or_inconsistent_product_source_map() -> None:
    metadata = _catalog_metadata_module()
    catalog = json.loads(
        (ROOT / "governance" / "component-catalog-v2.json").read_text(encoding="utf-8")
    )
    catalyst = next(row for row in catalog["workloads"] if row["workloadId"] == "catalyst")
    catalyst["sourcePolicy"]["productSources"] = []
    with pytest.raises(metadata.CatalogMetadataError, match="source map"):
        metadata._validate_catalog_v2_relationships(catalog)


def test_standalone_operator_binding_is_catalog_selected_and_digest_bound() -> None:
    catalog = _catalog()
    workload = catalog["workloads"][0]
    workload["requiredComponents"] = []
    workload["recommendedComponents"] = []
    workload["optionalComponents"] = ["plugin-a"]
    workload["targetPreferences"] = [{"componentId": "plugin-a", "targetId": "linux-u24-plugin"}]
    workload["sourcePolicy"] = {
        "mode": "standaloneOperator",
        "productComponentIds": [],
        "productSources": [],
        "operations": [
            "activate",
            "recover_binding",
            "deactivate",
            "runtime_status",
            "get_installation",
        ],
        "sourceId": "cyrene-plugin-standalone-operator",
    }
    workload["bindings"] = [
        {"componentId": "plugin-a", "bindingId": "resolver-binding-standalone-plugin-a"}
    ]
    result = _resolve(
        catalog,
        _release_envelopes(catalog, {"plugin-a"}),
        selections={"includeComponentIds": ["plugin-a"]},
    )
    plugin = next(row for row in result["selectedComponents"] if row["componentId"] == "plugin-a")
    assert result["status"] == "ready"
    assert plugin["sourcePolicy"]["sourceId"] == "cyrene-plugin-standalone-operator"
    assert plugin["bindingId"] == "resolver-binding-standalone-plugin-a"
    assert plugin in result["planDigestMaterial"]["selectedComponents"]


def test_uninstall_targets_one_installed_member_without_release_lookup() -> None:
    catalog = _catalog()
    installed = {"plugin-b": _installed_identity("plugin-b", "linux-u24-plugin")}
    result = _resolve(
        catalog,
        {},
        action="uninstall",
        installed_components=installed,
        selections={"includeComponentIds": ["plugin-b"]},
    )

    assert result["status"] == "ready"
    assert result["action"] == "uninstall"
    assert result["planDigestMaterial"]["action"] == "uninstall"
    assert [row["componentId"] for row in result["selectedComponents"]] == ["plugin-b"]
    assert set(result["closureReasons"][0]) == {
        "componentId",
        "reasonCode",
        "fromComponentId",
        "rootComponentId",
        "versionRange",
        "depth",
    }
    assert result["selectedComponents"][0]["installationId"] == "installation-plugin-b-001"
    assert result["selectedComponents"][0]["publisherIdentity"] == {
        "id": "resolver-publisher",
        "repository": REPOSITORY,
        "workflow": WORKFLOW,
        "tagFormat": "component-version-source-sha",
    }
    assert (
        result["selectedComponents"][0]["manifestAssetDigest"]
        == installed["plugin-b"]["manifestAssetDigest"]
    )


def test_uninstall_requires_verified_installation_identity_and_blocks_dependents() -> None:
    catalog = _catalog()
    no_id = _installed_identity("plugin-b", "linux-u24-plugin")
    no_id.pop("installationId")
    missing_identity = _resolve(
        catalog,
        {},
        action="uninstall",
        installed_components={"plugin-b": no_id},
        selections={"includeComponentIds": ["plugin-b"]},
    )
    assert any(
        item["code"] == "INSTALLED_IDENTITY_UNAVAILABLE" for item in missing_identity["blockers"]
    )

    catalog["workloads"][0]["optionalComponents"].append("runtime")
    dependent = _resolve(
        catalog,
        {},
        action="uninstall",
        installed_components={
            "runtime": _installed_identity("runtime", "linux-u24-systemd"),
            "app": _installed_identity("app", "linux-u24-python"),
        },
        selections={"includeComponentIds": ["runtime"]},
    )
    assert any(
        item["code"] == "INSTALLED_DEPENDENT" and item["details"]["dependentComponentId"] == "app"
        for item in dependent["blockers"]
    )


def test_echo_oci_uninstall_uses_deployment_receipt_without_package_installation_id() -> None:
    metadata = _catalog_metadata_module()
    catalog = metadata._validate_catalog(
        (ROOT / "governance" / "component-catalog-v2.json").read_bytes(),
        ROOT / "governance",
    )
    image_digest = _sha("echo-oci-image")
    installed_echo = {
        "installed": True,
        "version": "0.1.0",
        "releaseId": "preview-cyrene-echo-0.1.0-" + SOURCE_COMMIT,
        "manifestDigest": _sha("echo-oci-canonical-manifest"),
        "manifestAssetDigest": _sha("echo-oci-raw-manifest"),
        "digest": image_digest,
        "targetId": "linux-ubuntu-24.04-x86_64-oci",
        "releaseIdentity": _sha("echo-oci-release-receipt"),
        "pointerIdentity": "sha256-echo-oci-release",
        "imageDigest": image_digest,
        "releasePath": "/var/lib/cyrene/workloads/cyrene-echo/releases/echo-release",
    }

    result = resolve_workload(
        catalog,
        _sha("catalog-v2-echo-uninstall"),
        "echo",
        "linux-ubuntu-24.04-x86_64",
        {"includeComponentIds": ["cyrene-echo"]},
        {"cyrene-echo": installed_echo},
        {"indexes": []},
        action="uninstall",
    ).to_dict()

    assert result["status"] == "ready"
    assert [row["componentId"] for row in result["selectedComponents"]] == ["cyrene-echo"]
    selected = result["selectedComponents"][0]
    assert selected["artifactKind"] == "oci-image"
    assert selected["installationId"] is None
    assert "installationId" not in selected["installedIdentity"]
    assert selected["installedIdentity"]["releaseIdentity"] == installed_echo["releaseIdentity"]
    assert selected["installedIdentity"]["pointerIdentity"] == installed_echo["pointerIdentity"]
    assert selected["installedIdentity"]["imageDigest"] == image_digest

    changed_receipt = {
        **installed_echo,
        "releasePath": "/var/lib/cyrene/workloads/echo/releases/other",
    }
    changed = resolve_workload(
        catalog,
        _sha("catalog-v2-echo-uninstall"),
        "echo",
        "linux-ubuntu-24.04-x86_64",
        {"includeComponentIds": ["cyrene-echo"]},
        {"cyrene-echo": changed_receipt},
        {"indexes": []},
        action="uninstall",
    ).to_dict()
    assert changed["status"] == "ready"
    assert changed["planDigest"] != result["planDigest"]


def test_uninstall_requires_exactly_one_direct_component_selection() -> None:
    catalog = _catalog()
    result = _resolve(
        catalog,
        {},
        action="uninstall",
        installed_components={"plugin-b": _installed_identity("plugin-b", "linux-u24-plugin")},
        selections={"includeComponentIds": ["app", "plugin-b"]},
    )
    assert any(item["code"] == "UNINSTALL_SELECTION_INVALID" for item in result["blockers"])


def _manifest_with_artifact(artifact: dict[str, Any], digest: str) -> dict[str, Any]:
    release_id = f"stable-component-1.0.0-{SOURCE_COMMIT}"
    return {
        "schemaVersion": 2,
        "releaseId": release_id,
        "componentId": "component",
        "version": "1.0.0",
        "channel": "stable",
        "target": HOST,
        "artifact": artifact,
        "dependencies": [],
        "restart": {"group": "none"},
        "source": {
            "repository": f"https://github.com/{REPOSITORY}",
            "ref": f"refs/tags/{release_id}",
            "commit": SOURCE_COMMIT,
        },
        "provenance": {
            "attestation": {
                "kind": "github-artifact-attestation",
                "subjectName": "component-release-manifest-v2.json",
                "repository": REPOSITORY,
                "workflow": WORKFLOW,
                "predicateType": "https://slsa.dev/provenance/v1",
                "run": {
                    "id": "12345",
                    "attempt": 1,
                    "url": f"https://github.com/{REPOSITORY}/actions/runs/12345",
                },
            }
        },
        "manifestDigest": digest,
        "protocolVersion": "cyrene.test.v1",
        "contentDigest": artifact.get("sha256", artifact.get("archive", {}).get("sha256")),
    }


def test_manifest_v2_accepts_plugin_package_static_web_and_optional_sbom_variants() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    governance = ROOT / "governance"
    manifest_v2_schema = json.loads(
        (governance / "component-release-manifest-v2.schema.json").read_text(encoding="utf-8")
    )
    manifest_v1_schema = json.loads(
        (governance / "component-release-manifest-v1.schema.json").read_text(encoding="utf-8")
    )
    store = {
        manifest_v1_schema["$id"]: manifest_v1_schema,
        manifest_v2_schema["$id"]: manifest_v2_schema,
        "component-release-manifest-v1.schema.json": manifest_v1_schema,
        "component-release-manifest-v2.schema.json": manifest_v2_schema,
    }
    resolver = jsonschema.validators.RefResolver.from_schema(manifest_v2_schema, store=store)
    validator = jsonschema.Draft202012Validator(
        manifest_v2_schema,
        resolver=resolver,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )
    digest = _sha("artifact")
    plugin = {
        "kind": "plugin-package",
        "packageId": "cyrene.tools.sample-plugin",
        "capabilityId": "tools.sample-plugin.v1",
        "interfaceVersion": "1.0.0",
        "archive": {
            "format": "zip",
            "uri": f"https://github.com/{REPOSITORY}/releases/download/x/package.zip",
            "sha256": digest,
            "sizeBytes": 1024,
            "files": {"plugin/main.py": digest},
            "maxEntries": 10,
            "maxUncompressedBytes": 4096,
        },
        "descriptor": {
            "uri": f"https://github.com/{REPOSITORY}/releases/download/x/descriptor.json",
            "sha256": digest,
            "sizeBytes": 128,
        },
        "requirementsLock": {
            "uri": f"https://github.com/{REPOSITORY}/releases/download/x/requirements.lock",
            "sha256": digest,
            "sizeBytes": 128,
        },
        "preparerWheels": [
            {
                "uri": f"https://github.com/{REPOSITORY}/releases/download/x/preparer.whl",
                "sha256": digest,
                "sizeBytes": 128,
            }
        ],
        "packageReleaseMetadata": {
            "uri": f"https://github.com/{REPOSITORY}/releases/download/x/package-release.json",
            "sha256": digest,
            "sizeBytes": 128,
        },
        "sbom": {
            "uri": f"https://github.com/{REPOSITORY}/releases/download/x/sbom.json",
            "sha256": digest,
            "sizeBytes": 128,
            "format": "cyclonedx-json",
            "specVersion": "1.6",
        },
    }
    web = {
        "kind": "static-web",
        "format": "tar.gz",
        "uri": f"https://github.com/{REPOSITORY}/releases/download/x/client.tar.gz",
        "sha256": digest,
        "sizeBytes": 1024,
        "files": {"index.html": digest, "assets/app.js": digest},
        "maxEntries": 10,
        "maxUncompressedBytes": 4096,
        "entrypoint": "index.html",
    }
    for artifact in (plugin, web):
        manifest = _manifest_with_artifact(artifact, _sha("manifest"))
        assert list(validator.iter_errors(manifest)) == []

    invalid = copy.deepcopy(plugin)
    invalid["sbom"]["specVersion"] = "1.5"
    assert list(validator.iter_errors(_manifest_with_artifact(invalid, _sha("manifest"))))
