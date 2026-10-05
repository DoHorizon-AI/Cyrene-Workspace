"""Validate the exact catalog pinned into a native installer package.

The signed form carries the Workspace release identity and digest recorded in
the source receipt. Explicit development builds use a separate, unsigned form
that is accepted only when a caller supplies the source-compiled catalog pin.
模块职责：校验安装包绑定的组件目录字节、来源身份和 generation。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

REPOSITORY = "DoHorizon-AI/Cyrene-Workspace"
CATALOG_WORKFLOW = f"{REPOSITORY}/.github/workflows/component-catalog-release.yml"
CATALOG_ASSET_NAME = "component-catalog-v1.json"
BINDING_ASSET_NAME = "bootstrap-catalog-binding-v1.json"
INSTALLED_CATALOG_PATH = Path("/usr/share/cyrene") / CATALOG_ASSET_NAME
INSTALLED_BINDING_PATH = Path("/usr/share/cyrene") / BINDING_ASSET_NAME
DEVELOPMENT_PROVENANCE = "development-source-pin"

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_TYPED_SHA256_PATTERN = re.compile(r"sha256:([0-9a-f]{64})\Z")
_CATALOG_TAG_PATTERN = re.compile(r"catalog-(stable|preview)-([0-9a-f]{40})\Z")
_SIGNED_CATALOG_KEYS = {
    "repository",
    "workflow",
    "releaseId",
    "source",
    "assetName",
    "sha256",
    "attestationBundleSha256",
    "generation",
}
_DEVELOPMENT_CATALOG_KEYS = {"assetName", "sha256", "generation"}


class BootstrapCatalogBindingError(ValueError):
    """A bootstrap catalog or its package binding is missing or inconsistent."""


def _duplicate_checked_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys before interpreting pinned data."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BootstrapCatalogBindingError(f"JSON contains duplicate object key {key!r}")
        result[key] = value
    return result


def _read_object(path: Path, label: str, *, require_root: bool) -> tuple[dict[str, Any], bytes]:
    """Read one strict UTF-8 JSON object and preserve its original bytes."""

    content = _read_regular_file(path, label, require_root=require_root)
    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=_duplicate_checked_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BootstrapCatalogBindingError(f"{label} is not valid UTF-8 JSON: {error}") from error
    if not isinstance(value, dict):
        raise BootstrapCatalogBindingError(f"{label} must be a JSON object")
    return value, content


def _check_root_safe_path(path: Path) -> None:
    """Require a root-owned, non-writable directory chain with no symlinks."""

    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    root_metadata = current.lstat()
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != 0
        or root_metadata.st_mode & 0o022
    ):
        raise BootstrapCatalogBindingError("filesystem root is not root-safe")
    for part in absolute.parts[1:-1]:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise BootstrapCatalogBindingError(
                f"unsafe or missing catalog path parent: {current}"
            ) from error
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise BootstrapCatalogBindingError(f"catalog path parent is not root-safe: {current}")


def _read_regular_file(path: Path, label: str, *, require_root: bool) -> bytes:
    """Read a regular, non-symlink file without following its final component."""

    if not isinstance(path, Path):
        path = Path(path)
    if require_root:
        _check_root_safe_path(path)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise BootstrapCatalogBindingError(f"{label} is missing or unsafe: {path}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise BootstrapCatalogBindingError(f"{label} must be a regular, non-symlink file: {path}")
    if require_root and (metadata.st_uid != 0 or metadata.st_mode & 0o022):
        raise BootstrapCatalogBindingError(
            f"{label} must be root-owned and not group/world-writable"
        )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BootstrapCatalogBindingError(f"cannot safely open {label}: {path}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise BootstrapCatalogBindingError(f"{label} changed while it was opened: {path}")
        if require_root and (opened.st_uid != 0 or opened.st_mode & 0o022):
            raise BootstrapCatalogBindingError(f"{label} is not root-safe: {path}")
        chunks: list[bytes] = []
        size = 0
        while block := os.read(descriptor, 1024 * 1024):
            size += len(block)
            if size > 32 * 1024 * 1024:
                raise BootstrapCatalogBindingError(f"{label} exceeds the 32 MiB size limit")
            chunks.append(block)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _catalog_generation(catalog: dict[str, Any]) -> int:
    """Return the positive integer generation from a v1 catalog document."""

    generation = catalog.get("generation")
    if (
        type(catalog.get("schemaVersion")) is not int
        or catalog.get("schemaVersion") != 1
        or type(generation) is not int
        or generation < 1
    ):
        raise BootstrapCatalogBindingError("catalog schema or generation is invalid")
    return generation


def _validate_signed_binding(document: dict[str, Any], generation: int, digest: str) -> None:
    """Validate the immutable Workspace release identity and catalog byte pin."""

    if (
        set(document) != {"schemaVersion", "catalog"}
        or type(document.get("schemaVersion")) is not int
        or document.get("schemaVersion") != 1
    ):
        raise BootstrapCatalogBindingError("signed catalog binding fields do not match schema v1")
    catalog = document.get("catalog")
    if not isinstance(catalog, dict) or set(catalog) != _SIGNED_CATALOG_KEYS:
        raise BootstrapCatalogBindingError("signed catalog binding release fields are malformed")
    source = catalog.get("source")
    match = _CATALOG_TAG_PATTERN.fullmatch(str(catalog.get("releaseId", "")))
    if match is None:
        raise BootstrapCatalogBindingError("catalog releaseId must be an exact channel-pinned tag")
    channel, release_commit = match.groups()
    allowed_refs = (
        {"refs/heads/develop"}
        if channel == "preview"
        else {"refs/heads/main", "refs/heads/release"}
    )
    if (
        catalog.get("repository") != REPOSITORY
        or catalog.get("workflow") != CATALOG_WORKFLOW
        or catalog.get("assetName") != CATALOG_ASSET_NAME
        or not isinstance(source, dict)
        or set(source) != {"ref", "commit"}
        or source.get("commit") != release_commit
        or source.get("ref") not in allowed_refs
        or not isinstance(catalog.get("sha256"), str)
        or not _SHA256_PATTERN.fullmatch(catalog["sha256"])
        or not isinstance(catalog.get("attestationBundleSha256"), str)
        or not _SHA256_PATTERN.fullmatch(catalog["attestationBundleSha256"])
        or type(catalog.get("generation")) is not int
        or catalog.get("generation") != generation
        or catalog.get("sha256") != digest
    ):
        raise BootstrapCatalogBindingError(
            "signed catalog binding identity, digest, or generation does not match catalog bytes"
        )


def _validate_development_binding(
    document: dict[str, Any],
    generation: int,
    digest: str,
    source_catalog_digest: str | None,
) -> None:
    """Require the explicit development pin to match source-compiled authority."""

    if (
        set(document) != {"schemaVersion", "provenance", "catalog"}
        or type(document.get("schemaVersion")) is not int
        or document.get("schemaVersion") != 1
        or document.get("provenance") != DEVELOPMENT_PROVENANCE
    ):
        raise BootstrapCatalogBindingError("development catalog binding fields are malformed")
    catalog = document.get("catalog")
    if not isinstance(catalog, dict) or set(catalog) != _DEVELOPMENT_CATALOG_KEYS:
        raise BootstrapCatalogBindingError("development catalog source pin is malformed")
    source_pin = (
        _TYPED_SHA256_PATTERN.fullmatch(source_catalog_digest)
        if isinstance(source_catalog_digest, str)
        else None
    )
    if (
        source_pin is None
        or catalog.get("assetName") != CATALOG_ASSET_NAME
        or catalog.get("sha256") != source_pin.group(1)
        or catalog.get("sha256") != digest
        or type(catalog.get("generation")) is not int
        or catalog.get("generation") != generation
    ):
        raise BootstrapCatalogBindingError(
            "development catalog does not match a caller-supplied source-compiled digest"
        )


def load_bootstrap_catalog_binding(
    binding_path: Path,
    catalog_path: Path,
    *,
    require_root: bool = True,
    source_catalog_digest: str | None = None,
) -> dict[str, Any]:
    """Load a binding only when its catalog bytes, identity, and generation agree.

    Args:
        binding_path: Installed catalog binding JSON path.
        catalog_path: Installed catalog JSON path.
        require_root: Enforce fixed installed paths, root ownership, and safe parents.
        source_catalog_digest: Typed source-compiled ``sha256:<hex>`` pin for dev mode only.
    Returns:
        The validated binding document.
    Raises:
        BootstrapCatalogBindingError: If either file or any binding field is unsafe or mismatched.
    """

    binding_path = Path(binding_path)
    catalog_path = Path(catalog_path)
    if require_root and (
        Path(os.path.abspath(binding_path)) != INSTALLED_BINDING_PATH
        or Path(os.path.abspath(catalog_path)) != INSTALLED_CATALOG_PATH
    ):
        raise BootstrapCatalogBindingError(
            "catalog binding paths are not the fixed installed paths"
        )
    binding, _ = _read_object(binding_path, "bootstrap catalog binding", require_root=require_root)
    catalog, catalog_bytes = _read_object(
        catalog_path, "bootstrap component catalog", require_root=require_root
    )
    generation = _catalog_generation(catalog)
    digest = hashlib.sha256(catalog_bytes).hexdigest()
    if binding.get("provenance") == DEVELOPMENT_PROVENANCE:
        _validate_development_binding(binding, generation, digest, source_catalog_digest)
    else:
        _validate_signed_binding(binding, generation, digest)
    return binding
