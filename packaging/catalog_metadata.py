"""Fetch and verify the immutable component catalog release metadata.

This module selects an immutable GitHub Release, validates its exact assets and
source identity, verifies the detached GitHub attestation, and writes only the
fully validated catalog, attestation bundle, and metadata. 组件目录下载、来源证明与原子落盘。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

REPOSITORY = "DoHorizon-AI/Cyrene-Workspace"
WORKFLOW = "DoHorizon-AI/Cyrene-Workspace/.github/workflows/component-catalog-release.yml"
CATALOG_ASSET = "component-catalog-v1.json"
ATTESTATION_ASSET = "component-catalog-v1.json.attestation.jsonl"
CATALOG_ASSETS = {
    1: (CATALOG_ASSET, ATTESTATION_ASSET),
    2: ("component-catalog-v2.json", "component-catalog-v2.json.attestation.jsonl"),
}
CHANNEL_REFS: dict[str, tuple[str, ...]] = {
    "stable": ("refs/heads/main", "refs/heads/release"),
    "preview": ("refs/heads/develop",),
}
CHANNELS = frozenset(CHANNEL_REFS)
TAG_PATTERN = re.compile(r"^catalog-(?:v2-)?(stable|preview)-([0-9a-f]{40})$")
API_BASE = "https://api.github.com"
_MAX_API_BYTES = 8 * 1024 * 1024
_MAX_ASSET_BYTES = 64 * 1024 * 1024
_MAX_RELEASE_PAGES = 100


class CatalogMetadataError(RuntimeError):
    """An expected, fail-closed catalog discovery or validation error."""


AttestationVerifier = Callable[..., Any]


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects that leave the GitHub API or release asset hosts."""

    def __init__(self, allowed_hosts: frozenset[str]) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> urllib.request.Request | None:
        target = urllib.parse.urlsplit(new_url)
        if target.scheme != "https" or target.hostname not in self.allowed_hosts:
            raise CatalogMetadataError("GitHub returned an untrusted HTTPS redirect.")
        redirected = super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )
        source = urllib.parse.urlsplit(request.full_url)
        if redirected is not None and (
            target.hostname != "api.github.com" or target.netloc.lower() != source.netloc.lower()
        ):
            # urllib copies normal headers to redirected requests; never forward API credentials off-origin.
            redirected.remove_header("Authorization")
        return redirected


def _read_url(url: str, *, accept: str, limit: int, asset: bool = False) -> bytes:
    """Read a bounded HTTPS response from the pinned GitHub endpoints."""

    headers = {
        "Accept": accept,
        "User-Agent": "CyreneComponentCatalogFetcher/1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    parsed_url = urllib.parse.urlsplit(url)
    if (
        parsed_url.scheme == "https"
        and parsed_url.hostname == "api.github.com"
        and parsed_url.username is None
        and parsed_url.password is None
        and parsed_url.port in (None, 443)
    ):
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    allowed_hosts = (
        frozenset(
            {
                "api.github.com",
                "github.com",
                "release-assets.githubusercontent.com",
                "objects.githubusercontent.com",
            }
        )
        if asset
        else frozenset({"api.github.com"})
    )
    opener = urllib.request.build_opener(_SafeRedirectHandler(allowed_hosts))
    try:
        with opener.open(request, timeout=30) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname not in allowed_hosts:
                raise CatalogMetadataError("GitHub returned content from an untrusted URL.")
            chunks: list[bytes] = []
            total = 0
            while chunk := response.read(min(1024 * 1024, limit + 1 - total)):
                total += len(chunk)
                if total > limit:
                    raise CatalogMetadataError("GitHub response exceeds the permitted size.")
                chunks.append(chunk)
            return b"".join(chunks)
    except CatalogMetadataError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise CatalogMetadataError(f"Cannot fetch GitHub release data: {error}") from error


def _read_api_json(url: str) -> Any:
    """Fetch one GitHub API JSON response with strict UTF-8 parsing."""

    payload = _read_url(
        url,
        accept="application/vnd.github+json",
        limit=_MAX_API_BYTES,
    )
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonstandard_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, CatalogMetadataError) as error:
        raise CatalogMetadataError("GitHub returned malformed release JSON.") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON objects containing duplicate property names."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogMetadataError(f"JSON object contains duplicate property {key!r}.")
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str) -> None:
    """Reject JavaScript-only numeric values accepted by Python's JSON parser."""

    raise CatalogMetadataError(f"JSON contains non-standard numeric constant {value!r}.")


def _release_by_tag(release_id: str) -> dict[str, Any]:
    """Fetch the one exact tag release and never substitute another tag."""

    encoded_tag = urllib.parse.quote(release_id, safe="")
    url = f"{API_BASE}/repos/{REPOSITORY}/releases/tags/{encoded_tag}"
    release = _read_api_json(url)
    if not isinstance(release, dict):
        raise CatalogMetadataError("GitHub exact-tag release response is not an object.")
    return release


def _latest_immutable_release(channel: str) -> dict[str, Any]:
    """Prefer the newest v2 release, falling back to v1 only when none exists."""

    candidates: dict[int, list[tuple[datetime, dict[str, Any]]]] = {1: [], 2: []}
    for page in range(1, _MAX_RELEASE_PAGES + 1):
        url = f"{API_BASE}/repos/{REPOSITORY}/releases?per_page=100&page={page}"
        releases = _read_api_json(url)
        if not isinstance(releases, list) or not all(isinstance(item, dict) for item in releases):
            raise CatalogMetadataError("GitHub release list response is malformed.")
        for release in releases:
            tag_name = release.get("tag_name")
            match = TAG_PATTERN.fullmatch(tag_name) if isinstance(tag_name, str) else None
            if match is None or match.group(1) != channel:
                continue
            if release.get("immutable") is not True:
                continue
            created_at = release.get("created_at")
            if not isinstance(created_at, str):
                raise CatalogMetadataError("Immutable release has no valid creation time.")
            try:
                created = datetime.fromisoformat(created_at)
            except ValueError as error:
                raise CatalogMetadataError(
                    "Immutable release has an invalid creation time."
                ) from error
            if created.tzinfo is None:
                raise CatalogMetadataError("Immutable release creation time has no timezone.")
            schema_version = _tag_schema_version(tag_name)
            candidates[schema_version].append((created, release))
        if len(releases) < 100:
            break
    else:
        raise CatalogMetadataError("GitHub release history exceeds the supported scan depth.")

    preferred_candidates = candidates[2] or candidates[1]
    if not preferred_candidates:
        raise CatalogMetadataError(f"No immutable {channel} component catalog release exists.")
    latest_time = max(candidate[0] for candidate in preferred_candidates)
    latest = [release for created, release in preferred_candidates if created == latest_time]
    if len(latest) != 1:
        raise CatalogMetadataError(f"Latest immutable {channel} release selection is ambiguous.")
    return latest[0]


def _tag_schema_version(tag_name: str) -> int:
    """Map the explicit v2 tag namespace to its catalog schema version."""

    return 2 if tag_name.startswith("catalog-v2-") else 1


def _validate_release(release: dict[str, Any], channel: str, requested_tag: str | None) -> str:
    """Check the immutable release envelope and return its exact source commit."""

    tag_name = release.get("tag_name")
    match = TAG_PATTERN.fullmatch(tag_name) if isinstance(tag_name, str) else None
    if match is None or match.group(1) != channel:
        raise CatalogMetadataError(
            "Release tag must be catalog-<channel>-<SHA-1> or catalog-v2-<channel>-<SHA-1>."
        )
    if requested_tag is not None and tag_name != requested_tag:
        raise CatalogMetadataError("GitHub exact-tag response differs from the requested release.")
    if release.get("immutable") is not True:
        raise CatalogMetadataError("Selected GitHub release is not immutable.")
    if release.get("draft") is not False:
        raise CatalogMetadataError("Selected GitHub release must be published and non-draft.")
    if release.get("prerelease") is not (channel == "preview"):
        raise CatalogMetadataError(
            "Selected GitHub release prerelease flag differs from its channel."
        )
    return match.group(2)


def _expected_browser_url(tag_name: str, asset_name: str) -> str:
    """Return the only accepted public release asset URL for an asset name."""

    return (
        f"https://github.com/{REPOSITORY}/releases/download/"
        f"{urllib.parse.quote(tag_name, safe='')}/{urllib.parse.quote(asset_name, safe='')}"
    )


def _find_assets(
    release: dict[str, Any], tag_name: str
) -> tuple[dict[str, tuple[str, int]], str, str]:
    """Select one exact v1 or v2 catalog and its matching attestation assets."""

    assets = release.get("assets")
    if not isinstance(assets, list):
        raise CatalogMetadataError("Selected GitHub release has no asset list.")
    asset_names = {name for pair in CATALOG_ASSETS.values() for name in pair}
    if len(assets) != 2:
        raise CatalogMetadataError(
            "Selected GitHub catalog release must contain exactly one raw catalog and its attestation."
        )
    found: dict[str, tuple[str, int]] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise CatalogMetadataError("Selected GitHub release contains a malformed asset record.")
        name = asset.get("name")
        if not isinstance(name, str):
            raise CatalogMetadataError("Selected GitHub release contains an asset without a name.")
        if name not in asset_names:
            continue
        if name in found:
            raise CatalogMetadataError(
                f"Selected GitHub release contains duplicate asset {name!r}."
            )
        asset_id = asset.get("id")
        size = asset.get("size")
        if (
            not isinstance(asset_id, int)
            or isinstance(asset_id, bool)
            or asset_id < 1
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or asset.get("state") != "uploaded"
        ):
            raise CatalogMetadataError(f"Selected GitHub release asset {name!r} is malformed.")
        api_url = asset.get("url")
        expected_api_url = f"{API_BASE}/repos/{REPOSITORY}/releases/assets/{asset_id}"
        if api_url != expected_api_url:
            raise CatalogMetadataError(f"Release asset {name!r} is outside the trusted repository.")
        if asset.get("browser_download_url") != _expected_browser_url(tag_name, name):
            raise CatalogMetadataError(f"Release asset {name!r} download URL is not exact.")
        found[name] = (expected_api_url, size)
    pairs = [pair for pair in CATALOG_ASSETS.values() if set(pair) == found.keys()]
    if len(pairs) != 1:
        missing = sorted(asset_names - found.keys())
        raise CatalogMetadataError(
            "Selected GitHub catalog release must contain one matching catalog/attestation pair; "
            f"available catalog assets are {', '.join(missing)}."
        )
    if len(pairs[0]) != 2:
        raise CatalogMetadataError("Catalog release must contain one exact catalog asset pair.")
    if pairs[0][0] != CATALOG_ASSETS[_tag_schema_version(tag_name)][0]:
        raise CatalogMetadataError("Release tag schema version differs from its catalog assets.")
    return found, pairs[0][0], pairs[0][1]


def _download_asset(url: str, *, size: int, name: str) -> bytes:
    """Download an exact release asset and compare its declared length."""

    if size > _MAX_ASSET_BYTES:
        raise CatalogMetadataError(f"Release asset {name!r} exceeds the supported size.")
    payload = _read_url(
        url,
        accept="application/octet-stream",
        limit=min(_MAX_ASSET_BYTES, size),
        asset=True,
    )
    if len(payload) != size:
        raise CatalogMetadataError(f"Release asset {name!r} size differs from GitHub metadata.")
    return payload


def _read_local_json(path: Path, label: str) -> Any:
    """Load one local schema without accepting duplicate JSON keys."""

    try:
        raw = path.read_bytes()
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonstandard_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, CatalogMetadataError) as error:
        raise CatalogMetadataError(f"Cannot read {label} at {path}: {error}") from error


def _validate_catalog(payload: bytes, schema_root: Path) -> dict[str, Any]:
    """Validate the exact downloaded bytes against all local Draft 2020-12 schemas."""

    try:
        catalog = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonstandard_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, CatalogMetadataError) as error:
        raise CatalogMetadataError(
            "Downloaded component catalog is not strict UTF-8 JSON."
        ) from error
    if not isinstance(catalog, dict):
        raise CatalogMetadataError("Downloaded component catalog must be a JSON object.")

    schema_version = catalog.get("schemaVersion")
    if isinstance(schema_version, bool) or schema_version not in {1, 2}:
        raise CatalogMetadataError("Component catalog schemaVersion must be 1 or 2.")
    catalog_schema = _read_local_json(
        schema_root / f"component-catalog-v{schema_version}.schema.json", "catalog schema"
    )
    manifest_v1_schema = _read_local_json(
        schema_root / "component-release-manifest-v1.schema.json", "component manifest schema"
    )
    manifest_v2_schema = _read_local_json(
        schema_root / "component-release-manifest-v2.schema.json", "component manifest v2 schema"
    )
    manifest_schemas = {
        "component-release-manifest-v1.schema.json": manifest_v1_schema,
        "component-release-manifest-v2.schema.json": manifest_v2_schema,
    }
    if not isinstance(catalog_schema, dict) or not all(
        isinstance(schema, dict) for schema in manifest_schemas.values()
    ):
        raise CatalogMetadataError("Component catalog schemas must be JSON objects.")

    try:
        from jsonschema import Draft202012Validator
        from jsonschema.validators import RefResolver

        Draft202012Validator.check_schema(catalog_schema)
        schema_store: dict[str, Any] = {}
        for filename, schema in manifest_schemas.items():
            Draft202012Validator.check_schema(schema)
            schema_id = schema.get("$id")
            if not isinstance(schema_id, str) or schema_id in schema_store:
                raise CatalogMetadataError(
                    f"Component manifest schema {filename} has a missing or duplicate identifier."
                )
            schema_store[schema_id] = schema
            schema_store[filename] = schema
        resolver = RefResolver.from_schema(
            catalog_schema,
            store=schema_store,
        )
        validator = Draft202012Validator(
            catalog_schema,
            resolver=resolver,
            format_checker=Draft202012Validator.FORMAT_CHECKER,
        )
        errors = sorted(
            validator.iter_errors(catalog),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        )
    except CatalogMetadataError:
        raise
    except Exception as error:
        raise CatalogMetadataError(
            f"Cannot validate the component catalog schemas: {error}"
        ) from error
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "$"
        raise CatalogMetadataError(
            f"Component catalog schema violation at {location}: {first.message}"
        )
    generation = catalog.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool):
        raise CatalogMetadataError("Component catalog generation must be an integer.")
    if schema_version == 2:
        _validate_catalog_v2_relationships(catalog)
    return catalog


def _validate_catalog_v2_relationships(catalog: dict[str, Any]) -> None:
    """Validate identity and workload relationships that JSON Schema cannot express."""

    publishers = catalog.get("publishers")
    components = catalog.get("components")
    workloads = catalog.get("workloads")
    targets = catalog.get("targets")
    if not all(isinstance(value, list) for value in (publishers, components, workloads, targets)):
        raise CatalogMetadataError("Catalog v2 identity arrays are malformed.")

    publisher_by_id: dict[str, dict[str, Any]] = {}
    for publisher in publishers:
        if not isinstance(publisher, dict):
            raise CatalogMetadataError("Catalog v2 publisher row is malformed.")
        repository = publisher.get("repository")
        publisher_id = publisher.get("id", repository)
        if not isinstance(repository, str) or not isinstance(publisher_id, str):
            raise CatalogMetadataError("Catalog v2 publisher identity is malformed.")
        if publisher_id in publisher_by_id:
            raise CatalogMetadataError(
                f"Catalog v2 publisher identity {publisher_id!r} is duplicated."
            )
        publisher_by_id[publisher_id] = publisher

    component_by_id: dict[str, dict[str, Any]] = {}
    package_ids: set[str] = set()
    for component in components:
        if not isinstance(component, dict):
            raise CatalogMetadataError("Catalog v2 component row is malformed.")
        component_id = component.get("componentId")
        if not isinstance(component_id, str) or component_id in component_by_id:
            raise CatalogMetadataError("Catalog v2 component IDs must be unique strings.")
        component_by_id[component_id] = component
        repository = component.get("publisher")
        publisher_id = component.get("publisherId", repository)
        publisher = publisher_by_id.get(publisher_id) if isinstance(publisher_id, str) else None
        if publisher is None or publisher.get("repository") != repository:
            raise CatalogMetadataError(
                f"Catalog component {component_id!r} does not bind to its exact publisher identity and repository."
            )
        package = component.get("pluginPackage")
        if package is not None:
            if not isinstance(package, dict):
                raise CatalogMetadataError(
                    f"Catalog plugin mapping for {component_id!r} is malformed."
                )
            package_id = package.get("packageId")
            if not isinstance(package_id, str) or package_id in package_ids:
                raise CatalogMetadataError(
                    "Catalog plugin package IDs must map uniquely to components."
                )
            package_ids.add(package_id)
        component_targets = component.get("targets", [])
        if not isinstance(component_targets, list):
            raise CatalogMetadataError(f"Catalog component {component_id!r} has malformed targets.")
        for component_target in component_targets:
            if not isinstance(component_target, dict) or component_target.get("targetId") not in {
                target.get("id") for target in targets if isinstance(target, dict)
            }:
                raise CatalogMetadataError(
                    f"Catalog component {component_id!r} references an unknown target."
                )
        dependencies = component.get("dependencies", [])
        if not isinstance(dependencies, list):
            raise CatalogMetadataError(
                f"Catalog component {component_id!r} has malformed dependencies."
            )
        for dependency in dependencies:
            if not isinstance(dependency, dict) or dependency.get("componentId") not in {
                item.get("componentId") for item in components if isinstance(item, dict)
            }:
                raise CatalogMetadataError(
                    f"Catalog component {component_id!r} references an unknown dependency."
                )

    target_by_id: dict[str, dict[str, Any]] = {}
    for target in targets:
        if not isinstance(target, dict) or not isinstance(target.get("id"), str):
            raise CatalogMetadataError("Catalog v2 target row is malformed.")
        if target["id"] in target_by_id:
            raise CatalogMetadataError(f"Catalog v2 target ID {target['id']!r} is duplicated.")
        target_by_id[target["id"]] = target

    workload_ids: set[str] = set()
    binding_ids: set[str] = set()
    for workload in workloads:
        if not isinstance(workload, dict):
            raise CatalogMetadataError("Catalog v2 workload row is malformed.")
        workload_id = workload.get("workloadId")
        if not isinstance(workload_id, str) or workload_id in workload_ids:
            raise CatalogMetadataError("Catalog v2 workload IDs must be unique strings.")
        workload_ids.add(workload_id)
        memberships: set[str] = set()
        category_members: set[str] = set()
        for key in ("requiredComponents", "recommendedComponents", "optionalComponents"):
            values = workload.get(key)
            if not isinstance(values, list):
                raise CatalogMetadataError(f"Workload {workload_id!r} has malformed {key}.")
            if len(set(values)) != len(values) or category_members.intersection(values):
                raise CatalogMetadataError(
                    f"Workload {workload_id!r} component membership categories must be disjoint."
                )
            category_members.update(values)
            memberships.update(values)
        minimum_explicit_optional = workload.get("minimumExplicitOptionalSelections", 0)
        optional_members = workload.get("optionalComponents", [])
        if (
            not isinstance(minimum_explicit_optional, int)
            or isinstance(minimum_explicit_optional, bool)
            or minimum_explicit_optional < 0
            or not isinstance(optional_members, list)
            or minimum_explicit_optional > len(optional_members)
        ):
            raise CatalogMetadataError(
                f"Workload {workload_id!r} has an invalid minimum explicit optional selection count."
            )
        groups = workload.get("choiceGroups", [])
        if not isinstance(groups, list):
            raise CatalogMetadataError(f"Workload {workload_id!r} has malformed choices.")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("componentIds"), list):
                raise CatalogMetadataError(
                    f"Workload {workload_id!r} has a malformed choice group."
                )
            if len(set(group["componentIds"])) != len(
                group["componentIds"]
            ) or category_members.intersection(group["componentIds"]):
                raise CatalogMetadataError(
                    f"Workload {workload_id!r} choice members overlap another selection category."
                )
            category_members.update(group["componentIds"])
            memberships.update(group["componentIds"])
        missing_members = memberships - component_by_id.keys()
        if missing_members:
            raise CatalogMetadataError(
                f"Workload {workload_id!r} references unknown components: {', '.join(sorted(missing_members))}."
            )
        host_targets = workload.get("hostTargets")
        if not isinstance(host_targets, list) or any(
            item not in target_by_id for item in host_targets
        ):
            raise CatalogMetadataError(
                f"Workload {workload_id!r} references an unknown host target."
            )
        preferences = workload.get("targetPreferences", [])
        if not isinstance(preferences, list):
            raise CatalogMetadataError(
                f"Workload {workload_id!r} has malformed target preferences."
            )
        preference_ids: set[str] = set()
        for preference in preferences:
            if not isinstance(preference, dict):
                raise CatalogMetadataError(
                    f"Workload {workload_id!r} has a malformed target preference."
                )
            component_id = preference.get("componentId")
            target_id = preference.get("targetId")
            component = component_by_id.get(component_id) if isinstance(component_id, str) else None
            if component is None or component_id not in memberships:
                raise CatalogMetadataError(
                    f"Workload {workload_id!r} target preference is not a member binding."
                )
            if target_id not in target_by_id or target_id not in {
                row.get("targetId") for row in component.get("targets", []) if isinstance(row, dict)
            }:
                raise CatalogMetadataError(
                    f"Workload {workload_id!r} target preference is not supported by its component row."
                )
            if component_id in preference_ids:
                raise CatalogMetadataError(
                    f"Workload {workload_id!r} has duplicate target preferences."
                )
            preference_ids.add(component_id)
        bindings = workload.get("bindings", [])
        if not isinstance(bindings, list):
            raise CatalogMetadataError(f"Workload {workload_id!r} has malformed plugin bindings.")
        bound_components: set[str] = set()
        for binding in bindings:
            if not isinstance(binding, dict):
                raise CatalogMetadataError(
                    f"Workload {workload_id!r} has a malformed plugin binding."
                )
            component_id = binding.get("componentId")
            binding_id = binding.get("bindingId")
            component = component_by_id.get(component_id) if isinstance(component_id, str) else None
            if (
                component is None
                or component_id not in memberships
                or not isinstance(component.get("pluginPackage"), dict)
                or not isinstance(binding_id, str)
                or not binding_id
            ):
                raise CatalogMetadataError(
                    f"Workload {workload_id!r} binding does not identify a member plugin."
                )
            if component_id in bound_components or binding_id in binding_ids:
                raise CatalogMetadataError("Catalog v2 workload binding identities must be unique.")
            bound_components.add(component_id)
            binding_ids.add(binding_id)
        expected_plugins = {
            component_id
            for component_id in memberships
            if isinstance(component_by_id[component_id].get("pluginPackage"), dict)
        }
        if bound_components != expected_plugins:
            raise CatalogMetadataError(
                f"Workload {workload_id!r} must declare one explicit binding for each member plugin."
            )
        source_policy = workload.get("sourcePolicy")
        if not isinstance(source_policy, dict):
            raise CatalogMetadataError(f"Workload {workload_id!r} has no source policy object.")
        product_ids = source_policy.get("productComponentIds", [])
        if not isinstance(product_ids, list) or any(
            item not in component_by_id for item in product_ids
        ):
            raise CatalogMetadataError(
                f"Workload {workload_id!r} source policy references unknown products."
            )
        product_sources = source_policy.get("productSources")
        if not isinstance(product_sources, list):
            raise CatalogMetadataError(
                f"Workload {workload_id!r} source policy has no explicit product source map."
            )
        source_map: dict[str, str] = {}
        for row in product_sources:
            if not isinstance(row, dict):
                raise CatalogMetadataError(
                    f"Workload {workload_id!r} product source mapping is malformed."
                )
            component_id = row.get("componentId")
            source_id = row.get("sourceId")
            if (
                not isinstance(component_id, str)
                or component_id not in component_by_id
                or not isinstance(source_id, str)
                or not source_id
                or component_id in source_map
                or source_id in source_map.values()
            ):
                raise CatalogMetadataError(
                    f"Workload {workload_id!r} product source mapping is unknown or ambiguous."
                )
            source_map[component_id] = source_id
        if set(source_map) != set(product_ids):
            raise CatalogMetadataError(
                f"Workload {workload_id!r} product source map must explicitly match its product IDs."
            )
        if source_policy.get("mode") == "actualProduct" and not set(product_ids).issubset(
            memberships
        ):
            raise CatalogMetadataError(
                f"Workload {workload_id!r} source products must be workload members."
            )


def _verify_source_ref(
    *,
    payload: bytes,
    subject_name: str,
    bundle_path: Path,
    digest: str,
    source_commit: str,
    channel: str,
    verify_attestation: AttestationVerifier,
) -> str:
    """Call the component attestation verifier for every channel ref exactly once."""

    successful_refs: list[str] = []
    failures: list[str] = []
    for source_ref in CHANNEL_REFS[channel]:
        try:
            result = verify_attestation(
                payload,
                subject_name=subject_name,
                digest=digest,
                repository=REPOSITORY,
                workflow=WORKFLOW,
                source_ref=source_ref,
                source_commit=source_commit,
                bundle_path=bundle_path,
            )
        except Exception as error:  # noqa: BLE001 - try every trusted ref before failing closed
            failures.append(f"{source_ref}: {type(error).__name__}: {error}")
        else:
            if result is False:
                failures.append(f"{source_ref}: verifier returned false")
            else:
                successful_refs.append(source_ref)
    if len(successful_refs) != 1:
        details = "; ".join(failures) or "multiple refs passed"
        raise CatalogMetadataError(
            f"Catalog attestation must verify for exactly one trusted source ref; {details}."
        )
    return successful_refs[0]


def _stage_file(path: Path, payload: bytes) -> Path:
    """Write and fsync a sibling temporary file for a later atomic replacement."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.is_dir():
        raise CatalogMetadataError(f"Output destination is not a regular file path: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _atomic_write_outputs(outputs: list[tuple[Path, bytes]]) -> None:
    """Stage all validated output files before atomically replacing destinations."""

    targets: list[tuple[Path, bytes]] = []
    for path, payload in outputs:
        if path.is_symlink() or path.is_dir():
            raise CatalogMetadataError(f"Output destination is not a regular file path: {path}")
        target = path.resolve(strict=False)
        for existing_target, _ in targets:
            same_path = os.path.normcase(os.fspath(target)) == os.path.normcase(
                os.fspath(existing_target)
            )
            if not same_path and target.exists() and existing_target.exists():
                same_path = os.path.samefile(target, existing_target)
            if same_path:
                raise CatalogMetadataError(
                    "Catalog, attestation, and metadata outputs must differ."
                )
        targets.append((target, payload))

    staged: list[tuple[Path, Path]] = []
    try:
        for target, payload in targets:
            staged.append((_stage_file(target, payload), target))
        for temporary, destination in staged:
            os.replace(temporary, destination)
        for parent in {target.parent for _, target in staged}:
            try:
                directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            except OSError:
                continue
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except (OSError, CatalogMetadataError) as error:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
        if isinstance(error, CatalogMetadataError):
            raise
        raise CatalogMetadataError(
            f"Cannot atomically write verified catalog outputs: {error}"
        ) from error


def fetch_verified_catalog(
    *,
    channel: str,
    release_id: str | None = None,
    latest_channel: str | None = None,
    output_path: str | Path,
    metadata_path: str | Path,
    schema_root: str | Path,
    verify_attestation: AttestationVerifier,
    attestation_output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Fetch, validate, attest, and persist one immutable component catalog.

    Exactly one of ``release_id`` and ``latest_channel`` must be set. All release,
    schema, and attestation checks complete before the three output files are staged.

    Args:
        channel: The expected ``stable`` or ``preview`` release channel.
        release_id: Exact channel-prefixed release tag to fetch.
        latest_channel: Channel whose newest immutable release should be selected.
        output_path: Destination for the original catalog asset bytes.
        metadata_path: Destination for the verified release metadata JSON.
        schema_root: Directory containing catalog v1 and component manifest v1/v2 schemas.
        verify_attestation: Existing component verifier for GitHub artifact proofs.
        attestation_output_path: Optional destination for the original detached bundle.
    Returns:
        The verified metadata object written to ``metadata_path``.
    Raises:
        CatalogMetadataError: If any release, download, schema, proof, or write check fails.
    """

    if not isinstance(channel, str) or channel not in CHANNELS:
        raise CatalogMetadataError("channel must be exactly 'stable' or 'preview'.")
    if (release_id is None) == (latest_channel is None):
        raise CatalogMetadataError("Set exactly one of release_id and latest_channel.")
    if not callable(verify_attestation):
        raise CatalogMetadataError("verify_attestation must be callable.")

    if release_id is not None:
        if not isinstance(release_id, str):
            raise CatalogMetadataError(
                "release_id must be a versioned catalog channel-prefixed full SHA-1 tag."
            )
        match = TAG_PATTERN.fullmatch(release_id)
        if match is None or match.group(1) != channel:
            raise CatalogMetadataError(
                "release_id must match the requested channel and full SHA-1 tag."
            )
        release = _release_by_tag(release_id)
        requested_tag = release_id
    else:
        if not isinstance(latest_channel, str) or latest_channel not in CHANNELS:
            raise CatalogMetadataError("latest_channel must be exactly 'stable' or 'preview'.")
        if latest_channel != channel:
            raise CatalogMetadataError("latest_channel must equal the requested channel.")
        release = _latest_immutable_release(channel)
        requested_tag = None

    source_commit = _validate_release(release, channel, requested_tag)
    tag_name = release["tag_name"]
    assets, catalog_asset, attestation_asset = _find_assets(release, tag_name)
    catalog_bytes = _download_asset(
        assets[catalog_asset][0], size=assets[catalog_asset][1], name=catalog_asset
    )
    attestation_bytes = _download_asset(
        assets[attestation_asset][0],
        size=assets[attestation_asset][1],
        name=attestation_asset,
    )
    catalog_digest = "sha256:" + hashlib.sha256(catalog_bytes).hexdigest()
    catalog = _validate_catalog(catalog_bytes, Path(schema_root))
    if catalog["schemaVersion"] != _tag_schema_version(tag_name):
        raise CatalogMetadataError("Release tag schema version differs from the catalog payload.")

    bundle_file: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="cyrene-component-catalog-attestation-", suffix=".jsonl", delete=False
        ) as stream:
            stream.write(attestation_bytes)
            stream.flush()
            os.fsync(stream.fileno())
            bundle_file = Path(stream.name)
        source_ref = _verify_source_ref(
            payload=catalog_bytes,
            subject_name=catalog_asset,
            bundle_path=bundle_file,
            digest=catalog_digest,
            source_commit=source_commit,
            channel=channel,
            verify_attestation=verify_attestation,
        )
    finally:
        if bundle_file is not None:
            bundle_file.unlink(missing_ok=True)

    metadata: dict[str, Any] = {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "workflow": WORKFLOW,
        "channel": channel,
        "releaseId": tag_name,
        "sourceCommit": source_commit,
        "sourceRef": source_ref,
        "catalogSha256": catalog_digest,
        "catalogSchemaVersion": catalog["schemaVersion"],
        "generation": catalog["generation"],
        "subjectName": catalog_asset,
        "attestationAssetName": attestation_asset,
    }
    metadata_bytes = (
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    attestation_path = (
        Path(attestation_output_path)
        if attestation_output_path is not None
        else Path(f"{output_path}.attestation.jsonl")
    )
    _atomic_write_outputs(
        [
            (Path(output_path), catalog_bytes),
            (attestation_path, attestation_bytes),
            (Path(metadata_path), metadata_bytes),
        ]
    )
    return metadata
