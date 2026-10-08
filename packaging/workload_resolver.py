"""Resolve catalog workloads into deterministic, trust-bound component plans.

The resolver is intentionally pure: it performs no network, filesystem, signature,
attestation, or machine-inventory discovery. Callers must pass an authenticated
catalog digest, verified release-index/manifests, and an installed inventory.
Resolver output binds those inputs and the explicit user selections into a plan
that a trusted updater can verify again before download or activation.
工作负载解析器只计算闭包和计划，不下载、不验签，也不接受调用方提供的宿主权限身份。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\+sha\.[0-9a-f]{40})?$"
)
_EXACT_RANGE_RE = re.compile(r"^=(\d+\.\d+\.\d+)$")
_INTERVAL_RANGE_RE = re.compile(r"^>=(\d+\.\d+\.\d+), <(\d+\.\d+\.\d+)$")
_BINDING_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{0,159}$")
_HTTPS_REPOSITORY_PREFIX = "https://github.com/"
_REQUIREDNESS = frozenset({"required", "recommended", "optional", "choice", "dependency"})


def _canonical_bytes(value: Any) -> bytes:
    """Return the resolver's stable UTF-8 JSON representation."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sorted_unique_strings(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise TypeError(f"{field} must be an array of non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} must not contain duplicate values")
    return sorted(value)


def _normalize_selections(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise TypeError("selections must be an object")
    unknown = set(value) - {"includeComponentIds", "excludeComponentIds", "choices"}
    if unknown:
        raise ValueError("selections contains unsupported fields")
    includes = _sorted_unique_strings(
        value.get("includeComponentIds", []), field="includeComponentIds"
    )
    excludes = _sorted_unique_strings(
        value.get("excludeComponentIds", []), field="excludeComponentIds"
    )
    raw_choices = value.get("choices", {})
    if not isinstance(raw_choices, Mapping) or any(
        not isinstance(key, str) or not key or not isinstance(item, str) or not item
        for key, item in raw_choices.items()
    ):
        raise TypeError("choices must map non-empty choice IDs to component IDs")
    if set(includes) & set(excludes):
        raise ValueError("a component cannot be both included and excluded")
    return {
        "includeComponentIds": includes,
        "excludeComponentIds": excludes,
        "choices": {key: raw_choices[key] for key in sorted(raw_choices)},
    }


def _parse_semver(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.fullmatch(value)
    return tuple(map(int, match.groups())) if match else None


def _range_matches(version: Any, version_range: Any) -> bool:
    """Match the exact SemVer subset already used by component update plans."""

    parsed = _parse_semver(version)
    if parsed is None:
        return False
    if version_range in (None, "", "*"):
        return True
    if not isinstance(version_range, str):
        return False
    exact = _EXACT_RANGE_RE.fullmatch(version_range)
    if exact:
        return parsed == _parse_semver(exact.group(1))
    interval = _INTERVAL_RANGE_RE.fullmatch(version_range)
    if interval:
        lower = _parse_semver(interval.group(1))
        upper = _parse_semver(interval.group(2))
        return lower is not None and upper is not None and lower < upper and lower <= parsed < upper
    return False


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def _source_repository_matches(value: Any, repository: str) -> bool:
    return value == f"{_HTTPS_REPOSITORY_PREFIX}{repository}"


def _id_map(rows: Any, key: str) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        return {}
    return {
        row[key]: row for row in rows if isinstance(row, dict) and isinstance(row.get(key), str)
    }


def _virtual_v1_workload(
    catalog: Mapping[str, Any], workload_id: str, target_id: str
) -> dict[str, Any] | None:
    """Preserve old single-component resolution without changing strict v1 JSON."""

    component_ids = _id_map(catalog.get("components"), "componentId")
    if catalog.get("schemaVersion") != 1 or workload_id not in component_ids:
        return None
    return {
        "workloadId": workload_id,
        "hostTargets": [target_id],
        "requiredComponents": [workload_id],
        "recommendedComponents": [],
        "optionalComponents": [],
        "choiceGroups": [],
        "conflicts": [],
        "targetPreferences": [],
        "bindings": [],
        "sourcePolicy": None,
        "legacySingleComponent": True,
    }


def _find_workload(
    catalog: Mapping[str, Any], workload_id: str, target_id: str
) -> dict[str, Any] | None:
    workloads = catalog.get("workloads")
    if isinstance(workloads, list):
        matches = [
            row
            for row in workloads
            if isinstance(row, dict) and row.get("workloadId") == workload_id
        ]
        if len(matches) == 1:
            return matches[0]
        return None
    return _virtual_v1_workload(catalog, workload_id, target_id)


def _publisher_for_component(
    catalog: Mapping[str, Any], component: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Resolve a publisher by versioned identity, never repository alone."""

    repository = component.get("publisher")
    if not isinstance(repository, str):
        return None
    publisher_id = component.get("publisherId", repository)
    publishers = catalog.get("publishers")
    if not isinstance(publishers, list):
        return None
    matches = [
        row
        for row in publishers
        if isinstance(row, dict)
        and row.get("id", row.get("repository")) == publisher_id
        and row.get("repository") == repository
    ]
    return matches[0] if len(matches) == 1 else None


def _publisher_identity(
    catalog: Mapping[str, Any], component: Mapping[str, Any]
) -> dict[str, str] | None:
    """Return the exact catalog publisher authority for a selected component."""

    publisher = _publisher_for_component(catalog, component)
    repository = component.get("publisher")
    workflow = publisher.get("workflow") if isinstance(publisher, Mapping) else None
    publisher_id = publisher.get("id", repository) if isinstance(publisher, Mapping) else None
    tag_format = (
        publisher.get("tagFormat", "source-sha") if isinstance(publisher, Mapping) else None
    )
    if (
        not isinstance(publisher, Mapping)
        or not isinstance(repository, str)
        or not isinstance(publisher_id, str)
        or not isinstance(workflow, str)
        or tag_format not in {"source-sha", "component-source-sha", "component-version-source-sha"}
    ):
        return None
    return {
        "id": publisher_id,
        "repository": repository,
        "workflow": workflow,
        "tagFormat": tag_format,
    }


def _expected_release_tag(
    component: Mapping[str, Any],
    publisher: Mapping[str, Any],
    channel: Any,
    version: Any,
    source_commit: Any,
) -> tuple[str, str] | None:
    """Resolve one catalog-pinned tag format without accepting arbitrary patterns."""

    component_id = component.get("componentId")
    if (
        not isinstance(component_id, str)
        or channel not in {"stable", "preview"}
        or not isinstance(version, str)
        or not version
        or not isinstance(source_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", source_commit) is None
    ):
        return None
    tag_format = publisher.get("tagFormat", "source-sha")
    if tag_format not in {
        "source-sha",
        "component-source-sha",
        "component-version-source-sha",
    }:
        return None
    release_discovery = component.get("releaseDiscovery")
    prefixes = (
        release_discovery.get("tagPrefixes") if isinstance(release_discovery, Mapping) else None
    )
    pinned_prefix = prefixes.get(channel) if isinstance(prefixes, Mapping) else None
    component_prefix = f"{channel}-{component_id}-"
    if isinstance(prefixes, Mapping) and (
        prefixes.get("stable") != f"stable-{component_id}-"
        or prefixes.get("preview") != f"preview-{component_id}-"
    ):
        return None
    if tag_format == "source-sha":
        prefix = pinned_prefix if isinstance(pinned_prefix, str) else f"{channel}-"
        return tag_format, prefix + source_commit
    if isinstance(pinned_prefix, str) and pinned_prefix != component_prefix:
        return None
    if tag_format == "component-source-sha":
        return tag_format, component_prefix + source_commit
    return tag_format, f"{component_prefix}{version}-{source_commit}"


def _normalize_installed(value: Any) -> dict[str, dict[str, Any]]:
    """Accept a list or ID-keyed mapping of locally observed installed packages."""

    result: dict[str, dict[str, Any]] = {}
    if value is None:
        return result
    if isinstance(value, Mapping):
        rows: list[tuple[str | None, Any]] = [(str(key), item) for key, item in value.items()]
    elif isinstance(value, list):
        rows = [(None, item) for item in value]
    else:
        return result
    for keyed_id, item in rows:
        if isinstance(item, str):
            component_id = keyed_id
            record: dict[str, Any] = {"version": item}
        elif isinstance(item, Mapping):
            component_id = item.get("componentId", keyed_id)
            record = dict(item)
        else:
            continue
        if isinstance(component_id, str) and component_id:
            result[component_id] = record
    return result


def _index_envelopes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    indexes = value.get("indexes")
    return [item for item in indexes if isinstance(item, dict)] if isinstance(indexes, list) else []


def _target_compatible(
    catalog: Mapping[str, Any],
    host_target_id: str,
    component_target_id: str,
    *,
    legacy_host: bool = False,
) -> bool:
    targets = _id_map(catalog.get("targets"), "id")
    host = targets.get(host_target_id)
    component = targets.get(component_target_id)
    if host is None or component is None:
        # v1 catalogs may use the component target ID as the requested host target.
        selected = component if host is None else host
        selected_data = selected.get("target") if isinstance(selected, Mapping) else None
        component_data = component.get("target") if isinstance(component, Mapping) else None
        if not isinstance(selected_data, Mapping) or not isinstance(component_data, Mapping):
            return host_target_id == component_target_id
        keys = ("os", "osVersion", "distribution", "distributionVersion", "architecture", "abi")
        signature = {key: selected_data[key] for key in keys if key in selected_data}
        return bool(signature) and all(
            component_data.get(key) == value for key, value in signature.items()
        )
    host_data = host.get("target")
    component_data = component.get("target")
    if not isinstance(host_data, Mapping) or not isinstance(component_data, Mapping):
        return False
    if legacy_host:
        keys = ("os", "osVersion", "distribution", "distributionVersion", "architecture", "abi")
        signature = {key: host_data[key] for key in keys if key in host_data}
        return bool(signature) and all(
            component_data.get(key) == value for key, value in signature.items()
        )
    shared_dimensions = host_data.keys() & component_data.keys()
    return bool(shared_dimensions) and all(
        component_data[key] == host_data[key] for key in shared_dimensions
    )


def _component_target(
    catalog: Mapping[str, Any],
    component: Mapping[str, Any],
    workload: Mapping[str, Any],
    host_target_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    component_id = str(component.get("componentId", ""))
    preferred = None
    prefs = workload.get("targetPreferences")
    if isinstance(prefs, list):
        rows = [
            row for row in prefs if isinstance(row, dict) and row.get("componentId") == component_id
        ]
        if len(rows) > 1:
            return None, "ambiguous-target-preference"
        if rows:
            preferred = rows[0].get("targetId")
    targets = component.get("targets")
    if not isinstance(targets, list):
        return None, "component-has-no-targets"
    matches = [
        row
        for row in targets
        if isinstance(row, dict)
        and isinstance(row.get("targetId"), str)
        and (preferred is None or row.get("targetId") == preferred)
        and _target_compatible(
            catalog,
            host_target_id,
            row["targetId"],
            legacy_host=workload.get("legacySingleComponent") is True,
        )
    ]
    if len(matches) != 1:
        if preferred is not None and not matches:
            return None, "preferred-target-unavailable"
        return None, "component-target-ambiguous-or-incompatible"
    target = matches[0]
    if target.get("support") != "supported":
        return target, "component-target-unsupported"
    if preferred is None:
        host_targets = _id_map(catalog.get("targets"), "id")
        host = host_targets.get(host_target_id)
        if isinstance(host, dict) and host.get("hostSupport") != "supported":
            return target, "host-target-unsupported"
    return target, None


def _expected_artifact_kind(component: Mapping[str, Any], target: Mapping[str, Any]) -> str | None:
    artifact_kind = target.get("artifactKind")
    if isinstance(artifact_kind, str):
        return artifact_kind
    kinds = component.get("artifactKinds")
    if isinstance(kinds, list) and len(kinds) == 1 and isinstance(kinds[0], str):
        return kinds[0]
    return None


def _candidate_rows(
    trusted_release_index: Any,
    catalog: Mapping[str, Any],
    component: Mapping[str, Any],
    target_id: str,
    expected_artifact_kind: str | None,
    blockers: list[dict[str, Any]],
    *,
    requiredness: str,
) -> list[dict[str, Any]]:
    """Collect only candidates tied to an exact trusted index and publisher workflow."""

    component_id = component.get("componentId")
    publisher = _publisher_for_component(catalog, component)
    if publisher is None:
        blockers.append(
            _blocker(
                "PUBLISHER_IDENTITY_INVALID",
                component_id,
                requiredness,
                target_id,
                "Catalog component does not resolve to exactly one matching publisher identity.",
                details={
                    "publisherId": component.get("publisherId"),
                    "publisher": component.get("publisher"),
                },
            )
        )
        return []
    repository = component.get("publisher")
    workflow = publisher.get("workflow")
    candidates: list[dict[str, Any]] = []
    malformed_binding = False
    expected_channel = catalog.get("defaultChannel", "stable")
    for envelope in _index_envelopes(trusted_release_index):
        index = envelope.get("index")
        identity_fields = (
            envelope.get("repository"),
            envelope.get("assetName"),
            envelope.get("assetUri"),
            envelope.get("assetDigest"),
            envelope.get("indexDigest"),
            envelope.get("channel"),
            envelope.get("releaseTag"),
        )
        if not isinstance(index, Mapping) or not all(
            isinstance(value, str) and value for value in identity_fields
        ):
            continue
        if (
            envelope.get("assetName") != "component-release-index-v1.json"
            or not _valid_digest(envelope.get("assetDigest"))
            or not _valid_digest(envelope.get("indexDigest"))
            or envelope.get("indexDigest") != index.get("indexDigest")
            or envelope.get("repository") != repository
            or index.get("repository") != repository
            or envelope.get("channel") != expected_channel
            or index.get("channel") != envelope.get("channel")
        ):
            continue
        source = envelope.get("source")
        if (
            not isinstance(source, Mapping)
            or not _source_repository_matches(source.get("repository"), repository)
            or not isinstance(source.get("ref"), str)
            or not isinstance(source.get("commit"), str)
        ):
            malformed_binding = True
            continue
        index_attestation = envelope.get("attestationRef")
        if (
            not _attestation_matches(index_attestation, repository, workflow)
            or index_attestation.get("subjectName") != envelope.get("assetName")
            or index_attestation.get("subjectDigest") != envelope.get("assetDigest")
            or index_attestation.get("sourceCommit") != source.get("commit")
        ):
            malformed_binding = True
            continue
        manifests = envelope.get("manifests")
        releases = index.get("releases")
        if not isinstance(manifests, list) or not isinstance(releases, list):
            continue
        for wrapped in manifests:
            if not isinstance(wrapped, Mapping):
                continue
            manifest = wrapped.get("manifest")
            if not isinstance(manifest, Mapping):
                continue
            wrapped_component = wrapped.get("componentId", manifest.get("componentId"))
            version = wrapped.get("version", manifest.get("version"))
            manifest_digest = wrapped.get("manifestDigest", manifest.get("manifestDigest"))
            manifest_asset_digest = wrapped.get("manifestAssetDigest")
            manifest_uri = wrapped.get("manifestUri")
            if not _valid_digest(manifest_asset_digest):
                malformed_binding = True
                continue
            if (
                wrapped_component != component_id
                or not isinstance(version, str)
                or not _valid_digest(manifest_digest)
                or not isinstance(manifest_uri, str)
                or manifest.get("componentId") != component_id
                or manifest.get("version") != version
                or manifest.get("manifestDigest") != manifest_digest
            ):
                continue
            target = manifest.get("target")
            release = _match_index_release(
                releases, component_id, version, target, manifest_uri, manifest_digest
            )
            if release is None:
                malformed_binding = True
                continue
            if wrapped.get("releaseTag", envelope.get("releaseTag")) != envelope.get(
                "releaseTag"
            ) or manifest.get("releaseId", envelope.get("releaseTag")) != envelope.get(
                "releaseTag"
            ):
                malformed_binding = True
                continue
            release_tag = envelope["releaseTag"]
            channel = envelope["channel"]
            target_row = _id_map(component.get("targets"), "targetId").get(target_id)
            catalog_target = _id_map(catalog.get("targets"), "id").get(target_id)
            expected_target = (
                catalog_target.get("target") if isinstance(catalog_target, Mapping) else None
            )
            if (
                target_row is None
                or not isinstance(expected_target, Mapping)
                or target != expected_target
            ):
                continue
            artifact = manifest.get("artifact")
            if not isinstance(artifact, Mapping):
                continue
            artifact_kind = artifact.get("kind", expected_artifact_kind)
            if expected_artifact_kind is not None and artifact_kind != expected_artifact_kind:
                malformed_binding = True
                continue
            content_digest = _manifest_content_digest(manifest, artifact)
            artifact_digest = wrapped.get("artifactDigest")
            if (
                not _valid_digest(content_digest)
                or not _valid_digest(artifact_digest)
                or artifact_digest != content_digest
            ):
                malformed_binding = True
                continue
            if not _manifest_matches_catalog(component, manifest, artifact):
                malformed_binding = True
                continue
            attestation = wrapped.get("attestationRef", index_attestation)
            if (
                not _attestation_matches(attestation, repository, workflow)
                or attestation.get("subjectName") != manifest_uri.rsplit("/", 1)[-1]
                or attestation.get("subjectDigest") != manifest_asset_digest
            ):
                malformed_binding = True
                continue
            source = manifest.get("source")
            source_commit = source.get("commit") if isinstance(source, Mapping) else None
            if (
                not isinstance(source, Mapping)
                or not _source_repository_matches(source.get("repository"), repository)
                or not isinstance(source_commit, str)
                or attestation.get("sourceCommit") != source_commit
            ):
                malformed_binding = True
                continue
            tag_identity = _expected_release_tag(
                component, publisher, channel, version, source_commit
            )
            if tag_identity is None or release_tag != tag_identity[1]:
                malformed_binding = True
                continue
            publisher_id = publisher.get("id", repository)
            publisher_identity = {
                "id": publisher_id,
                "repository": repository,
                "workflow": workflow,
                "tagFormat": tag_identity[0],
            }
            index_identity = {
                "publisherIdentity": publisher_identity,
                "repository": repository,
                "assetName": envelope["assetName"],
                "assetUri": envelope["assetUri"],
                "assetDigest": envelope["assetDigest"],
                "indexDigest": envelope["indexDigest"],
                "channel": envelope["channel"],
                "releaseTag": envelope["releaseTag"],
            }
            candidates.append(
                {
                    "componentId": component_id,
                    "version": version,
                    "targetId": target_id,
                    "artifactKind": expected_artifact_kind or str(artifact_kind),
                    "releaseId": manifest.get("releaseId", envelope["releaseTag"]),
                    "manifestUri": manifest_uri,
                    "manifestDigest": manifest_digest,
                    "manifestAssetDigest": manifest_asset_digest,
                    "digest": content_digest,
                    "indexIdentity": index_identity,
                    "publisherIdentity": publisher_identity,
                    "attestationRef": copy.deepcopy(attestation),
                    "manifest": manifest,
                    "packageId": _package_identity(component, artifact)[0],
                    "capabilityId": _package_identity(component, artifact)[1],
                }
            )
    if malformed_binding:
        blockers.append(
            _blocker(
                "TRUSTED_INDEX_BINDING_INVALID",
                component_id,
                requiredness,
                target_id,
                "A candidate did not preserve the catalog publisher, index, manifest, target, artifact, or attestation binding.",
                details={"repository": repository, "workflow": workflow},
            )
        )
    return candidates


def _attestation_matches(value: Any, repository: Any, workflow: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("repository") == repository
        and value.get("workflow") == workflow
        and isinstance(value.get("sourceCommit"), str)
        and bool(value.get("sourceCommit"))
        and isinstance(value.get("subjectName"), str)
        and bool(value.get("subjectName"))
        and _valid_digest(value.get("subjectDigest"))
    )


def _match_index_release(
    releases: list[Any],
    component_id: Any,
    version: str,
    target: Any,
    manifest_uri: str,
    manifest_digest: str,
) -> Mapping[str, Any] | None:
    matches = [
        row
        for row in releases
        if isinstance(row, Mapping)
        and row.get("componentId") == component_id
        and row.get("version") == version
        and row.get("target") == target
        and row.get("manifestUri") == manifest_uri
        and row.get("manifestDigest") == manifest_digest
    ]
    return matches[0] if len(matches) == 1 else None


def _manifest_content_digest(manifest: Mapping[str, Any], artifact: Mapping[str, Any]) -> Any:
    if artifact.get("kind") == "plugin-package":
        archive = artifact.get("archive")
        artifact_digest = archive.get("sha256") if isinstance(archive, Mapping) else None
    else:
        artifact_digest = artifact.get("sha256", artifact.get("digest"))
    content_digest = manifest.get("contentDigest")
    if content_digest is not None and content_digest != artifact_digest:
        return None
    return artifact_digest


def _package_identity(
    component: Mapping[str, Any], artifact: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    mapping = component.get("pluginPackage")
    if isinstance(mapping, Mapping):
        package_id, capability_id = mapping.get("packageId"), mapping.get("capabilityId")
        if artifact.get("kind") == "plugin-package" and (
            artifact.get("packageId") != package_id or artifact.get("capabilityId") != capability_id
        ):
            return None, None
        return package_id if isinstance(package_id, str) else None, capability_id if isinstance(
            capability_id, str
        ) else None
    return None, None


def _manifest_matches_catalog(
    component: Mapping[str, Any], manifest: Mapping[str, Any], artifact: Mapping[str, Any]
) -> bool:
    expected_protocol = component.get("protocolVersion")
    if isinstance(expected_protocol, str) and manifest.get("protocolVersion") != expected_protocol:
        return False
    expected_restart = component.get("restart")
    if isinstance(expected_restart, Mapping) and manifest.get("restart") != expected_restart:
        return False
    expected_dependencies = component.get("dependencies")
    manifest_dependencies = manifest.get("dependencies")
    if (
        isinstance(expected_dependencies, list)
        and isinstance(manifest_dependencies, list)
        and expected_dependencies != manifest_dependencies
    ):
        return False
    mapping = component.get("pluginPackage")
    if isinstance(mapping, Mapping):
        return (
            artifact.get("kind") == "plugin-package"
            and artifact.get("packageId") == mapping.get("packageId")
            and artifact.get("capabilityId") == mapping.get("capabilityId")
            and artifact.get("interfaceVersion") == mapping.get("interfaceVersion")
        )
    return True


def _blocker(
    code: str,
    component_id: str | None,
    requiredness: str | None,
    target_id: str | None,
    message: str,
    *,
    capability_id: str | None = None,
    retryable: bool = False,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "componentId": component_id,
        "capabilityId": capability_id,
        "requiredness": requiredness,
        "targetId": target_id,
        "message": message,
        "retryable": retryable,
        "details": dict(details or {}),
    }


def _warning(
    code: str,
    component_id: str | None,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "componentId": component_id,
        "message": message,
        "details": dict(details or {}),
    }


def _version_key(value: str) -> tuple[int, int, int, str]:
    parsed = _parse_semver(value)
    return (*parsed, "") if parsed is not None else (0, 0, 0, value)


def _component_bindings(workload: Mapping[str, Any]) -> tuple[dict[str, str], set[str]]:
    rows = workload.get("bindings")
    if not isinstance(rows, list):
        return {}, set()
    found: dict[str, list[str]] = defaultdict(list)
    seen_ids: set[str] = set()
    invalid: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            invalid.add("<malformed>")
            continue
        component_id, binding_id = row.get("componentId"), row.get("bindingId")
        if (
            not isinstance(component_id, str)
            or not isinstance(binding_id, str)
            or _BINDING_ID_RE.fullmatch(binding_id) is None
        ):
            invalid.add(component_id if isinstance(component_id, str) else "<malformed>")
            continue
        if binding_id in seen_ids:
            invalid.add(component_id)
        seen_ids.add(binding_id)
        found[component_id].append(binding_id)
    invalid.update(
        component_id for component_id, binding_ids in found.items() if len(binding_ids) != 1
    )
    return (
        {component_id: ids[0] for component_id, ids in found.items() if len(ids) == 1},
        invalid,
    )


def _source_policy_is_valid(policy: Any) -> bool:
    if not isinstance(policy, Mapping):
        return False
    if set(policy) - {"mode", "productComponentIds", "productSources", "operations", "sourceId"}:
        return False
    operations = [
        "activate",
        "recover_binding",
        "deactivate",
        "runtime_status",
        "get_installation",
    ]
    if policy.get("operations") != operations:
        return False
    products = policy.get("productComponentIds")
    if not isinstance(products, list) or any(not isinstance(item, str) for item in products):
        return False
    if len(set(products)) != len(products):
        return False
    product_sources = policy.get("productSources")
    if not isinstance(product_sources, list):
        return False
    source_map: dict[str, str] = {}
    for row in product_sources:
        if not isinstance(row, Mapping):
            return False
        component_id = row.get("componentId")
        source_id = row.get("sourceId")
        if (
            set(row) != {"componentId", "sourceId"}
            or not isinstance(component_id, str)
            or not isinstance(source_id, str)
            or not re.fullmatch(r"[a-z][a-z0-9._-]{0,159}", source_id)
            or component_id in source_map
            or source_id in source_map.values()
        ):
            return False
        source_map[component_id] = source_id
    if set(source_map) != set(products):
        return False
    if policy.get("mode") == "actualProduct":
        return bool(products) and "sourceId" not in policy
    if policy.get("mode") == "standaloneOperator":
        return not products and policy.get("sourceId") == "cyrene-plugin-standalone-operator"
    return False


def _select_direct_components(
    workload: Mapping[str, Any],
    selections: Mapping[str, Any],
    components: Mapping[str, dict[str, Any]],
    blockers: list[dict[str, Any]],
) -> dict[str, tuple[str, str]]:
    """Return direct selections as componentId -> (requiredness, human reason)."""

    chosen: dict[str, tuple[str, str]] = {}
    workload_id = str(workload.get("workloadId", ""))
    required = set(workload.get("requiredComponents", []))
    recommended = set(workload.get("recommendedComponents", []))
    optional = set(workload.get("optionalComponents", []))
    groups = workload.get("choiceGroups", [])
    group_members: dict[str, set[str]] = {}
    if isinstance(groups, list):
        for group in groups:
            if isinstance(group, Mapping) and isinstance(group.get("choiceId"), str):
                member_ids = group.get("componentIds", [])
                if isinstance(member_ids, list):
                    group_members[group["choiceId"]] = set(member_ids)
    choice_components = set().union(*group_members.values()) if group_members else set()
    selectable = required | recommended | optional | choice_components
    includes = set(selections["includeComponentIds"])
    excludes = set(selections["excludeComponentIds"])
    choices = selections["choices"]
    minimum_explicit_optional = workload.get("minimumExplicitOptionalSelections", 0)
    if (
        not isinstance(minimum_explicit_optional, int)
        or isinstance(minimum_explicit_optional, bool)
        or minimum_explicit_optional < 0
    ):
        blockers.append(
            _blocker(
                "WORKLOAD_SELECTION_POLICY_INVALID",
                None,
                None,
                None,
                "minimumExplicitOptionalSelections must be a non-negative integer.",
            )
        )
    elif len(includes & optional) < minimum_explicit_optional:
        blockers.append(
            _blocker(
                "MINIMUM_SELECTION_NOT_MET",
                None,
                "optional",
                None,
                "Workload requires more explicitly selected optional components.",
                details={
                    "minimumExplicitOptionalSelections": minimum_explicit_optional,
                    "selectedOptionalComponentIds": sorted(includes & optional),
                },
            )
        )

    for component_id in sorted(selectable):
        if component_id not in components:
            blockers.append(
                _blocker(
                    "CATALOG_COMPONENT_MISSING",
                    component_id,
                    None,
                    None,
                    "Workload references a missing catalog component.",
                )
            )
    for component_id in sorted(includes - selectable):
        blockers.append(
            _blocker(
                "INVALID_SELECTION",
                component_id,
                None,
                None,
                "Included component is not a selectable member of this workload.",
            )
        )
    for component_id in sorted(excludes - (recommended | optional | choice_components)):
        if component_id in required:
            blockers.append(
                _blocker(
                    "REQUIRED_COMPONENT_UNSELECTABLE",
                    component_id,
                    "required",
                    None,
                    "A workload-required component cannot be explicitly excluded.",
                )
            )
        else:
            blockers.append(
                _blocker(
                    "INVALID_SELECTION",
                    component_id,
                    None,
                    None,
                    "Excluded component is not an excludable member of this workload.",
                )
            )

    for component_id in sorted(required):
        chosen[component_id] = ("required", f"Required by workload {workload_id}.")
    for component_id in sorted(recommended - excludes):
        chosen[component_id] = (
            "recommended",
            f"Recommended by workload {workload_id}; included by default.",
        )
    for component_id in sorted(optional & includes):
        chosen[component_id] = (
            "optional",
            f"Optional component explicitly selected for workload {workload_id}.",
        )
    for component_id in sorted((recommended | optional) & excludes):
        chosen.pop(component_id, None)

    for choice_id in sorted(choices):
        if choice_id not in group_members:
            blockers.append(
                _blocker(
                    "INVALID_CHOICE",
                    choices[choice_id],
                    "choice",
                    None,
                    "Choice refers to an unknown workload choice group.",
                    details={"choiceId": choice_id},
                )
            )
            continue
        component_id = choices[choice_id]
        if component_id not in group_members[choice_id]:
            blockers.append(
                _blocker(
                    "INVALID_CHOICE",
                    component_id,
                    "choice",
                    None,
                    "Choice is not a member of the selected choice group.",
                    details={"choiceId": choice_id},
                )
            )
            continue
        if component_id in excludes:
            blockers.append(
                _blocker(
                    "SELECTION_CONFLICT",
                    component_id,
                    "choice",
                    None,
                    "A workload choice cannot select a component that was explicitly excluded.",
                    details={"choiceId": choice_id},
                )
            )
            continue
        chosen[component_id] = (
            "choice",
            f"Selected for choice {choice_id} in workload {workload_id}.",
        )
    for choice_id, member_ids in sorted(group_members.items()):
        if choice_id not in choices:
            blockers.append(
                _blocker(
                    "CHOICE_REQUIRED",
                    None,
                    "choice",
                    None,
                    "A workload choice group requires one explicit selection.",
                    details={"choiceId": choice_id, "componentIds": sorted(member_ids)},
                )
            )
    for component_id in sorted(includes & recommended):
        if component_id not in excludes:
            chosen.setdefault(
                component_id,
                ("recommended", f"Recommended by workload {workload_id}; explicitly included."),
            )
    return chosen


def _resolve_candidate(
    candidates: list[dict[str, Any]],
    constraints: list[tuple[str | None, str | None]],
    installed: Mapping[str, Any],
    component_id: str,
) -> dict[str, Any] | None:
    valid = [
        candidate
        for candidate in candidates
        if all(
            _range_matches(candidate["version"], version_range) for version_range, _ in constraints
        )
    ]
    if not valid:
        return None
    installed_version = installed.get("version") if isinstance(installed, Mapping) else None
    installed_digest = installed.get("digest") if isinstance(installed, Mapping) else None
    installed_is_present = (
        isinstance(installed, Mapping) and installed.get("installed", True) is True
    )
    installed_match = [
        item
        for item in valid
        if item["version"] == installed_version
        and installed_is_present
        and isinstance(installed_digest, str)
        and installed_digest == item["digest"]
    ]
    if installed_match:
        valid = installed_match
    valid.sort(
        key=lambda item: (_version_key(item["version"]), item["manifestDigest"]), reverse=True
    )
    highest = valid[0]
    same_version = [item for item in valid if item["version"] == highest["version"]]
    identities = {
        (
            item["manifestDigest"],
            item["digest"],
            item["indexIdentity"]["repository"],
            item["indexIdentity"]["indexDigest"],
        )
        for item in same_version
    }
    if len(identities) > 1:
        return None
    return highest


@dataclass(frozen=True)
class Resolution:
    """Canonical structured workload resolution with stable digest properties."""

    _payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._payload)


def _finalize_resolution(
    *,
    action: str,
    catalog_digest: str,
    workload_id: str,
    target_id: str,
    selection_binding: Mapping[str, Any],
    selected_components: list[dict[str, Any]],
    closure_reasons: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
) -> Resolution:
    """Sort all structured output and bind the action and evidence into plan IDs."""

    blockers.sort(
        key=lambda row: (
            str(row.get("code")),
            str(row.get("componentId") or ""),
            str(row.get("targetId") or ""),
            _canonical_bytes(row.get("details", {})),
        )
    )
    warnings.sort(
        key=lambda row: (
            str(row.get("code")),
            str(row.get("componentId") or ""),
            _canonical_bytes(row.get("details", {})),
        )
    )
    selected_components.sort(key=lambda row: str(row.get("componentId", "")))
    closure_reasons.sort(
        key=lambda row: (
            row["componentId"],
            row["depth"],
            row["rootComponentId"] or "",
            row["fromComponentId"] or "",
            row["reasonCode"],
        )
    )
    selection = copy.deepcopy(dict(selection_binding))
    plan_material = {
        "schemaVersion": 1,
        "action": action,
        "catalogDigest": catalog_digest,
        "workloadId": workload_id,
        "targetId": target_id,
        "selectionBinding": selection,
        "selectedComponents": selected_components,
        "closureReasons": closure_reasons,
        "warnings": warnings,
        "blockers": blockers,
    }
    plan_digest = "sha256:" + hashlib.sha256(_canonical_bytes(plan_material)).hexdigest()
    plan_id = "plan-" + plan_digest.removeprefix("sha256:")[:32]
    return Resolution(
        {
            "schemaVersion": 1,
            "status": "blocked" if blockers else "ready",
            "action": action,
            "catalogDigest": catalog_digest,
            "workloadId": workload_id,
            "targetId": target_id,
            "selectedComponents": selected_components,
            "closureReasons": closure_reasons,
            "warnings": warnings,
            "blockers": blockers,
            "selectionBinding": selection,
            "planDigestMaterial": plan_material,
            "planDigest": plan_digest,
            "planId": plan_id,
        }
    )


def _workload_membership_requiredness(workload: Mapping[str, Any], component_id: str) -> str | None:
    """Return direct workload selection category for one component identity."""

    for key, requiredness in (
        ("requiredComponents", "required"),
        ("recommendedComponents", "recommended"),
        ("optionalComponents", "optional"),
    ):
        members = workload.get(key)
        if isinstance(members, list) and component_id in members:
            return requiredness
    groups = workload.get("choiceGroups")
    if isinstance(groups, list) and any(
        isinstance(group, Mapping)
        and isinstance(group.get("componentIds"), list)
        and component_id in group["componentIds"]
        for group in groups
    ):
        return "choice"
    return None


def _installed_identity_projection(
    record: Any, *, include_installation_id: bool = True
) -> dict[str, Any] | None:
    """Keep only stable local installation identity fields in a plan digest."""

    if not isinstance(record, Mapping):
        return None
    fields = (
        "componentId",
        "installed",
        "version",
        "releaseId",
        "manifestUri",
        "manifestDigest",
        "manifestAssetDigest",
        "digest",
        "targetId",
        "releaseIdentity",
        "releasePath",
        "archivePath",
        "pointerIdentity",
        "bundleIdentity",
        "imageDigest",
        "imageReference",
        "receiptPath",
        "receiptRef",
        "deploymentRef",
        "indexIdentity",
        "deploymentIdentity",
        "imageIdentity",
        "receiptIdentity",
    )
    if include_installation_id:
        fields = (*fields, "installationId")
    identity: dict[str, Any] = {}
    for field in fields:
        value = record.get(field)
        if isinstance(value, (str, bool)):
            identity[field] = value
        elif field in {
            "indexIdentity",
            "deploymentIdentity",
            "imageIdentity",
            "receiptIdentity",
        } and isinstance(value, Mapping):
            identity[field] = copy.deepcopy(dict(value))
    return identity or None


def _resolve_uninstall_component(
    *,
    catalog: Mapping[str, Any],
    workload: Mapping[str, Any],
    components: Mapping[str, dict[str, Any]],
    component_bindings: Mapping[str, str],
    normalized_selections: Mapping[str, Any],
    installed: Mapping[str, dict[str, Any]],
    invalid_bindings: set[str],
    host_target_id: str,
    blockers: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve exactly one locally installed component without release discovery."""

    includes = normalized_selections.get("includeComponentIds", [])
    excludes = normalized_selections.get("excludeComponentIds", [])
    choices = normalized_selections.get("choices", {})
    if len(includes) != 1 or excludes or choices:
        blockers.append(
            _blocker(
                "UNINSTALL_SELECTION_INVALID",
                None,
                None,
                host_target_id,
                "Uninstall requires exactly one included workload component and no excludes or choices.",
            )
        )
        return [], []
    component_id = includes[0]
    requiredness = _workload_membership_requiredness(workload, component_id)
    if requiredness is None:
        blockers.append(
            _blocker(
                "COMPONENT_NOT_IN_WORKLOAD",
                component_id,
                None,
                host_target_id,
                "Only a direct component member of this workload can be uninstalled by this plan.",
            )
        )
        return [], []
    component = components.get(component_id)
    if component is None:
        blockers.append(
            _blocker(
                "COMPONENT_NOT_FOUND",
                component_id,
                requiredness,
                host_target_id,
                "Selected component does not exist in the active catalog.",
            )
        )
        return [], []

    record = installed.get(component_id)
    identity_fields = (
        "version",
        "releaseId",
        "manifestDigest",
        "manifestAssetDigest",
        "digest",
        "targetId",
    )
    if (
        not isinstance(record, Mapping)
        or record.get("installed") is not True
        or any(
            not isinstance(record.get(key), str) or not record.get(key) for key in identity_fields
        )
        or not _valid_digest(record.get("manifestDigest"))
        or not _valid_digest(record.get("manifestAssetDigest"))
        or not _valid_digest(record.get("digest"))
    ):
        blockers.append(
            _blocker(
                "INSTALLED_IDENTITY_UNAVAILABLE",
                component_id,
                requiredness,
                host_target_id,
                "Uninstall requires a locally verified release, manifest, version, target, and content digest.",
            )
        )
        return [], []

    installed_target_id = record["targetId"]
    target_rows = component.get("targets")
    matching_targets = (
        [
            row
            for row in target_rows
            if isinstance(row, Mapping) and row.get("targetId") == installed_target_id
        ]
        if isinstance(target_rows, list)
        else []
    )
    target_catalog = _id_map(catalog.get("targets"), "id").get(installed_target_id)
    if (
        len(matching_targets) != 1
        or not isinstance(target_catalog, Mapping)
        or not _target_compatible(catalog, host_target_id, installed_target_id)
    ):
        blockers.append(
            _blocker(
                "INSTALLED_TARGET_UNAVAILABLE",
                component_id,
                requiredness,
                installed_target_id,
                "Installed component target is not represented by the active catalog for this host.",
                details={"hostTargetId": host_target_id},
            )
        )
        return [], []

    for dependent_id, dependent in components.items():
        if dependent_id == component_id:
            continue
        dependent_record = installed.get(dependent_id)
        if (
            not isinstance(dependent_record, Mapping)
            or dependent_record.get("installed") is not True
        ):
            continue
        dependencies = dependent.get("dependencies")
        if not isinstance(dependencies, list):
            continue
        for dependency in dependencies:
            if not isinstance(dependency, Mapping) or dependency.get("componentId") != component_id:
                continue
            if _range_matches(record.get("version"), dependency.get("versionRange")):
                blockers.append(
                    _blocker(
                        "INSTALLED_DEPENDENT",
                        component_id,
                        requiredness,
                        installed_target_id,
                        "Another installed component still depends on this installation.",
                        details={
                            "dependentComponentId": dependent_id,
                            "dependentInstallationId": dependent_record.get("installationId"),
                        },
                    )
                )

    target_row = matching_targets[0]
    artifact_kind = target_row.get("artifactKind")
    if artifact_kind not in component.get("artifactKinds", []):
        blockers.append(
            _blocker(
                "INSTALLED_TARGET_UNAVAILABLE",
                component_id,
                requiredness,
                installed_target_id,
                "Installed component artifact kind is not admitted by its catalog row.",
            )
        )
        return [], []

    requires_package_installation_id = artifact_kind == "plugin-package"
    installation_id = record.get("installationId")
    if requires_package_installation_id and (
        not isinstance(installation_id, str) or not installation_id
    ):
        blockers.append(
            _blocker(
                "INSTALLED_IDENTITY_UNAVAILABLE",
                component_id,
                requiredness,
                installed_target_id,
                "Package Runtime uninstall requires the verified package installation ID.",
            )
        )
        return [], []

    is_plugin = requires_package_installation_id and isinstance(
        component.get("pluginPackage"), Mapping
    )
    publisher_identity = _publisher_identity(catalog, component)
    if publisher_identity is None:
        blockers.append(
            _blocker(
                "PUBLISHER_IDENTITY_INVALID",
                component_id,
                requiredness,
                installed_target_id,
                "Installed component does not resolve to exactly one catalog publisher identity.",
            )
        )
    binding_id = component_bindings.get(component_id) if is_plugin else None
    source_policy = copy.deepcopy(workload.get("sourcePolicy")) if is_plugin else None
    if is_plugin and (
        not binding_id
        or component_id in invalid_bindings
        or "<malformed>" in invalid_bindings
        or not _source_policy_is_valid(source_policy)
    ):
        blockers.append(
            _blocker(
                "SOURCE_BINDING_INVALID",
                component_id,
                requiredness,
                installed_target_id,
                "Uninstall of a plugin requires its explicit workload source policy and binding ID.",
            )
        )

    installed_identity = _installed_identity_projection(
        record, include_installation_id=requires_package_installation_id
    )
    row = {
        "componentId": component_id,
        "version": record["version"],
        "targetId": installed_target_id,
        "artifactKind": artifact_kind,
        "releaseId": record["releaseId"],
        "manifestUri": record.get("manifestUri"),
        "manifestDigest": record["manifestDigest"],
        "manifestAssetDigest": record["manifestAssetDigest"],
        "digest": record["digest"],
        "indexIdentity": copy.deepcopy(record.get("indexIdentity")),
        "reason": "Explicit uninstall request.",
        "requiredness": requiredness,
        "sourcePolicy": source_policy,
        "bindingId": binding_id,
        "attestationRef": copy.deepcopy(record.get("attestationRef")),
        "publisherIdentity": publisher_identity,
        "installed": True,
        "installationId": installation_id if requires_package_installation_id else None,
        "installedIdentity": installed_identity,
        "capabilityId": (
            component["pluginPackage"].get("capabilityId")
            if is_plugin and isinstance(component.get("pluginPackage"), Mapping)
            else None
        ),
        "packageId": (
            component["pluginPackage"].get("packageId")
            if is_plugin and isinstance(component.get("pluginPackage"), Mapping)
            else None
        ),
    }
    closure = [
        {
            "componentId": component_id,
            "reasonCode": "explicit-uninstall",
            "fromComponentId": None,
            "rootComponentId": component_id,
            "versionRange": None,
            "depth": 0,
        }
    ]
    return [row], closure


# Public convenience for assembly fetchers: no other catalog components should be fetched.
def potential_component_ids(catalog: Mapping[str, Any], workload_id: str) -> tuple[str, ...]:
    """Return workload members plus transitive runtime dependencies, sorted uniquely."""

    if not isinstance(catalog, Mapping):
        return ()
    workloads = catalog.get("workloads")
    workload = None
    if isinstance(workloads, list):
        rows = [
            row
            for row in workloads
            if isinstance(row, Mapping) and row.get("workloadId") == workload_id
        ]
        workload = rows[0] if len(rows) == 1 else None
    if workload is None:
        workload = _virtual_v1_workload(catalog, workload_id, workload_id)
    if workload is None:
        return ()
    components = _id_map(catalog.get("components"), "componentId")
    roots: set[str] = set()
    for key in ("requiredComponents", "recommendedComponents", "optionalComponents"):
        values = workload.get(key)
        if isinstance(values, list):
            roots.update(item for item in values if isinstance(item, str))
    groups = workload.get("choiceGroups")
    if isinstance(groups, list):
        for group in groups:
            if isinstance(group, Mapping) and isinstance(group.get("componentIds"), list):
                roots.update(item for item in group["componentIds"] if isinstance(item, str))
    visited: set[str] = set()
    pending = list(roots)
    while pending:
        component_id = pending.pop()
        if component_id in visited:
            continue
        visited.add(component_id)
        component = components.get(component_id)
        if component is None:
            continue
        dependencies = component.get("dependencies")
        if not isinstance(dependencies, list):
            continue
        for dependency in dependencies:
            if not isinstance(dependency, Mapping) or not isinstance(
                dependency.get("componentId"), str
            ):
                continue
            dependency_id = dependency["componentId"]
            target = components.get(dependency_id)
            if isinstance(target, Mapping) and target.get("role") == "build-dependency":
                continue
            pending.append(dependency_id)
    return tuple(sorted(visited))


def resolve_workload(
    catalog: Mapping[str, Any],
    catalog_digest: str,
    workload_id: str,
    target_id: str,
    selections: Mapping[str, Any] | None,
    installed_components: Any,
    trusted_release_index: Any,
    action: str = "install",
) -> Resolution:
    """Resolve an explicit workload selection and bind every output to trusted inputs.

    ``trusted_release_index`` is a verified envelope with ``indexes`` containing
    exact index asset identity, attested index content, verified manifests, and
    manifest attestation references. This function checks internal identities
    again; it cannot establish cryptographic trust on its own.
    """

    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if action not in ("install", "uninstall"):
        blockers.append(
            _blocker(
                "INVALID_ACTION",
                None,
                None,
                target_id if isinstance(target_id, str) else None,
                "action must be exactly 'install' or 'uninstall'.",
            )
        )
        action = ""
    if not isinstance(catalog, Mapping):
        catalog = {}
    if not _valid_digest(catalog_digest):
        blockers.append(
            _blocker(
                "INVALID_CATALOG_DIGEST",
                None,
                None,
                target_id if isinstance(target_id, str) else None,
                "Catalog digest must be a lowercase SHA-256 identity.",
            )
        )
        catalog_digest = catalog_digest if isinstance(catalog_digest, str) else ""
    if not isinstance(workload_id, str) or not workload_id:
        workload_id = ""
        blockers.append(
            _blocker(
                "INVALID_WORKLOAD_ID", None, None, None, "workload_id must be a non-empty string."
            )
        )
    if not isinstance(target_id, str) or not target_id:
        target_id = ""
        blockers.append(
            _blocker("INVALID_TARGET_ID", None, None, None, "target_id must be a non-empty string.")
        )
    try:
        normalized_selections = _normalize_selections(selections)
    except (TypeError, ValueError) as error:
        normalized_selections = {
            "includeComponentIds": [],
            "excludeComponentIds": [],
            "choices": {},
        }
        blockers.append(_blocker("INVALID_SELECTION", None, None, target_id or None, str(error)))
    installed = _normalize_installed(installed_components)

    workload = _find_workload(catalog, workload_id, target_id)
    if workload is None:
        blockers.append(
            _blocker(
                "WORKLOAD_NOT_FOUND",
                None,
                None,
                target_id or None,
                "Catalog has no uniquely matching workload.",
            )
        )
        workload = {
            "workloadId": workload_id,
            "hostTargets": [],
            "requiredComponents": [],
            "recommendedComponents": [],
            "optionalComponents": [],
            "choiceGroups": [],
            "conflicts": [],
            "targetPreferences": [],
            "bindings": [],
            "sourcePolicy": None,
        }
    host_targets = workload.get("hostTargets")
    if not isinstance(host_targets, list) or target_id not in host_targets:
        blockers.append(
            _blocker(
                "UNSUPPORTED_WORKLOAD_TARGET",
                None,
                None,
                target_id or None,
                "Workload does not support the requested host target.",
            )
        )
    components = _id_map(catalog.get("components"), "componentId")
    if action == "uninstall":
        component_bindings, invalid_bindings = _component_bindings(workload)
        selected_components, closure_reasons = _resolve_uninstall_component(
            catalog=catalog,
            workload=workload,
            components=components,
            component_bindings=component_bindings,
            invalid_bindings=invalid_bindings,
            normalized_selections=normalized_selections,
            installed=installed,
            host_target_id=target_id,
            blockers=blockers,
        )
        return _finalize_resolution(
            action=action,
            catalog_digest=catalog_digest,
            workload_id=workload_id,
            target_id=target_id,
            selection_binding=normalized_selections,
            selected_components=selected_components,
            closure_reasons=closure_reasons,
            warnings=warnings,
            blockers=blockers,
        )
    if action != "install":
        return _finalize_resolution(
            action=action,
            catalog_digest=catalog_digest,
            workload_id=workload_id,
            target_id=target_id,
            selection_binding=normalized_selections,
            selected_components=[],
            closure_reasons=[],
            warnings=warnings,
            blockers=blockers,
        )
    direct = _select_direct_components(workload, normalized_selections, components, blockers)
    selected_roots = set(direct)

    # Gather the dependency graph and all version ranges before selecting any release.
    constraints: dict[str, list[tuple[str | None, str | None]]] = defaultdict(list)
    incoming: dict[str, list[tuple[str, str, str | None, int]]] = defaultdict(list)
    roots_for: dict[str, set[str]] = defaultdict(set)
    direct_meta: dict[str, tuple[str, str]] = direct.copy()
    visiting: list[str] = []

    def visit(component_id: str, root_id: str, depth: int, visited_for_root: set[str]) -> None:
        component = components.get(component_id)
        if component is None:
            return
        roots_for[component_id].add(root_id)
        if component_id in visiting:
            cycle = visiting[visiting.index(component_id) :] + [component_id]
            blockers.append(
                _blocker(
                    "DEPENDENCY_CYCLE",
                    component_id,
                    "dependency",
                    target_id or None,
                    "Catalog dependency graph contains a cycle.",
                    details={"cycle": cycle},
                )
            )
            return
        if component_id in visited_for_root:
            return
        visited_for_root.add(component_id)
        visiting.append(component_id)
        dependencies = component.get("dependencies", [])
        if not isinstance(dependencies, list):
            dependencies = []
        for dependency in dependencies:
            if not isinstance(dependency, Mapping):
                blockers.append(
                    _blocker(
                        "INVALID_DEPENDENCY",
                        component_id,
                        None,
                        target_id or None,
                        "Catalog component dependency is malformed.",
                    )
                )
                continue
            dependency_id = dependency.get("componentId")
            version_range = dependency.get("versionRange")
            if not isinstance(dependency_id, str) or dependency_id not in components:
                blockers.append(
                    _blocker(
                        "MISSING_DEPENDENCY",
                        dependency_id if isinstance(dependency_id, str) else component_id,
                        "dependency",
                        target_id or None,
                        "Component dependency does not exist in the catalog.",
                        details={"fromComponentId": component_id},
                    )
                )
                continue
            dependency_component = components[dependency_id]
            if dependency_component.get("role") == "build-dependency":
                continue
            if version_range is not None and not (
                _EXACT_RANGE_RE.fullmatch(version_range)
                or _INTERVAL_RANGE_RE.fullmatch(version_range)
            ):
                blockers.append(
                    _blocker(
                        "VERSION_RANGE_UNSUPPORTED",
                        dependency_id,
                        "dependency",
                        target_id or None,
                        "Catalog dependency uses an unsupported version range.",
                        details={"fromComponentId": component_id, "versionRange": version_range},
                    )
                )
                continue
            incoming[dependency_id].append((component_id, root_id, version_range, depth + 1))
            constraints[dependency_id].append((version_range, component_id))
            visit(dependency_id, root_id, depth + 1, visited_for_root)
        visiting.pop()

    for component_id in sorted(selected_roots):
        if component_id in components:
            roots_for[component_id].add(component_id)
            visit(component_id, component_id, 0, set())

    excluded_in_closure = set(normalized_selections["excludeComponentIds"]) & set(roots_for)
    for component_id in sorted(excluded_in_closure):
        if component_id not in direct_meta:
            blockers.append(
                _blocker(
                    "REQUIRED_COMPONENT_UNSELECTABLE",
                    component_id,
                    "dependency",
                    target_id or None,
                    "An explicitly excluded component is still required by the selected dependency closure.",
                )
            )

    closure_reasons: list[dict[str, Any]] = []
    for component_id, (requiredness, _) in sorted(direct_meta.items()):
        closure_reasons.append(
            {
                "componentId": component_id,
                "reasonCode": f"workload-{requiredness}",
                "fromComponentId": None,
                "rootComponentId": component_id,
                "versionRange": None,
                "depth": 0,
            }
        )
    for component_id in sorted(components):
        if component_id not in roots_for:
            continue
        for parent_id, root_id, version_range, depth in sorted(incoming.get(component_id, [])):
            closure_reasons.append(
                {
                    "componentId": component_id,
                    "reasonCode": "dependency",
                    "fromComponentId": parent_id,
                    "rootComponentId": root_id,
                    "versionRange": version_range,
                    "depth": depth,
                }
            )
    # Check component conflicts against both the computed closure and installed state.
    conflicts = workload.get("conflicts", [])
    if isinstance(conflicts, list):
        for conflict in conflicts:
            if not isinstance(conflict, Mapping) or not isinstance(
                conflict.get("componentIds"), list
            ):
                continue
            conflict_ids = conflict["componentIds"]
            if len(conflict_ids) != 2:
                continue
            present = set(roots_for) | (set(installed) & set(conflict_ids))
            if set(conflict_ids).issubset(present):
                blockers.append(
                    _blocker(
                        "COMPONENT_CONFLICT",
                        None,
                        None,
                        target_id or None,
                        str(conflict.get("reason", "Selected components conflict.")),
                        details={"componentIds": sorted(conflict_ids)},
                    )
                )

    # Select per-component target and trusted release, accumulating exact provenance.
    selected_components: list[dict[str, Any]] = []
    target_for: dict[str, str] = {}
    requiredness_for: dict[str, str] = {
        key: "dependency" if incoming.get(key) else val[0] for key, val in direct_meta.items()
    }
    component_bindings, invalid_bindings = _component_bindings(workload)
    for component_id in sorted(invalid_bindings):
        blockers.append(
            _blocker(
                "SOURCE_BINDING_INVALID",
                None if component_id == "<malformed>" else component_id,
                None,
                target_id or None,
                "Catalog workload has a duplicate or malformed explicit component binding.",
            )
        )
    for component_id in sorted(roots_for):
        component = components.get(component_id)
        if component is None:
            continue
        requiredness = requiredness_for.get(component_id, "dependency")
        component_target, target_error = _component_target(catalog, component, workload, target_id)
        chosen_target_id = (
            component_target.get("targetId") if isinstance(component_target, Mapping) else None
        )
        target_for[component_id] = chosen_target_id if isinstance(chosen_target_id, str) else ""
        if target_error:
            code = "UNSUPPORTED_TARGET" if "unsupported" in target_error else "TARGET_UNAVAILABLE"
            blockers.append(
                _blocker(
                    code,
                    component_id,
                    requiredness,
                    chosen_target_id or target_id or None,
                    "Catalog does not provide one supported component target compatible with the workload host.",
                    details={"reason": target_error, "hostTargetId": target_id},
                )
            )
            continue
        artifact_kind = _expected_artifact_kind(component, component_target)
        candidates = _candidate_rows(
            trusted_release_index,
            catalog,
            component,
            chosen_target_id,
            artifact_kind,
            blockers,
            requiredness=requiredness,
        )
        component_constraints = constraints.get(component_id, [])
        if component_id in selected_roots:
            component_constraints = [(None, None), *component_constraints]
        candidate = _resolve_candidate(
            candidates, component_constraints, installed.get(component_id, {}), component_id
        )
        if candidate is None:
            package = component.get("pluginPackage")
            capability = package.get("capabilityId") if isinstance(package, Mapping) else None
            available = bool(candidates)
            conflicting_ranges = [item[0] for item in component_constraints if item[0] is not None]
            if available:
                blockers.append(
                    _blocker(
                        "VERSION_CONFLICT",
                        component_id,
                        requiredness,
                        chosen_target_id,
                        "No trusted candidate version satisfies every dependency constraint.",
                        capability_id=capability if isinstance(capability, str) else None,
                        details={"versionRanges": conflicting_ranges},
                    )
                )
            elif isinstance(capability, str):
                blockers.append(
                    _blocker(
                        "MISSING_CAPABILITY",
                        component_id,
                        requiredness,
                        chosen_target_id,
                        "No trusted release index binds the required package capability for this component and target.",
                        capability_id=capability,
                        retryable=True,
                        details={
                            "packageId": package.get("packageId"),
                            "publisherId": component.get("publisherId", component.get("publisher")),
                        },
                    )
                )
            else:
                blockers.append(
                    _blocker(
                        "MISSING_RELEASE",
                        component_id,
                        requiredness,
                        chosen_target_id,
                        "No trusted release index binds this component and target.",
                        retryable=True,
                        details={
                            "publisherId": component.get("publisherId", component.get("publisher"))
                        },
                    )
                )
            continue
        installed_record = installed.get(component_id, {})
        is_installed = (
            isinstance(installed_record, Mapping)
            and installed_record.get("installed", True) is True
            and installed_record.get("version") == candidate["version"]
            and installed_record.get("digest") == candidate["digest"]
        )
        is_plugin = isinstance(component.get("pluginPackage"), Mapping)
        binding_id = component_bindings.get(component_id) if is_plugin else None
        if is_plugin and not binding_id:
            blockers.append(
                _blocker(
                    "SOURCE_BINDING_MISSING",
                    component_id,
                    requiredness,
                    chosen_target_id,
                    "Plugin package has no explicit catalog workload binding ID.",
                )
            )
        source_policy = copy.deepcopy(workload.get("sourcePolicy")) if is_plugin else None
        if not isinstance(source_policy, Mapping):
            source_policy = None
        if is_plugin and not _source_policy_is_valid(source_policy):
            blockers.append(
                _blocker(
                    "SOURCE_POLICY_INVALID",
                    component_id,
                    requiredness,
                    chosen_target_id,
                    "Selected plugin workload has an unsupported or incomplete catalog source policy.",
                )
            )
        selected_components.append(
            {
                "componentId": component_id,
                "version": candidate["version"],
                "targetId": chosen_target_id,
                "artifactKind": candidate["artifactKind"],
                "releaseId": candidate["releaseId"],
                "manifestUri": candidate["manifestUri"],
                "manifestDigest": candidate["manifestDigest"],
                "manifestAssetDigest": candidate["manifestAssetDigest"],
                "digest": candidate["digest"],
                "indexIdentity": candidate["indexIdentity"],
                "publisherIdentity": candidate["publisherIdentity"],
                "reason": direct_meta.get(component_id, ("dependency", ""))[1]
                or f"Dependency closure of {component_id}.",
                "requiredness": requiredness,
                "sourcePolicy": source_policy,
                "bindingId": binding_id,
                "attestationRef": candidate["attestationRef"],
                "installed": bool(is_installed),
                "installationId": (
                    installed_record.get("installationId")
                    if is_installed
                    and is_plugin
                    and isinstance(installed_record.get("installationId"), str)
                    else None
                ),
                "installedIdentity": (
                    _installed_identity_projection(
                        installed_record, include_installation_id=is_plugin
                    )
                    if isinstance(installed_record, Mapping)
                    and installed_record.get("installed", True) is True
                    else None
                ),
                "capabilityId": candidate["capabilityId"],
                "packageId": candidate["packageId"],
            }
        )

    return _finalize_resolution(
        action=action,
        catalog_digest=catalog_digest,
        workload_id=workload_id,
        target_id=target_id,
        selection_binding=normalized_selections,
        selected_components=selected_components,
        closure_reasons=closure_reasons,
        warnings=warnings,
        blockers=blockers,
    )


__all__ = ["Resolution", "potential_component_ids", "resolve_workload"]
