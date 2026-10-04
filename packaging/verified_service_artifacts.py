#!/usr/bin/env python3
"""Verify and stage published Product bundles without rebuilding their bytes.

The builder keeps the release archives, outer manifests, and detached
attestations byte-for-byte. It also extracts each verified archive into the
package's stage-only service directory so the existing installer can copy an
immutable release without activating it.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = "index.json"
DEFAULT_CATALOG = WORKSPACE_ROOT / "packaging" / "component-catalog-bootstrap-v1.json"
DEFAULT_RELEASE_LOCK = WORKSPACE_ROOT / "release-lock.json"
SERVICES = ("catalyst", "exchange", "navigator", "reactor", "yield")
SERVICE_COMPONENTS = {f"cyrene-{service}": service for service in SERVICES}
SERVICE_REPOSITORIES = {
    "catalyst": "Cyrene-Catalyst",
    "exchange": "Cyrene-Exchange",
    "navigator": "Cyrene-Navigator",
    "reactor": "Cyrene-Reactor",
    "yield": "Cyrene-Yield",
}
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RELEASE_PATTERN = re.compile(r"^(stable|preview)-([0-9a-f]{40})$")
PRODUCT_TARGETS = {
    "linux-ubuntu-22.04-x86_64-python-3.12": {
        "os": "linux",
        "osVersion": "22.04",
        "distribution": "ubuntu",
        "distributionVersion": "22.04",
        "architecture": "x86_64",
        "abi": "glibc-2.35",
        "runtime": "python:3.12",
    },
    "linux-ubuntu-24.04-x86_64-python-3.12": {
        "os": "linux",
        "osVersion": "24.04",
        "distribution": "ubuntu",
        "distributionVersion": "24.04",
        "architecture": "x86_64",
        "abi": "glibc-2.39",
        "runtime": "python:3.12",
    },
}
MAX_RELEASE_METADATA_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 200_000
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024 * 1024
GH_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "CyreneVerifiedServiceArtifactBuilder/1",
    "X-GitHub-Api-Version": "2022-11-28",
}


class VerifiedServiceArtifactError(RuntimeError):
    """A release input did not satisfy the pinned Product artifact contract."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise VerifiedServiceArtifactError(f"{label} is missing or unsafe: {path}")
    try:
        content = path.read_bytes()
        value = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise VerifiedServiceArtifactError(f"cannot read {label} at {path}: {error}") from error
    if not isinstance(value, dict):
        raise VerifiedServiceArtifactError(f"{label} must be a JSON object: {path}")
    return value, content


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VerifiedServiceArtifactError(f"{label} must be a JSON object")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerifiedServiceArtifactError(f"{label} must be a non-empty string")
    return value


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise VerifiedServiceArtifactError(f"cannot hash input file {path}: {error}") from error
    return digest.hexdigest()


def _canonical_jcs(value: Any) -> bytes:
    """Encode the safe JSON subset used by component release manifests."""

    def encode(item: Any) -> str:
        if item is None:
            return "null"
        if item is True:
            return "true"
        if item is False:
            return "false"
        if isinstance(item, int):
            if abs(item) > 9_007_199_254_740_991:
                raise VerifiedServiceArtifactError("manifest integer exceeds the safe JSON range")
            return str(item)
        if isinstance(item, float):
            raise VerifiedServiceArtifactError(
                "component manifest must not contain floating-point values"
            )
        if isinstance(item, str):
            if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                raise VerifiedServiceArtifactError(
                    "manifest contains an unpaired Unicode surrogate"
                )
            return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if isinstance(item, list):
            return "[" + ",".join(encode(member) for member in item) + "]"
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise VerifiedServiceArtifactError("manifest object keys must be strings")
            keys = sorted(item, key=lambda key: key.encode("utf-16be"))
            return "{" + ",".join(encode(key) + ":" + encode(item[key]) for key in keys) + "}"
        raise VerifiedServiceArtifactError(f"unsupported manifest value: {type(item).__name__}")

    return encode(value).encode("utf-8")


def _manifest_digest(manifest: dict[str, Any]) -> str:
    unsigned = dict(manifest)
    unsigned.pop("manifestDigest", None)
    return "sha256:" + _sha256_bytes(_canonical_jcs(unsigned))


def _canonical_product_asset_names(
    component_id: str, target_profile: str, target_fields: dict[str, Any]
) -> tuple[str, str]:
    """Return Product's pinned archive and manifest basenames for one target."""
    expected_target = PRODUCT_TARGETS.get(target_profile)
    if expected_target is None or target_fields != expected_target:
        raise VerifiedServiceArtifactError(
            f"catalog target fields do not match the canonical Product naming profile: {target_profile}"
        )

    year, month = expected_target["osVersion"].split(".")
    glibc_version = expected_target["abi"].removeprefix("glibc-").replace(".", "-")
    python_version = expected_target["runtime"].removeprefix("python:")
    python_version_slug = python_version.replace(".", "-")
    archive_name = (
        f"{component_id}-linux-ubuntu-{expected_target['osVersion']}-x86_64-"
        f"python-{python_version}.tar.gz"
    )
    manifest_name = (
        f"{component_id}-linux-ubuntu-{year}-{month}-x86-64-"
        f"glibc-{glibc_version}-python-{python_version_slug}.manifest.json"
    )
    return archive_name, manifest_name


def _safe_relative(value: Any, label: str) -> PurePosixPath:
    raw = _require_string(value, label)
    path = PurePosixPath(raw)
    if (
        "\\" in raw
        or path.is_absolute()
        or not path.parts
        or path.as_posix() != raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise VerifiedServiceArtifactError(f"{label} must be a normalized relative path")
    return path


def _regular_input(root: Path, relative: Any, label: str) -> tuple[Path, str, int]:
    path = root.joinpath(*_safe_relative(relative, label).parts)
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root) or path.is_symlink() or not path.is_file():
        raise VerifiedServiceArtifactError(f"{label} is missing or unsafe: {path}")
    current = root
    for part in path.relative_to(root).parts[:-1]:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            raise VerifiedServiceArtifactError(
                f"{label} passes through an unsafe directory: {current}"
            )
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise VerifiedServiceArtifactError(f"{label} is not a regular file: {path}")
    return path, _sha256_file(path), info.st_size


def _read_github_release(repository: str, release_id: str) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{repository}/releases/tags/{release_id}"
    request = urllib.request.Request(url, headers=GH_API_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            if urllib.parse.urlsplit(response.geturl()).hostname != "api.github.com":
                raise VerifiedServiceArtifactError(
                    "GitHub release metadata redirected outside api.github.com"
                )
            content = response.read(MAX_RELEASE_METADATA_BYTES + 1)
    except (urllib.error.URLError, TimeoutError) as error:
        raise VerifiedServiceArtifactError(
            f"cannot read immutable Product release metadata: {error}"
        ) from error
    if len(content) > MAX_RELEASE_METADATA_BYTES:
        raise VerifiedServiceArtifactError("Product release metadata exceeds its size limit")
    try:
        result = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise VerifiedServiceArtifactError(
            f"GitHub returned invalid Product release metadata: {error}"
        ) from error
    return _require_object(result, "GitHub Product release metadata")


def _verify_release_assets(
    metadata: dict[str, Any], release_id: str, channel: str, assets: list[tuple[str, str, int]]
) -> None:
    if (
        metadata.get("tag_name") != release_id
        or metadata.get("immutable") is not True
        or metadata.get("draft") is not False
        or metadata.get("prerelease") is not (channel == "preview")
    ):
        raise VerifiedServiceArtifactError(
            f"Product release {release_id} is not the expected immutable release"
        )
    raw_assets = metadata.get("assets")
    if not isinstance(raw_assets, list):
        raise VerifiedServiceArtifactError("immutable Product release metadata has no assets list")
    by_name = {item.get("name"): item for item in raw_assets if isinstance(item, dict)}
    for name, sha256, size in assets:
        item = by_name.get(name)
        if (
            not isinstance(item, dict)
            or item.get("digest") != f"sha256:{sha256}"
            or item.get("size") != size
            or item.get("state") != "uploaded"
        ):
            raise VerifiedServiceArtifactError(
                f"immutable Product release asset does not match local bytes: {name}"
            )


def _verify_attestation(
    artifact: Path,
    bundle: Path,
    *,
    repository: str,
    workflow: str,
    source_ref: str,
    source_commit: str,
    subject_name: str,
    sha256: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    if shutil.which("gh") is None:
        raise VerifiedServiceArtifactError(
            "GitHub CLI (`gh`) is required to verify Product attestations"
        )
    command = [
        "gh",
        "attestation",
        "verify",
        str(artifact),
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
        "https://slsa.dev/provenance/v1",
        "--cert-oidc-issuer",
        "https://token.actions.githubusercontent.com",
        "--format",
        "json",
    ]
    try:
        result = runner(command, check=False, capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VerifiedServiceArtifactError(
            f"GitHub attestation verification could not complete: {error}"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
        raise VerifiedServiceArtifactError(f"GitHub Product artifact attestation failed: {detail}")
    try:
        verification = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise VerifiedServiceArtifactError(
            "GitHub CLI returned malformed attestation verification JSON"
        ) from error
    if not isinstance(verification, list):
        raise VerifiedServiceArtifactError(
            "GitHub CLI returned an unexpected attestation verification shape"
        )
    for item in verification:
        verification_result = item.get("verificationResult") if isinstance(item, dict) else None
        statement = (
            verification_result.get("statement") if isinstance(verification_result, dict) else None
        )
        subjects = statement.get("subject") if isinstance(statement, dict) else None
        if (
            not isinstance(statement, dict)
            or statement.get("predicateType") != "https://slsa.dev/provenance/v1"
            or not isinstance(subjects, list)
        ):
            continue
        if any(
            isinstance(subject, dict)
            and subject.get("name") == subject_name
            and isinstance(subject.get("digest"), dict)
            and subject["digest"].get("sha256") == sha256
            for subject in subjects
        ):
            return
    raise VerifiedServiceArtifactError(
        "verified SLSA statement does not bind the exact Product archive name and SHA-256"
    )


def _extract_bundle_archive(
    archive_path: Path,
    destination: Path,
    expected_files: dict[str, Any],
) -> Path:
    """Safely unpack a rootless tar and verify every outer manifest file digest."""

    if destination.exists() or destination.is_symlink():
        raise VerifiedServiceArtifactError(f"staging directory already exists: {destination}")
    destination.mkdir(parents=True, mode=0o755)
    found: dict[str, str] = {}
    seen: set[str] = set()
    expanded = 0
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for count, member in enumerate(archive, start=1):
                if count > MAX_ARCHIVE_ENTRIES:
                    raise VerifiedServiceArtifactError("Product archive has too many entries")
                raw_name = (
                    member.name[:-1]
                    if member.isdir() and member.name.endswith("/")
                    else member.name
                )
                path = _safe_relative(raw_name, "Product archive member")
                normalized = path.as_posix()
                if normalized in seen:
                    raise VerifiedServiceArtifactError(
                        f"Product archive contains duplicate entry {raw_name}"
                    )
                seen.add(normalized)
                target = destination.joinpath(*path.parts)
                if member.isdir():
                    if member.size != 0:
                        raise VerifiedServiceArtifactError(
                            f"Product archive directory contains bytes: {raw_name}"
                        )
                    target.mkdir(parents=True, exist_ok=True, mode=0o755)
                    continue
                if not member.isfile() or member.size < 0:
                    raise VerifiedServiceArtifactError(
                        f"Product archive has a link or special entry: {raw_name}"
                    )
                expanded += member.size
                if expanded > MAX_ARCHIVE_BYTES:
                    raise VerifiedServiceArtifactError(
                        "Product archive exceeds the expanded-size limit"
                    )
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                stream = archive.extractfile(member)
                if stream is None:
                    raise VerifiedServiceArtifactError(
                        f"Product archive entry is unreadable: {raw_name}"
                    )
                digest = hashlib.sha256()
                mode = 0o755 if member.mode & 0o111 else 0o644
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
                with stream, os.fdopen(descriptor, "wb") as output:
                    for block in iter(lambda stream=stream: stream.read(1024 * 1024), b""):
                        output.write(block)
                        digest.update(block)
                target.chmod(mode)
                found[normalized] = digest.hexdigest()
    except (OSError, tarfile.TarError) as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise VerifiedServiceArtifactError(
            f"cannot safely extract Product archive: {error}"
        ) from error
    expected: dict[str, str] = {}
    for name, digest in expected_files.items():
        path = _safe_relative(name, "artifact.files path").as_posix()
        if (
            not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or not SHA256_PATTERN.fullmatch(digest[7:])
        ):
            raise VerifiedServiceArtifactError(
                f"outer manifest has an invalid file digest for {path}"
            )
        expected[path] = digest.removeprefix("sha256:")
    if found != expected:
        shutil.rmtree(destination, ignore_errors=True)
        raise VerifiedServiceArtifactError(
            "Product archive payload differs from its verified outer manifest"
        )
    candidates = [path for path in destination.rglob("manifest.json") if path.is_file()]
    if len(candidates) != 1:
        shutil.rmtree(destination, ignore_errors=True)
        raise VerifiedServiceArtifactError(
            "Product archive must contain exactly one inner service manifest"
        )
    bundle_root = candidates[0].parent
    return bundle_root


def _catalog_inputs(
    catalog: dict[str, Any], target_profile: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    components = catalog.get("components")
    publishers = catalog.get("publishers")
    targets = catalog.get("targets")
    if (
        not isinstance(components, list)
        or not isinstance(publishers, list)
        or not isinstance(targets, list)
    ):
        raise VerifiedServiceArtifactError(
            "bootstrap component catalog lacks components, publishers, or targets"
        )
    component_map = {item.get("componentId"): item for item in components if isinstance(item, dict)}
    publisher_map = {item.get("repository"): item for item in publishers if isinstance(item, dict)}
    target_map = {item.get("id"): item for item in targets if isinstance(item, dict)}
    target = target_map.get(target_profile)
    if not isinstance(target, dict) or target.get("hostSupport") != "supported":
        raise VerifiedServiceArtifactError(
            f"bootstrap catalog does not support target {target_profile}"
        )
    return component_map, publisher_map, target


def _load_profile(release_lock_path: Path, target_profile: str) -> dict[str, Any]:
    lock, _ = _read_json(release_lock_path, "Workspace release-lock.json")
    profiles = _require_object(lock.get("nativePythonProfiles"), "nativePythonProfiles")
    profile = _require_object(
        profiles.get(target_profile), f"nativePythonProfiles.{target_profile}"
    )
    expected_target = PRODUCT_TARGETS.get(target_profile)
    if (
        expected_target is None
        or any(profile.get(key) != value for key, value in expected_target.items())
        or profile.get("pythonVersion") != "3.12.14"
        or profile.get("pythonExecutable") != "/opt/cyrene/python/3.12.14/bin/python3.12"
        or profile.get("pythonInput") != "packaging/python-runtime.lock.json"
        or profile.get("abi") not in {"glibc-2.35", "glibc-2.39"}
    ):
        raise VerifiedServiceArtifactError(
            f"release-lock profile does not match the pinned private runtime: {target_profile}"
        )
    return lock


def _load_service_bundle_module() -> Any:
    module_path = Path(__file__).resolve().with_name("service_bundle.py")
    spec = importlib.util.spec_from_file_location("_cyrene_verified_service_bundle", module_path)
    if spec is None or spec.loader is None:
        raise VerifiedServiceArtifactError("cannot load the existing service bundle validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_and_stage(
    *,
    input_root: Path,
    target_profile: str,
    output_root: Path,
    release_lock_path: Path = DEFAULT_RELEASE_LOCK,
    catalog_path: Path = DEFAULT_CATALOG,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    release_reader: Callable[[str, str], dict[str, Any]] = _read_github_release,
    attestation_verifier: Callable[..., None] = _verify_attestation,
) -> dict[str, Any]:
    """Verify official release bytes and stage payload without rebuilding."""

    if input_root.is_symlink() or not input_root.is_dir():
        raise VerifiedServiceArtifactError(
            f"verified Product artifact directory is missing: {input_root}"
        )
    input_root = input_root.resolve()
    index, index_bytes = _read_json(input_root / DEFAULT_INDEX, "verified-service-artifacts index")
    if (
        set(index) != {"schemaVersion", "targetProfile", "services"}
        or index.get("schemaVersion") != 1
    ):
        raise VerifiedServiceArtifactError(
            "verified-service-artifacts index must use the exact v1 shape"
        )
    if index.get("targetProfile") != target_profile:
        raise VerifiedServiceArtifactError(
            "verified-service-artifacts targetProfile differs from the selected DEB target"
        )
    records = _require_object(index.get("services"), "verified-service-artifacts.services")
    if set(records) != set(SERVICE_COMPONENTS):
        raise VerifiedServiceArtifactError(
            "verified-service-artifacts must contain exactly the five managed services"
        )

    release_lock = _load_profile(release_lock_path, target_profile)
    catalog, _ = _read_json(catalog_path, "bootstrap component catalog")
    component_catalog, publisher_catalog, target = _catalog_inputs(catalog, target_profile)
    target_fields = target.get("target")
    if not isinstance(target_fields, dict):
        raise VerifiedServiceArtifactError(f"catalog target {target_profile} has no target object")
    components = release_lock.get("repositories")
    if not isinstance(components, dict):
        raise VerifiedServiceArtifactError("release-lock.json has no repository source pins")

    if output_root.exists() or output_root.is_symlink():
        raise VerifiedServiceArtifactError(
            f"verified Product staging output already exists: {output_root}"
        )
    output_root.mkdir(parents=True, mode=0o755)
    raw_output = output_root / "verified-published-bytes" / target_profile
    bundles_output = output_root / "service-artifacts"
    raw_output.mkdir(parents=True)
    bundles_output.mkdir(parents=True)
    source_index_path = raw_output / DEFAULT_INDEX
    shutil.copyfile(input_root / DEFAULT_INDEX, source_index_path)
    shutil.copyfile(input_root / DEFAULT_INDEX, bundles_output / DEFAULT_INDEX)
    seen_asset_paths: set[str] = {DEFAULT_INDEX}
    service_evidence: dict[str, Any] = {}
    bundle_module = _load_service_bundle_module()

    try:
        for component_id, service in sorted(SERVICE_COMPONENTS.items()):
            record = _require_object(records.get(component_id), f"services.{component_id}")
            if set(record) != {
                "componentId",
                "repository",
                "releaseId",
                "source",
                "artifact",
                "manifest",
                "attestation",
            }:
                raise VerifiedServiceArtifactError(
                    f"services.{component_id} has an invalid field set"
                )
            component = _require_object(
                component_catalog.get(component_id), f"catalog component {component_id}"
            )
            component_repository = _require_string(
                component.get("publisher"), f"{component_id}.publisher"
            )
            publisher = _require_object(
                publisher_catalog.get(component_repository), f"publisher {component_repository}"
            )
            trusted_workflow = _require_string(
                publisher.get("workflow"), f"{component_repository}.workflow"
            )
            expected_repository = SERVICE_REPOSITORIES[service]
            if (
                record.get("componentId") != component_id
                or component_repository != f"DoHorizon-AI/{expected_repository}"
                or record.get("repository") != component_repository
                or component.get("kind") != "python-bundle"
                or component.get("pythonBundleService") != service
            ):
                raise VerifiedServiceArtifactError(
                    f"Product publisher identity differs for {component_id}"
                )
            if not any(
                isinstance(item, dict)
                and item.get("targetId") == target_profile
                and item.get("artifactKind") == "python-bundle"
                and item.get("support") == "supported"
                for item in component.get("targets", [])
            ):
                raise VerifiedServiceArtifactError(
                    f"{component_id} is not supported for {target_profile}"
                )
            expected_archive_name, expected_manifest_name = _canonical_product_asset_names(
                component_id, target_profile, target_fields
            )

            release_id = _require_string(record.get("releaseId"), f"{component_id}.releaseId")
            release_match = RELEASE_PATTERN.fullmatch(release_id)
            source = _require_object(record.get("source"), f"{component_id}.source")
            if set(source) != {"ref", "commit"}:
                raise VerifiedServiceArtifactError(
                    f"{component_id}.source has an invalid field set"
                )
            source_ref = _require_string(source.get("ref"), f"{component_id}.source.ref")
            source_commit = _require_string(source.get("commit"), f"{component_id}.source.commit")
            if (
                release_match is None
                or release_match.group(2) != source_commit
                or COMMIT_PATTERN.fullmatch(source_commit) is None
                or release_match.group(1) not in {"stable", "preview"}
            ):
                raise VerifiedServiceArtifactError(
                    f"{component_id} release tag must bind its exact source commit"
                )
            channel = release_match.group(1)
            allowed_refs = _require_object(catalog.get("channels"), "catalog.channels").get(channel)
            allowed_refs = _require_object(allowed_refs, f"catalog.channels.{channel}").get(
                "sourceRefs"
            )
            if not isinstance(allowed_refs, list) or source_ref not in allowed_refs:
                raise VerifiedServiceArtifactError(
                    f"{component_id} source ref is not trusted for {channel}"
                )
            pinned_commit = components.get(expected_repository)
            if pinned_commit != source_commit:
                raise VerifiedServiceArtifactError(
                    f"{component_id} source commit differs from release-lock.json"
                )

            artifact = _require_object(record.get("artifact"), f"{component_id}.artifact")
            if set(artifact) != {"path", "sha256", "sizeBytes", "kind", "format"}:
                raise VerifiedServiceArtifactError(
                    f"{component_id}.artifact has an invalid field set"
                )
            if artifact.get("kind") != "python-bundle" or artifact.get("format") != "tar.gz":
                raise VerifiedServiceArtifactError(
                    f"{component_id} artifact must be a published tar.gz Python bundle"
                )
            artifact_path, artifact_sha, artifact_size = _regular_input(
                input_root, artifact.get("path"), f"{component_id}.artifact.path"
            )
            artifact_name = artifact_path.name
            if artifact_name != expected_archive_name:
                raise VerifiedServiceArtifactError(
                    f"{component_id} archive asset name is not canonical for {target_profile}"
                )
            if (
                artifact.get("sha256") != artifact_sha
                or artifact.get("sizeBytes") != artifact_size
                or artifact_size < 1
            ):
                raise VerifiedServiceArtifactError(
                    f"{component_id} archive SHA/size differs from index.json"
                )
            manifest_record = _require_object(record.get("manifest"), f"{component_id}.manifest")
            if set(manifest_record) != {"path", "sha256", "manifestDigest"}:
                raise VerifiedServiceArtifactError(
                    f"{component_id}.manifest has an invalid field set"
                )
            manifest_path, manifest_sha, manifest_size = _regular_input(
                input_root, manifest_record.get("path"), f"{component_id}.manifest.path"
            )
            if manifest_path.name != expected_manifest_name:
                raise VerifiedServiceArtifactError(
                    f"{component_id} manifest asset name is not canonical for {target_profile}"
                )
            if manifest_record.get("sha256") != manifest_sha or manifest_size < 1:
                raise VerifiedServiceArtifactError(
                    f"{component_id} outer manifest SHA differs from index.json"
                )
            outer_manifest, _ = _read_json(manifest_path, f"{component_id} outer release manifest")
            if manifest_record.get("manifestDigest") != _manifest_digest(outer_manifest):
                raise VerifiedServiceArtifactError(
                    f"{component_id} outer manifest digest is invalid"
                )
            if (
                outer_manifest.get("schemaVersion") != 2
                or outer_manifest.get("componentId") != component_id
                or outer_manifest.get("releaseId") != release_id
                or outer_manifest.get("channel") != channel
                or outer_manifest.get("target") != target_fields
            ):
                raise VerifiedServiceArtifactError(
                    f"{component_id} outer manifest identity or target differs"
                )
            outer_source = _require_object(
                outer_manifest.get("source"), f"{component_id} manifest.source"
            )
            if (
                outer_source.get("repository") != f"https://github.com/{component_repository}"
                or outer_source.get("ref") != source_ref
                or outer_source.get("commit") != source_commit
            ):
                raise VerifiedServiceArtifactError(
                    f"{component_id} outer manifest source differs from index.json"
                )
            artifact_metadata = _require_object(
                outer_manifest.get("artifact"), f"{component_id} manifest.artifact"
            )
            parsed_uri = urllib.parse.urlsplit(
                _require_string(artifact_metadata.get("uri"), f"{component_id}.artifact.uri")
            )
            if (
                artifact_metadata.get("kind") != "python-bundle"
                or artifact_metadata.get("format") != "tar.gz"
                or artifact_metadata.get("sha256") != f"sha256:{artifact_sha}"
                or artifact_metadata.get("sizeBytes") != artifact_size
                or parsed_uri.scheme != "https"
                or parsed_uri.hostname != "github.com"
                or parsed_uri.path
                != f"/{component_repository}/releases/download/{release_id}/{artifact_name}"
            ):
                raise VerifiedServiceArtifactError(
                    f"{component_id} outer manifest does not bind its exact GitHub archive"
                )
            expected_files = _require_object(
                artifact_metadata.get("files"), f"{component_id}.artifact.files"
            )
            if not expected_files:
                raise VerifiedServiceArtifactError(
                    f"{component_id} outer manifest has no archive payload map"
                )

            attestation = _require_object(record.get("attestation"), f"{component_id}.attestation")
            if set(attestation) != {
                "path",
                "sha256",
                "repository",
                "workflow",
                "predicateType",
                "subjectName",
                "sourceRef",
                "sourceCommit",
            }:
                raise VerifiedServiceArtifactError(
                    f"{component_id}.attestation has an invalid field set"
                )
            attestation_path, attestation_sha, attestation_size = _regular_input(
                input_root, attestation.get("path"), f"{component_id}.attestation.path"
            )
            if (
                attestation_path.name != artifact_name + ".attestation.jsonl"
                or attestation.get("sha256") != attestation_sha
                or attestation_size < 1
                or attestation.get("repository") != component_repository
                or attestation.get("workflow") != trusted_workflow
                or attestation.get("predicateType") != "https://slsa.dev/provenance/v1"
                or attestation.get("subjectName") != artifact_name
                or attestation.get("sourceRef") != source_ref
                or attestation.get("sourceCommit") != source_commit
            ):
                raise VerifiedServiceArtifactError(
                    f"{component_id} detached attestation receipt identity differs"
                )
            provenance = _require_object(
                outer_manifest.get("provenance"), f"{component_id}.manifest.provenance"
            )
            manifest_attestation = _require_object(
                provenance.get("attestation"), f"{component_id}.manifest.attestation"
            )
            if (
                manifest_attestation.get("kind") != "github-artifact-attestation"
                or manifest_attestation.get("repository") != component_repository
                or manifest_attestation.get("workflow") != trusted_workflow
                or manifest_attestation.get("predicateType") != "https://slsa.dev/provenance/v1"
                or (
                    "subjectName" in manifest_attestation
                    and manifest_attestation.get("subjectName") != artifact_name
                )
                or (
                    "sourceRef" in manifest_attestation
                    and manifest_attestation.get("sourceRef") != source_ref
                )
                or (
                    "sourceCommit" in manifest_attestation
                    and manifest_attestation.get("sourceCommit") != source_commit
                )
            ):
                raise VerifiedServiceArtifactError(
                    f"{component_id} outer provenance differs from trusted publisher identity"
                )

            release_metadata = release_reader(component_repository, release_id)
            _verify_release_assets(
                release_metadata,
                release_id,
                channel,
                [
                    (artifact_name, artifact_sha, artifact_size),
                    (manifest_path.name, manifest_sha, manifest_size),
                    (attestation_path.name, attestation_sha, attestation_size),
                ],
            )
            attestation_verifier(
                artifact_path,
                attestation_path,
                repository=component_repository,
                workflow=trusted_workflow,
                source_ref=source_ref,
                source_commit=source_commit,
                subject_name=artifact_name,
                sha256=artifact_sha,
                runner=runner,
            )

            for raw_path in (artifact, manifest_record, attestation):
                asset_path = _safe_relative(raw_path.get("path"), f"{component_id} asset path")
                asset_key = asset_path.as_posix()
                if asset_key in seen_asset_paths:
                    raise VerifiedServiceArtifactError(
                        f"two Product services reuse one input asset path: {asset_key}"
                    )
                seen_asset_paths.add(asset_key)
                destination = raw_output.joinpath(*asset_path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(input_root.joinpath(*asset_path.parts), destination)

            extracted_root = _extract_bundle_archive(
                artifact_path,
                bundles_output / service,
                expected_files,
            )
            try:
                inner_manifest = bundle_module.validate_bundle(
                    extracted_root,
                    expected_service=service,
                    expected_target_profile=target_profile,
                    release_lock_path=release_lock_path,
                )
            except (ImportError, OSError, RuntimeError, ValueError) as error:
                raise VerifiedServiceArtifactError(
                    f"{component_id} inner service bundle is invalid: {error}"
                ) from error
            if (
                inner_manifest.get("schema_version") != 2
                or inner_manifest.get("service") != service
                or inner_manifest.get("source_commit") != source_commit
            ):
                raise VerifiedServiceArtifactError(
                    f"{component_id} inner bundle source or schema differs"
                )
            package_service_root = output_root / "service-artifacts" / service
            package_service_root.mkdir(parents=True, exist_ok=False)
            staged_bundle = package_service_root / inner_manifest["version"]
            shutil.copytree(extracted_root, staged_bundle, symlinks=False)
            for path in sorted(staged_bundle.rglob("*"), reverse=True):
                if path.is_dir():
                    path.chmod(0o755)
                else:
                    path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
            staged_manifest_path = staged_bundle / "manifest.json"
            if staged_manifest_path.is_symlink() or not staged_manifest_path.is_file():
                raise VerifiedServiceArtifactError(
                    f"{component_id} inner manifest disappeared during staging"
                )

            service_evidence[component_id] = {
                "componentId": component_id,
                "repository": component_repository,
                "releaseId": release_id,
                "source": {"ref": source_ref, "commit": source_commit},
                "artifact": {"sha256": artifact_sha, "sizeBytes": artifact_size},
                "manifest": {
                    "sha256": manifest_sha,
                    "manifestDigest": manifest_record["manifestDigest"],
                },
                "attestation": {
                    "sha256": attestation_sha,
                    "repository": component_repository,
                    "workflow": trusted_workflow,
                    "subjectName": artifact_name,
                    "sourceRef": source_ref,
                    "sourceCommit": source_commit,
                },
            }

    except BaseException:
        shutil.rmtree(output_root, ignore_errors=True)
        raise

    return {
        "targetProfile": target_profile,
        "serviceArtifactsIndexSha256": "sha256:" + _sha256_bytes(index_bytes),
        "services": service_evidence,
        "rawAssetsRoot": str(raw_output),
        "serviceArtifactsRoot": str(bundles_output),
        "serviceArtifactsIndex": str(bundles_output / "index.json"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verified_service_artifacts.py",
        description="Verify immutable Product release assets and stage the unchanged bundle bytes.",
    )
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--target-profile", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--release-lock", type=Path, default=DEFAULT_RELEASE_LOCK)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--json", action="store_true", help="Write one JSON result to stdout.")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = verify_and_stage(
            input_root=arguments.input_root,
            target_profile=arguments.target_profile,
            output_root=arguments.output_root,
            release_lock_path=arguments.release_lock,
            catalog_path=arguments.catalog,
        )
        if arguments.json:
            print(json.dumps(result, sort_keys=True))
        else:
            print(f"Verified and staged published Product bundles for {arguments.target_profile}.")
            print(f"Original verified bytes: {result['rawAssetsRoot']}")
            print(f"Stage-only service bundles: {result['serviceArtifactsRoot']}")
        return 0
    except VerifiedServiceArtifactError as error:
        print(f"verified-service-artifacts: ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
