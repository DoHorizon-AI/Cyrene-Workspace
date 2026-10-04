#!/usr/bin/env python3
"""
Native installer release assembly and verification helpers.

This module records exact, independently verified component inputs, assembles
the Ubuntu 22.04/24.04 DEB release envelope, verifies its detached SLSA bundles,
and publishes one immutable GitHub release. It does not resolve latest releases.
模块职责：记录已验证的精确组件输入，组装并校验双 Ubuntu DEB 发布资产。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

REPOSITORY = "DoHorizon-AI/Cyrene-Workspace"
WORKFLOW_PATH = ".github/workflows/native-installer-release.yml"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
CATALOG_WORKFLOW = f"{REPOSITORY}/.github/workflows/component-catalog-release.yml"
COMPONENT_WORKFLOW = ".github/workflows/component-release.yml"
CHANNEL_REFS = {
    "preview": {"refs/heads/develop"},
    "stable": {"refs/heads/main", "refs/heads/release"},
}
PROFILE_IDS = (
    "linux-ubuntu-22.04-x86_64-python-3.12",
    "linux-ubuntu-24.04-x86_64-python-3.12",
)
PROFILE_UBUNTU = {
    PROFILE_IDS[0]: "22.04",
    PROFILE_IDS[1]: "24.04",
}
PLATFORM_TARGET_IDS = {
    "22.04": "linux-ubuntu-22.04-x86_64-systemd",
    "24.04": "linux-ubuntu-24.04-x86_64-systemd",
}
PLATFORM_CORE_COMPONENTS = (
    "cyrene-kernel",
    "cyrene-sandboxd",
    "cyrene-linux-sys-adapter",
    "cyrene-nvidia-adapter",
    "cyrene-runtime-maintenance",
)
SDK_COMPONENT = "cyrene-runtime-maintenance-sdk"
SDK_TARGET = "linux-ubuntu-24.04-x86_64-python-3.12-library"
PRODUCTS = {
    "catalyst": ("DoHorizon-AI/Cyrene-Catalyst", "cyrene-catalyst"),
    "exchange": ("DoHorizon-AI/Cyrene-Exchange", "cyrene-exchange"),
    "navigator": ("DoHorizon-AI/Cyrene-Navigator", "cyrene-navigator"),
    "reactor": ("DoHorizon-AI/Cyrene-Reactor", "cyrene-reactor"),
    "yield": ("DoHorizon-AI/Cyrene-Yield", "cyrene-yield"),
}
SHA1_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
CHANNEL_TAG_PATTERN = re.compile(r"(stable|preview)-([0-9a-f]{40})\Z")
CATALOG_TAG_PATTERN = re.compile(r"catalog-(stable|preview)-([0-9a-f]{40})\Z")
ASSET_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}\Z")
IMMUTABLE_SETTINGS_READ_TOKEN = "CYRENE_IMMUTABLE_RELEASES_READ_TOKEN"
RELEASE_API_BASE = f"https://api.github.com/repos/{REPOSITORY}"
UPLOAD_HOST = "uploads.github.com"
RELEASE_SUBJECT_NAMES = (
    "native-installer-release-v1.json",
    "native-installer-source-receipt-v1.json",
    "python-runtime.lock.json",
    "release-lock.json",
    "SHA256SUMS",
)
SERVICE_ARTIFACT_INDEX_PATH = "usr/share/cyrene/service-artifacts/index.json"
INSTALL_CONTRACT_PATH = "usr/share/cyrene/native-install-contract-v1.json"
INSTALL_CONTRACT_POLICY = {
    "schemaVersion": 1,
    "initializationMode": "stage-only",
    "serviceArtifactsMode": "verified-published-bytes",
    "serviceActivation": "deferred",
    "brokerAction": "preserve-existing",
    "oldRuntimeAction": "preserve",
}
MAINTAINER_SCRIPT_NAMES = ("postinst", "prerm", "postrm")
LOCKED_UV_EXECUTABLE_SHA256 = "e8a4e7b4fd6283892fccfc4f335fb16cdf01064bb135172e4e2e73b478eb2076"
LOCKED_UV_ARCHIVE_SHA256 = "23f02075b652bb1df64178cfae41b5caf160822e720e2663568f3f5d63bc52c0"
LOCKED_UV_RELEASE_COMMIT = "7af826859382eb191e47467540850caa8f493e5b"
LOCKED_PYTHON_ARCHIVE_SHA256 = "ef605200f8174e87ecfc308e52a88127543f85dd5c940dc5e92cab244b98a003"
LOCKED_PYTHON_RELEASE_COMMIT = "b498734a5791d0e6786695a226fd398a41c6f7f6"


class ReleaseError(ValueError):
    """Raised when a native installer release cannot be proven complete."""


@dataclass(frozen=True)
class ComponentRequest:
    """One exact component/target tuple requested from a verified release."""

    key: str
    repository: str
    component_id: str
    target_id: str
    release_id: str
    artifact_kind: str


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON objects before interpreting release inputs."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseError(f"JSON contains duplicate object key {key!r}")
        result[key] = value
    return result


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    """Read one UTF-8 JSON object without accepting duplicate keys."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseError(f"cannot read {label} as UTF-8 JSON: {error}") from error
    if not isinstance(value, dict):
        raise ReleaseError(f"{label} must be a JSON object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """Write deterministic UTF-8 JSON with a trailing newline."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    """Return a file's raw SHA-256 digest using bounded memory."""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _require_file(path: Path, label: str) -> None:
    """Require a regular, non-symlink file before hashing or publishing it."""

    if path.is_symlink() or not path.is_file():
        raise ReleaseError(f"{label} must be a regular, non-symlink file: {path}")


def _require_deb_file(package_root: Path, relative_path: str, label: str) -> Path:
    """Require a safe regular package file beneath the extracted DEB root."""

    relative = PurePosixPath(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in relative_path
    ):
        raise ReleaseError(f"{label} path is not a safe package-relative path")
    path = package_root.joinpath(*relative.parts)
    root_resolved = package_root.resolve()
    if not path.resolve(strict=False).is_relative_to(root_resolved):
        raise ReleaseError(f"{label} path escapes the extracted DEB root")
    current = package_root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise ReleaseError(f"{label} path passes through an unsafe package directory")
    _require_file(path, label)
    return path


def _require_deb_directory(package_root: Path, relative_path: str, label: str) -> Path:
    """Require a safe directory beneath the extracted DEB root."""

    relative = PurePosixPath(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in relative_path
    ):
        raise ReleaseError(f"{label} path is not a safe package-relative path")
    path = package_root.joinpath(*relative.parts)
    if not path.resolve(strict=False).is_relative_to(package_root.resolve()):
        raise ReleaseError(f"{label} path escapes the extracted DEB root")
    current = package_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise ReleaseError(f"{label} path passes through an unsafe package directory")
    return path


def _source_channel(source_ref: str) -> str:
    """Map the allowed Workspace publication refs to their immutable channel."""

    if source_ref == "refs/heads/develop":
        return "preview"
    if source_ref in {"refs/heads/main", "refs/heads/release"}:
        return "stable"
    raise ReleaseError("source ref must be develop, main, or release")


def _is_supported_component_manifest_schema(value: Any) -> bool:
    """Accept only the published integer v1 or v2 component manifest versions."""
    return type(value) is int and value in {1, 2}


def _release_source(release_id: str, channel: str, label: str) -> str:
    """Extract a full SHA from an exact stable/preview release tag."""

    match = CHANNEL_TAG_PATTERN.fullmatch(release_id) if isinstance(release_id, str) else None
    if match is None or match.group(1) != channel:
        raise ReleaseError(f"{label} must be an exact {channel}-<40 lowercase SHA> tag")
    return match.group(2)


def _read_dispatch_inputs(path: Path, source_ref: str, source_commit: str) -> dict[str, Any]:
    """Validate exact manual-dispatch locators, rejecting latest or partial inputs."""

    if not SHA1_PATTERN.fullmatch(source_commit):
        raise ReleaseError("source commit must be a full lowercase 40-character Git SHA")
    channel = _source_channel(source_ref)
    value = _read_json_object(path, "release locator input")
    expected_keys = {
        "schemaVersion",
        "nativeProfiles",
        "workspaceCatalog",
        "platformReleaseId",
        "productReleaseIds",
    }
    if set(value) != expected_keys or value.get("schemaVersion") != 1:
        raise ReleaseError("release locator input must match schemaVersion 1 exactly")
    profiles = value.get("nativeProfiles")
    if not isinstance(profiles, list) or tuple(profiles) != PROFILE_IDS:
        raise ReleaseError("nativeProfiles must select Ubuntu 22.04 and 24.04 in canonical order")
    catalog = value.get("workspaceCatalog")
    if not isinstance(catalog, dict) or set(catalog) != {"releaseId", "sha256"}:
        raise ReleaseError("workspaceCatalog must contain only releaseId and raw SHA-256")
    catalog_match = CATALOG_TAG_PATTERN.fullmatch(catalog.get("releaseId", ""))
    if catalog_match is None or catalog_match.group(1) != channel:
        raise ReleaseError("workspaceCatalog.releaseId must be an exact same-channel catalog tag")
    if not isinstance(catalog.get("sha256"), str) or not SHA256_PATTERN.fullmatch(
        catalog["sha256"]
    ):
        raise ReleaseError("workspaceCatalog.sha256 must be a full lowercase raw SHA-256")
    _release_source(value.get("platformReleaseId"), channel, "platformReleaseId")
    products = value.get("productReleaseIds")
    if not isinstance(products, dict) or set(products) != set(PRODUCTS):
        raise ReleaseError("productReleaseIds must name exactly the five supported Python Products")
    for product, release_id in products.items():
        _release_source(release_id, channel, f"productReleaseIds.{product}")
    return value


def _component_requests(inputs: dict[str, Any]) -> tuple[ComponentRequest, ...]:
    """Expand exact input tags into fixed catalog component and target requests."""

    requests = [
        ComponentRequest(
            key="platform-sdk",
            repository="DoHorizon-AI/Cyrene-Platform",
            component_id=SDK_COMPONENT,
            target_id=SDK_TARGET,
            release_id=inputs["platformReleaseId"],
            artifact_kind="python-bundle",
        )
    ]
    for profile_id in PROFILE_IDS:
        ubuntu = PROFILE_UBUNTU[profile_id]
        target_id = profile_id
        requests.extend(
            ComponentRequest(
                key=f"product/{product}/{ubuntu}",
                repository=PRODUCTS[product][0],
                component_id=PRODUCTS[product][1],
                target_id=target_id,
                release_id=inputs["productReleaseIds"][product],
                artifact_kind="python-bundle",
            )
            for product in PRODUCTS
        )
        platform_target = PLATFORM_TARGET_IDS[ubuntu]
        requests.extend(
            ComponentRequest(
                key=f"platform-core/{component}/{ubuntu}",
                repository="DoHorizon-AI/Cyrene-Platform",
                component_id=component,
                target_id=platform_target,
                release_id=inputs["platformReleaseId"],
                artifact_kind="native-binary",
            )
            for component in PLATFORM_CORE_COMPONENTS
        )
    return tuple(requests)


def _expected_attestation(
    value: Any, *, repository: str, subject_name: str, label: str
) -> dict[str, Any]:
    """Check recorded attestation identity fields emitted by the official fetcher."""

    if not isinstance(value, dict):
        raise ReleaseError(f"{label} has no attestation metadata")
    workflow = f"{repository}/{COMPONENT_WORKFLOW}"
    if (
        value.get("kind") != "github-artifact-attestation"
        or value.get("repository") != repository
        or value.get("workflow") != workflow
        or value.get("predicateType") != PREDICATE_TYPE
        or value.get("subjectName") != subject_name
    ):
        raise ReleaseError(f"{label} does not identify the fixed publisher/workflow/subject")
    # The fetcher also cryptographically checks the statement's exact ref and commit.
    return value


def _attestation_projection(
    value: dict[str, Any], *, source_ref: str, source_commit: str
) -> dict[str, Any]:
    """Bind official verifier metadata to the index/manifest's exact source tuple."""

    return {
        "repository": value["repository"],
        "workflow": value["workflow"],
        "predicateType": value["predicateType"],
        "subjectName": value["subjectName"],
        "sourceRef": source_ref,
        "sourceCommit": source_commit,
        "uri": value.get("uri"),
        "run": value.get("run"),
    }


def _run_attestation_verify(
    subject: Path,
    bundle: Path,
    *,
    repository: str,
    workflow: str,
    source_ref: str,
    source_commit: str,
    gh_executable: str,
    label: str,
) -> None:
    """Cryptographically verify one detached GitHub SLSA bundle against exact source pins."""

    command = [
        gh_executable,
        "attestation",
        "verify",
        str(subject),
        "--bundle",
        str(bundle),
        "--repo",
        repository,
        "--signer-workflow",
        workflow,
        "--source-ref",
        source_ref,
        "--source-digest",
        source_commit,
        "--predicate-type",
        PREDICATE_TYPE,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ReleaseError(f"detached SLSA bundle did not verify for {label}")


def _verified_component_record(
    request: ComponentRequest,
    report_path: Path,
    attestation_path: Path,
    catalog: dict[str, Any],
    channel: str,
) -> dict[str, Any]:
    """Derive a source receipt tuple from a successful official component fetch report."""

    report = _read_json_object(report_path, f"verified fetch report {request.key}")
    index = report.get("index")
    manifest = report.get("manifest")
    if not isinstance(index, dict) or not isinstance(manifest, dict):
        raise ReleaseError(f"verified fetch report {request.key} is missing index/manifest objects")
    source = index.get("source")
    manifest_source = manifest.get("source")
    source_commit = _release_source(request.release_id, channel, request.key)
    if (
        index.get("repository") != request.repository
        or index.get("channel") != channel
        or not isinstance(source, dict)
        or source.get("repository") != f"https://github.com/{request.repository}"
        or source.get("commit") != source_commit
        or source.get("ref") not in CHANNEL_REFS[channel]
        or manifest.get("releaseId") != request.release_id
        or manifest.get("componentId") != request.component_id
        or manifest.get("channel") != channel
        or manifest.get("target")
        != _target_from_catalog(catalog, request.component_id, request.target_id)
        or manifest_source != source
    ):
        raise ReleaseError(f"verified fetch report {request.key} differs from its exact locator")

    index_path = report_path.parent / "component-release-index-v1.json"
    manifest_path = Path(str(report.get("manifestPath", "")))
    artifact_path = Path(str(report.get("artifactPath", "")))
    _require_file(index_path, f"{request.key} index")
    _require_file(manifest_path, f"{request.key} manifest")
    _require_file(artifact_path, f"{request.key} artifact")
    _require_file(attestation_path, f"{request.key} detached artifact attestation")
    if attestation_path.name != f"{artifact_path.name}.attestation.jsonl":
        raise ReleaseError(f"{request.key} attestation asset name is not canonical")
    index_attestation = _expected_attestation(
        index.get("provenance", {}).get("attestation")
        if isinstance(index.get("provenance"), dict)
        else None,
        repository=request.repository,
        subject_name="component-release-index-v1.json",
        label=f"{request.key} index",
    )
    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("kind") != request.artifact_kind:
        raise ReleaseError(f"{request.key} artifact kind differs from the expected catalog type")
    raw_artifact_sha = _sha256(artifact_path)
    declared_sha = str(artifact.get("sha256", ""))
    if declared_sha.removeprefix("sha256:") != raw_artifact_sha:
        raise ReleaseError(f"{request.key} artifact bytes differ from the verified manifest")
    artifact_size = artifact_path.stat().st_size
    if artifact.get("sizeBytes") != artifact_size:
        raise ReleaseError(f"{request.key} artifact size differs from its verified manifest")
    manifest_bytes_sha = _sha256(manifest_path)
    manifest_raw = _read_json_object(manifest_path, f"{request.key} manifest file")
    if manifest_raw != manifest:
        raise ReleaseError(f"{request.key} manifest report differs from the saved verified bytes")
    manifest_digest = manifest.get("manifestDigest")
    if not isinstance(manifest_digest, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", manifest_digest
    ):
        raise ReleaseError(f"{request.key} manifest digest is malformed")
    index_bytes = _read_json_object(index_path, f"{request.key} index file")
    if index_bytes != index:
        raise ReleaseError(f"{request.key} index report differs from saved verified bytes")
    release_entry = next(
        (
            row
            for row in index.get("releases", [])
            if isinstance(row, dict)
            and row.get("componentId") == request.component_id
            and row.get("target") == manifest.get("target")
        ),
        None,
    )
    if (
        not isinstance(release_entry, dict)
        or release_entry.get("manifestDigest") != manifest_digest
    ):
        raise ReleaseError(f"{request.key} is absent from its verified immutable release index")
    expected_manifest_url = (
        f"https://github.com/{request.repository}/releases/download/{request.release_id}/"
        f"{manifest_path.name}"
    )
    if release_entry.get("manifestUri") != expected_manifest_url:
        raise ReleaseError(f"{request.key} manifest URL differs from its exact immutable tag")
    if artifact.get("uri") != (
        f"https://github.com/{request.repository}/releases/download/{request.release_id}/"
        f"{artifact_path.name}"
    ):
        raise ReleaseError(f"{request.key} artifact URL differs from its exact immutable tag")
    manifest_attestation = _expected_attestation(
        manifest.get("provenance", {}).get("attestation")
        if isinstance(manifest.get("provenance"), dict)
        else None,
        repository=request.repository,
        subject_name=artifact_path.name,
        label=f"{request.key} artifact",
    )
    _run_attestation_verify(
        artifact_path,
        attestation_path,
        repository=request.repository,
        workflow=f"{request.repository}/{COMPONENT_WORKFLOW}",
        source_ref=source["ref"],
        source_commit=source_commit,
        gh_executable=os.environ.get("GH_EXECUTABLE", "gh"),
        label=request.key,
    )
    artifact_attestation = {
        "assetName": attestation_path.name,
        "sha256": _sha256(attestation_path),
        "repository": request.repository,
        "workflow": f"{request.repository}/{COMPONENT_WORKFLOW}",
        "predicateType": PREDICATE_TYPE,
        "subjectName": artifact_path.name,
        "sourceRef": source["ref"],
        "sourceCommit": source_commit,
        "uri": manifest_attestation.get("uri"),
        "run": manifest_attestation.get("run"),
    }
    return {
        "key": request.key,
        "repository": request.repository,
        "releaseId": request.release_id,
        "source": {"ref": source["ref"], "commit": source_commit},
        "componentId": request.component_id,
        "targetId": request.target_id,
        "target": manifest["target"],
        "artifactKind": request.artifact_kind,
        "index": {
            "assetName": index_path.name,
            "rawSha256": _sha256(index_path),
            "declaredDigest": index.get("indexDigest"),
            "attestation": _attestation_projection(
                index_attestation, source_ref=source["ref"], source_commit=source_commit
            ),
        },
        "manifest": {
            "assetName": manifest_path.name,
            "rawSha256": manifest_bytes_sha,
            "declaredDigest": manifest_digest,
            "attestation": _attestation_projection(
                manifest_attestation, source_ref=source["ref"], source_commit=source_commit
            ),
        },
        "artifact": {
            "assetName": artifact_path.name,
            "sha256": raw_artifact_sha,
            "sizeBytes": artifact_size,
            "format": artifact.get("format"),
            "kind": artifact.get("kind"),
            "attestation": artifact_attestation,
        },
    }


def _target_from_catalog(
    catalog: dict[str, Any], component_id: str, target_id: str
) -> dict[str, Any]:
    """Read the exact target object declared for a component in the pinned catalog."""

    component = next(
        (
            item
            for item in catalog.get("components", [])
            if isinstance(item, dict) and item.get("componentId") == component_id
        ),
        None,
    )
    target_row = next(
        (
            item
            for item in catalog.get("targets", [])
            if isinstance(item, dict) and item.get("id") == target_id
        ),
        None,
    )
    if not isinstance(component, dict) or not isinstance(target_row, dict):
        raise ReleaseError(f"trusted catalog does not declare {component_id}/{target_id}")
    component_target = next(
        (
            item
            for item in component.get("targets", [])
            if isinstance(item, dict) and item.get("targetId") == target_id
        ),
        None,
    )
    if not isinstance(component_target, dict) or component_target.get("support") not in {
        "supported",
        "contract-only",
    }:
        raise ReleaseError(f"trusted catalog does not support {component_id}/{target_id}")
    target = target_row.get("target")
    if not isinstance(target, dict):
        raise ReleaseError(f"trusted catalog target {target_id} is malformed")
    return target


def _service_index_summary(index: dict[str, Any], profile_id: str) -> dict[str, dict[str, Any]]:
    """Validate a staged five-Product index and return its rows by canonical component ID."""

    if (
        set(index) != {"schemaVersion", "targetProfile", "services"}
        or index.get("schemaVersion") != 1
        or index.get("targetProfile") != profile_id
    ):
        raise ReleaseError("embedded verified service index has an unexpected schema or profile")
    services = index.get("services")
    component_ids = {component_id for _, component_id in PRODUCTS.values()}
    if not isinstance(services, dict) or set(services) != component_ids:
        raise ReleaseError("embedded verified service index must contain exactly the five Products")
    by_component: dict[str, dict[str, Any]] = {}
    for product, (repository, component_id) in PRODUCTS.items():
        row = services.get(component_id)
        if not isinstance(row, dict) or set(row) != {
            "componentId",
            "repository",
            "releaseId",
            "source",
            "artifact",
            "manifest",
            "attestation",
        }:
            raise ReleaseError(f"embedded verified service index row is malformed: {product}")
        source = row.get("source")
        artifact = row.get("artifact")
        manifest = row.get("manifest")
        attestation = row.get("attestation")
        if (
            row.get("componentId") != component_id
            or row.get("repository") != repository
            or not isinstance(source, dict)
            or set(source) != {"ref", "commit"}
            or not SHA1_PATTERN.fullmatch(str(source.get("commit", "")))
            or not isinstance(artifact, dict)
            or set(artifact) != {"path", "sha256", "sizeBytes", "kind", "format"}
            or not isinstance(manifest, dict)
            or set(manifest) != {"path", "sha256", "manifestDigest"}
            or not isinstance(attestation, dict)
            or set(attestation)
            != {
                "path",
                "sha256",
                "repository",
                "workflow",
                "predicateType",
                "subjectName",
                "sourceRef",
                "sourceCommit",
            }
        ):
            raise ReleaseError(f"embedded verified service index identity is invalid: {product}")
        for label, value in (
            ("artifact", artifact),
            ("manifest", manifest),
            ("attestation", attestation),
        ):
            if not SHA256_PATTERN.fullmatch(str(value.get("sha256", ""))):
                raise ReleaseError(
                    f"embedded verified service index {label} SHA is malformed: {product}"
                )
            _safe_index_relative_path(value.get("path"), f"{product}.{label}.path")
        if (
            not isinstance(artifact.get("sizeBytes"), int)
            or isinstance(artifact.get("sizeBytes"), bool)
            or artifact.get("sizeBytes") < 1
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(manifest.get("manifestDigest", "")))
            or attestation.get("repository") != repository
            or attestation.get("workflow") != f"{repository}/{COMPONENT_WORKFLOW}"
            or attestation.get("predicateType") != PREDICATE_TYPE
            or attestation.get("subjectName") != Path(str(artifact.get("path", ""))).name
            or attestation.get("sourceRef") != source.get("ref")
            or attestation.get("sourceCommit") != source.get("commit")
        ):
            raise ReleaseError(
                f"embedded verified service index attestation/digest is invalid: {product}"
            )
        by_component[component_id] = row
    return by_component


def _safe_index_relative_path(value: Any, label: str) -> Path:
    """Accept a normalized portable index path without traversal or platform tricks."""

    if not isinstance(value, str) or not value or "\\" in value:
        raise ReleaseError(f"{label} must be a normalized POSIX-relative path")
    parts = value.split("/")
    path = Path(*parts)
    if value.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ReleaseError(f"{label} must be a normalized POSIX-relative path")
    return path


def _receipt_product_rows(receipt: dict[str, Any], profile_id: str) -> dict[str, dict[str, Any]]:
    """Select exact Product tuples for one native profile from the trusted source receipt."""

    ubuntu = PROFILE_UBUNTU[profile_id]
    return {
        row["componentId"]: row
        for row in receipt.get("verifiedTuples", [])
        if isinstance(row, dict)
        and row.get("key", "").startswith("product/")
        and row.get("key", "").endswith(f"/{ubuntu}")
    }


def _validate_index_against_receipt(
    index: dict[str, Any],
    receipt: dict[str, Any],
    profile_id: str,
    artifact_root: Path,
    *,
    verify_attestations: bool,
    gh_executable: str,
) -> dict[str, dict[str, Any]]:
    """Check every embedded Product byte, digest, target, source, and detached SLSA bundle."""

    index_rows = _service_index_summary(index, profile_id)
    receipt_rows = _receipt_product_rows(receipt, profile_id)
    if set(index_rows) != set(receipt_rows) or set(index_rows) != {
        component_id for _, component_id in PRODUCTS.values()
    }:
        raise ReleaseError(
            "embedded service index Product set differs from verified source receipt"
        )
    for component_id, row in index_rows.items():
        proof = receipt_rows[component_id]
        source = row["source"]
        artifact = row["artifact"]
        manifest = row["manifest"]
        attestation = row["attestation"]
        proof_artifact = proof["artifact"]
        proof_manifest = proof["manifest"]
        proof_attestation = proof_artifact["attestation"]
        if (
            row["releaseId"] != proof["releaseId"]
            or source != proof["source"]
            or proof.get("targetId") != profile_id
            or proof.get("target") is None
            or artifact["sha256"] != proof_artifact["sha256"]
            or artifact["sizeBytes"] != proof_artifact["sizeBytes"]
            or artifact["kind"] != proof_artifact["kind"]
            or artifact["format"] != proof_artifact["format"]
            or manifest["sha256"] != proof_manifest["rawSha256"]
            or manifest["manifestDigest"] != proof_manifest["declaredDigest"]
            or attestation["sha256"] != proof_attestation["sha256"]
            or Path(str(artifact.get("path", ""))).name != proof_artifact["assetName"]
            or Path(str(manifest.get("path", ""))).name != proof_manifest["assetName"]
            or Path(str(attestation.get("path", ""))).name != proof_attestation["assetName"]
            or attestation["repository"] != proof_attestation["repository"]
            or attestation["workflow"] != proof_attestation["workflow"]
            or attestation["predicateType"] != proof_attestation["predicateType"]
            or attestation["subjectName"] != proof_attestation["subjectName"]
            or attestation["sourceRef"] != source["ref"]
            or attestation["sourceCommit"] != source["commit"]
        ):
            raise ReleaseError(
                f"embedded service index tuple differs from source receipt: {component_id}"
            )
        artifact_path = artifact_root / _safe_index_relative_path(
            artifact["path"], f"{component_id}.artifact.path"
        )
        manifest_path = artifact_root / _safe_index_relative_path(
            manifest["path"], f"{component_id}.manifest.path"
        )
        bundle_path = artifact_root / _safe_index_relative_path(
            attestation["path"], f"{component_id}.attestation.path"
        )
        for path, label in (
            (artifact_path, f"{component_id} archived artifact"),
            (manifest_path, f"{component_id} archived manifest"),
            (bundle_path, f"{component_id} archived attestation bundle"),
        ):
            _require_file(path, label)
        if (
            _sha256(artifact_path) != artifact["sha256"]
            or artifact_path.stat().st_size != artifact["sizeBytes"]
            or _sha256(manifest_path) != manifest["sha256"]
            or _sha256(bundle_path) != attestation["sha256"]
            or bundle_path.name != f"{artifact_path.name}.attestation.jsonl"
        ):
            raise ReleaseError(
                f"embedded service bytes differ from the verified tuple: {component_id}"
            )
        manifest_document = _read_json_object(manifest_path, f"embedded {component_id} manifest")
        if (
            manifest_document.get("componentId") != component_id
            or manifest_document.get("releaseId") != row["releaseId"]
            or not _is_supported_component_manifest_schema(manifest_document.get("schemaVersion"))
            or manifest_document.get("channel") != _source_channel(source["ref"])
            or manifest_document.get("target") != proof["target"]
            or manifest_document.get("source")
            != {
                "repository": f"https://github.com/{row['repository']}",
                "ref": source["ref"],
                "commit": source["commit"],
            }
            or not isinstance(manifest_document.get("artifact"), dict)
            or str(manifest_document["artifact"].get("sha256", "")).removeprefix("sha256:")
            != artifact["sha256"]
            or manifest_document["artifact"].get("kind") != "python-bundle"
            or manifest_document["artifact"].get("format") != "tar.gz"
            or manifest_document["artifact"].get("sizeBytes") != artifact["sizeBytes"]
            or manifest_document["artifact"].get("uri")
            != (
                f"https://github.com/{row['repository']}/releases/download/"
                f"{row['releaseId']}/{artifact_path.name}"
            )
            or manifest_document.get("manifestDigest") != manifest["manifestDigest"]
        ):
            raise ReleaseError(
                f"embedded manifest identity differs from the verified tuple: {component_id}"
            )
        if verify_attestations:
            _run_attestation_verify(
                artifact_path,
                bundle_path,
                repository=row["repository"],
                workflow=attestation["workflow"],
                source_ref=source["ref"],
                source_commit=source["commit"],
                gh_executable=gh_executable,
                label=f"embedded {component_id}",
            )
    return index_rows


def _postinst_service_bootstrap_calls(content: str) -> list[tuple[int, list[str], bool]]:
    """Find executable Cyrene calls at shell command positions.

    Return each call's arguments and whether it is the unwrapped default command.
    Shell words such as account names, echo text, and comments are not commands.
    """
    calls: list[tuple[int, list[str], bool]] = []
    control_words = {"if", "then", "else", "elif", "while", "until", "do", "!"}
    assignment = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)

    for line_number, line in enumerate(content.splitlines()):
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            tokens = list(lexer)
        except ValueError as error:
            raise ReleaseError("DEB postinst contains malformed shell quoting") from error

        segments: list[list[str]] = []
        current: list[str] = []
        for token in tokens:
            if token and all(character in ";&|" for character in token):
                if current:
                    segments.append(current)
                    current = []
            else:
                current.append(token)
        if current:
            segments.append(current)

        for segment in segments:
            command = list(segment)
            while command and command[0] in control_words:
                command.pop(0)
            had_prefix = False
            while command and assignment.fullmatch(command[0]):
                had_prefix = True
                command.pop(0)

            # Recognize common shell wrappers so they cannot hide an activation
            # call from the stage-only check. Wrapping is not the default command.
            while command and PurePosixPath(command[0]).name in {"env", "command", "exec"}:
                wrapper = PurePosixPath(command.pop(0)).name
                had_prefix = True
                if wrapper == "command" and command and command[0] in {"-v", "-V"}:
                    command = []
                    break
                if wrapper == "env":
                    while command and command[0].startswith("-"):
                        option = command.pop(0)
                        if option == "--":
                            break
                    while command and assignment.fullmatch(command[0]):
                        command.pop(0)
            if not command or PurePosixPath(command[0]).name != "cyrene":
                continue

            executable = command[0]
            is_default = (
                not had_prefix
                and executable in {"cyrene", "/usr/bin/cyrene"}
                and command[1:] == ["service-bootstrap"]
            )
            calls.append((line_number, command[1:], is_default))
    return calls


def _static_installer_activation_check(control_directory: Path) -> dict[str, str]:
    """Hash maintainer scripts and reject commands that activate services or legacy runtime."""

    script_hashes: dict[str, str] = {}
    unsafe_patterns = (
        re.compile(
            r"\bsystemctl\s+(?:enable|start|restart|try-restart|preset|reload-or-restart)\b"
        ),
        re.compile(r"\b(?:deb-systemd-invoke|invoke-rc\.d)\b"),
        re.compile(r"\b(?:update-rc\.d|chkconfig|rc-update)\b|\bservice\s+\S+"),
        re.compile(r"\b(?:curl|wget|apt-get|pip|uv)\b"),
        re.compile(r"\bactivate-missing\b|\bproduct-enable\b|\bservice-enable\b"),
        re.compile(
            r"\b(?:broker|activity|token|admissionstate|activationstate)\b"
            r"|\bactive[-_ ]?pointer\b"
        ),
        re.compile(r"\bold-runtime\b|/opt/cyrene/(?:current|active)\b"),
    )
    for name in MAINTAINER_SCRIPT_NAMES:
        path = control_directory / name
        _require_file(path, f"DEB {name} maintainer script")
        raw = path.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ReleaseError(f"DEB {name} maintainer script must be UTF-8") from error
        script_hashes[name] = hashlib.sha256(raw).hexdigest()
        effective_lines = [line.split("#", 1)[0] for line in content.splitlines()]
        effective = "\n".join(effective_lines).lower()
        daemon_reload = re.compile(r"(?:/usr/bin/)?\bsystemctl\s+daemon-reload\b")
        non_daemon_systemctl = any(
            re.search(
                r"\bsystemctl\b",
                re.sub(
                    r"\bcommand\s+-v\s+(?:/usr/bin/)?systemctl\b",
                    "",
                    daemon_reload.sub("", line.lower()),
                ),
            )
            is not None
            for line in effective_lines
        )
        if non_daemon_systemctl or any(pattern.search(effective) for pattern in unsafe_patterns):
            raise ReleaseError(
                f"DEB {name} contains a service activation or legacy-runtime operation"
            )
        if name == "postinst":
            _validate_runtime_state_staging(content)
            invocations = _postinst_service_bootstrap_calls(content)
            if len(invocations) != 1 or not invocations[0][2]:
                raise ReleaseError(
                    "DEB postinst must only stage via the default service-bootstrap command"
                )
    return script_hashes


def _validate_runtime_state_staging(postinst: str) -> None:
    """Allow only missing-directory creation and read-only validation of broker state."""

    effective_lines = [
        (line_number, line.split("#", 1)[0].strip())
        for line_number, line in enumerate(postinst.splitlines())
        if line.split("#", 1)[0].strip()
    ]
    start = [
        index
        for index, (_line_number, line) in enumerate(effective_lines)
        if line == "RUNTIME_STATE_DIR=/var/lib/cyrene/runtime"
    ]
    bootstrap_line_numbers = {
        line_number
        for line_number, _arguments, _is_default in _postinst_service_bootstrap_calls(postinst)
    }
    bootstrap = [
        index
        for index, (line_number, _line) in enumerate(effective_lines)
        if line_number in bootstrap_line_numbers
    ]
    if len(start) != 1 or len(bootstrap) != 1 or start[0] >= bootstrap[0]:
        raise ReleaseError("DEB postinst must identify one broker-state staging block")

    block = [
        " ".join(line.split()) for _line_number, line in effective_lines[start[0] : bootstrap[0]]
    ]
    expected = [
        "RUNTIME_STATE_DIR=/var/lib/cyrene/runtime",
        "if ! getent group cyrene-runtime-maintenance >/dev/null 2>&1; then",
        "groupadd --system cyrene-runtime-maintenance",
        "fi",
        'AUTHORITY_GID="$(getent group cyrene-runtime-maintenance | cut -d: -f3)"',
        'case "${AUTHORITY_GID}" in',
        "''|*[!0-9]*)",
        'echo "ERROR: cyrene-runtime-maintenance group has no valid numeric gid." >&2',
        "exit 1",
        ";;",
        "esac",
        'if [ -L "${RUNTIME_STATE_DIR}" ]; then',
        'echo "ERROR: ${RUNTIME_STATE_DIR} is a symlink; refusing to follow existing runtime state." >&2',
        "exit 1",
        'elif [ ! -e "${RUNTIME_STATE_DIR}" ]; then',
        'install -d -o root -g cyrene-runtime-maintenance -m 2770 "$RUNTIME_STATE_DIR"',
        'elif [ ! -d "${RUNTIME_STATE_DIR}" ]; then',
        'echo "ERROR: ${RUNTIME_STATE_DIR} exists but is not a directory." >&2',
        "exit 1",
        "fi",
        'RUNTIME_STATE_META="$(stat -c \'%u:%g:%a\' -- "$RUNTIME_STATE_DIR")"',
        'if [ "${RUNTIME_STATE_META}" != "0:${AUTHORITY_GID}:2770" ]; then',
        'echo "ERROR: Refusing to repair existing runtime state in place (${RUNTIME_STATE_DIR}: ${RUNTIME_STATE_META})." >&2',
        "exit 1",
        "fi",
    ]
    if block != expected:
        raise ReleaseError(
            "DEB postinst broker-state handling must create only a missing directory and "
            "must reject unsafe existing state without repair"
        )
    remainder = effective_lines[: start[0]] + effective_lines[bootstrap[0] :]
    if any(
        re.search(r"RUNTIME_STATE_DIR|/var/lib/cyrene/runtime\b", line, re.IGNORECASE)
        for _line_number, line in remainder
    ):
        raise ReleaseError(
            "DEB postinst uses broker state outside its validated safe staging block"
        )


def _ast_function(source_path: Path, function_name: str, label: str) -> ast.FunctionDef:
    """Return one top-level function from a regular packaged Python source file."""

    _require_file(source_path, label)
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    except (OSError, UnicodeDecodeError, SyntaxError) as error:
        raise ReleaseError(f"packaged {label} is not valid Python source") from error
    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    ]
    if len(functions) != 1 or not isinstance(functions[0], ast.FunctionDef):
        raise ReleaseError(f"packaged {label} must define exactly one {function_name} function")
    return functions[0]


def _call_name(node: ast.Call) -> str | None:
    """Return a simple or qualified AST call name."""

    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _contains_node(container: ast.AST, candidate: ast.AST) -> bool:
    """Report whether an AST node occurs under another node."""

    return any(node is candidate for node in ast.walk(container))


def _deb_tar_member_metadata(deb_path: Path, relative_path: str) -> tuple[int, int, int]:
    """Read one file's ownership and mode from the immutable DEB data archive."""

    normalized = relative_path.removeprefix("./")
    process = subprocess.Popen(
        ["dpkg-deb", "--fsys-tarfile", str(deb_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if process.stdout is None:
        process.kill()
        process.wait()
        raise ReleaseError("dpkg-deb did not expose the package data archive")
    matches: list[tuple[int, int, int, bool]] = []
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                if member.name.removeprefix("./") == normalized:
                    matches.append((member.uid, member.gid, member.mode, member.isfile()))
    except (OSError, tarfile.TarError) as error:
        process.kill()
        process.wait()
        raise ReleaseError("dpkg-deb could not read the package data archive metadata") from error
    finally:
        process.stdout.close()
    if process.wait() != 0 or len(matches) != 1:
        raise ReleaseError(f"DEB must contain exactly one regular {relative_path} payload file")
    uid, gid, mode, regular_file = matches[0]
    if not regular_file or uid != 0 or gid != 0 or mode != 0o755:
        raise ReleaseError(
            f"DEB payload executable must be a root-owned mode-0755 file: {relative_path}"
        )
    return uid, gid, mode


def _tar_member_sha256(archive_path: Path, member_name: str, label: str) -> str:
    """Hash one regular member in a locked gzipped source archive."""

    digest = hashlib.sha256()
    matches = 0
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive:
                if member.name != member_name:
                    continue
                matches += 1
                if not member.isfile():
                    raise ReleaseError(f"{label} archive entry is not a regular file")
                content = archive.extractfile(member)
                if content is None:
                    raise ReleaseError(f"cannot read {label} archive entry")
                with content:
                    while block := content.read(1024 * 1024):
                        digest.update(block)
    except (OSError, tarfile.TarError) as error:
        raise ReleaseError(f"cannot inspect {label} archive member") from error
    if matches != 1:
        raise ReleaseError(f"{label} archive must contain its locked executable exactly once")
    return digest.hexdigest()


def _expected_python_runtime_record(runtime_lock: dict[str, Any]) -> dict[str, Any]:
    """Project the exact source evidence record written by the private runtime stager."""

    python = runtime_lock["python"]
    resolver = runtime_lock["buildResolver"]
    python_archive = python["archive"]
    uv_archive = resolver["binaryArchive"]
    distribution = resolver["distributionMetadata"]
    return {
        "schemaVersion": 1,
        "pythonArchive": {
            "url": python_archive["url"],
            "sha256": python_archive["sha256"],
            "sizeBytes": python_archive["size"],
            "repository": python_archive["release"]["repository"],
            "releaseTag": python_archive["release"]["tag"],
            "releaseCommit": python_archive["release"]["commit"],
            "assetId": python_archive["release"]["assetId"],
        },
        "uvResolver": {
            "version": resolver["version"],
            "installedPath": resolver["installedPath"],
            "usage": resolver["usage"],
            "url": uv_archive["url"],
            "archiveSha256": uv_archive["sha256"],
            "executableSha256": uv_archive["executableSha256"],
            "repository": uv_archive["release"]["repository"],
            "releaseTag": uv_archive["release"]["tag"],
            "releaseCommit": uv_archive["release"]["commit"],
            "assetId": uv_archive["release"]["assetId"],
        },
        "uvDistributionMetadata": {
            "url": distribution["url"],
            "sha256": distribution["sha256"],
            "mappingKey": distribution["mappingKey"],
        },
        "runtimeRequirementsSha256": runtime_lock["runtimeDependencies"]["requirementsSha256"],
        "runtimeSystemPackages": runtime_lock["runtimeDependencies"]["systemPackages"],
    }


def _validate_deb_private_runtime(
    package_root: Path, deb_path: Path, receipt: dict[str, Any]
) -> None:
    """Verify the actual locked CPython and uv files inside the DEB payload."""

    receipt_lock = receipt.get("pythonRuntimeLock")
    if not isinstance(receipt_lock, dict) or not isinstance(receipt_lock.get("document"), dict):
        raise ReleaseError("source receipt has no verified Python runtime lock document")
    runtime_lock = receipt_lock["document"]
    payload = runtime_lock.get("payload")
    if not isinstance(payload, dict):
        raise ReleaseError("DEB private Python runtime payload section is malformed")
    lock_path = _require_deb_file(
        package_root,
        payload.get("lockPath", ""),
        "DEB embedded private Python runtime lock",
    )
    embedded_lock = _read_json_object(lock_path, "DEB embedded private Python runtime lock")
    if _sha256(lock_path) != receipt_lock.get("sha256") or embedded_lock != runtime_lock:
        raise ReleaseError("DEB private Python runtime lock differs from the source receipt")

    python = runtime_lock.get("python")
    resolver = runtime_lock.get("buildResolver")
    dependencies = runtime_lock.get("runtimeDependencies")
    payload = runtime_lock.get("payload")
    if not all(isinstance(value, dict) for value in (python, resolver, dependencies, payload)):
        raise ReleaseError("DEB private Python runtime lock is missing required sections")
    archive = python.get("archive")
    uv_archive = resolver.get("binaryArchive")
    if not isinstance(archive, dict) or not isinstance(uv_archive, dict):
        raise ReleaseError("DEB private Python lock archives are malformed")
    if (
        python.get("implementation") != "CPython"
        or python.get("version") != "3.12.14"
        or python.get("target") != "x86_64-unknown-linux-gnu"
        or python.get("installRoot") != "/opt/cyrene/python/3.12.14"
        or python.get("executable") != "/opt/cyrene/python/3.12.14/bin/python3.12"
        or resolver.get("tool") != "uv"
        or resolver.get("version") != "0.12.21"
        or resolver.get("installedPath") != "/opt/cyrene/uv/0.12.21/uv"
        or resolver.get("usage")
        != {
            "build": "resolve-frozen-python-distribution-mapping",
            "runtime": "explicit-trainer-environment-prepare",
            "automaticRuntimeBootstrap": False,
        }
    ):
        raise ReleaseError("DEB private Python or uv runtime identity differs from the frozen lock")

    python_archive_path = _require_deb_file(
        package_root,
        payload.get("runtimeArchivePath", ""),
        "DEB locked CPython archive",
    )
    if (
        _sha256(python_archive_path) != archive.get("sha256")
        or python_archive_path.stat().st_size != archive.get("size")
        or archive.get("sha256") != LOCKED_PYTHON_ARCHIVE_SHA256
        or archive.get("url")
        != "https://github.com/astral-sh/python-build-standalone/releases/download/20260929/cpython-3.12.14%2B20260929-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
        or archive.get("release", {}).get("repository") != "astral-sh/python-build-standalone"
        or archive.get("release", {}).get("tag") != "20260929"
        or archive.get("release", {}).get("commit") != LOCKED_PYTHON_RELEASE_COMMIT
        or archive.get("release", {}).get("immutable") is not True
    ):
        raise ReleaseError("DEB CPython archive bytes differ from the immutable runtime lock")
    python_executable_path = _require_deb_file(
        package_root,
        python["executable"].lstrip("/"),
        "DEB private CPython executable",
    )
    if not os.access(python_executable_path, os.X_OK):
        raise ReleaseError("DEB private CPython executable is not executable")
    python_install_root = PurePosixPath(python["installRoot"])
    try:
        python_executable_relative = PurePosixPath(python["executable"]).relative_to(
            python_install_root
        )
    except ValueError as error:
        raise ReleaseError("locked CPython executable is outside its install root") from error
    archive_member = (PurePosixPath(python["archiveRoot"]) / python_executable_relative).as_posix()
    if _sha256(python_executable_path) != _tar_member_sha256(
        python_archive_path, archive_member, "locked CPython"
    ):
        raise ReleaseError("DEB private CPython executable differs from its pinned PBS archive")
    _deb_tar_member_metadata(deb_path, python_executable_path.relative_to(package_root).as_posix())

    uv_executable_path = _require_deb_file(
        package_root,
        resolver["installedPath"].lstrip("/"),
        "DEB locked uv executable",
    )
    expected_uv_sha = uv_archive.get("executableSha256")
    if (
        not os.access(uv_executable_path, os.X_OK)
        or _sha256(uv_executable_path) != expected_uv_sha
        or expected_uv_sha != LOCKED_UV_EXECUTABLE_SHA256
        or uv_archive.get("sha256") != LOCKED_UV_ARCHIVE_SHA256
        or uv_archive.get("release", {}).get("repository") != "astral-sh/uv"
        or uv_archive.get("release", {}).get("tag") != "0.12.21"
        or uv_archive.get("release", {}).get("commit") != LOCKED_UV_RELEASE_COMMIT
        or uv_archive.get("release", {}).get("immutable") is not True
    ):
        raise ReleaseError("DEB uv executable differs from the pinned official binary")
    _deb_tar_member_metadata(deb_path, uv_executable_path.relative_to(package_root).as_posix())

    verification_path = _require_deb_file(
        package_root,
        payload.get("verificationRecordPath", ""),
        "DEB private Python verification record",
    )
    verification_record = _read_json_object(
        verification_path, "DEB private Python verification record"
    )
    if verification_record != _expected_python_runtime_record(runtime_lock):
        raise ReleaseError("DEB private Python verification record differs from its runtime lock")

    requirements_path = _require_deb_file(
        package_root,
        payload.get("requirementsPath", ""),
        "DEB private Python requirements lock",
    )
    if _sha256(requirements_path) != dependencies.get("requirementsSha256"):
        raise ReleaseError("DEB private Python requirements lock differs from its runtime lock")
    wheel_directory = _require_deb_directory(
        package_root,
        payload.get("wheelDirectory", ""),
        "DEB private Python wheel directory",
    )
    wheels = dependencies.get("wheels")
    if not isinstance(wheels, list):
        raise ReleaseError("DEB private Python wheel lock is malformed")
    expected_wheel_names: set[str] = set()
    for wheel in wheels:
        if not isinstance(wheel, dict) or not isinstance(wheel.get("filename"), str):
            raise ReleaseError("DEB private Python wheel record is malformed")
        wheel_path = _require_deb_file(
            package_root,
            (PurePosixPath(payload["wheelDirectory"]) / wheel["filename"]).as_posix(),
            "DEB locked private Python wheel",
        )
        if _sha256(wheel_path) != wheel.get("sha256"):
            raise ReleaseError("DEB private Python wheel differs from its locked digest")
        expected_wheel_names.add(wheel["filename"])
    actual_wheel_names = {
        path.name for path in wheel_directory.iterdir() if path.is_file() and not path.is_symlink()
    }
    if actual_wheel_names != expected_wheel_names:
        raise ReleaseError("DEB private Python wheel directory differs from its locked package set")


def _validate_packaged_stage_only_behavior(package_root: Path, profile_id: str) -> None:
    """Derive fail-closed and preservation claims from package units and Python code."""

    if profile_id not in PROFILE_IDS:
        raise ReleaseError(
            f"stage-only behavior check received an unsupported profile: {profile_id}"
        )
    broker_unit = "cyrene-runtime-maintenance.service"
    unit_root = package_root / "lib/systemd/system"
    if any(path.name == broker_unit for path in package_root.rglob(broker_unit)):
        raise ReleaseError("bootstrap DEB must not claim to provide the Platform broker unit")

    for _, component_id in PRODUCTS.values():
        unit_path = unit_root / f"{component_id}.service"
        _require_file(unit_path, f"DEB Product systemd unit {component_id}")
        try:
            unit_text = unit_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ReleaseError(f"DEB Product systemd unit is not UTF-8: {component_id}") from error
        section = ""
        unit_dependencies: dict[str, list[str]] = {"Requires": [], "After": []}
        for raw_line in unit_text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                continue
            if section == "Unit" and "=" in line:
                key, value = line.split("=", 1)
                if key in unit_dependencies:
                    if value:
                        unit_dependencies[key].extend(value.split())
                    else:
                        unit_dependencies[key].clear()
        if any(unit_dependencies[key].count(broker_unit) != 1 for key in ("Requires", "After")):
            raise ReleaseError(
                f"DEB Product unit {component_id} must require and follow the Platform broker"
            )

    product_unit_names = {f"{component_id}.service" for _, component_id in PRODUCTS.values()}
    enabled_product_links = [
        path
        for path in package_root.rglob("*")
        if path.is_symlink()
        and path.name in product_unit_names
        and (".wants" in path.parts or ".requires" in path.parts)
    ]
    if enabled_product_links:
        raise ReleaseError("bootstrap DEB must not enable Product services on a fresh host")
    dropin_names = {
        f"{unit_name}.service.d"
        for unit_name in (component_id for _, component_id in PRODUCTS.values())
    }
    if any(path.name in dropin_names for path in package_root.rglob("*")):
        raise ReleaseError(
            "bootstrap DEB must not ship Product unit drop-ins that alter broker gates"
        )

    cli_path = package_root / "usr/lib/cyrene/scripts/cyrene.py"
    bundle_path = package_root / "usr/lib/cyrene/scripts/service_bundle.py"
    command_function = _ast_function(cli_path, "cmd_service_bootstrap", "Cyrene CLI")
    command_calls = [
        node
        for node in ast.walk(command_function)
        if isinstance(node, ast.Call) and _call_name(node) == "bootstrap_package_bundles"
    ]
    if len(command_calls) != 1:
        raise ReleaseError("packaged service-bootstrap must call the bundle staging helper once")
    activate_argument = next(
        (
            keyword.value
            for keyword in command_calls[0].keywords
            if keyword.arg == "activate_missing"
        ),
        None,
    )
    if not (
        isinstance(activate_argument, ast.Call)
        and isinstance(activate_argument.func, ast.Name)
        and activate_argument.func.id == "bool"
        and len(activate_argument.args) == 1
        and isinstance(activate_argument.args[0], ast.Attribute)
        and isinstance(activate_argument.args[0].value, ast.Name)
        and activate_argument.args[0].value.id == "args"
        and activate_argument.args[0].attr == "activate_missing"
    ):
        raise ReleaseError(
            "packaged service-bootstrap does not forward the explicit activation flag"
        )

    bundle_function = _ast_function(
        bundle_path, "bootstrap_package_bundles", "service bundle helper"
    )
    activation_calls = [
        node
        for node in ast.walk(bundle_function)
        if isinstance(node, ast.Call) and _call_name(node) == "activate_release"
    ]
    guarded_activation = [
        node
        for node in ast.walk(bundle_function)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "activate_missing"
        and any(_contains_node(node, call) for call in activation_calls)
    ]
    stage_calls = [
        node
        for node in ast.walk(bundle_function)
        if isinstance(node, ast.Call) and _call_name(node) == "stage_release"
    ]
    forbidden_state_writes = [
        node
        for node in ast.walk(bundle_function)
        if isinstance(node, ast.Call)
        and _call_name(node)
        in {"replace", "symlink_to", "unlink", "rename", "write_text", "write_bytes"}
    ]
    if (
        len(activation_calls) != 1
        or len(guarded_activation) != 1
        or len(stage_calls) != 1
        or forbidden_state_writes
    ):
        raise ReleaseError(
            "packaged service bootstrap must stage releases and guard its only activation "
            "behind activate_missing"
        )
    stage_function = _ast_function(bundle_path, "stage_release", "service bundle helper")
    stage_body = stage_function.body
    if (
        stage_body
        and isinstance(stage_body[0], ast.Expr)
        and isinstance(stage_body[0].value, ast.Constant)
        and isinstance(stage_body[0].value.value, str)
    ):
        stage_body = stage_body[1:]
    stage_source = "\n".join(ast.unparse(statement) for statement in stage_body).lower()
    if any(
        token in stage_source
        for token in ("activate_release", "symlink_to", "os.replace", "active pointer")
    ):
        raise ReleaseError("packaged stage_release helper must not change active Product pointers")

    parser_function = _ast_function(cli_path, "build_parser", "Cyrene CLI")
    activation_options = [
        node
        for node in ast.walk(parser_function)
        if isinstance(node, ast.Call)
        and _call_name(node) == "add_argument"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "--activate-missing"
    ]
    if len(activation_options) != 1:
        raise ReleaseError(
            "packaged service-bootstrap activation option must be explicit and unique"
        )
    activation_action = {
        keyword.arg: keyword.value
        for keyword in activation_options[0].keywords
        if keyword.arg in {"action", "default"}
    }
    if (
        not isinstance(activation_action.get("action"), ast.Constant)
        or activation_action["action"].value != "store_true"
        or "default" in activation_action
    ):
        raise ReleaseError("packaged service-bootstrap must leave activation disabled by default")


def _inspect_deb_initialization(
    deb_path: Path,
    profile_id: str,
    receipt: dict[str, Any],
    *,
    verify_attestations: bool,
    gh_executable: str,
) -> dict[str, Any]:
    """Extract the actual DEB and prove its marker, service index, and scripts are stage-only."""

    _require_file(deb_path, f"DEB for {profile_id}")
    with tempfile.TemporaryDirectory(prefix="cyrene-native-deb-verify-") as temporary:
        extraction = Path(temporary) / "root"
        control = Path(temporary) / "control"
        extraction.mkdir()
        control.mkdir()
        for operation, destination in (("--extract", extraction), ("--control", control)):
            result = subprocess.run(
                ["dpkg-deb", operation, str(deb_path), str(destination)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise ReleaseError(f"dpkg-deb could not inspect the {profile_id} package")
        marker_path = extraction / INSTALL_CONTRACT_PATH
        index_path = extraction / SERVICE_ARTIFACT_INDEX_PATH
        original_root = extraction / "usr/share/cyrene/verified-service-artifacts" / profile_id
        original_index_path = original_root / "index.json"
        _require_file(marker_path, f"DEB install contract marker for {profile_id}")
        _require_file(index_path, f"DEB service artifact index for {profile_id}")
        _require_file(original_index_path, f"DEB original verified service index for {profile_id}")
        marker = _read_json_object(marker_path, f"DEB install contract marker for {profile_id}")
        index = _read_json_object(index_path, f"DEB embedded service index for {profile_id}")
        original_index = _read_json_object(
            original_index_path, f"DEB original service index for {profile_id}"
        )
        if _sha256(original_index_path) != _sha256(index_path) or original_index != index:
            raise ReleaseError(
                "DEB original verified service index differs from staged payload index"
            )
        script_hashes = _static_installer_activation_check(control)
        expected_marker_keys = {
            *INSTALL_CONTRACT_POLICY,
            "targetProfile",
            "serviceArtifactsIndexSha256",
            "services",
            "maintainerScriptsSha256",
        }
        if set(marker) != expected_marker_keys:
            raise ReleaseError("DEB install contract marker has unexpected fields")
        expected_policy = {
            **INSTALL_CONTRACT_POLICY,
            "targetProfile": profile_id,
            "serviceArtifactsIndexSha256": _sha256(index_path),
            "services": _marker_services_from_index(index, profile_id, receipt),
            "maintainerScriptsSha256": script_hashes,
        }
        if marker != expected_policy:
            raise ReleaseError(
                "DEB install contract marker differs from actual package bytes or policy"
            )
        service_rows = _validate_index_against_receipt(
            index,
            receipt,
            profile_id,
            original_root,
            verify_attestations=verify_attestations,
            gh_executable=gh_executable,
        )
        original_rows = _validate_index_against_receipt(
            original_index,
            receipt,
            profile_id,
            original_root,
            verify_attestations=False,
            gh_executable=gh_executable,
        )
        if original_rows != service_rows:
            raise ReleaseError(
                "DEB original verified service bytes differ from staged index evidence"
            )
        _validate_deb_private_runtime(extraction, deb_path, receipt)
        _validate_packaged_stage_only_behavior(extraction, profile_id)
        return {
            "targetId": profile_id,
            "debSha256": _sha256(deb_path),
            "markerPath": f"/{INSTALL_CONTRACT_PATH}",
            "markerSha256": _sha256(marker_path),
            "serviceArtifactsIndexPath": f"/{SERVICE_ARTIFACT_INDEX_PATH}",
            "serviceArtifactsIndexSha256": _sha256(index_path),
            "maintainerScriptsSha256": script_hashes,
            "services": {
                component_id: _marker_service_summary(row)
                for component_id, row in service_rows.items()
            },
            "checks": {
                "serviceActivation": "deferred",
                "brokerAction": "preserve-existing",
                "oldRuntimeAction": "preserve",
                "maintainerScriptsStaticScan": "passed",
                "verifiedServiceBytesPreserved": "passed",
                "freshBrokerUnavailable": "fail-closed",
                "upgradeState": "preserve-existing",
                "activeRuntimePointers": "preserve-existing",
                "pinnedPrivateRuntime": "passed",
            },
        }


def _marker_service_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Return only the immutable tuple fields that the DEB contract marker must state."""

    return {
        "componentId": row["componentId"],
        "repository": row["repository"],
        "releaseId": row["releaseId"],
        "source": row["source"],
        "artifact": {
            "sha256": row["artifact"]["sha256"],
            "sizeBytes": row["artifact"]["sizeBytes"],
        },
        "manifest": {
            "sha256": row["manifest"]["sha256"],
            "manifestDigest": row["manifest"]["manifestDigest"],
        },
        "attestation": {
            "sha256": row["attestation"]["sha256"],
            "repository": row["attestation"]["repository"],
            "workflow": row["attestation"]["workflow"],
            "subjectName": row["attestation"]["subjectName"],
            "sourceRef": row["attestation"]["sourceRef"],
            "sourceCommit": row["attestation"]["sourceCommit"],
        },
    }


def _marker_service_summary_from_tuple(row: dict[str, Any]) -> dict[str, Any]:
    """Translate a full official fetch receipt tuple into the DEB marker projection."""

    artifact = row.get("artifact")
    manifest = row.get("manifest")
    if not isinstance(artifact, dict) or not isinstance(manifest, dict):
        raise ReleaseError("Product tuple is missing artifact or manifest evidence")
    attestation = artifact.get("attestation")
    if not isinstance(attestation, dict):
        raise ReleaseError("Product tuple is missing artifact attestation evidence")
    return {
        "componentId": row.get("componentId"),
        "repository": row.get("repository"),
        "releaseId": row.get("releaseId"),
        "source": row.get("source"),
        "artifact": {"sha256": artifact.get("sha256"), "sizeBytes": artifact.get("sizeBytes")},
        "manifest": {
            "sha256": manifest.get("rawSha256"),
            "manifestDigest": manifest.get("declaredDigest"),
        },
        "attestation": {
            "sha256": attestation.get("sha256"),
            "repository": attestation.get("repository"),
            "workflow": attestation.get("workflow"),
            "subjectName": attestation.get("subjectName"),
            "sourceRef": attestation.get("sourceRef"),
            "sourceCommit": attestation.get("sourceCommit"),
        },
    }


def _marker_services_from_index(
    index: dict[str, Any], profile_id: str, receipt: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Build exact marker service identity rows from embedded bytes and signed source tuples."""

    rows = _service_index_summary(index, profile_id)
    receipt_rows = _receipt_product_rows(receipt, profile_id)
    if set(rows) != set(receipt_rows):
        raise ReleaseError("DEB service marker cannot be built from an incomplete source receipt")
    result = {}
    for component_id, row in rows.items():
        proof = receipt_rows[component_id]
        if (
            row["releaseId"] != proof["releaseId"]
            or row["source"] != proof["source"]
            or proof.get("targetId") != profile_id
            or row["artifact"]["sha256"] != proof["artifact"]["sha256"]
            or row["artifact"]["sizeBytes"] != proof["artifact"]["sizeBytes"]
            or row["manifest"]["sha256"] != proof["manifest"]["rawSha256"]
            or row["attestation"]["sha256"] != proof["artifact"]["attestation"]["sha256"]
        ):
            raise ReleaseError(
                f"DEB service marker input differs from signed receipt: {component_id}"
            )
        result[component_id] = _marker_service_summary(row)
    return result


def _record_inputs(arguments: argparse.Namespace) -> int:
    """Write a source receipt from actual catalog metadata and official fetch reports."""

    inputs = _read_dispatch_inputs(arguments.inputs, arguments.source_ref, arguments.source_commit)
    catalog = _read_json_object(arguments.catalog, "verified Workspace catalog")
    catalog_metadata = _read_json_object(arguments.catalog_metadata, "verified catalog metadata")
    channel = _source_channel(arguments.source_ref)
    catalog_match = CATALOG_TAG_PATTERN.fullmatch(inputs["workspaceCatalog"]["releaseId"])
    assert catalog_match is not None
    catalog_commit = catalog_match.group(2)
    if (
        catalog_metadata.get("schemaVersion") != 1
        or catalog_metadata.get("repository") != REPOSITORY
        or catalog_metadata.get("workflow") != CATALOG_WORKFLOW
        or catalog_metadata.get("channel") != channel
        or catalog_metadata.get("releaseId") != inputs["workspaceCatalog"]["releaseId"]
        or catalog_metadata.get("sourceCommit") != catalog_commit
        or catalog_metadata.get("sourceRef") not in CHANNEL_REFS[channel]
        or catalog_metadata.get("catalogSha256") != f"sha256:{inputs['workspaceCatalog']['sha256']}"
        or _sha256(arguments.catalog) != inputs["workspaceCatalog"]["sha256"]
    ):
        raise ReleaseError(
            "verified catalog metadata does not match the exact user-selected release"
        )
    if arguments.catalog_attestation is None:
        raise ReleaseError("verified Workspace catalog detached attestation bundle is required")
    _require_file(arguments.catalog_attestation, "verified Workspace catalog attestation")
    _run_attestation_verify(
        arguments.catalog,
        arguments.catalog_attestation,
        repository=REPOSITORY,
        workflow=CATALOG_WORKFLOW,
        source_ref=catalog_metadata["sourceRef"],
        source_commit=catalog_commit,
        gh_executable=os.environ.get("GH_EXECUTABLE", "gh"),
        label="Workspace component catalog",
    )
    if catalog.get("schemaVersion") != 1:
        raise ReleaseError("verified catalog has an unsupported schema version")

    reports: dict[str, Path] = {}
    attestations: dict[str, Path] = {}
    for raw in arguments.fetch_report:
        key, separator, path = raw.partition("=")
        if not separator or not key or key in reports:
            raise ReleaseError("each --fetch-report must be a unique key=path pair")
        reports[key] = Path(path)
    for raw in arguments.component_attestation:
        key, separator, path = raw.partition("=")
        if not separator or not key or key in attestations:
            raise ReleaseError("each --component-attestation must be a unique key=path pair")
        attestations[key] = Path(path)
    requests = _component_requests(inputs)
    expected_report_keys = {request.key for request in requests}
    if reports.keys() != expected_report_keys or attestations.keys() != expected_report_keys:
        missing = sorted(expected_report_keys - reports.keys())
        extra = sorted((reports.keys() | attestations.keys()) - expected_report_keys)
        missing_attestations = sorted(expected_report_keys - attestations.keys())
        raise ReleaseError(
            "fetch report/attestation sets differ from required tuples; "
            f"missing={missing}, missing_attestations={missing_attestations}, extra={extra}"
        )
    tuples = [
        _verified_component_record(
            request,
            reports[request.key],
            attestations[request.key],
            catalog,
            channel,
        )
        for request in requests
    ]

    by_key = {item["key"]: item for item in tuples}
    for product in PRODUCTS:
        low = by_key[f"product/{product}/22.04"]["source"]
        high = by_key[f"product/{product}/24.04"]["source"]
        if low != high:
            raise ReleaseError(f"Product {product} target tuples do not share one source commit")
    for component in PLATFORM_CORE_COMPONENTS:
        low = by_key[f"platform-core/{component}/22.04"]["source"]
        high = by_key[f"platform-core/{component}/24.04"]["source"]
        if low != high:
            raise ReleaseError(f"Platform {component} target tuples do not share one source commit")

    receipt = {
        "schemaVersion": 1,
        "workspaceSource": {
            "repository": REPOSITORY,
            "ref": arguments.source_ref,
            "commit": arguments.source_commit,
            "workflow": f"{REPOSITORY}/{WORKFLOW_PATH}",
        },
        "releaseInputs": {
            "nativeProfiles": inputs["nativeProfiles"],
            "workspaceCatalog": {
                "repository": catalog_metadata["repository"],
                "workflow": catalog_metadata["workflow"],
                "releaseId": catalog_metadata["releaseId"],
                "source": {"ref": catalog_metadata["sourceRef"], "commit": catalog_commit},
                "assetName": "component-catalog-v1.json",
                "sha256": inputs["workspaceCatalog"]["sha256"],
                "attestationBundleSha256": (
                    _sha256(arguments.catalog_attestation)
                    if arguments.catalog_attestation is not None
                    else None
                ),
            },
            "platformReleaseId": inputs["platformReleaseId"],
            "productReleaseIds": inputs["productReleaseIds"],
        },
        "verifiedTuples": tuples,
        "pythonRuntimeLock": {
            "path": arguments.python_runtime_lock.as_posix(),
            "sha256": _sha256(arguments.python_runtime_lock),
            "document": _read_json_object(arguments.python_runtime_lock, "Python runtime lock"),
        },
        "workspaceReleaseLock": {
            "path": arguments.release_lock.as_posix(),
            "sha256": _sha256(arguments.release_lock),
            "document": _read_json_object(arguments.release_lock, "Workspace release lock"),
        },
    }
    _write_json(arguments.output, receipt)
    print(f"Wrote source receipt for {len(tuples)} verified component target tuples")
    return 0


def _validate_source_receipt(
    receipt: dict[str, Any], source: dict[str, Any], *, require_safe_initialization: bool = True
) -> None:
    """Validate all offline locator and component tuple bindings in the signed receipt."""

    expected_top_keys = {
        "schemaVersion",
        "workspaceSource",
        "releaseInputs",
        "verifiedTuples",
        "pythonRuntimeLock",
        "workspaceReleaseLock",
    }
    actual_top_keys = set(receipt)
    allowed_top_keys = expected_top_keys | {"safeInitialization"}
    if (
        not expected_top_keys.issubset(actual_top_keys)
        or not actual_top_keys.issubset(allowed_top_keys)
        or (require_safe_initialization and "safeInitialization" not in actual_top_keys)
        or receipt.get("schemaVersion") != 1
    ):
        raise ReleaseError("source receipt fields do not match the approved schema version")
    if receipt.get("workspaceSource") != source:
        raise ReleaseError("source receipt is not bound to the release source identity")
    channel = _source_channel(str(source.get("ref", "")))
    release_inputs = receipt.get("releaseInputs")
    if not isinstance(release_inputs, dict) or set(release_inputs) != {
        "nativeProfiles",
        "workspaceCatalog",
        "platformReleaseId",
        "productReleaseIds",
    }:
        raise ReleaseError("source receipt releaseInputs do not match the approved schema")
    if release_inputs.get("nativeProfiles") != list(PROFILE_IDS):
        raise ReleaseError("source receipt does not bind both native Python profiles")
    catalog = release_inputs.get("workspaceCatalog")
    if not isinstance(catalog, dict) or set(catalog) != {
        "repository",
        "workflow",
        "releaseId",
        "source",
        "assetName",
        "sha256",
        "attestationBundleSha256",
    }:
        raise ReleaseError("source receipt catalog evidence fields are malformed")
    catalog_source = catalog.get("source")
    catalog_match = CATALOG_TAG_PATTERN.fullmatch(str(catalog.get("releaseId", "")))
    if (
        catalog.get("repository") != REPOSITORY
        or catalog.get("workflow") != CATALOG_WORKFLOW
        or catalog.get("assetName") != "component-catalog-v1.json"
        or catalog_match is None
        or catalog_match.group(1) != channel
        or not isinstance(catalog_source, dict)
        or catalog_source.get("commit") != catalog_match.group(2)
        or catalog_source.get("ref") not in CHANNEL_REFS[channel]
        or not SHA256_PATTERN.fullmatch(str(catalog.get("sha256", "")))
        or not SHA256_PATTERN.fullmatch(str(catalog.get("attestationBundleSha256", "")))
    ):
        raise ReleaseError("source receipt Workspace catalog source/digest binding is invalid")
    platform_release_id = release_inputs.get("platformReleaseId")
    _release_source(platform_release_id, channel, "receipt platformReleaseId")
    products = release_inputs.get("productReleaseIds")
    if not isinstance(products, dict) or set(products) != set(PRODUCTS):
        raise ReleaseError("source receipt must bind exactly the five Product release tags")
    for product, release_id in products.items():
        _release_source(release_id, channel, f"receipt Product {product}")

    inputs = {
        "nativeProfiles": list(PROFILE_IDS),
        "workspaceCatalog": {"releaseId": catalog["releaseId"], "sha256": catalog["sha256"]},
        "platformReleaseId": platform_release_id,
        "productReleaseIds": products,
    }
    requests = _component_requests(inputs)
    tuple_rows = receipt.get("verifiedTuples")
    if not isinstance(tuple_rows, list):
        raise ReleaseError("source receipt verifiedTuples must be an array")
    tuples = {row.get("key"): row for row in tuple_rows if isinstance(row, dict)}
    if set(tuples) != {request.key for request in requests} or len(tuples) != len(tuple_rows):
        raise ReleaseError("source receipt must bind each required component target exactly once")
    for request in requests:
        row = tuples[request.key]
        expected_source_commit = _release_source(request.release_id, channel, request.key)
        row_source = row.get("source")
        if (
            row.get("repository") != request.repository
            or row.get("releaseId") != request.release_id
            or row.get("componentId") != request.component_id
            or row.get("targetId") != request.target_id
            or row.get("artifactKind") != request.artifact_kind
            or not isinstance(row.get("target"), dict)
            or not isinstance(row_source, dict)
            or row_source.get("commit") != expected_source_commit
            or row_source.get("ref") not in CHANNEL_REFS[channel]
        ):
            raise ReleaseError(f"source receipt tuple identity is invalid: {request.key}")
        for name, attested_subject in (
            ("index", "component-release-index-v1.json"),
            ("manifest", None),
        ):
            value = row.get(name)
            if not isinstance(value, dict) or set(value) != {
                "assetName",
                "rawSha256",
                "declaredDigest",
                "attestation",
            }:
                raise ReleaseError(f"source receipt {request.key} {name} evidence is malformed")
            if not ASSET_NAME_PATTERN.fullmatch(str(value.get("assetName", ""))):
                raise ReleaseError(f"source receipt {request.key} {name} asset name is unsafe")
            if not SHA256_PATTERN.fullmatch(str(value.get("rawSha256", ""))):
                raise ReleaseError(f"source receipt {request.key} {name} raw digest is malformed")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("declaredDigest", ""))):
                raise ReleaseError(
                    f"source receipt {request.key} {name} declared digest is malformed"
                )
            attestation = value.get("attestation")
            expected_subject = attested_subject or row.get("artifact", {}).get("assetName")
            if (
                not isinstance(attestation, dict)
                or attestation.get("repository") != request.repository
                or attestation.get("workflow") != f"{request.repository}/{COMPONENT_WORKFLOW}"
                or attestation.get("predicateType") != PREDICATE_TYPE
                or attestation.get("subjectName") != expected_subject
                or attestation.get("sourceRef") != row_source.get("ref")
                or attestation.get("sourceCommit") != row_source.get("commit")
            ):
                raise ReleaseError(
                    f"source receipt {request.key} {name} attestation identity is invalid"
                )
        artifact = row.get("artifact")
        if not isinstance(artifact, dict) or set(artifact) != {
            "assetName",
            "sha256",
            "sizeBytes",
            "format",
            "kind",
            "attestation",
        }:
            raise ReleaseError(f"source receipt {request.key} artifact evidence is malformed")
        artifact_attestation = artifact.get("attestation")
        if (
            not ASSET_NAME_PATTERN.fullmatch(str(artifact.get("assetName", "")))
            or not SHA256_PATTERN.fullmatch(str(artifact.get("sha256", "")))
            or not isinstance(artifact.get("sizeBytes"), int)
            or isinstance(artifact.get("sizeBytes"), bool)
            or artifact.get("sizeBytes") < 0
            or not isinstance(artifact_attestation, dict)
            or artifact_attestation.get("assetName")
            != f"{artifact.get('assetName')}.attestation.jsonl"
            or not SHA256_PATTERN.fullmatch(str(artifact_attestation.get("sha256", "")))
            or artifact_attestation.get("repository") != request.repository
            or artifact_attestation.get("workflow") != f"{request.repository}/{COMPONENT_WORKFLOW}"
            or artifact_attestation.get("predicateType") != PREDICATE_TYPE
            or artifact_attestation.get("subjectName") != artifact.get("assetName")
            or artifact_attestation.get("sourceRef") != row_source.get("ref")
            or artifact_attestation.get("sourceCommit") != row_source.get("commit")
        ):
            raise ReleaseError(f"source receipt {request.key} artifact SHA/attestation is invalid")

    runtime_lock = receipt.get("pythonRuntimeLock")
    if (
        not isinstance(runtime_lock, dict)
        or set(runtime_lock) != {"path", "sha256", "document"}
        or runtime_lock.get("path") != "packaging/python-runtime.lock.json"
        or not SHA256_PATTERN.fullmatch(str(runtime_lock.get("sha256", "")))
        or not isinstance(runtime_lock.get("document"), dict)
    ):
        raise ReleaseError("source receipt Python runtime lock identity is invalid")
    runtime_python = runtime_lock["document"].get("python")
    runtime_archive = runtime_python.get("archive") if isinstance(runtime_python, dict) else None
    runtime_archive_release = (
        runtime_archive.get("release") if isinstance(runtime_archive, dict) else None
    )
    if (
        not isinstance(runtime_python, dict)
        or runtime_python.get("implementation") != "CPython"
        or runtime_python.get("version") != "3.12.14"
        or runtime_python.get("target") != "x86_64-unknown-linux-gnu"
        or runtime_python.get("installRoot") != "/opt/cyrene/python/3.12.14"
        or not isinstance(runtime_archive, dict)
        or not isinstance(runtime_archive_release, dict)
        or runtime_archive_release.get("repository") != "astral-sh/python-build-standalone"
        or runtime_archive_release.get("tag") != "20260929"
        or runtime_archive_release.get("immutable") is not True
        or runtime_archive_release.get("commit") != LOCKED_PYTHON_RELEASE_COMMIT
        or runtime_archive.get("sha256") != LOCKED_PYTHON_ARCHIVE_SHA256
        or runtime_archive.get("size") != 34277962
        or runtime_archive.get("url")
        != "https://github.com/astral-sh/python-build-standalone/releases/download/20260929/cpython-3.12.14%2B20260929-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
    ):
        raise ReleaseError(
            "source receipt Python runtime lock does not identify the pinned CPython 3.12.14 archive"
        )
    resolver = runtime_lock["document"].get("buildResolver")
    uv_archive = resolver.get("binaryArchive") if isinstance(resolver, dict) else None
    uv_release = uv_archive.get("release") if isinstance(uv_archive, dict) else None
    if (
        not isinstance(resolver, dict)
        or not isinstance(uv_archive, dict)
        or not isinstance(uv_release, dict)
        or resolver.get("tool") != "uv"
        or resolver.get("version") != "0.12.21"
        or resolver.get("installedPath") != "/opt/cyrene/uv/0.12.21/uv"
        or resolver.get("usage")
        != {
            "build": "resolve-frozen-python-distribution-mapping",
            "runtime": "explicit-trainer-environment-prepare",
            "automaticRuntimeBootstrap": False,
        }
        or uv_archive.get("url")
        != "https://github.com/astral-sh/uv/releases/download/0.12.21/uv-x86_64-unknown-linux-gnu.tar.gz"
        or uv_archive.get("sha256") != LOCKED_UV_ARCHIVE_SHA256
        or uv_archive.get("executableSha256") != LOCKED_UV_EXECUTABLE_SHA256
        or uv_archive.get("size") != 19782662
        or uv_release.get("repository") != "astral-sh/uv"
        or uv_release.get("tag") != "0.12.21"
        or uv_release.get("commit") != LOCKED_UV_RELEASE_COMMIT
        or uv_release.get("immutable") is not True
        or uv_release.get("assetId") != 599238020
    ):
        raise ReleaseError("source receipt uv resolver does not match the pinned official release")
    release_lock = receipt.get("workspaceReleaseLock")
    if (
        not isinstance(release_lock, dict)
        or set(release_lock) != {"path", "sha256", "document"}
        or release_lock.get("path") != "release-lock.json"
        or not SHA256_PATTERN.fullmatch(str(release_lock.get("sha256", "")))
        or not isinstance(release_lock.get("document"), dict)
    ):
        raise ReleaseError("source receipt Workspace release lock identity is invalid")
    release_profiles = release_lock["document"].get("nativePythonProfiles")
    if not isinstance(release_profiles, dict) or set(release_profiles) != set(PROFILE_IDS):
        raise ReleaseError(
            "source receipt release lock does not declare exactly both native profiles"
        )
    for profile_id in PROFILE_IDS:
        profile = release_profiles.get(profile_id)
        if (
            not isinstance(profile, dict)
            or profile.get("os") != "linux"
            or profile.get("distribution") != "ubuntu"
            or profile.get("distributionVersion") != PROFILE_UBUNTU[profile_id]
            or profile.get("architecture") != "x86_64"
            or profile.get("pythonVersion") != "3.12.14"
            or profile.get("pythonExecutable") != "/opt/cyrene/python/3.12.14/bin/python3.12"
        ):
            raise ReleaseError(
                f"source receipt release lock has an invalid native profile: {profile_id}"
            )
    safe_init = receipt.get("safeInitialization")
    if require_safe_initialization:
        if (
            not isinstance(safe_init, dict)
            or set(safe_init) != {"verified", "mode", "targets"}
            or safe_init.get("verified") is not True
            or safe_init.get("mode") != "stage-only-verified-published-bytes"
        ):
            raise ReleaseError("source receipt has no verified stage-only initialization evidence")
        evidence = safe_init.get("targets")
        if (
            not isinstance(evidence, list)
            or {row.get("targetId") for row in evidence if isinstance(row, dict)}
            != set(PROFILE_IDS)
            or len(evidence) != len(PROFILE_IDS)
        ):
            raise ReleaseError(
                "source receipt stage-only evidence must cover both DEBs exactly once"
            )
        for row in evidence:
            if not isinstance(row, dict) or set(row) != {
                "targetId",
                "debSha256",
                "markerPath",
                "markerSha256",
                "serviceArtifactsIndexPath",
                "serviceArtifactsIndexSha256",
                "maintainerScriptsSha256",
                "services",
                "checks",
            }:
                raise ReleaseError("source receipt stage-only target evidence is malformed")
            if (
                row.get("targetId") not in PROFILE_IDS
                or not SHA256_PATTERN.fullmatch(str(row.get("debSha256", "")))
                or row.get("markerPath") != f"/{INSTALL_CONTRACT_PATH}"
                or not SHA256_PATTERN.fullmatch(str(row.get("markerSha256", "")))
                or row.get("serviceArtifactsIndexPath") != f"/{SERVICE_ARTIFACT_INDEX_PATH}"
                or not SHA256_PATTERN.fullmatch(str(row.get("serviceArtifactsIndexSha256", "")))
                or row.get("checks")
                != {
                    "serviceActivation": "deferred",
                    "brokerAction": "preserve-existing",
                    "oldRuntimeAction": "preserve",
                    "maintainerScriptsStaticScan": "passed",
                    "verifiedServiceBytesPreserved": "passed",
                    "freshBrokerUnavailable": "fail-closed",
                    "upgradeState": "preserve-existing",
                    "activeRuntimePointers": "preserve-existing",
                    "pinnedPrivateRuntime": "passed",
                }
            ):
                raise ReleaseError("source receipt stage-only target identity/checks are invalid")
            script_hashes = row.get("maintainerScriptsSha256")
            if (
                not isinstance(script_hashes, dict)
                or set(script_hashes) != set(MAINTAINER_SCRIPT_NAMES)
                or any(not SHA256_PATTERN.fullmatch(str(value)) for value in script_hashes.values())
            ):
                raise ReleaseError("source receipt maintainer script hashes are malformed")
            profile_products = _receipt_product_rows(receipt, row["targetId"])
            services = row.get("services")
            if not isinstance(services, dict) or set(services) != {
                component_id for _, component_id in PRODUCTS.values()
            }:
                raise ReleaseError("source receipt stage-only evidence is missing a Product tuple")
            for component_id, service in services.items():
                proof = profile_products.get(component_id)
                if proof is None or service != _marker_service_summary_from_tuple(proof):
                    raise ReleaseError(
                        f"source receipt stage-only Product evidence differs from the tuple: {component_id}"
                    )


def _asset_names(version: str) -> dict[str, str]:
    """Return safe deterministic names for all payload subjects in one release."""

    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+~:-]{0,63}", version):
        raise ReleaseError("package version must be a safe Debian version segment")
    return {
        "22.04": f"cyrene_{version}_ubuntu-22.04_amd64.deb",
        "24.04": f"cyrene_{version}_ubuntu-24.04_amd64.deb",
    }


def _release_version_from_lock(lock: dict[str, Any]) -> str:
    """Read the package version from the exact source-bound release lock name."""

    name = lock.get("name")
    if not isinstance(name, str) or not name.startswith("cyrene-"):
        raise ReleaseError("Workspace release lock name must begin with cyrene-")
    version = name.removeprefix("cyrene-")
    _asset_names(version)
    return version


def _assemble(arguments: argparse.Namespace) -> int:
    """Create the two DEB assets, release manifest, receipt, and SHA256SUMS."""

    if not SHA1_PATTERN.fullmatch(arguments.source_commit):
        raise ReleaseError("source commit must be a full lowercase 40-character Git SHA")
    channel = _source_channel(arguments.source_ref)
    expected_tag = f"native-installer-{channel}-{arguments.source_commit}"
    if arguments.release_id != expected_tag:
        raise ReleaseError("release ID must bind the exact Workspace channel and source commit")
    source_receipt = _read_json_object(arguments.source_receipt, "source receipt")
    source = source_receipt.get("workspaceSource")
    if (
        source_receipt.get("schemaVersion") != 1
        or not isinstance(source, dict)
        or source.get("repository") != REPOSITORY
        or source.get("ref") != arguments.source_ref
        or source.get("commit") != arguments.source_commit
        or source.get("workflow") != f"{REPOSITORY}/{WORKFLOW_PATH}"
    ):
        raise ReleaseError("source receipt is not bound to this exact Workspace workflow run")
    if arguments.source_receipt.name != "native-installer-source-receipt-v1.json":
        raise ReleaseError("source receipt file must use its canonical release asset name")
    _validate_source_receipt(source_receipt, source, require_safe_initialization=False)
    if arguments.version != _release_version_from_lock(
        source_receipt["workspaceReleaseLock"]["document"]
    ):
        raise ReleaseError("package version does not match the source-bound Workspace release lock")
    _require_file(arguments.deb_22, "Ubuntu 22.04 DEB")
    _require_file(arguments.deb_24, "Ubuntu 24.04 DEB")
    if arguments.deb_22 == arguments.deb_24:
        raise ReleaseError("Ubuntu 22.04 and 24.04 must be separate build outputs")
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ReleaseError("release output directory must be empty")
    names = _asset_names(arguments.version)
    deb_assets = []
    target_evidence = []
    for ubuntu, source_path in (("22.04", arguments.deb_22), ("24.04", arguments.deb_24)):
        destination = output / names[ubuntu]
        destination.write_bytes(source_path.read_bytes())
        profile_id = f"linux-ubuntu-{ubuntu}-x86_64-python-3.12"
        target_evidence.append(
            _inspect_deb_initialization(
                destination,
                profile_id,
                source_receipt,
                verify_attestations=True,
                gh_executable=arguments.gh_executable,
            )
        )
        deb_assets.append(
            {
                "targetId": profile_id,
                "assetName": destination.name,
                "sha256": _sha256(destination),
                "sizeBytes": destination.stat().st_size,
            }
        )
    source_receipt["safeInitialization"] = {
        "verified": True,
        "mode": "stage-only-verified-published-bytes",
        "targets": target_evidence,
    }
    _validate_source_receipt(source_receipt, source)
    lock_path = arguments.python_runtime_lock
    _require_file(lock_path, "Python runtime lock release asset")
    lock_metadata = source_receipt["pythonRuntimeLock"]
    if (
        lock_path.name != "python-runtime.lock.json"
        or _sha256(lock_path) != lock_metadata["sha256"]
        or _read_json_object(lock_path, "Python runtime lock release asset")
        != lock_metadata["document"]
    ):
        raise ReleaseError(
            "Python runtime lock release asset differs from the verified source receipt"
        )
    lock_destination = output / lock_path.name
    lock_destination.write_bytes(lock_path.read_bytes())
    lock_asset = {
        "assetName": lock_destination.name,
        "sha256": _sha256(lock_destination),
        "sizeBytes": lock_destination.stat().st_size,
    }
    release_lock_path = arguments.release_lock
    _require_file(release_lock_path, "Workspace release lock release asset")
    release_lock_metadata = source_receipt["workspaceReleaseLock"]
    if (
        release_lock_path.name != "release-lock.json"
        or _sha256(release_lock_path) != release_lock_metadata["sha256"]
        or _read_json_object(release_lock_path, "Workspace release lock release asset")
        != release_lock_metadata["document"]
    ):
        raise ReleaseError("Workspace release lock asset differs from verified source receipt")
    release_lock_destination = output / release_lock_path.name
    release_lock_destination.write_bytes(release_lock_path.read_bytes())
    release_lock_asset = {
        "assetName": release_lock_destination.name,
        "sha256": _sha256(release_lock_destination),
        "sizeBytes": release_lock_destination.stat().st_size,
    }
    catalog_path = arguments.catalog
    catalog_attestation_path = arguments.catalog_attestation
    _require_file(catalog_path, "verified Workspace catalog release asset")
    _require_file(catalog_attestation_path, "verified Workspace catalog attestation bundle")
    catalog_evidence = source_receipt["releaseInputs"]["workspaceCatalog"]
    if (
        catalog_path.name != catalog_evidence["assetName"]
        or _sha256(catalog_path) != catalog_evidence["sha256"]
        or _sha256(catalog_attestation_path) != catalog_evidence["attestationBundleSha256"]
    ):
        raise ReleaseError(
            "Workspace catalog bytes differ from the selected immutable source receipt"
        )
    _run_attestation_verify(
        catalog_path,
        catalog_attestation_path,
        repository=REPOSITORY,
        workflow=CATALOG_WORKFLOW,
        source_ref=catalog_evidence["source"]["ref"],
        source_commit=catalog_evidence["source"]["commit"],
        gh_executable=arguments.gh_executable,
        label="Workspace catalog release asset",
    )
    catalog_destination = output / catalog_path.name
    catalog_destination.write_bytes(catalog_path.read_bytes())
    catalog_bundle_destination = output / f"{catalog_path.name}.attestation.jsonl"
    catalog_bundle_destination.write_bytes(catalog_attestation_path.read_bytes())
    catalog_asset = {
        "assetName": catalog_destination.name,
        "sha256": _sha256(catalog_destination),
        "sizeBytes": catalog_destination.stat().st_size,
        "attestationAssetName": catalog_bundle_destination.name,
        "attestationSha256": _sha256(catalog_bundle_destination),
    }
    receipt_bytes = (
        json.dumps(source_receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    (output / arguments.source_receipt.name).write_bytes(receipt_bytes)
    receipt_asset = {
        "assetName": arguments.source_receipt.name,
        "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "sizeBytes": len(receipt_bytes),
    }
    manifest = {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "releaseId": arguments.release_id,
        "version": arguments.version,
        "channel": channel,
        "source": source,
        "workflow": f"{REPOSITORY}/{WORKFLOW_PATH}",
        "run": {"id": arguments.run_id, "attempt": arguments.run_attempt},
        "targets": deb_assets,
        "sourceReceipt": receipt_asset,
        "workspaceCatalog": catalog_asset,
        "pythonRuntimeLock": lock_asset,
        "workspaceReleaseLock": release_lock_asset,
        "checksumAsset": "SHA256SUMS",
    }
    manifest_path = output / "native-installer-release-v1.json"
    _write_json(manifest_path, manifest)
    checksums = [(output / asset["assetName"]).name for asset in deb_assets] + [
        arguments.source_receipt.name,
        catalog_destination.name,
        lock_destination.name,
        release_lock_destination.name,
        manifest_path.name,
    ]
    (output / "SHA256SUMS").write_text(
        "".join(f"{_sha256(output / name)}  {name}\n" for name in sorted(checksums)),
        encoding="ascii",
    )
    print(f"Assembled {arguments.release_id} with two native target DEBs")
    return 0


def _parse_checksums(path: Path) -> dict[str, str]:
    """Parse a strict SHA256SUMS file without duplicate or path-traversal names."""

    result: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="ascii").splitlines(), start=1):
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]{0,199})", line)
        if match is None:
            raise ReleaseError(f"SHA256SUMS line {line_number} has an invalid format")
        digest, name = match.groups()
        if name in result:
            raise ReleaseError(f"SHA256SUMS contains duplicate asset {name!r}")
        result[name] = digest
    return result


def verify_release_directory(
    directory: Path,
    *,
    expected_repository: str = REPOSITORY,
    expected_source_ref: str | None = None,
    expected_source_commit: str | None = None,
    verify_attestations: bool = True,
    gh_executable: str = "gh",
) -> dict[str, Any]:
    """Check the offline release envelope and each detached GitHub SLSA bundle.

    Args:
        directory: Directory with downloaded immutable release assets.
        expected_repository: Fixed Workspace signer repository identity.
        expected_source_ref: Optional source ref assertion from the administrator.
        expected_source_commit: Optional source SHA assertion from the administrator.
        verify_attestations: Run ``gh attestation verify`` against local bundle files.
        gh_executable: GitHub CLI executable used for detached bundle verification.
    Returns:
        Parsed release manifest after all byte and provenance checks pass.
    """

    root = directory.resolve()
    manifest_path = root / "native-installer-release-v1.json"
    receipt_path = root / "native-installer-source-receipt-v1.json"
    checksums_path = root / "SHA256SUMS"
    for path, label in (
        (manifest_path, "release manifest"),
        (receipt_path, "source receipt"),
        (checksums_path, "SHA256SUMS"),
    ):
        _require_file(path, label)
    manifest = _read_json_object(manifest_path, "release manifest")
    receipt = _read_json_object(receipt_path, "source receipt")
    source = manifest.get("source")
    if (
        set(manifest)
        != {
            "schemaVersion",
            "repository",
            "releaseId",
            "version",
            "channel",
            "source",
            "workflow",
            "run",
            "targets",
            "sourceReceipt",
            "workspaceCatalog",
            "pythonRuntimeLock",
            "workspaceReleaseLock",
            "checksumAsset",
        }
        or manifest.get("schemaVersion") != 1
        or manifest.get("repository") != expected_repository
        or manifest.get("workflow") != f"{expected_repository}/{WORKFLOW_PATH}"
        or not isinstance(source, dict)
        or set(source) != {"repository", "ref", "commit"}
        or not SHA1_PATTERN.fullmatch(str(source.get("commit", "")))
        or source.get("repository") != expected_repository
        or source.get("ref") not in set().union(*CHANNEL_REFS.values())
        or manifest.get("releaseId")
        != (f"native-installer-{_source_channel(source['ref'])}-{source['commit']}")
    ):
        raise ReleaseError(
            "release manifest does not identify the fixed repository/workflow/source"
        )
    if expected_source_ref is not None and source.get("ref") != expected_source_ref:
        raise ReleaseError("release manifest source ref differs from the expected ref")
    if expected_source_commit is not None and source.get("commit") != expected_source_commit:
        raise ReleaseError("release manifest source commit differs from the expected commit")
    receipt_source = receipt.get("workspaceSource")
    if receipt_source != {
        "repository": expected_repository,
        "ref": source["ref"],
        "commit": source["commit"],
        "workflow": f"{expected_repository}/{WORKFLOW_PATH}",
    }:
        raise ReleaseError("source receipt source identity differs from the release manifest")
    if manifest.get("channel") != _source_channel(source["ref"]):
        raise ReleaseError("release manifest channel differs from its source ref")
    _validate_source_receipt(receipt, receipt_source)
    run = manifest.get("run")
    if (
        not isinstance(run, dict)
        or set(run) != {"id", "attempt"}
        or not isinstance(run.get("id"), int)
        or isinstance(run.get("id"), bool)
        or run.get("id") < 1
        or not isinstance(run.get("attempt"), int)
        or isinstance(run.get("attempt"), bool)
        or run.get("attempt") < 1
    ):
        raise ReleaseError("release manifest workflow run identity is malformed")
    names = _asset_names(str(manifest.get("version", "")))
    if manifest.get("version") != _release_version_from_lock(
        receipt["workspaceReleaseLock"]["document"]
    ):
        raise ReleaseError("release package version differs from the source-bound Workspace lock")
    targets = manifest.get("targets")
    if not isinstance(targets, list) or len(targets) != 2:
        raise ReleaseError("release manifest must contain exactly two target DEBs")
    targets_by_id = {
        target.get("targetId"): target for target in targets if isinstance(target, dict)
    }
    expected_targets = {
        PROFILE_IDS[0]: names["22.04"],
        PROFILE_IDS[1]: names["24.04"],
    }
    if set(targets_by_id) != set(expected_targets):
        raise ReleaseError(
            "release manifest target profile set is not exactly Ubuntu 22.04 and 24.04"
        )
    expected_subjects = set(RELEASE_SUBJECT_NAMES)
    expected_asset_hashes: dict[str, str] = {}
    for target_id, asset_name in expected_targets.items():
        target = targets_by_id[target_id]
        if (
            set(target) != {"targetId", "assetName", "sha256", "sizeBytes"}
            or target.get("targetId") != target_id
            or target.get("assetName") != asset_name
        ):
            raise ReleaseError(f"release manifest has a non-canonical asset name for {target_id}")
        path = root / asset_name
        _require_file(path, f"release asset {asset_name}")
        digest = _sha256(path)
        if target.get("sha256") != digest or target.get("sizeBytes") != path.stat().st_size:
            raise ReleaseError(f"release asset bytes differ from manifest: {asset_name}")
        expected_asset_hashes[asset_name] = digest
        expected_subjects.add(asset_name)
        proof = next(
            row for row in receipt["safeInitialization"]["targets"] if row["targetId"] == target_id
        )
        actual_proof = _inspect_deb_initialization(
            path,
            target_id,
            receipt,
            verify_attestations=verify_attestations,
            gh_executable=gh_executable,
        )
        if actual_proof != proof:
            raise ReleaseError(f"offline DEB safe-initialization proof differs for {target_id}")
    receipt_asset = manifest.get("sourceReceipt")
    if (
        not isinstance(receipt_asset, dict)
        or set(receipt_asset) != {"assetName", "sha256", "sizeBytes"}
        or receipt_asset.get("assetName") != receipt_path.name
        or receipt_asset.get("sha256") != _sha256(receipt_path)
        or receipt_asset.get("sizeBytes") != receipt_path.stat().st_size
        or manifest.get("checksumAsset") != checksums_path.name
    ):
        raise ReleaseError("release manifest source receipt/checksum asset metadata is invalid")
    expected_asset_hashes[receipt_path.name] = _sha256(receipt_path)
    expected_asset_hashes[manifest_path.name] = _sha256(manifest_path)
    catalog_metadata = manifest.get("workspaceCatalog")
    catalog_receipt = receipt["releaseInputs"]["workspaceCatalog"]
    if (
        not isinstance(catalog_metadata, dict)
        or set(catalog_metadata)
        != {"assetName", "sha256", "sizeBytes", "attestationAssetName", "attestationSha256"}
        or catalog_metadata.get("assetName") != catalog_receipt["assetName"]
        or catalog_metadata.get("attestationAssetName")
        != f"{catalog_metadata.get('assetName')}.attestation.jsonl"
    ):
        raise ReleaseError("release manifest Workspace catalog metadata is malformed")
    catalog_path = root / catalog_metadata["assetName"]
    catalog_bundle_path = root / catalog_metadata["attestationAssetName"]
    _require_file(catalog_path, "Workspace catalog release asset")
    _require_file(catalog_bundle_path, "Workspace catalog detached attestation bundle")
    if (
        _sha256(catalog_path) != catalog_receipt["sha256"]
        or catalog_metadata.get("sha256") != _sha256(catalog_path)
        or catalog_metadata.get("sizeBytes") != catalog_path.stat().st_size
        or _sha256(catalog_bundle_path) != catalog_receipt["attestationBundleSha256"]
        or catalog_metadata.get("attestationSha256") != _sha256(catalog_bundle_path)
    ):
        raise ReleaseError(
            "Workspace catalog bytes or attestation bundle differ from source receipt"
        )
    catalog_source = catalog_receipt["source"]
    if verify_attestations:
        _run_attestation_verify(
            catalog_path,
            catalog_bundle_path,
            repository=REPOSITORY,
            workflow=CATALOG_WORKFLOW,
            source_ref=catalog_source["ref"],
            source_commit=catalog_source["commit"],
            gh_executable=gh_executable,
            label="offline Workspace catalog",
        )
    expected_asset_hashes[catalog_path.name] = _sha256(catalog_path)
    lock_metadata = manifest.get("pythonRuntimeLock")
    if (
        not isinstance(lock_metadata, dict)
        or set(lock_metadata) != {"assetName", "sha256", "sizeBytes"}
        or lock_metadata.get("assetName") != "python-runtime.lock.json"
    ):
        raise ReleaseError("release manifest Python runtime lock metadata is malformed")
    lock_path = root / lock_metadata["assetName"]
    _require_file(lock_path, "Python runtime lock release asset")
    lock_document = _read_json_object(lock_path, "Python runtime lock release asset")
    receipt_lock = receipt["pythonRuntimeLock"]
    if (
        lock_metadata.get("sha256") != _sha256(lock_path)
        or lock_metadata.get("sizeBytes") != lock_path.stat().st_size
        or receipt_lock.get("sha256") != _sha256(lock_path)
        or receipt_lock.get("document") != lock_document
    ):
        raise ReleaseError(
            "release Python runtime lock bytes differ from the signed source receipt"
        )
    expected_asset_hashes[lock_path.name] = _sha256(lock_path)
    expected_subjects.add(lock_path.name)
    release_lock_metadata = manifest.get("workspaceReleaseLock")
    if (
        not isinstance(release_lock_metadata, dict)
        or set(release_lock_metadata) != {"assetName", "sha256", "sizeBytes"}
        or release_lock_metadata.get("assetName") != "release-lock.json"
    ):
        raise ReleaseError("release manifest Workspace lock metadata is malformed")
    release_lock_path = root / release_lock_metadata["assetName"]
    _require_file(release_lock_path, "Workspace release lock release asset")
    release_lock_document = _read_json_object(
        release_lock_path, "Workspace release lock release asset"
    )
    receipt_release_lock = receipt["workspaceReleaseLock"]
    if (
        release_lock_metadata.get("sha256") != _sha256(release_lock_path)
        or release_lock_metadata.get("sizeBytes") != release_lock_path.stat().st_size
        or receipt_release_lock.get("sha256") != _sha256(release_lock_path)
        or receipt_release_lock.get("document") != release_lock_document
    ):
        raise ReleaseError("release Workspace lock bytes differ from the signed source receipt")
    expected_asset_hashes[release_lock_path.name] = _sha256(release_lock_path)
    expected_subjects.add(release_lock_path.name)
    checksum_values = _parse_checksums(checksums_path)
    if checksum_values != expected_asset_hashes:
        raise ReleaseError("SHA256SUMS does not enumerate exactly the release payload and receipt")

    subject_paths = [root / name for name in sorted(expected_subjects)]
    for path in subject_paths:
        _require_file(path, f"attestation subject {path.name}")
        bundle = root / f"{path.name}.attestation.jsonl"
        _require_file(bundle, f"detached attestation bundle for {path.name}")
        if bundle.stat().st_size == 0:
            raise ReleaseError(f"detached attestation bundle is empty: {bundle.name}")
        if verify_attestations:
            command = [
                gh_executable,
                "attestation",
                "verify",
                str(path),
                "--bundle",
                str(bundle),
                "--repo",
                expected_repository,
                "--signer-workflow",
                f"{expected_repository}/{WORKFLOW_PATH}",
                "--source-ref",
                source["ref"],
                "--source-digest",
                source["commit"],
                "--predicate-type",
                PREDICATE_TYPE,
            ]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                raise ReleaseError(
                    f"offline attestation verification failed for {path.name}: "
                    f"{result.stderr.strip() or 'gh attestation verify returned nonzero'}"
                )
    expected_directory_names = (
        set(expected_asset_hashes)
        | {f"{name}.attestation.jsonl" for name in expected_subjects}
        | {catalog_bundle_path.name}
    )
    actual_directory_names = set()
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file():
            raise ReleaseError(f"release directory contains a non-asset entry: {path.name}")
        actual_directory_names.add(path.name)
    if actual_directory_names != expected_directory_names:
        raise ReleaseError(
            "release directory contains missing or unexpected immutable release assets"
        )
    return manifest


def _api_json(
    method: str,
    url: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
    missing_ok: bool = False,
    octets: bytes | None = None,
) -> tuple[int, Any]:
    """Send one authenticated GitHub API request without exposing credentials."""

    if not url.startswith("https://api.github.com/") and not url.startswith(
        f"https://{UPLOAD_HOST}/"
    ):
        raise ReleaseError("GitHub API request escaped trusted HTTPS hosts")
    data = octets
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif octets is not None:
        headers["Content-Type"] = "application/octet-stream"
        headers["Accept"] = "application/vnd.github+json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            if not body:
                return response.status, None
            try:
                return response.status, json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return response.status, body
    except urllib.error.HTTPError as error:
        if missing_ok and error.code == 404:
            return 404, None
        raise ReleaseError(f"GitHub API request failed with HTTP {error.code}") from error
    except (OSError, urllib.error.URLError) as error:
        raise ReleaseError("GitHub API request could not be completed over HTTPS") from error


def _settings_token() -> str:
    """Read the dedicated immutable-settings token without printing its value."""

    token = os.environ.get(IMMUTABLE_SETTINGS_READ_TOKEN, "")
    if not token:
        raise ReleaseError(
            f"configure {IMMUTABLE_SETTINGS_READ_TOKEN} with repository Administration read access"
        )
    return token


def _write_token() -> str:
    """Read the workflow's content-write credential without printing it."""

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ReleaseError("GitHub content-write credential is not available")
    return token


def verify_immutable_releases_enabled(repository: str = REPOSITORY) -> None:
    """Use the dedicated secret to prove immutable releases are enabled."""

    token = _settings_token()
    status, settings = _api_json(
        "GET",
        f"https://api.github.com/repos/{repository}/immutable-releases",
        token=token,
    )
    if status != 200 or not isinstance(settings, dict) or settings.get("enabled") is not True:
        raise ReleaseError("repository immutable releases are not confirmed enabled")


def _tag_commit(token: str, tag: str) -> str | None:
    """Resolve a GitHub tag ref through lightweight or bounded annotated tags."""

    quoted = urllib.parse.quote(tag, safe="-")
    status, reference = _api_json(
        "GET",
        f"https://api.github.com/repos/{REPOSITORY}/git/ref/tags/{quoted}",
        token=token,
        missing_ok=True,
    )
    if status == 404:
        return None
    obj = reference.get("object", {}) if isinstance(reference, dict) else {}
    for _ in range(8):
        kind, sha = obj.get("type"), obj.get("sha")
        if kind == "commit" and isinstance(sha, str):
            return sha
        if kind != "tag" or not isinstance(sha, str):
            raise ReleaseError(f"release source tag {tag} does not resolve to a Git commit")
        _, annotated = _api_json(
            "GET", f"https://api.github.com/repos/{REPOSITORY}/git/tags/{sha}", token=token
        )
        obj = annotated.get("object", {}) if isinstance(annotated, dict) else {}
    raise ReleaseError(f"release source tag {tag} exceeds annotated-tag depth limit")


def _ensure_source_tag(token: str, tag: str, source_commit: str) -> None:
    """Create or verify the immutable source tag before creating its release."""

    actual = _tag_commit(token, tag)
    if actual is None:
        status, _ = _api_json(
            "POST",
            f"https://api.github.com/repos/{REPOSITORY}/git/refs",
            token=token,
            payload={"ref": f"refs/tags/{tag}", "sha": source_commit},
        )
        if status not in {200, 201}:
            raise ReleaseError("GitHub did not confirm source tag creation")
        actual = _tag_commit(token, tag)
    if actual != source_commit:
        raise ReleaseError(f"source tag {tag} does not resolve to the selected source commit")


def _release_by_tag(token: str, tag: str) -> dict[str, Any] | None:
    """Read an exact release tag, preserving a 404 as an absent release."""

    status, result = _api_json(
        "GET",
        f"{RELEASE_API_BASE}/releases/tags/{urllib.parse.quote(tag, safe='-')}",
        token=token,
        missing_ok=True,
    )
    if status == 404:
        return None
    if not isinstance(result, dict):
        raise ReleaseError("GitHub exact release lookup returned a malformed object")
    return result


def _preflight_release(arguments: argparse.Namespace) -> int:
    """Verify immutable settings, exact source identity, and release tag state."""

    _read_dispatch_inputs(arguments.inputs, arguments.source_ref, arguments.source_commit)
    verify_immutable_releases_enabled()
    channel = _source_channel(arguments.source_ref)
    release_id = f"native-installer-{channel}-{arguments.source_commit}"
    token = _write_token()
    _ensure_source_tag(token, release_id, arguments.source_commit)
    existing = _release_by_tag(token, release_id)
    if existing is not None and existing.get("draft") is not True:
        raise ReleaseError("an immutable installer release already exists for this source commit")
    if existing is not None and existing.get("prerelease") is not (channel == "preview"):
        raise ReleaseError(
            "existing installer draft channel does not match the selected source ref"
        )
    print(f"Immutable releases and source tag are valid for {release_id}")
    return 0


def _release_asset_hashes(directory: Path) -> dict[str, str]:
    """Hash every release asset, including detached proof bundles."""

    paths = sorted(path for path in directory.iterdir() if path.is_file())
    result: dict[str, str] = {}
    for path in paths:
        if not ASSET_NAME_PATTERN.fullmatch(path.name):
            raise ReleaseError(f"release asset has an unsafe name: {path.name!r}")
        result[path.name] = _sha256(path)
    return result


def _get_release_by_id(token: str, release_id: int, *, retries: int = 4) -> dict[str, Any]:
    """Read a just-created draft by API database ID with bounded visibility retry."""

    for attempt in range(retries):
        try:
            status, result = _api_json(
                "GET", f"{RELEASE_API_BASE}/releases/{release_id}", token=token, missing_ok=True
            )
        except ReleaseError:
            status, result = 0, None
        if status == 200 and isinstance(result, dict):
            return result
        if attempt + 1 < retries:
            time.sleep(min(2**attempt, 4))
    raise ReleaseError("GitHub did not expose the release by its create-response database ID")


def _create_draft(token: str, tag: str, source_commit: str, channel: str) -> dict[str, Any]:
    """Create one draft and retain its database ID for all subsequent operations."""

    existing = _release_by_tag(token, tag)
    if existing is not None:
        if existing.get("draft") is not True or existing.get("tag_name") != tag:
            raise ReleaseError("release tag already belongs to a published or mismatched release")
        # Drafts from interrupted runs are reused only after their exact source tag is proven.
        return existing
    status, result = _api_json(
        "POST",
        f"{RELEASE_API_BASE}/releases",
        token=token,
        payload={
            "tag_name": tag,
            "target_commitish": source_commit,
            "name": f"Cyrene native installer {tag}",
            "body": "Ubuntu 22.04 and 24.04 native bootstrap packages with detached SLSA proofs.",
            "draft": True,
            "prerelease": channel == "preview",
        },
    )
    if status not in {200, 201} or not isinstance(result, dict):
        raise ReleaseError(
            "GitHub did not return the created draft; retry the workflow so it can resolve the exact tag once"
        )
    if not isinstance(result.get("id"), int) or isinstance(result.get("id"), bool):
        raise ReleaseError("GitHub create-release response has no database ID")
    return result


def _upload_asset(token: str, draft: dict[str, Any], path: Path) -> None:
    """Upload one binary asset to the selected draft release by database ID."""

    template = draft.get("upload_url")
    if not isinstance(template, str):
        raise ReleaseError("draft create response is missing its asset upload URL")
    upload_base = template.split("{", 1)[0]
    parsed = urllib.parse.urlsplit(upload_base)
    if parsed.scheme != "https" or parsed.hostname != UPLOAD_HOST:
        raise ReleaseError("draft asset upload URL is outside uploads.github.com")
    query = urllib.parse.urlencode({"name": path.name})
    status, result = _api_json(
        "POST",
        f"{upload_base}?{query}",
        token=token,
        octets=path.read_bytes(),
    )
    if status not in {200, 201} or not isinstance(result, dict) or result.get("name") != path.name:
        raise ReleaseError(f"GitHub did not confirm upload of release asset {path.name}")


def _publish(arguments: argparse.Namespace) -> int:
    """Upload verified subjects through one draft ID and publish the immutable release."""

    manifest = verify_release_directory(
        arguments.directory,
        expected_source_ref=arguments.source_ref,
        expected_source_commit=arguments.source_commit,
    )
    verify_immutable_releases_enabled()
    token = _write_token()
    release_id = manifest["releaseId"]
    channel = manifest["channel"]
    source_commit = manifest["source"]["commit"]
    _ensure_source_tag(token, release_id, source_commit)
    draft = _create_draft(token, release_id, source_commit, channel)
    # Reuse the response ID (or the explicit draft's ID) instead of polling a draft tag URL.
    database_id = draft.get("id")
    if not isinstance(database_id, int) or isinstance(database_id, bool):
        raise ReleaseError("draft response does not expose a numeric database ID")
    live = _get_release_by_id(token, database_id)
    if live.get("tag_name") != release_id or live.get("draft") is not True:
        raise ReleaseError("draft database ID resolves to a different or published release")
    if live.get("prerelease") is not (channel == "preview"):
        raise ReleaseError("draft release channel differs from the verified source ref")
    expected_assets = _release_asset_hashes(arguments.directory)
    existing_assets = live.get("assets", [])
    if existing_assets:
        raise ReleaseError(
            "draft already contains assets; inspect and remove the failed draft before retrying"
        )
    for name in sorted(expected_assets):
        _upload_asset(token, live, arguments.directory / name)
    uploaded = _get_release_by_id(token, database_id)
    actual_assets = uploaded.get("assets", [])
    if (
        uploaded.get("draft") is not True
        or {item.get("name") for item in actual_assets if isinstance(item, dict)}
        != set(expected_assets)
        or len(actual_assets) != len(expected_assets)
        or any(item.get("state") != "uploaded" for item in actual_assets)
    ):
        raise ReleaseError("draft assets are incomplete before publication")
    status, _ = _api_json(
        "PATCH",
        f"{RELEASE_API_BASE}/releases/{database_id}",
        token=token,
        payload={"draft": False},
    )
    if status not in {200, 201}:
        raise ReleaseError("GitHub did not confirm release publication")
    published = _get_release_by_id(token, database_id)
    if (
        published.get("tag_name") != release_id
        or published.get("draft") is not False
        or published.get("immutable") is not True
        or published.get("prerelease") is not (channel == "preview")
        or {item.get("name") for item in published.get("assets", []) if isinstance(item, dict)}
        != set(expected_assets)
    ):
        raise ReleaseError("published release did not confirm immutable tag/channel/assets by ID")
    print(f"Published immutable release {release_id} using release database ID {database_id}")
    return 0


def _validate_inputs_command(arguments: argparse.Namespace) -> int:
    """Validate manual input locators and emit their canonical JSON form."""

    result = _read_dispatch_inputs(arguments.inputs, arguments.source_ref, arguments.source_commit)
    _write_json(arguments.output, result)
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        channel = _source_channel(arguments.source_ref)
        catalog_match = CATALOG_TAG_PATTERN.fullmatch(result["workspaceCatalog"]["releaseId"])
        assert catalog_match is not None
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"channel={channel}\n")
            output.write(f"catalog_release_id={result['workspaceCatalog']['releaseId']}\n")
            output.write(f"catalog_sha256={result['workspaceCatalog']['sha256']}\n")
            output.write(f"platform_release_id={result['platformReleaseId']}\n")
            output.write(
                f"platform_source_commit={_release_source(result['platformReleaseId'], channel, 'platformReleaseId')}\n"
            )
            for product, release_id in sorted(result["productReleaseIds"].items()):
                output.write(f"{product}_release_id={release_id}\n")
                output.write(
                    f"{product}_source_commit={_release_source(release_id, channel, product)}\n"
                )
    print("Exact native profile, catalog, Platform, and Product release locators are valid")
    return 0


def _write_fetch_plan(arguments: argparse.Namespace) -> int:
    """Expand validated locators into the fixed, exact catalog component fetch set."""

    inputs = _read_dispatch_inputs(arguments.inputs, arguments.source_ref, arguments.source_commit)
    channel = _source_channel(arguments.source_ref)
    plan = [
        {
            "key": request.key,
            "repository": request.repository,
            "componentId": request.component_id,
            "targetId": request.target_id,
            "releaseId": request.release_id,
            "sourceCommit": _release_source(request.release_id, channel, request.key),
        }
        for request in _component_requests(inputs)
    ]
    _write_json(arguments.output, {"schemaVersion": 1, "channel": channel, "requests": plan})
    print(f"Wrote exact fetch plan for {len(plan)} catalog component target tuples")
    return 0


def _write_service_artifacts(arguments: argparse.Namespace) -> int:
    """Stage verified Product archive/manifest/bundle bytes for one target DEB."""

    if arguments.profile not in PROFILE_IDS:
        raise ReleaseError("target profile must be one of the two locked native Python profiles")
    receipt = _read_json_object(arguments.source_receipt, "verified source receipt")
    _validate_source_receipt(
        receipt, receipt.get("workspaceSource", {}), require_safe_initialization=False
    )
    ubuntu = PROFILE_UBUNTU[arguments.profile]
    reports: dict[str, Path] = {}
    attestations: dict[str, Path] = {}
    for raw, destination, label in (
        *((item, reports, "--service-report") for item in arguments.service_report),
        *(
            (item, attestations, "--component-attestation")
            for item in arguments.component_attestation
        ),
    ):
        key, separator, path = raw.partition("=")
        if not separator or key in destination:
            raise ReleaseError(f"each {label} must use one unique service=path pair")
        destination[key] = Path(path)
    required = {f"product/{product}/{ubuntu}" for product in PRODUCTS}
    if reports.keys() != required or attestations.keys() != required:
        raise ReleaseError(
            "service report and detached-bundle inputs must contain all five exact Products"
        )
    tuple_map = {row["key"]: row for row in receipt["verifiedTuples"]}
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ReleaseError("verified service artifact output directory must be empty")
    services: dict[str, Any] = {}
    for product, (repository, component_id) in sorted(PRODUCTS.items()):
        key = f"product/{product}/{ubuntu}"
        report = _read_json_object(reports[key], f"Product fetch report {key}")
        manifest = report.get("manifest")
        index = report.get("index")
        source = index.get("source", {}) if isinstance(index, dict) else {}
        artifact = manifest.get("artifact", {}) if isinstance(manifest, dict) else {}
        artifact_path = Path(str(report.get("artifactPath", "")))
        manifest_path = Path(str(report.get("manifestPath", "")))
        index_path = reports[key].parent / "component-release-index-v1.json"
        bundle_path = attestations[key]
        record = tuple_map[key]
        _require_file(index_path, f"Product release index {key}")
        _require_file(artifact_path, f"Product release artifact {key}")
        _require_file(manifest_path, f"Product release manifest {key}")
        _require_file(bundle_path, f"Product detached artifact attestation {key}")
        if (
            not isinstance(manifest, dict)
            or not isinstance(index, dict)
            or index.get("repository") != repository
            or index.get("channel") != _source_channel(receipt["workspaceSource"]["ref"])
            or source.get("repository") != f"https://github.com/{repository}"
            or source != record["source"] | {"repository": f"https://github.com/{repository}"}
            or manifest.get("releaseId") != record["releaseId"]
            or manifest.get("componentId") != component_id
            or manifest.get("source") != source
            or manifest.get("target")
            != _target_from_catalog(
                _read_json_object(arguments.catalog, "verified Workspace catalog"),
                component_id,
                arguments.profile,
            )
            or _sha256(index_path) != record["index"]["rawSha256"]
            or _sha256(artifact_path) != record["artifact"]["sha256"]
            or _sha256(manifest_path) != record["manifest"]["rawSha256"]
            or _sha256(bundle_path) != record["artifact"]["attestation"]["sha256"]
            or artifact_path.name != record["artifact"]["assetName"]
            or manifest_path.name != record["manifest"]["assetName"]
            or artifact.get("sizeBytes") != record["artifact"]["sizeBytes"]
            or artifact.get("kind") != "python-bundle"
            or bundle_path.name != f"{artifact_path.name}.attestation.jsonl"
        ):
            raise ReleaseError(f"Product artifact bytes differ from verified receipt for {product}")
        product_dir = output / product
        product_dir.mkdir()
        artifact_destination = product_dir / artifact_path.name
        manifest_destination = product_dir / manifest_path.name
        bundle_destination = product_dir / bundle_path.name
        artifact_destination.write_bytes(artifact_path.read_bytes())
        manifest_destination.write_bytes(manifest_path.read_bytes())
        bundle_destination.write_bytes(bundle_path.read_bytes())
        services[component_id] = {
            "componentId": component_id,
            "repository": repository,
            "releaseId": record["releaseId"],
            "source": record["source"],
            "artifact": {
                "path": artifact_destination.relative_to(output).as_posix(),
                "sha256": record["artifact"]["sha256"],
                "sizeBytes": record["artifact"]["sizeBytes"],
                "kind": record["artifact"]["kind"],
                "format": record["artifact"]["format"],
            },
            "manifest": {
                "path": manifest_destination.relative_to(output).as_posix(),
                "sha256": record["manifest"]["rawSha256"],
                "manifestDigest": record["manifest"]["declaredDigest"],
            },
            "attestation": {
                "path": bundle_destination.relative_to(output).as_posix(),
                "sha256": record["artifact"]["attestation"]["sha256"],
                "repository": record["artifact"]["attestation"]["repository"],
                "workflow": record["artifact"]["attestation"]["workflow"],
                "predicateType": record["artifact"]["attestation"]["predicateType"],
                "subjectName": record["artifact"]["attestation"]["subjectName"],
                "sourceRef": record["artifact"]["attestation"]["sourceRef"],
                "sourceCommit": record["artifact"]["attestation"]["sourceCommit"],
            },
        }
    _write_json(
        output / "index.json",
        {"schemaVersion": 1, "targetProfile": arguments.profile, "services": services},
    )
    print(f"Staged verified published Product bytes for {arguments.profile}")
    return 0


def _parser() -> argparse.ArgumentParser:
    """Build the focused release producer/verification command-line interface."""

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    locators = commands.add_parser("validate-inputs")
    locators.add_argument("--inputs", type=Path, required=True)
    locators.add_argument("--source-ref", required=True)
    locators.add_argument("--source-commit", required=True)
    locators.add_argument("--output", type=Path, required=True)
    locators.set_defaults(handler=_validate_inputs_command)

    fetch_plan = commands.add_parser("write-fetch-plan")
    fetch_plan.add_argument("--inputs", type=Path, required=True)
    fetch_plan.add_argument("--source-ref", required=True)
    fetch_plan.add_argument("--source-commit", required=True)
    fetch_plan.add_argument("--output", type=Path, required=True)
    fetch_plan.set_defaults(handler=_write_fetch_plan)

    receipt = commands.add_parser("record-inputs")
    receipt.add_argument("--inputs", type=Path, required=True)
    receipt.add_argument("--source-ref", required=True)
    receipt.add_argument("--source-commit", required=True)
    receipt.add_argument("--catalog", type=Path, required=True)
    receipt.add_argument("--catalog-metadata", type=Path, required=True)
    receipt.add_argument("--catalog-attestation", type=Path, required=True)
    receipt.add_argument("--python-runtime-lock", type=Path, required=True)
    receipt.add_argument("--release-lock", type=Path, required=True)
    receipt.add_argument("--fetch-report", action="append", default=[], metavar="KEY=PATH")
    receipt.add_argument("--component-attestation", action="append", default=[], metavar="KEY=PATH")
    receipt.add_argument("--output", type=Path, required=True)
    receipt.set_defaults(handler=_record_inputs)

    service_artifacts = commands.add_parser("stage-service-artifacts")
    service_artifacts.add_argument("--profile", required=True)
    service_artifacts.add_argument("--source-receipt", type=Path, required=True)
    service_artifacts.add_argument("--catalog", type=Path, required=True)
    service_artifacts.add_argument(
        "--service-report", action="append", default=[], metavar="SERVICE=PATH"
    )
    service_artifacts.add_argument(
        "--component-attestation", action="append", default=[], metavar="SERVICE=PATH"
    )
    service_artifacts.add_argument("--output", type=Path, required=True)
    service_artifacts.set_defaults(handler=_write_service_artifacts)

    assemble = commands.add_parser("assemble")
    assemble.add_argument("--release-id", required=True)
    assemble.add_argument("--source-ref", required=True)
    assemble.add_argument("--source-commit", required=True)
    assemble.add_argument("--source-receipt", type=Path, required=True)
    assemble.add_argument("--catalog", type=Path, required=True)
    assemble.add_argument("--catalog-attestation", type=Path, required=True)
    assemble.add_argument("--python-runtime-lock", type=Path, required=True)
    assemble.add_argument("--release-lock", type=Path, required=True)
    assemble.add_argument("--version", required=True)
    assemble.add_argument("--run-id", type=int, required=True)
    assemble.add_argument("--run-attempt", type=int, required=True)
    assemble.add_argument("--deb-22", type=Path, required=True)
    assemble.add_argument("--deb-24", type=Path, required=True)
    assemble.add_argument("--output", type=Path, required=True)
    assemble.add_argument("--gh-executable", default="gh")
    assemble.set_defaults(handler=_assemble)

    verify = commands.add_parser("verify")
    verify.add_argument("--directory", type=Path, required=True)
    verify.add_argument("--expected-repository", default=REPOSITORY)
    verify.add_argument("--expected-source-ref")
    verify.add_argument("--expected-source-commit")
    verify.add_argument("--gh-executable", default="gh")
    verify.add_argument("--skip-attestations", action="store_true")
    verify.set_defaults(
        handler=lambda args: (
            verify_release_directory(
                args.directory,
                expected_repository=args.expected_repository,
                expected_source_ref=args.expected_source_ref,
                expected_source_commit=args.expected_source_commit,
                verify_attestations=not args.skip_attestations,
                gh_executable=args.gh_executable,
            )
            and print("Offline release hashes, source binding, and detached attestations are valid")
            or 0
        )
    )

    preflight = commands.add_parser("preflight")
    preflight.add_argument("--inputs", type=Path, required=True)
    preflight.add_argument("--source-ref", required=True)
    preflight.add_argument("--source-commit", required=True)
    preflight.set_defaults(handler=_preflight_release)

    publish = commands.add_parser("publish")
    publish.add_argument("--directory", type=Path, required=True)
    publish.add_argument("--source-ref", required=True)
    publish.add_argument("--source-commit", required=True)
    publish.set_defaults(handler=_publish)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    """Run one native installer release action and report safe error details."""

    arguments = _parser().parse_args(argv)
    try:
        result = arguments.handler(arguments)
        return int(result or 0)
    except (OSError, ReleaseError, ValueError, KeyError, TypeError) as error:
        print(f"native installer release failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
