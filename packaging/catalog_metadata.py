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
CHANNEL_REFS: dict[str, tuple[str, ...]] = {
    "stable": ("refs/heads/main", "refs/heads/release"),
    "preview": ("refs/heads/develop",),
}
CHANNELS = frozenset(CHANNEL_REFS)
TAG_PATTERN = re.compile(r"^catalog-(stable|preview)-([0-9a-f]{40})$")
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
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)


def _read_url(url: str, *, accept: str, limit: int, asset: bool = False) -> bytes:
    """Read a bounded HTTPS response from the pinned GitHub endpoints."""

    headers = {
        "Accept": accept,
        "User-Agent": "CyreneComponentCatalogFetcher/1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
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
    """Select the newest immutable release with this channel's tag prefix."""

    prefix = f"catalog-{channel}-"
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for page in range(1, _MAX_RELEASE_PAGES + 1):
        url = f"{API_BASE}/repos/{REPOSITORY}/releases?per_page=100&page={page}"
        releases = _read_api_json(url)
        if not isinstance(releases, list) or not all(isinstance(item, dict) for item in releases):
            raise CatalogMetadataError("GitHub release list response is malformed.")
        for release in releases:
            tag_name = release.get("tag_name")
            if not isinstance(tag_name, str) or not tag_name.startswith(prefix):
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
            candidates.append((created, release))
        if len(releases) < 100:
            break
    else:
        raise CatalogMetadataError("GitHub release history exceeds the supported scan depth.")

    if not candidates:
        raise CatalogMetadataError(f"No immutable {channel} component catalog release exists.")
    latest_time = max(candidate[0] for candidate in candidates)
    latest = [release for created, release in candidates if created == latest_time]
    if len(latest) != 1:
        raise CatalogMetadataError(f"Latest immutable {channel} release selection is ambiguous.")
    return latest[0]


def _validate_release(release: dict[str, Any], channel: str, requested_tag: str | None) -> str:
    """Check the immutable release envelope and return its exact source commit."""

    tag_name = release.get("tag_name")
    match = TAG_PATTERN.fullmatch(tag_name) if isinstance(tag_name, str) else None
    if match is None or match.group(1) != channel:
        raise CatalogMetadataError(
            "Release tag must be catalog-<channel>- followed by a full lowercase SHA-1."
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


def _find_assets(release: dict[str, Any], tag_name: str) -> dict[str, tuple[str, int]]:
    """Require exactly one uploaded catalog and attestation asset from this repo."""

    assets = release.get("assets")
    if not isinstance(assets, list):
        raise CatalogMetadataError("Selected GitHub release has no asset list.")
    wanted = {CATALOG_ASSET, ATTESTATION_ASSET}
    if len(assets) != len(wanted):
        raise CatalogMetadataError(
            "Selected GitHub catalog release must contain exactly the raw catalog and attestation assets."
        )
    found: dict[str, tuple[str, int]] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise CatalogMetadataError("Selected GitHub release contains a malformed asset record.")
        name = asset.get("name")
        if not isinstance(name, str):
            raise CatalogMetadataError("Selected GitHub release contains an asset without a name.")
        if name not in wanted:
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
    if found.keys() != wanted:
        missing = sorted(wanted - found.keys())
        raise CatalogMetadataError(
            f"Selected GitHub release is missing assets: {', '.join(missing)}."
        )
    return found


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

    catalog_schema = _read_local_json(
        schema_root / "component-catalog-v1.schema.json", "catalog schema"
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
    return catalog


def _verify_source_ref(
    *,
    payload: bytes,
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
                subject_name=CATALOG_ASSET,
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
                "release_id must be a catalog channel-prefixed full SHA-1 tag."
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
    assets = _find_assets(release, tag_name)
    catalog_bytes = _download_asset(
        assets[CATALOG_ASSET][0], size=assets[CATALOG_ASSET][1], name=CATALOG_ASSET
    )
    attestation_bytes = _download_asset(
        assets[ATTESTATION_ASSET][0],
        size=assets[ATTESTATION_ASSET][1],
        name=ATTESTATION_ASSET,
    )
    catalog_digest = "sha256:" + hashlib.sha256(catalog_bytes).hexdigest()
    catalog = _validate_catalog(catalog_bytes, Path(schema_root))

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
        "generation": catalog["generation"],
        "subjectName": CATALOG_ASSET,
        "attestationAssetName": ATTESTATION_ASSET,
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
