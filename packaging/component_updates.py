"""Workspace-native component update protocol and transaction engine.

This module deliberately keeps candidate discovery, staging, maintenance admission,
activation, and rollback in separate operations. Product v2 manifests are not read here.
此模块分离候选发现、暂存、维护准入、激活与回滚，不读取 Product v2 manifest。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

PROTOCOL_VERSION = "cyrene.component-updates.helper.v1"
PRODUCT_CONTRACT_ATTESTATION_WORKFLOW = "/.github/workflows/product-contract.yml"
PRODUCT_POLICY_ATTESTATION_WORKFLOW = "/.github/workflows/product-policy-release.yml"
COMPONENT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
DIGEST_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")
BUNDLE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
SEMVER3_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
PLAN_ID_PATTERN = re.compile(r"^plan-[0-9a-f]{32}$")
MAX_SAFE_INTEGER = 9_007_199_254_740_991
INSTALLED_CATALOG = Path("/usr/share/cyrene/component-catalog-v1.json")
ACTIVE_CATALOG_ROOT = Path("/usr/share/cyrene/component-catalogs")
ACTIVE_CATALOG_POINTER = Path("/usr/share/cyrene/component-catalog-state.json")
CATALOG_SCHEMA_ROOT = Path("/usr/share/cyrene/catalog-schemas")
DEFAULT_CATALOG = (
    INSTALLED_CATALOG
    if INSTALLED_CATALOG.is_file()
    else Path(__file__).resolve().with_name("component-catalog-bootstrap-v1.json")
)
DEFAULT_ACTIVITY_CATALOG = Path("/var/lib/cyrene/runtime/activity-sources.json")
DEFAULT_SOCKET = Path("/run/cyrene/runtime-maintenance.sock")
BROKER_COMPONENT_ID = "cyrene-runtime-maintenance"
BROKER_BOOTSTRAP_JOURNAL = "native-first-bootstrap/runtime-maintenance-first-install.json"
DEFAULT_INSTALL_ROOT = Path("/usr/lib/cyrene")
DEFAULT_STATE_ROOT = Path("/var/lib/cyrene-updates")
DEFAULT_DATA_BUNDLE_ROOT = Path("/var/lib/cyrene-product-bundles")
DEFAULT_RELEASE_LOCK = Path("/usr/lib/cyrene/release-lock.json")
DEFAULT_PRIVATE_PYTHON = Path("/opt/cyrene/python/3.12.14/bin/python3.12")
DEFAULT_AUTHORITY_ADMIN_SOCKET = Path("/run/cyrene-workspace-authority/admin.sock")
DEFAULT_CHANNEL = "stable"
TRUSTED_CATALOG_DIGEST = "sha256:9908229d8abee4cb3f1b5a55d8be5264310939e4b43700de0dfd37be7318c701"
USER_AGENT = "CyreneComponentUpdater/1"
BEGIN_NO_TOKEN_STATUSES = frozenset(
    {
        "ACTIVE_TASKS",
        "UNKNOWN",
        "IDLE_RUNTIME_REQUIRES_UNLOAD",
        "MAINTENANCE_ACTIVE",
        "STALE_READINESS",
        "USER_CONFIRMATION_REQUIRED",
    }
)
PRODUCT_CONTRACT_ROOT_ENV = {
    "cy-workspace-web-bff": "CYRENE_WORKSPACE_WEB_BFF_PRODUCT_CONTRACT_ROOT_V2",
    "cy-workspace-connector": "CYRENE_WORKSPACE_CONNECTOR_PRODUCT_CONTRACT_ROOT_V2",
}
NATIVE_PYTHON_TARGET_FIELDS = (
    "os",
    "osVersion",
    "distribution",
    "distributionVersion",
    "architecture",
    "abi",
    "runtime",
)


class UpdateError(RuntimeError):
    """An expected, user-actionable update refusal."""

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        *,
        maintenance_not_acquired: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.maintenance_not_acquired = maintenance_not_acquired


@dataclass(frozen=True)
class Candidate:
    """One verified manifest selected by a trusted channel index."""

    component: dict[str, Any]
    manifest: dict[str, Any]
    manifest_digest: str
    artifact_digest: str
    manifest_uri: str
    index: dict[str, Any]
    index_uri: str
    manifest_bytes: bytes | None = None


def _jcs_string(value: str) -> str:
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise UpdateError("INVALID_JSON", "Canonical JSON does not permit unpaired surrogates.")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def canonical_jcs(value: Any) -> bytes:
    """Serialize the RFC 8785 JSON subset used by the component contracts.

    Public manifests intentionally permit safe integers only (no floating point).
    Object keys are ordered by UTF-16 code units as required by JCS.
    """

    def encode(item: Any) -> str:
        if item is None:
            return "null"
        if item is True:
            return "true"
        if item is False:
            return "false"
        if isinstance(item, int):
            if abs(item) > MAX_SAFE_INTEGER:
                raise UpdateError("INVALID_JSON", "Canonical JSON integer exceeds the safe range.")
            return str(item)
        if isinstance(item, float):
            raise UpdateError(
                "INVALID_JSON", "Component JSON must not contain floating-point values."
            )
        if isinstance(item, str):
            return _jcs_string(item)
        if isinstance(item, list):
            return "[" + ",".join(encode(value) for value in item) + "]"
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise UpdateError("INVALID_JSON", "Canonical JSON object keys must be strings.")
            keys = sorted(item, key=lambda key: key.encode("utf-16be"))
            return "{" + ",".join(_jcs_string(key) + ":" + encode(item[key]) for key in keys) + "}"
        raise UpdateError(
            "INVALID_JSON", f"Unsupported canonical JSON value: {type(item).__name__}."
        )

    return encode(value).encode("utf-8")


def _digest_json(value: dict[str, Any], field: str) -> str:
    material = dict(value)
    material.pop(field, None)
    return "sha256:" + hashlib.sha256(canonical_jcs(material)).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and DIGEST_PATTERN.fullmatch(value) is not None


def _trusted_contract_lock(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"repository", "commit", "path", "sha256"}:
        return False
    if (
        value.get("repository") != "DoHorizon-AI/Cyrene-Workspace"
        or not isinstance(value.get("commit"), str)
        or re.fullmatch(r"[0-9a-f]{40}", value["commit"]) is None
        or value.get("path") != "governance/workspace-connection-protocols-v2.lock.json"
        or not _valid_digest(value.get("sha256"))
    ):
        return False
    try:
        _safe_relative(value["path"], field="compatibility.contractLock.path")
    except UpdateError:
        return False
    return True


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise UpdateError("INVALID_JSON", f"JSON object contains duplicate key {key!r}.")
        value[key] = item
    return value


def _running_as_root() -> bool:
    """Report whether this process has the OS identity required for apply."""

    return hasattr(os, "geteuid") and os.geteuid() == 0


def _maintenance_request_id(transaction: dict[str, Any]) -> str:
    """Return the durable broker transaction ID bound to this plan."""

    plan_id = transaction.get("planId")
    if not isinstance(plan_id, str) or PLAN_ID_PATTERN.fullmatch(plan_id) is None:
        raise UpdateError("INVALID_TRANSACTION", "Maintenance transaction has an invalid planId.")
    expected = "cyrene-update-" + plan_id
    request_id = transaction.get("requestId", expected)
    if request_id != expected:
        raise UpdateError(
            "INVALID_TRANSACTION", "Maintenance requestId does not match the confirmed plan."
        )
    transaction["requestId"] = request_id
    return request_id


def _parse_supported_version_range(
    value: Any,
) -> tuple[str, tuple[int, int, int], tuple[int, int, int] | None]:
    """Parse the deliberately small SemVer requirement subset in the catalog."""

    if not isinstance(value, str):
        raise UpdateError(
            "DEPENDENCY_RANGE_UNSUPPORTED", "A component dependency has no supported versionRange."
        )
    exact = re.fullmatch(r"=([0-9]+\.[0-9]+\.[0-9]+)", value)
    if exact:
        match = SEMVER3_PATTERN.fullmatch(exact.group(1))
        if match is None:
            raise UpdateError(
                "DEPENDENCY_RANGE_UNSUPPORTED", f"Unsupported component versionRange {value!r}."
            )
        return "exact", tuple(map(int, match.groups())), None
    interval = re.fullmatch(r">=([0-9]+\.[0-9]+\.[0-9]+), <([0-9]+\.[0-9]+\.[0-9]+)", value)
    if interval:
        lower_match = SEMVER3_PATTERN.fullmatch(interval.group(1))
        upper_match = SEMVER3_PATTERN.fullmatch(interval.group(2))
        if lower_match is None or upper_match is None:
            raise UpdateError(
                "DEPENDENCY_RANGE_UNSUPPORTED", f"Unsupported component versionRange {value!r}."
            )
        lower = tuple(map(int, lower_match.groups()))
        upper = tuple(map(int, upper_match.groups()))
        if lower >= upper:
            raise UpdateError(
                "DEPENDENCY_RANGE_UNSUPPORTED", f"Invalid component versionRange {value!r}."
            )
        return "interval", lower, upper
    raise UpdateError(
        "DEPENDENCY_RANGE_UNSUPPORTED", f"Unsupported component versionRange {value!r}."
    )


def _version_satisfies(version: Any, version_range: Any) -> bool:
    kind, lower, upper = _parse_supported_version_range(version_range)
    if not isinstance(version, str):
        return False
    match = SEMVER3_PATTERN.fullmatch(version)
    if match is None:
        return False
    parsed = tuple(map(int, match.groups()))
    return (
        parsed == lower
        if kind == "exact"
        else (parsed >= lower and upper is not None and parsed < upper)
    )


def _safe_relative(value: Any, *, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise UpdateError("INVALID_MANIFEST", f"{field} must be a safe relative path.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise UpdateError("INVALID_MANIFEST", f"{field} escapes the artifact root.")
    if path.as_posix() != value:
        raise UpdateError("INVALID_MANIFEST", f"{field} is not normalized.")
    return path


def _artifact_digest_from_manifest(manifest: Any) -> str | None:
    if not isinstance(manifest, dict):
        return None
    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict):
        return None
    value = artifact.get("sha256", artifact.get("digest"))
    return value if isinstance(value, str) else None


def _artifact_kind_from_manifest(manifest: Any) -> str | None:
    if not isinstance(manifest, dict):
        return None
    artifact = manifest.get("artifact")
    kind = artifact.get("kind") if isinstance(artifact, dict) else None
    return kind if isinstance(kind, str) else None


def _native_release_directory_identity(directory_name: str) -> tuple[str, str | None]:
    """Parse a legacy or digest-specific native release directory basename."""

    match = re.fullmatch(r"(.+)--([0-9a-f]{64})", directory_name)
    if match:
        return match.group(1), match.group(2)
    return directory_name, None


def _native_release_pointer_identity(item: dict[str, Any]) -> str:
    pointer_identity = item.get("pointerIdentity")
    if isinstance(pointer_identity, str):
        version, pointer_digest = _native_release_directory_identity(pointer_identity)
        if VERSION_PATTERN.fullmatch(version) and (
            pointer_digest is None or len(pointer_digest) == 64
        ):
            return pointer_identity
    version = item.get("version")
    manifest_digest = item.get("manifestDigest")
    if (
        not isinstance(version, str)
        or VERSION_PATTERN.fullmatch(version) is None
        or not _valid_digest(manifest_digest)
    ):
        raise UpdateError(
            "TRANSACTION_IDENTITY_UNKNOWN", "Native release has no verified pointer identity."
        )
    return f"{version}--{manifest_digest.removeprefix('sha256:')}"


def _read_object(path: Path, description: str) -> dict[str, Any]:
    if path.is_symlink():
        raise UpdateError("UNSAFE_STATE", f"Refusing a symlink while reading {description}: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateError("INVALID_LOCAL_STATE", f"Cannot read {description}: {error}") from error
    if not isinstance(value, dict):
        raise UpdateError("INVALID_LOCAL_STATE", f"{description} must be a JSON object.")
    return value


def _atomic_json(path: Path, value: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _verify_private_directory(path.parent)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(
            temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), mode
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(
                json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
                + b"\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _verify_root_protected_directory(path: Path, *, create: bool = False) -> None:
    """Require the complete public catalog path to be root-owned and non-writable."""

    absolute = path.absolute()
    if create:
        absolute.mkdir(parents=True, exist_ok=True, mode=0o755)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as error:
            raise UpdateError(
                "UNSAFE_CATALOG", f"Cannot inspect protected catalog directory {current}: {error}"
            ) from error
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
            or not stat.S_IMODE(info.st_mode) & 0o001
        ):
            raise UpdateError(
                "UNSAFE_CATALOG",
                f"Catalog directory and ancestors must be root-owned real directories without group/world write access: {current}",
            )


def _atomic_root_catalog_bytes(path: Path, payload: bytes) -> None:
    """Atomically publish one immutable, root-readable catalog object."""

    _verify_root_protected_directory(path.parent, create=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o644,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, 0, 0)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _read_root_catalog_object(path: Path, description: str) -> bytes:
    if path.is_symlink():
        raise UpdateError("UNSAFE_CATALOG", f"Refusing a symbolic link for {description}: {path}")
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
            or not stat.S_IMODE(info.st_mode) & 0o004
        ):
            raise UpdateError(
                "UNSAFE_CATALOG",
                f"{description} must be root-owned, readable, and not group/world writable.",
            )
        return path.read_bytes()
    except OSError as error:
        raise UpdateError("UNSAFE_CATALOG", f"Cannot read {description}: {error}") from error


def _stage_initial_catalog_directory(
    metadata: dict[str, Any], catalog_bytes: bytes, attestation_bytes: bytes
) -> Path:
    """Build a complete first catalog snapshot outside the active path."""

    parent = ACTIVE_CATALOG_ROOT.parent
    _verify_root_protected_directory(parent)
    if ACTIVE_CATALOG_ROOT.exists() or ACTIVE_CATALOG_ROOT.is_symlink():
        raise UpdateError("UNSAFE_CATALOG", "The active catalog directory already exists.")
    staging = parent / f".{ACTIVE_CATALOG_ROOT.name}.staging-{os.getpid()}-{uuid.uuid4().hex}"
    created = False
    try:
        staging.mkdir(mode=0o700)
        created = True
        os.chown(staging, 0, 0)
        os.chmod(staging, 0o755)
        _verify_root_protected_directory(staging)
        digest_hex = metadata["catalogSha256"].removeprefix("sha256:")
        catalog_name = f"catalog-{digest_hex}.json"
        attestation_name = f"catalog-{digest_hex}-{metadata['sourceCommit']}.attestation.jsonl"
        _atomic_root_catalog_bytes(staging / catalog_name, catalog_bytes)
        _atomic_root_catalog_bytes(staging / attestation_name, attestation_bytes)
        directory_fd = os.open(staging, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return staging
    except BaseException:
        if created and staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _activate_initial_catalog_directory(staging: Path) -> None:
    """Atomically expose a fully populated first active catalog directory."""

    parent = ACTIVE_CATALOG_ROOT.parent
    _verify_root_protected_directory(parent)
    _verify_root_protected_directory(staging)
    if ACTIVE_CATALOG_ROOT.exists() or ACTIVE_CATALOG_ROOT.is_symlink():
        raise UpdateError("UNSAFE_CATALOG", "The active catalog directory appeared during import.")
    os.rename(staging, ACTIVE_CATALOG_ROOT)
    directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _verify_private_directory(path: Path) -> None:
    """Require a real, current-user-owned directory with no group/other access."""

    absolute = path.absolute()
    try:
        if absolute.resolve(strict=True) != absolute:
            raise UpdateError(
                "UNSAFE_STATE", f"Refusing a symlinked update state directory: {path}"
            )
        info = absolute.lstat()
    except OSError as error:
        raise UpdateError(
            "UNSAFE_STATE", f"Cannot inspect update state directory {path}: {error}"
        ) from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise UpdateError(
            "UNSAFE_STATE",
            f"Update state directory must be owned by uid {os.geteuid()} and mode 0700: {path}",
        )


class ComponentUpdater:
    """Drive the fixed JSON helper protocol against trusted local catalog data."""

    def __init__(
        self,
        *,
        catalog_path: Path = DEFAULT_CATALOG,
        activity_catalog_path: Path = DEFAULT_ACTIVITY_CATALOG,
        socket_path: Path = DEFAULT_SOCKET,
        broker_path: Path | None = None,
        install_root: Path = DEFAULT_INSTALL_ROOT,
        state_root: Path = DEFAULT_STATE_ROOT,
        release_lock_path: Path = DEFAULT_RELEASE_LOCK,
        trusted_catalog_digest: str | None = TRUSTED_CATALOG_DIGEST,
        opener: Callable[..., Any] = urllib.request.urlopen,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        systemd_unit_dirs: tuple[Path, ...] | None = None,
        data_bundle_root: Path = DEFAULT_DATA_BUNDLE_ROOT,
        authority_admin_socket: Path = DEFAULT_AUTHORITY_ADMIN_SOCKET,
        load_active_catalog: bool = True,
        allow_incomplete_catalog: bool = False,
    ) -> None:
        self.catalog_path = Path(catalog_path)
        self.activity_catalog_path = Path(activity_catalog_path)
        self.socket_path = Path(socket_path)
        # None means resolve only the catalog-pinned, installed broker. An explicit
        # path preserves the legacy/test override without changing its semantics.
        self.broker_path = Path(broker_path) if broker_path is not None else None
        self.install_root = Path(install_root)
        self.state_root = Path(state_root)
        self.data_bundle_root = Path(data_bundle_root)
        self.release_lock_path = Path(release_lock_path)
        self.authority_admin_socket = Path(authority_admin_socket)
        self.opener = opener
        self.runner = runner
        self.allow_incomplete_catalog = allow_incomplete_catalog
        self.systemd_unit_dirs = (
            tuple(Path(path) for path in systemd_unit_dirs)
            if systemd_unit_dirs is not None
            else (
                Path("/etc/systemd/system"),
                Path("/lib/systemd/system"),
                Path("/usr/lib/systemd/system"),
            )
        )
        if self.catalog_path.is_symlink():
            raise UpdateError(
                "UNSAFE_CATALOG", "The trusted component catalog path must not be a symbolic link."
            )
        catalog_info = self.catalog_path.lstat()
        if not stat.S_ISREG(catalog_info.st_mode):
            raise UpdateError(
                "UNSAFE_CATALOG", "The trusted component catalog must be a regular file."
            )
        if self.catalog_path == INSTALLED_CATALOG and (
            catalog_info.st_uid != 0 or stat.S_IMODE(catalog_info.st_mode) & 0o022
        ):
            raise UpdateError(
                "UNSAFE_CATALOG",
                "The installed component catalog must be root-owned and not group/world writable.",
            )
        bootstrap_bytes = self.catalog_path.read_bytes()
        bootstrap_digest = "sha256:" + hashlib.sha256(bootstrap_bytes).hexdigest()
        if trusted_catalog_digest is not None and bootstrap_digest != trusted_catalog_digest:
            raise UpdateError(
                "CATALOG_DIGEST_MISMATCH",
                "The installed component catalog does not match its compiled authority pin.",
            )
        self.bootstrap_catalog_digest = bootstrap_digest
        self.bootstrap_catalog_bytes = bootstrap_bytes
        self.load_active_catalog = load_active_catalog
        self._index_cache: dict[tuple[str, ...], tuple[dict[str, Any], str]] = {}
        self._readiness_cache: dict[tuple[str, bool], dict[str, Any]] = {}
        self.catalog_source: dict[str, Any] | None = None
        self.catalog_bytes = bootstrap_bytes
        self.catalog_digest = bootstrap_digest
        try:
            catalog_value = json.loads(bootstrap_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError(
                "INVALID_LOCAL_STATE", f"Cannot read component catalog: {error}"
            ) from error
        if load_active_catalog:
            active = self._read_active_catalog()
            if active is not None:
                catalog_bytes, metadata = active
                try:
                    catalog_value = json.loads(catalog_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise UpdateError(
                        "INVALID_CATALOG", f"The active component catalog is invalid: {error}"
                    ) from error
                self.catalog_bytes = catalog_bytes
                self.catalog_digest = metadata["catalogSha256"]
                self.catalog_source = metadata
        if not isinstance(catalog_value, dict):
            raise UpdateError("INVALID_CATALOG", "The component catalog must be a JSON object.")
        self.catalog = catalog_value
        if (
            isinstance(self.catalog.get("schemaVersion"), bool)
            or self.catalog.get("schemaVersion") != 1
            or not isinstance(self.catalog.get("components"), list)
        ):
            raise UpdateError(
                "INVALID_CATALOG", "The installed component catalog has an unsupported schema."
            )
        generation = self.catalog.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise UpdateError(
                "INVALID_CATALOG", "The installed component catalog generation is invalid."
            )
        self.catalog_generation = generation
        self.components = {
            item.get("componentId"): item
            for item in self.catalog["components"]
            if isinstance(item, dict) and isinstance(item.get("componentId"), str)
        }
        self.targets = {
            item["id"]: item
            for item in self.catalog.get("targets", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        self.native_python_profiles = self._read_native_python_profiles()
        self.publishers = {
            item["repository"]: item
            for item in self.catalog.get("publishers", [])
            if isinstance(item, dict) and isinstance(item.get("repository"), str)
        }

    def _read_native_python_profiles(self) -> dict[str, dict[str, Any]]:
        """Read package-pinned Python target profiles; a missing lock disables Python updates."""

        path = self.release_lock_path
        if not path.exists() and not path.is_symlink():
            return {}
        try:
            info = path.lstat()
            if (
                path.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                return {}
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        profiles = value.get("nativePythonProfiles") if isinstance(value, dict) else None
        if not isinstance(profiles, dict):
            return {}

        validated: dict[str, dict[str, Any]] = {}
        for profile_id, profile in profiles.items():
            if not isinstance(profile_id, str) or not isinstance(profile, dict):
                continue
            if (
                set(profile)
                != set(NATIVE_PYTHON_TARGET_FIELDS)
                | {"pythonVersion", "pythonExecutable", "pythonInput", "wheelResolver"}
                or profile.get("pythonVersion") != "3.12.14"
                or profile.get("pythonExecutable") != str(DEFAULT_PRIVATE_PYTHON)
                or profile.get("pythonInput") != "packaging/python-runtime.lock.json"
                or profile.get("runtime") != "python:3.12"
                or profile.get("architecture") != "x86_64"
                or profile.get("distribution") != "ubuntu"
                or (profile.get("osVersion"), profile.get("abi"))
                not in {("22.04", "glibc-2.35"), ("24.04", "glibc-2.39")}
            ):
                continue
            resolver = profile.get("wheelResolver")
            if not isinstance(resolver, dict) or not isinstance(
                resolver.get("allowedWheelTags"), dict
            ):
                continue
            allowed = resolver["allowedWheelTags"]
            pep600 = allowed.get("pep600")
            if (
                set(resolver) != {"tool", "version", "arguments", "allowedWheelTags"}
                or set(allowed) != {"purePython", "pep600"}
                or not isinstance(pep600, dict)
                or set(pep600) != {"architecture", "maxGlibc"}
                or resolver.get("tool") != "uv"
                or resolver.get("version") != "0.12.21"
                or resolver.get("arguments") != ["--python-platform", "x86_64-unknown-linux-gnu"]
                or allowed.get("purePython") != ["*-none-any"]
                or pep600.get("architecture") != "x86_64"
                or pep600.get("maxGlibc") != profile["abi"].removeprefix("glibc-")
            ):
                continue
            if (
                profile.get("os") != "linux"
                or profile.get("osVersion") not in {"22.04", "24.04"}
                or profile.get("distributionVersion") != profile.get("osVersion")
                or profile_id != f"linux-ubuntu-{profile['osVersion']}-x86_64-python-3.12"
            ):
                continue
            validated[profile_id] = profile
        return validated

    def _private_python_runtime_ready(self, profile: dict[str, Any]) -> bool:
        """Require the exact root-owned CPython runtime installed by the DEB."""

        executable = Path(str(profile.get("pythonExecutable", "")))
        if executable != DEFAULT_PRIVATE_PYTHON:
            return False
        current = Path(executable.anchor)
        try:
            for part in executable.parts[1:]:
                current = current / part
                info = current.lstat()
                if current == executable:
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != 0
                        or info.st_mode & 0o022
                        or not os.access(current, os.X_OK)
                    ):
                        return False
                elif not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    return False
            result = self.runner(
                [
                    str(executable),
                    "-I",
                    "-c",
                    (
                        "import platform,sys; "
                        "print(platform.python_implementation()); "
                        "print('.'.join(map(str, sys.version_info[:3]))); "
                        "print(platform.machine())"
                    ),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return False
        if result.returncode != 0:
            return False
        return result.stdout.strip().splitlines() == [
            "CPython",
            profile["pythonVersion"],
            "x86_64",
        ]

    def _read_active_catalog(self) -> tuple[bytes, dict[str, Any]] | None:
        """Load the public high-water pointer and its protected immutable snapshot."""

        root_exists = ACTIVE_CATALOG_ROOT.exists() or ACTIVE_CATALOG_ROOT.is_symlink()
        pointer_exists = ACTIVE_CATALOG_POINTER.exists() or ACTIVE_CATALOG_POINTER.is_symlink()
        if not root_exists and not pointer_exists:
            return None
        if not pointer_exists:
            if getattr(self, "allow_incomplete_catalog", False) and root_exists:
                # Only the explicit import command enables this recovery path. The public
                # updater and component runner fail closed when first activation stopped
                # after publishing its snapshot directory but before the state pointer.
                return None
            raise UpdateError(
                "UNSAFE_CATALOG",
                "A catalog snapshot directory exists without its protected high-water pointer.",
            )
        if not root_exists:
            raise UpdateError(
                "UNSAFE_CATALOG",
                "The protected catalog high-water pointer exists but its snapshot directory is missing.",
            )
        _verify_root_protected_directory(ACTIVE_CATALOG_ROOT)
        _verify_root_protected_directory(ACTIVE_CATALOG_POINTER.parent)
        if ACTIVE_CATALOG_POINTER.is_symlink() or not ACTIVE_CATALOG_POINTER.is_file():
            raise UpdateError("UNSAFE_CATALOG", "The protected active-catalog pointer is unsafe.")
        pointer_bytes = _read_root_catalog_object(
            ACTIVE_CATALOG_POINTER, "active component-catalog pointer"
        )
        try:
            pointer = json.loads(
                pointer_bytes.decode("utf-8"), object_pairs_hook=_unique_json_object
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError(
                "INVALID_CATALOG", f"Active catalog pointer is invalid: {error}"
            ) from error
        if (
            not isinstance(pointer, dict)
            or set(pointer) != {"schemaVersion", "catalogFile", "attestationFile", "metadata"}
            or isinstance(pointer.get("schemaVersion"), bool)
            or pointer.get("schemaVersion") != 1
        ):
            raise UpdateError("INVALID_CATALOG", "Active catalog pointer has an unsupported shape.")
        metadata = pointer.get("metadata")
        if not isinstance(metadata, dict):
            raise UpdateError("INVALID_CATALOG", "Active catalog metadata is missing.")
        required_metadata = {
            "schemaVersion",
            "repository",
            "workflow",
            "channel",
            "releaseId",
            "sourceCommit",
            "sourceRef",
            "catalogSha256",
            "generation",
            "subjectName",
            "attestationAssetName",
        }
        if set(metadata) != required_metadata:
            raise UpdateError(
                "INVALID_CATALOG", "Active catalog metadata has an unsupported shape."
            )
        digest = metadata.get("catalogSha256")
        match = re.fullmatch(r"sha256:([0-9a-f]{64})", str(digest))
        if (
            isinstance(metadata.get("schemaVersion"), bool)
            or metadata.get("schemaVersion") != 1
            or metadata.get("repository") != "DoHorizon-AI/Cyrene-Workspace"
            or metadata.get("workflow")
            != "DoHorizon-AI/Cyrene-Workspace/.github/workflows/component-catalog-release.yml"
            or metadata.get("channel") not in {"stable", "preview"}
            or not isinstance(metadata.get("releaseId"), str)
            or not isinstance(metadata.get("sourceCommit"), str)
            or re.fullmatch(r"[0-9a-f]{40}", metadata["sourceCommit"]) is None
            or metadata.get("sourceRef")
            not in (
                {"refs/heads/main", "refs/heads/release"}
                if metadata.get("channel") == "stable"
                else {"refs/heads/develop"}
            )
            or match is None
            or not isinstance(metadata.get("generation"), int)
            or isinstance(metadata.get("generation"), bool)
            or metadata["generation"] < 1
            or metadata.get("subjectName") != "component-catalog-v1.json"
            or metadata.get("attestationAssetName") != "component-catalog-v1.json.attestation.jsonl"
        ):
            raise UpdateError("INVALID_CATALOG", "Active catalog metadata identity is invalid.")
        expected_tag = f"catalog-{metadata['channel']}-{metadata['sourceCommit']}"
        if metadata.get("releaseId") != expected_tag:
            raise UpdateError(
                "INVALID_CATALOG", "Active catalog tag does not match its channel/source."
            )
        catalog_name = f"catalog-{match.group(1)}.json"
        attestation_name = f"catalog-{match.group(1)}-{metadata['sourceCommit']}.attestation.jsonl"
        if (
            pointer.get("catalogFile") != catalog_name
            or pointer.get("attestationFile") != attestation_name
        ):
            raise UpdateError(
                "INVALID_CATALOG", "Active catalog pointer paths do not match its digest."
            )
        catalog_bytes = _read_root_catalog_object(
            ACTIVE_CATALOG_ROOT / catalog_name, "active catalog"
        )
        if "sha256:" + hashlib.sha256(catalog_bytes).hexdigest() != digest:
            raise UpdateError(
                "CATALOG_DIGEST_MISMATCH", "Active catalog bytes differ from its receipt."
            )
        _read_root_catalog_object(
            ACTIVE_CATALOG_ROOT / attestation_name, "active catalog detached attestation"
        )
        try:
            catalog_value = json.loads(
                catalog_bytes.decode("utf-8"), object_pairs_hook=_unique_json_object
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError(
                "INVALID_CATALOG", f"Active catalog JSON is invalid: {error}"
            ) from error
        if (
            not isinstance(catalog_value, dict)
            or catalog_value.get("generation") != metadata["generation"]
        ):
            raise UpdateError(
                "INVALID_CATALOG", "Active catalog generation differs from its receipt."
            )
        return catalog_bytes, metadata

    def _ensure_state_root(self) -> Path:
        """Create or validate the private root-owned updater journal directory."""

        try:
            self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as error:
            raise UpdateError(
                "UNSAFE_STATE", f"Cannot prepare update state directory: {error}"
            ) from error
        _verify_private_directory(self.state_root)
        return self.state_root

    def _private_state_directory(self, name: str) -> Path:
        if name not in {"plans", "staged", "transactions", "locks", "installed"}:
            raise UpdateError("UNSAFE_STATE", "Invalid updater state directory.")
        root = self._ensure_state_root()
        directory = root / name
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
        except OSError as error:
            raise UpdateError(
                "UNSAFE_STATE", f"Cannot prepare update state directory {directory}: {error}"
            ) from error
        _verify_private_directory(directory)
        return directory

    def _read_catalog_floor(self) -> dict[str, Any] | None:
        """Read the private monotonic catalog receipt used by privileged operations."""

        floor_path = self.state_root / "catalog-state.json"
        if not self.state_root.exists() and not self.state_root.is_symlink():
            return None
        _verify_private_directory(self.state_root)
        if not floor_path.exists() and not floor_path.is_symlink():
            return None
        if floor_path.is_symlink():
            raise UpdateError("UNSAFE_CATALOG", "Catalog monotonic receipt must not be a symlink.")
        try:
            info = floor_path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise UpdateError(
                    "UNSAFE_CATALOG", "Catalog monotonic receipt must be a root-owned 0600 file."
                )
        except OSError as error:
            raise UpdateError(
                "UNSAFE_CATALOG", f"Cannot inspect catalog receipt: {error}"
            ) from error
        floor = _read_object(floor_path, "catalog monotonic receipt")
        if (
            set(floor) != {"schemaVersion", "generation", "catalogSha256", "releaseId"}
            or floor.get("schemaVersion") != 1
            or not isinstance(floor.get("generation"), int)
            or isinstance(floor.get("generation"), bool)
            or floor["generation"] < 1
            or not _valid_digest(floor.get("catalogSha256"))
            or not isinstance(floor.get("releaseId"), str)
        ):
            raise UpdateError("INVALID_CATALOG", "Catalog monotonic receipt has an invalid shape.")
        return floor

    def _write_catalog_floor(self, metadata: dict[str, Any]) -> None:
        self._private_state_directory("locks")
        _atomic_json(
            self.state_root / "catalog-state.json",
            {
                "schemaVersion": 1,
                "generation": metadata["generation"],
                "catalogSha256": metadata["catalogSha256"],
                "releaseId": metadata["releaseId"],
            },
            mode=0o600,
        )

    def _check_catalog_floor(self, metadata: dict[str, Any]) -> None:
        floor = self._read_catalog_floor()
        if floor is None:
            return
        generation = metadata["generation"]
        if generation < floor["generation"]:
            raise UpdateError(
                "CATALOG_ROLLBACK",
                "The active catalog generation is below the protected monotonic receipt.",
            )
        if (
            generation == floor["generation"]
            and metadata["catalogSha256"] != floor["catalogSha256"]
        ):
            raise UpdateError(
                "CATALOG_GENERATION_COLLISION",
                "The same catalog generation is already pinned to different raw catalog bytes.",
            )

    def _assert_no_pending_catalog_intent(self) -> None:
        """Do not switch trusted policy while maintenance or restore recovery is pending."""

        transaction_root = self.state_root / "transactions"
        if transaction_root.exists() or transaction_root.is_symlink():
            if transaction_root.is_symlink() or not transaction_root.is_dir():
                raise UpdateError("UNSAFE_STATE", "Update transaction directory is unsafe.")
            _verify_private_directory(transaction_root)
            for path in sorted(transaction_root.iterdir()):
                if path.is_symlink() or not path.is_file():
                    raise UpdateError(
                        "PENDING_MAINTENANCE", "A transaction entry blocks catalog activation."
                    )
                transaction = _read_object(path, "pending component update transaction")
                if transaction.get("phase") not in {"succeeded", "rolled_back"}:
                    raise UpdateError(
                        "PENDING_MAINTENANCE",
                        "Resolve pending maintenance or rollback recovery before importing a catalog.",
                    )

        for service in ("navigator", "yield", "reactor", "exchange", "catalyst"):
            intent = self.install_root / "services" / service / "update-journal.json"
            if intent.exists() or intent.is_symlink():
                raise UpdateError(
                    "PENDING_RESTORE_INTENT",
                    f"Resolve the pending {service} update/restore intent before importing a catalog.",
                )

    def _fetch_catalog_candidate(
        self,
        *,
        channel: str,
        release_id: str | None = None,
        latest: bool = False,
    ) -> tuple[dict[str, Any], bytes, bytes]:
        if channel not in {"stable", "preview"}:
            raise UpdateError("INVALID_CATALOG", "Catalog channel must be stable or preview.")
        if (release_id is None) == (not latest):
            raise UpdateError(
                "INVALID_REQUEST",
                "Specify exactly one catalog release ID or latest-candidate selector.",
            )
        try:
            import importlib.util

            metadata_path = Path(__file__).resolve().with_name("catalog_metadata.py")
            metadata_spec = importlib.util.spec_from_file_location(
                "_cyrene_catalog_metadata", metadata_path
            )
            if metadata_spec is None or metadata_spec.loader is None:
                raise ImportError("catalog metadata module has no import loader")
            catalog_metadata = importlib.util.module_from_spec(metadata_spec)
            metadata_spec.loader.exec_module(catalog_metadata)
        except (ImportError, OSError, AttributeError) as error:
            raise UpdateError(
                "CATALOG_HELPER_MISSING", "The trusted catalog metadata helper is missing."
            ) from error
        with tempfile.TemporaryDirectory(prefix="cyrene-catalog-") as temporary:
            root = Path(temporary)
            raw_path = root / "component-catalog-v1.json"
            metadata_path = root / "component-catalog-v1.metadata.json"
            attestation_path = root / "component-catalog-v1.json.attestation.jsonl"
            try:
                metadata = catalog_metadata.fetch_verified_catalog(
                    channel=channel,
                    release_id=release_id,
                    latest_channel=channel if latest else None,
                    output_path=raw_path,
                    metadata_path=metadata_path,
                    attestation_output_path=attestation_path,
                    schema_root=(
                        CATALOG_SCHEMA_ROOT
                        if CATALOG_SCHEMA_ROOT.is_dir()
                        else Path(__file__).resolve().parents[1] / "governance"
                    ),
                    verify_attestation=self._verify_attestation,
                )
            except Exception as error:
                code = getattr(error, "code", "CATALOG_VERIFICATION_FAILED")
                raise UpdateError(code, f"Trusted catalog verification failed: {error}") from error
            try:
                return metadata, raw_path.read_bytes(), attestation_path.read_bytes()
            except OSError as error:
                raise UpdateError(
                    "CATALOG_HELPER_OUTPUT_INVALID",
                    f"Verified catalog helper omitted an asset: {error}",
                ) from error

    def catalog_status(self) -> dict[str, Any]:
        """Report the active trusted catalog identity without changing it."""

        self._reload_catalog_for_operation()
        active = self._read_active_catalog()
        if active is None:
            metadata: dict[str, Any] = {
                "channel": "bootstrap",
                "releaseId": None,
                "sourceCommit": None,
                "sourceRef": None,
                "generation": self.catalog_generation,
                "catalogSha256": self.bootstrap_catalog_digest,
            }
        else:
            _, metadata = active
            self._check_catalog_floor(metadata) if _running_as_root() else None
        return {"status": "ready", "active": metadata}

    def catalog_check(
        self, *, channel: str, release_id: str | None = None, latest: bool = False
    ) -> dict[str, Any]:
        """Verify and describe a catalog candidate without activating it."""

        metadata, _, _ = self._fetch_catalog_candidate(
            channel=channel, release_id=release_id, latest=latest
        )
        return {"status": "candidate_verified", "activated": False, "candidate": metadata}

    def catalog_import(self, *, channel: str, release_id: str) -> dict[str, Any]:
        """Verify an exact immutable catalog release and explicitly activate its metadata."""

        self._require_authorized_process()
        self._ensure_state_root()
        metadata, catalog_bytes, attestation_bytes = self._fetch_catalog_candidate(
            channel=channel, release_id=release_id
        )
        with self._exclusive_update_lock():
            active = self._read_active_catalog()
            floor = self._read_catalog_floor()
            current_generation = self.catalog_generation
            current_digest = self.bootstrap_catalog_digest
            if active is not None:
                current_generation = active[1]["generation"]
                current_digest = active[1]["catalogSha256"]
            if floor is not None:
                if floor["generation"] > current_generation:
                    current_generation = floor["generation"]
                    current_digest = floor["catalogSha256"]
                elif (
                    floor["generation"] == current_generation
                    and floor["catalogSha256"] != current_digest
                ):
                    raise UpdateError(
                        "CATALOG_GENERATION_COLLISION",
                        "The protected catalog receipt conflicts with the active catalog bytes.",
                    )
            candidate_generation = metadata["generation"]
            if candidate_generation < current_generation:
                raise UpdateError(
                    "CATALOG_ROLLBACK", "Catalog import would roll back the active generation."
                )
            if (
                candidate_generation == current_generation
                and metadata["catalogSha256"] != current_digest
            ):
                raise UpdateError(
                    "CATALOG_GENERATION_COLLISION",
                    "The same catalog generation is already pinned to different raw catalog bytes.",
                )
            if (
                active is not None
                and active[1]["generation"] == candidate_generation
                and active[1]["catalogSha256"] == metadata["catalogSha256"]
                and candidate_generation == current_generation
            ):
                self._refresh_active_catalog()
                return {
                    "status": "already_active",
                    "active": active[1],
                    "activated": False,
                }

            self._assert_no_pending_catalog_intent()
            digest_hex = metadata["catalogSha256"].removeprefix("sha256:")
            catalog_name = f"catalog-{digest_hex}.json"
            attestation_name = f"catalog-{digest_hex}-{metadata['sourceCommit']}.attestation.jsonl"
            catalog_path = ACTIVE_CATALOG_ROOT / catalog_name
            attestation_path = ACTIVE_CATALOG_ROOT / attestation_name
            pointer = {
                "schemaVersion": 1,
                "catalogFile": catalog_name,
                "attestationFile": attestation_name,
                "metadata": metadata,
            }
            if not ACTIVE_CATALOG_ROOT.exists() and not ACTIVE_CATALOG_ROOT.is_symlink():
                staging = _stage_initial_catalog_directory(
                    metadata, catalog_bytes, attestation_bytes
                )
                try:
                    _activate_initial_catalog_directory(staging)
                    _atomic_root_catalog_bytes(
                        ACTIVE_CATALOG_POINTER,
                        json.dumps(pointer, ensure_ascii=False, sort_keys=True, indent=2).encode(
                            "utf-8"
                        )
                        + b"\n",
                    )
                    # The public state pointer carries the active generation/digest and remains
                    # readable by non-root service launchers. A crash before this write leaves
                    # an unmarked snapshot that only explicit import recovery may load.
                    self._write_catalog_floor(metadata)
                finally:
                    if staging.exists() and not staging.is_symlink():
                        shutil.rmtree(staging, ignore_errors=True)
            else:
                _verify_root_protected_directory(ACTIVE_CATALOG_ROOT)
                if catalog_path.exists() or catalog_path.is_symlink():
                    existing = _read_root_catalog_object(catalog_path, "catalog snapshot")
                    if (
                        "sha256:" + hashlib.sha256(existing).hexdigest()
                        != metadata["catalogSha256"]
                    ):
                        raise UpdateError(
                            "CATALOG_DIGEST_MISMATCH", "Catalog snapshot path collision detected."
                        )
                else:
                    _atomic_root_catalog_bytes(catalog_path, catalog_bytes)
                if attestation_path.exists() or attestation_path.is_symlink():
                    previous_proof = _read_root_catalog_object(
                        attestation_path, "catalog attestation snapshot"
                    )
                    if previous_proof != attestation_bytes:
                        raise UpdateError(
                            "CATALOG_PROOF_COLLISION",
                            "Detached proof differs for identical catalog bytes.",
                        )
                else:
                    _atomic_root_catalog_bytes(attestation_path, attestation_bytes)
                _atomic_root_catalog_bytes(
                    ACTIVE_CATALOG_POINTER,
                    json.dumps(pointer, ensure_ascii=False, sort_keys=True, indent=2).encode(
                        "utf-8"
                    )
                    + b"\n",
                )
                # The pointer carries the active generation and digest. If interrupted before
                # this receipt update, the next root load advances the floor from that pointer.
                self._write_catalog_floor(metadata)
            self._refresh_active_catalog()
            return {"status": "imported", "active": metadata, "activated": True}

    def _refresh_active_catalog(self) -> None:
        active = self._read_active_catalog()
        if active is None:
            raise UpdateError(
                "UNSAFE_CATALOG", "Catalog activation did not publish an active pointer."
            )
        catalog_bytes, metadata = active
        if _running_as_root():
            floor = self._read_catalog_floor()
            if floor is not None and (
                floor["generation"] > metadata["generation"]
                or (
                    floor["generation"] == metadata["generation"]
                    and floor["catalogSha256"] != metadata["catalogSha256"]
                )
            ):
                raise UpdateError(
                    "CATALOG_ROLLBACK",
                    "Active catalog pointer conflicts with its monotonic receipt.",
                )
            if floor is None or floor["generation"] < metadata["generation"]:
                self._write_catalog_floor(metadata)
        try:
            catalog_value = json.loads(
                catalog_bytes.decode("utf-8"), object_pairs_hook=_unique_json_object
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError(
                "INVALID_CATALOG", f"Active catalog JSON is invalid: {error}"
            ) from error
        if not isinstance(catalog_value, dict):
            raise UpdateError("INVALID_CATALOG", "Active catalog must be a JSON object.")
        self.catalog = catalog_value
        self.catalog_bytes = catalog_bytes
        self.catalog_digest = metadata["catalogSha256"]
        self.catalog_generation = metadata["generation"]
        self.catalog_source = metadata
        self.components = {
            item.get("componentId"): item
            for item in self.catalog.get("components", [])
            if isinstance(item, dict) and isinstance(item.get("componentId"), str)
        }
        self.targets = {
            item["id"]: item
            for item in self.catalog.get("targets", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        self.publishers = {
            item["repository"]: item
            for item in self.catalog.get("publishers", [])
            if isinstance(item, dict) and isinstance(item.get("repository"), str)
        }
        self._index_cache.clear()

    def _reload_catalog_for_operation(self) -> None:
        """Re-read the protected active pointer at each check/stage/apply boundary."""

        active = self._read_active_catalog()
        if active is None:
            if _running_as_root() and self._read_catalog_floor() is not None:
                raise UpdateError(
                    "UNSAFE_CATALOG", "A previously activated catalog pointer is missing."
                )
            if self.catalog_source is not None:
                try:
                    value = json.loads(self.bootstrap_catalog_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise UpdateError(
                        "INVALID_CATALOG", f"Bootstrap catalog is invalid: {error}"
                    ) from error
                self.catalog = value
                self.catalog_bytes = self.bootstrap_catalog_bytes
                self.catalog_digest = self.bootstrap_catalog_digest
                self.catalog_generation = value["generation"]
                self.catalog_source = None
                self.components = {
                    item.get("componentId"): item
                    for item in value.get("components", [])
                    if isinstance(item, dict) and isinstance(item.get("componentId"), str)
                }
                self.targets = {
                    item["id"]: item
                    for item in value.get("targets", [])
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
                self.publishers = {
                    item["repository"]: item
                    for item in value.get("publishers", [])
                    if isinstance(item, dict) and isinstance(item.get("repository"), str)
                }
                self._index_cache.clear()
            return
        self._refresh_active_catalog()

    def _require_authorized_process(self) -> None:
        if self.state_root == DEFAULT_STATE_ROOT and not _running_as_root():
            raise UpdateError(
                "PRIVILEGE_REQUIRED",
                "The fixed Linux update helper must run with its root-authorized OS identity.",
            )

    @contextmanager
    def _exclusive_update_lock(self):
        lock_path = self._private_state_directory("locks") / "update.lock"
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as error:
            raise UpdateError(
                "UNSAFE_STATE", f"Cannot open updater transaction lock: {error}"
            ) from error
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise UpdateError(
                    "UNSAFE_STATE", "Updater transaction lock is not a private regular file."
                )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise UpdateError(
                    "UPDATE_IN_PROGRESS",
                    "Another updater process is staging or applying a component plan.",
                    retryable=True,
                ) from error
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)

    def handle(self, request: Any) -> dict[str, Any]:
        """Validate and dispatch one fixed request, returning a shared envelope."""
        operation = request.get("operation") if isinstance(request, dict) else None
        envelope_operation = (
            operation
            if isinstance(operation, str) and operation in {"status", "check", "stage", "apply"}
            else "status"
        )
        try:
            if not isinstance(request, dict) or request.get("protocolVersion") != PROTOCOL_VERSION:
                raise UpdateError(
                    "INVALID_REQUEST", "Unsupported component update protocol version."
                )
            fields_by_operation = {
                "status": {
                    "protocolVersion",
                    "operation",
                    "bootstrapMode",
                    "includeProducts",
                },
                "check": {
                    "protocolVersion",
                    "operation",
                    "componentIds",
                    "channel",
                    "bootstrapMode",
                    "includeProducts",
                },
                "stage": {
                    "protocolVersion",
                    "operation",
                    "planId",
                    "planDigest",
                    "channel",
                    "bootstrapMode",
                },
                "apply": {
                    "protocolVersion",
                    "operation",
                    "planId",
                    "planDigest",
                    "confirmation",
                    "channel",
                    "bootstrapMode",
                },
            }
            if (
                operation not in fields_by_operation
                or set(request) - fields_by_operation[operation]
            ):
                raise UpdateError(
                    "INVALID_REQUEST", "The request contains an unsupported operation or field."
                )
            self._require_authorized_process()
            if "bootstrapMode" in request and request.get("bootstrapMode") != "first-core":
                raise UpdateError("INVALID_REQUEST", "Unsupported first-install bootstrap mode.")
            if "includeProducts" in request and request.get("bootstrapMode") != "first-core":
                raise UpdateError(
                    "INVALID_REQUEST", "Product first-start selection requires first-core mode."
                )
            if "includeProducts" in request and not isinstance(
                request.get("includeProducts"), bool
            ):
                raise UpdateError("INVALID_REQUEST", "includeProducts must be a boolean.")
            if request.get("bootstrapMode") == "first-core":
                import importlib.util

                helper_path = Path(__file__).with_name("native_core_bootstrap.py")
                if not helper_path.is_file():
                    helper_path = Path(__file__).resolve().parent / "native_core_bootstrap.py"
                spec = importlib.util.spec_from_file_location(
                    "_cyrene_native_core_bootstrap", helper_path
                )
                if spec is None or spec.loader is None:
                    raise UpdateError(
                        "HELPER_UNAVAILABLE", "First-Core bootstrap helper is missing."
                    )
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                bootstrap_result = module.handle(self, request)
                if bootstrap_result is not None:
                    return {
                        "protocolVersion": PROTOCOL_VERSION,
                        "ok": True,
                        "operation": envelope_operation,
                        "result": bootstrap_result,
                    }
            if operation == "status":
                result = self.status()
            elif operation == "check":
                result = self.check(request.get("componentIds"), channel=request.get("channel"))
            elif operation == "stage":
                result = self.stage(
                    request.get("planId"), request.get("planDigest"), channel=request.get("channel")
                )
            else:
                result = self.apply(
                    request.get("planId"),
                    request.get("planDigest"),
                    request.get("confirmation"),
                    channel=request.get("channel"),
                )
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "ok": True,
                "operation": envelope_operation,
                "result": result,
            }
        except UpdateError as error:
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "ok": False,
                "operation": envelope_operation,
                "error": {"code": error.code, "message": str(error), "retryable": error.retryable},
            }
        except Exception as error:  # noqa: BLE001 - preserve the fixed protocol envelope on unexpected helper errors.
            print(f"component update helper internal error: {error}", file=sys.stderr)
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "ok": False,
                "operation": envelope_operation,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "The component update helper could not complete this request.",
                    "retryable": False,
                },
            }

    def status(self) -> dict[str, Any]:
        """Read installed versions and live gate readiness without changing services."""

        self._reload_catalog_for_operation()
        self._ensure_state_root()
        rows = []
        plans = self._staged_plans()
        staged_by_component = {
            item["componentId"]: (plan, item)
            for plan in plans
            for item in plan.get("components", [])
        }
        for component in self._host_components():
            if self._target_for(component) is None:
                rows.append(self._unsupported_component(component))
                continue
            candidate = self._installed(component)
            gate = self._readiness(component)
            stage_record = staged_by_component.get(component["componentId"])
            if stage_record:
                phase = "staged"
            elif not candidate["activeVersion"]:
                phase = "available"
            else:
                phase = "current" if candidate.get("identityAttested") else "unknown"
            rows.append(self._result_component(component, gate, phase=phase, staged=stage_record))
        return {"status": "ready", "components": rows, "plans": plans}

    def check(
        self,
        component_ids: Any = None,
        *,
        channel: Any = None,
        include_readiness: bool = True,
    ) -> dict[str, Any]:
        """Discover trusted channel releases and produce digest-bound plans."""

        self._reload_catalog_for_operation()
        self._ensure_state_root()
        channel = self._resolve_channel(channel)
        if channel not in {"stable", "preview"}:
            raise UpdateError("INVALID_CATALOG", "The default release channel is invalid.")
        if component_ids is None:
            selected = list(self.components)
        elif (
            isinstance(component_ids, list)
            and component_ids
            and len(component_ids) <= 100
            and all(
                isinstance(value, str) and COMPONENT_ID_PATTERN.fullmatch(value)
                for value in component_ids
            )
            and len(set(component_ids)) == len(component_ids)
        ):
            selected = component_ids
        else:
            raise UpdateError(
                "INVALID_REQUEST", "componentIds must be a unique, non-empty component ID list."
            )
        unknown = sorted(set(selected) - set(self.components))
        if unknown:
            raise UpdateError(
                "INVALID_COMPONENT", f"Unknown trusted component IDs: {', '.join(unknown)}"
            )

        candidates: dict[str, Candidate] = {}
        skipped: dict[str, str] = {}
        unavailable: dict[str, UpdateError] = {}
        for component_id in selected:
            component = self.components[component_id]
            selected_target = self._target_for(component)
            if selected_target is None:
                skipped[component_id] = "unsupported"
                continue
            try:
                candidates[component_id] = self._candidate(component, selected_target, channel)
            except UpdateError as error:
                unavailable[component_id] = error

        candidates = self._expand_compatibility_groups(candidates, channel)
        self._validate_runtime_dependencies(candidates)
        selected = list(dict.fromkeys([*selected, *candidates.keys()]))
        plan_components = []
        for component_id, candidate in sorted(candidates.items()):
            installed = self._installed(candidate.component)
            same_release = (
                installed.get("active") is True
                and installed.get("identityAttested") is True
                and installed.get("releaseIdentity") == candidate.manifest_digest
                and installed.get("manifestDigest") == candidate.manifest_digest
                and installed.get("artifactDigest") == candidate.artifact_digest
            )
            if same_release:
                continue
            artifact = candidate.manifest["artifact"]
            plan_components.append(
                {
                    "componentId": component_id,
                    "version": candidate.manifest["version"],
                    "manifestDigest": candidate.manifest_digest,
                    "artifactDigest": artifact.get("digest", artifact.get("sha256")),
                    "restartGroup": candidate.component["restart"]["group"],
                }
            )

        plan = None
        if plan_components:
            digest_material = {
                "schemaVersion": 1,
                "channel": channel,
                "catalogGeneration": self.catalog_generation,
                "catalogDigest": self.catalog_digest,
                "components": plan_components,
            }
            plan_digest = _digest_json(digest_material, "planDigest")
            plan_id = "plan-" + plan_digest.split(":", 1)[1][:32]
            plan = {
                "planId": plan_id,
                "planDigest": plan_digest,
                "channel": channel,
                "catalogGeneration": self.catalog_generation,
                "catalogDigest": self.catalog_digest,
                "phase": "checked",
                "components": plan_components,
            }
        rows = []
        for component_id in selected:
            component = self.components[component_id]
            target = self._target_for(component)
            if component_id in skipped or target is None:
                rows.append(self._unsupported_component(component))
                continue
            if component_id in unavailable:
                error = unavailable[component_id]
                gate = (
                    self._readiness(component)
                    if include_readiness
                    else {
                        "status": "UNKNOWN",
                        "blocker_codes": ["READINESS_NOT_QUERIED"],
                        "message": "Check plan generation does not acquire the maintenance gate.",
                    }
                )
                row = self._result_component(
                    component, gate, phase="unknown", update_available=False
                )
                row["blockers"] = [{"code": error.code, "message": str(error)[:500]}]
                row["allowedActions"] = ["check"]
                rows.append(row)
                continue
            candidate = candidates[component_id]
            installed = self._installed(component)
            is_current = (
                installed.get("active") is True
                and installed.get("identityAttested") is True
                and installed.get("releaseIdentity") == candidate.manifest_digest
                and installed.get("manifestDigest") == candidate.manifest_digest
                and installed.get("artifactDigest") == candidate.artifact_digest
            )
            is_available = not is_current
            gate = (
                self._readiness(component)
                if include_readiness
                else {
                    "status": "UNKNOWN",
                    "blocker_codes": ["READINESS_NOT_QUERIED"],
                    "message": "Check plan generation does not acquire the maintenance gate.",
                }
            )
            rows.append(
                self._result_component(
                    component,
                    gate,
                    phase="available" if is_available else "current",
                    available_version=candidate.manifest["version"] if is_available else None,
                    update_available=is_available,
                )
            )
        result: dict[str, Any] = {
            "status": "checked",
            "components": rows,
            "plans": [plan] if plan else [],
        }
        if plan:
            result["plan"] = plan
            plan_directory = self._private_state_directory("plans")
            _atomic_json(plan_directory / f"{plan['planId']}.json", plan)
        return result

    def stage(self, plan_id: Any, plan_digest: Any, *, channel: Any = None) -> dict[str, Any]:
        self._require_authorized_process()
        with self._exclusive_update_lock():
            return self._stage_locked(plan_id, plan_digest, channel=channel)

    def _stage_locked(
        self, plan_id: Any, plan_digest: Any, *, channel: Any = None
    ) -> dict[str, Any]:
        """Download, verify, and install immutable payloads without gate or activation."""

        self._reload_catalog_for_operation()
        self._ensure_state_root()
        self._validate_plan_identity(plan_id, plan_digest)
        stored_plan = _read_object(
            self.state_root / "plans" / f"{plan_id}.json", "checked update plan"
        )
        if stored_plan.get("planDigest") != plan_digest:
            raise UpdateError(
                "PLAN_CHANGED",
                "The latest checked plan no longer matches this digest; run check again.",
                retryable=True,
            )
        if (
            stored_plan.get("catalogGeneration") != self.catalog_generation
            or stored_plan.get("catalogDigest") != self.catalog_digest
        ):
            raise UpdateError(
                "PLAN_CATALOG_CHANGED",
                "The trusted catalog changed after this plan was checked; run check again.",
                retryable=True,
            )
        channel = self._resolve_channel(
            channel if channel is not None else stored_plan.get("channel")
        )
        if stored_plan.get("channel") != channel:
            raise UpdateError(
                "PLAN_CHANNEL_MISMATCH",
                "Stage channel does not match the checked, digest-bound plan.",
            )
        component_ids = [
            item.get("componentId")
            for item in stored_plan.get("components", [])
            if isinstance(item, dict)
        ]
        checked = self.check(component_ids, channel=channel, include_readiness=False)
        plan = self._find_plan(checked, plan_id, plan_digest)
        candidate_map = self._resolve_plan_candidates(plan, channel)
        staged_root = self._private_state_directory("staged")
        plan_root = staged_root / plan_id
        if plan_root.is_symlink():
            raise UpdateError(
                "UNSAFE_STATE", f"Refusing a symlinked staged plan directory: {plan_root}"
            )
        if plan_root.exists():
            shutil.rmtree(plan_root)
        plan_root.mkdir(parents=True, mode=0o700)
        staged_components: list[dict[str, Any]] = []
        try:
            for plan_component in plan["components"]:
                candidate = candidate_map[plan_component["componentId"]]
                staged_components.append(
                    self._stage_candidate(candidate, plan_root, plan_id, plan_digest)
                )
            record = {
                "schemaVersion": 2,
                "plan": plan,
                "channel": channel,
                "components": staged_components,
                "phase": "staged",
                "createdAt": int(time.time()),
            }
            _atomic_json(plan_root / "stage.json", record)
        except Exception:
            shutil.rmtree(plan_root, ignore_errors=True)
            raise
        staged_plan = {**plan, "phase": "staged"}
        staged_rows = [
            self._result_component(
                self.components[item["componentId"]],
                {
                    "status": "UNKNOWN",
                    "blocker_codes": ["READINESS_NOT_QUERIED"],
                    "message": "Staging does not query or acquire the maintenance gate.",
                },
                phase="staged",
                available_version=item["version"],
                staged={"plan": staged_plan, "component": item},
            )
            for item in staged_components
        ]
        return {
            "status": "staged",
            "components": staged_rows,
            "plan": staged_plan,
            "plans": [staged_plan],
        }

    def apply(
        self,
        plan_id: Any,
        plan_digest: Any,
        confirmation: Any,
        *,
        channel: Any = None,
    ) -> dict[str, Any]:
        self._require_authorized_process()
        with self._exclusive_update_lock():
            return self._apply_locked(plan_id, plan_digest, confirmation, channel=channel)

    def _apply_locked(
        self,
        plan_id: Any,
        plan_digest: Any,
        confirmation: Any,
        *,
        channel: Any = None,
    ) -> dict[str, Any]:
        """Atomically gate the runtime, activate, restart, health-check, or rollback."""

        self._reload_catalog_for_operation()
        self._validate_plan_identity(plan_id, plan_digest)
        if (
            not isinstance(confirmation, dict)
            or set(confirmation) != {"planId", "planDigest", "confirmed"}
            or confirmation.get("confirmed") is not True
            or confirmation.get("planId") != plan_id
            or confirmation.get("planDigest") != plan_digest
        ):
            raise UpdateError(
                "CONFIRMATION_MISMATCH",
                "Apply requires confirmation bound to this exact planId and planDigest.",
            )
        stage_path = self.state_root / "staged" / plan_id / "stage.json"
        record = _read_object(stage_path, "staged update plan")
        if (
            record.get("phase") != "staged"
            or record.get("plan", {}).get("planDigest") != plan_digest
        ):
            raise UpdateError(
                "PLAN_NOT_STAGED", "This exact plan is not staged. Stage it again before apply."
            )
        stored_channel = record.get("channel")
        if stored_channel not in {"stable", "preview"} or (
            channel is not None and self._resolve_channel(channel) != stored_channel
        ):
            raise UpdateError(
                "PLAN_CHANNEL_MISMATCH",
                "Apply channel does not match the staged, digest-bound plan.",
            )
        self._validate_staged_record(
            record,
            expected_plan_id=plan_id,
            expected_plan_digest=plan_digest,
        )
        transaction_path = self._private_state_directory("transactions") / f"{plan_id}.json"
        existing = (
            _read_object(transaction_path, "update transaction")
            if transaction_path.exists()
            else None
        )
        if existing and existing.get("phase") not in {"succeeded", "rolled_back"}:
            return self._recover_transaction(existing, transaction_path, stage_path, confirmation)
        self._require_managed_services(record["components"])
        if not _running_as_root():
            raise UpdateError(
                "PRIVILEGE_REQUIRED",
                "Applying staged components requires the root-owned local update helper.",
            )

        target_kind = (
            "CORE_RUNTIME"
            if any(
                self.components[item["componentId"]]["restart"]["group"] == "core-runtime"
                for item in record["components"]
            )
            else "PACKAGE_ONLY"
        )
        readiness = self._readiness_for(target_kind, requires_restart=True, force=True)
        self._require_ready(readiness, target_kind)
        gate_catalog, gate_sources = self._activity_catalog()
        if readiness.get("install_catalog_generation") != gate_catalog["generation"]:
            raise UpdateError(
                "GATE_UNKNOWN",
                "Installed activity catalog changed during readiness; check again before applying.",
                retryable=True,
            )
        artifact_digests = {
            item["componentId"]: item["artifactDigest"] for item in record["components"]
        }
        authority_activation = self._prepare_authority_activation(record, plan_id, plan_digest)
        transaction = {
            "schemaVersion": 2,
            "planId": plan_id,
            "requestId": "cyrene-update-" + plan_id,
            "planDigest": plan_digest,
            "componentArtifactDigests": artifact_digests,
            "phase": "begin_pending",
            "targetKind": target_kind,
            "channel": stored_channel,
            "expectedGateGeneration": readiness.get("gate_generation"),
            "expectedCatalogGeneration": gate_catalog["generation"],
            "expectedActivitySources": gate_sources,
            "components": record["components"],
            "previous": self._capture_active_versions(record["components"]),
            "authorityActivation": authority_activation,
            "createdAt": int(time.time()),
        }
        _atomic_json(transaction_path, transaction)
        try:
            token = self._begin_maintenance(transaction)
        except UpdateError as error:
            if error.maintenance_not_acquired:
                self._clear_begin_pending(transaction, transaction_path)
            raise
        transaction["maintenanceToken"] = token
        transaction["phase"] = "applying"
        _atomic_json(transaction_path, transaction)
        try:
            self._activate_transaction(transaction)
            self._restart_transaction(transaction)
            self._health_transaction(transaction)
            self._activate_authority_bundle(transaction)
        except Exception as failure:
            healthy_rollback, rollback_message = self._rollback_transaction(transaction)
            transaction["phase"] = (
                "rollback_end_pending" if healthy_rollback else "rollback_required"
            )
            transaction["rollbackHealthy"] = healthy_rollback
            transaction["rollbackMessage"] = rollback_message
            _atomic_json(transaction_path, transaction)
            try:
                self._end_maintenance(
                    transaction,
                    outcome="ROLLED_BACK" if healthy_rollback else "FAILED",
                    healthy=healthy_rollback,
                )
            except UpdateError as end_error:
                transaction["recoveryError"] = str(end_error)
                _atomic_json(transaction_path, transaction)
                raise UpdateError(
                    "ROLLBACK_GATE_HELD",
                    f"Update failed ({failure}); rollback status: {rollback_message}; maintenance gate remains held because EndMaintenance failed: {end_error}",
                    retryable=True,
                ) from failure
            transaction["phase"] = "rolled_back" if healthy_rollback else "rollback_required"
            if healthy_rollback:
                transaction.pop("maintenanceToken", None)
            _atomic_json(transaction_path, transaction)
            raise UpdateError(
                "APPLY_ROLLED_BACK" if healthy_rollback else "ROLLBACK_UNHEALTHY",
                f"Update failed ({failure}). {rollback_message}",
                retryable=not healthy_rollback,
            ) from failure
        transaction["phase"] = "success_end_pending"
        _atomic_json(transaction_path, transaction)
        try:
            self._end_maintenance(transaction, outcome="SUCCESS", healthy=True)
        except UpdateError as end_error:
            transaction["recoveryError"] = str(end_error)
            _atomic_json(transaction_path, transaction)
            raise UpdateError(
                "GATE_END_PENDING",
                f"The new release is healthy, but maintenance completion is not confirmed: {end_error}. Retry this exact plan to reconcile the durable gate record.",
                retryable=True,
            ) from end_error
        transaction["phase"] = "succeeded"
        transaction.pop("maintenanceToken", None)
        transaction.pop("recoveryError", None)
        _atomic_json(transaction_path, transaction)
        return self._applied_result(transaction)

    def _resolve_channel(self, value: Any) -> str:
        channel = self.catalog.get("defaultChannel", DEFAULT_CHANNEL) if value is None else value
        if not isinstance(channel, str) or channel not in {"stable", "preview"}:
            raise UpdateError("INVALID_REQUEST", "channel must be stable or preview.")
        if channel not in self.catalog.get("channels", {}):
            raise UpdateError(
                "INVALID_CATALOG", f"The trusted catalog has no {channel} channel policy."
            )
        return channel

    def _validate_plan_identity(self, plan_id: Any, plan_digest: Any) -> None:
        if (
            not isinstance(plan_id, str)
            or PLAN_ID_PATTERN.fullmatch(plan_id) is None
            or not _valid_digest(plan_digest)
        ):
            raise UpdateError("INVALID_PLAN", "planId or sha256 planDigest is invalid.")

    @staticmethod
    def _find_plan(result: dict[str, Any], plan_id: str, plan_digest: str) -> dict[str, Any]:
        plan = next(
            (item for item in result.get("plans", []) if item.get("planId") == plan_id), None
        )
        if plan is None or plan.get("planDigest") != plan_digest:
            raise UpdateError(
                "PLAN_CHANGED",
                "The latest trusted release set no longer matches this plan; run check again.",
                retryable=True,
            )
        return plan

    def _host_components(self) -> list[dict[str, Any]]:
        return sorted(self.components.values(), key=lambda item: item["componentId"])

    def _target_for(self, component: dict[str, Any]) -> dict[str, Any] | None:
        try:
            host = platform.freedesktop_os_release()
        except OSError:
            return None
        if not sys.platform.startswith("linux"):
            return None
        host_arch = platform.machine().lower()
        architecture = (
            "x86_64"
            if host_arch in {"x86_64", "amd64"}
            else "aarch64"
            if host_arch in {"aarch64", "arm64"}
            else host_arch
        )
        libc_name, libc_version = platform.libc_ver()
        host_abi = f"{libc_name}-{libc_version}" if libc_name and libc_version else None
        host_version = host.get("VERSION_ID")
        for entry in component.get("targets", []):
            target = self.targets.get(entry.get("targetId"))
            if target is None or entry.get("support") != "supported":
                continue
            spec = target.get("target", {})
            if spec.get("os") != "linux" or spec.get("architecture") != architecture:
                continue
            portable_data_target = (
                entry.get("artifactKind") == "data-bundle"
                and entry.get("targetId") == "portable-contract-data-v1"
            )
            if portable_data_target:
                if architecture != spec.get("architecture"):
                    continue
            elif (
                host.get("ID") != "ubuntu"
                or host_version not in {"22.04", "24.04"}
                or spec.get("distribution") != "ubuntu"
                or spec.get("distributionVersion") != host_version
            ):
                continue
            if spec.get("abi") is not None and spec.get("abi") != host_abi:
                continue
            if entry.get("artifactKind") == "python-bundle":
                profile_id = entry.get("targetId")
                profile = self.native_python_profiles.get(profile_id)
                if (
                    profile is None
                    or any(profile.get(key) != spec.get(key) for key in NATIVE_PYTHON_TARGET_FIELDS)
                    or profile.get("osVersion") != host_version
                    or not self._private_python_runtime_ready(profile)
                ):
                    continue
            if entry.get("artifactKind") == "native-binary" and spec.get("runtime") != "systemd":
                continue
            if (
                entry.get("artifactKind") == "data-bundle"
                and spec.get("runtime") != "cyrene-authority-data"
            ):
                continue
            return {**target, "artifactKind": entry["artifactKind"]}
        return None

    def _unsupported_component(self, component: dict[str, Any]) -> dict[str, Any]:
        return {
            "componentId": component["componentId"],
            "installed": False,
            "supported": False,
            "phase": "unsupported",
            "target": None,
            "updateAvailable": False,
            "gate": {
                "state": "unknown",
                "activeTasks": [],
                "unknownActivitySources": [],
                "blockerCodes": ["UNSUPPORTED_TARGET"],
                "blockers": [
                    {
                        "code": "UNSUPPORTED_TARGET",
                        "message": "This component has no supported exact Ubuntu/glibc target in the trusted catalog.",
                    }
                ],
            },
            "allowedActions": [],
        }

    def _result_component(
        self,
        component: dict[str, Any],
        gate: dict[str, Any],
        *,
        phase: str,
        available_version: str | None = None,
        update_available: bool | None = None,
        staged: Any = None,
    ) -> dict[str, Any]:
        installed = self._installed(component)
        target = self._target_for(component)
        staged_version = None
        if isinstance(staged, tuple):
            staged_version = staged[1].get("version")
        elif isinstance(staged, dict):
            staged_version = staged.get("component", staged).get("version")
        actions = ["check"]
        if target is not None and update_available is True:
            actions.append("stage")
        is_data_bundle = component.get("kind") == "data-bundle"
        service_managed = target is not None and (
            self._authority_available() if is_data_bundle else self._unit_exists(component)
        )
        if (
            target is not None
            and phase == "staged"
            and gate.get("status") == "READY"
            and service_managed
        ):
            actions.append("apply")
        result = {
            "componentId": component["componentId"],
            "installed": installed["activeVersion"] is not None,
            "supported": target is not None,
            "activeVersion": installed["activeVersion"],
            "availableVersion": available_version,
            "stagedVersion": staged_version,
            "phase": phase,
            "target": target["target"] if target else None,
            "artifactKind": target.get("artifactKind") if target else None,
            "updateAvailable": bool(update_available),
            "gate": self._public_gate(gate),
            "allowedActions": actions,
        }
        if target is not None and not service_managed:
            result["blockers"] = [
                {
                    "code": "AUTHORITY_NOT_MANAGED" if is_data_bundle else "SERVICE_NOT_MANAGED",
                    "message": "Authority's restricted local activation socket is unavailable."
                    if is_data_bundle
                    else "The OS target is available, but no catalog-matched local systemd service is installed.",
                }
            ]
        return {key: value for key, value in result.items() if value is not None}

    def _installed(self, component: dict[str, Any]) -> dict[str, Any]:
        if component.get("kind") == "data-bundle":
            if not self._authority_available():
                return self._empty_installed()
            status = self._authority_request({"action": "status"})
            artifact_id = status.get("activeArtifactId")
            if artifact_id is None:
                return self._empty_installed()
            if not _valid_digest(artifact_id):
                raise UpdateError(
                    "AUTHORITY_STATUS_INVALID",
                    "Authority returned an invalid active artifact identity.",
                )
            receipt_directory = self._installed_component_directory(component["componentId"])
            releases = receipt_directory / "releases" if receipt_directory else None
            if releases is not None and releases.is_dir() and not releases.is_symlink():
                for receipt_path in releases.glob("*.json"):
                    identity = "sha256:" + receipt_path.stem
                    receipt = self._read_release_receipt(component["componentId"], identity)
                    if receipt is None or receipt.get("artifactDigest") != artifact_id:
                        continue
                    manifest = receipt.get("manifest")
                    self._validate_manifest_digest(manifest, identity)
                    if _artifact_kind_from_manifest(manifest) != "data-bundle":
                        raise UpdateError(
                            "INVALID_INSTALLED_RELEASE",
                            "Authority data-bundle receipt has the wrong artifact kind.",
                        )
                    return {
                        "activeVersion": manifest["version"],
                        "manifest": manifest,
                        "active": True,
                        "identityAttested": True,
                        "releaseIdentity": identity,
                        "manifestDigest": identity,
                        "artifactDigest": artifact_id,
                        "pointerIdentity": artifact_id.removeprefix("sha256:"),
                        "bundleIdentity": artifact_id,
                        "authorityGeneration": status.get("highestGeneration"),
                        "authorityActivationEpoch": status.get("activationEpoch"),
                    }
            return {
                "activeVersion": artifact_id,
                "manifest": None,
                "active": True,
                "identityAttested": True,
                "releaseIdentity": artifact_id,
                "manifestDigest": None,
                "artifactDigest": artifact_id,
                "pointerIdentity": artifact_id.removeprefix("sha256:"),
                "bundleIdentity": artifact_id,
                "authorityGeneration": status.get("highestGeneration"),
                "authorityActivationEpoch": status.get("activationEpoch"),
            }
        service = component.get("pythonBundleService")
        if service:
            try:
                module = self._load_service_bundle()
                active = module.resolve_active_release(service, install_root=self.install_root)
                if active is None:
                    if self._read_active_receipt(component["componentId"]) is not None:
                        raise UpdateError(
                            "INVALID_INSTALLED_RELEASE",
                            f"Installed identity receipt exists without an active {service} bundle.",
                        )
                    return self._empty_installed()
                inner = module.validate_bundle(active[0].parent, expected_service=service)
                bundle_identity = inner.get("artifact_digest")
                pointer_identity = active[1]
                receipt = self._read_active_receipt(component["componentId"])
                if receipt is None:
                    # Existing inner bundles remain readable and can be restored,
                    # but an inner hash cannot stand in for a verified outer release.
                    return {
                        **self._empty_installed(active_version=active[1], active=True),
                        "manifest": inner,
                        "pointerIdentity": pointer_identity,
                        "bundleIdentity": bundle_identity,
                    }
                outer = receipt["manifest"]
                if (
                    _artifact_kind_from_manifest(outer) != "python-bundle"
                    or receipt["bundleIdentity"] != bundle_identity
                    or receipt.get("pointerIdentity", pointer_identity) != pointer_identity
                ):
                    raise UpdateError(
                        "INVALID_INSTALLED_RELEASE",
                        f"Installed {component['componentId']} receipt does not match the active bundle pointer.",
                    )
                return {
                    "activeVersion": outer["version"],
                    "manifest": outer,
                    "active": True,
                    "identityAttested": True,
                    "releaseIdentity": receipt["releaseIdentity"],
                    "manifestDigest": receipt["manifestDigest"],
                    "artifactDigest": receipt["artifactDigest"],
                    "pointerIdentity": pointer_identity,
                    "bundleIdentity": bundle_identity,
                }
            except UpdateError:
                raise
            except Exception as error:
                raise UpdateError(
                    "INVALID_INSTALLED_RELEASE",
                    f"Installed {component['componentId']} release failed integrity validation: {error}",
                ) from error
        root = self.install_root / "components" / component["componentId"]
        active = root / "active"
        if not active.exists() and not active.is_symlink():
            if self._read_active_receipt(component["componentId"]) is not None:
                raise UpdateError(
                    "INVALID_INSTALLED_RELEASE",
                    f"Installed identity receipt exists without an active {component['componentId']} release.",
                )
            return self._empty_installed()
        info = active.lstat()
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Active pointer is unsafe for {component['componentId']}.",
            )
        target = os.readlink(active)
        match = re.fullmatch(r"releases/([^/]+)", target)
        if not match:
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Active pointer has an unsafe target for {component['componentId']}.",
            )
        pointer_identity = match.group(1)
        version, pointer_digest = _native_release_directory_identity(pointer_identity)
        if VERSION_PATTERN.fullmatch(version) is None:
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Active pointer has an unsafe release identity for {component['componentId']}.",
            )
        release = root / "releases" / pointer_identity
        if release.is_symlink() or not release.is_dir():
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Active release directory is unsafe for {component['componentId']}.",
            )
        manifest = _read_object(
            release / "component-manifest.json", f"installed {component['componentId']} manifest"
        )
        self._validate_manifest_digest(manifest, manifest.get("manifestDigest"))
        if (
            manifest.get("componentId") != component["componentId"]
            or manifest.get("version") != version
            or _artifact_kind_from_manifest(manifest) != "native-binary"
            or (
                pointer_digest is not None
                and pointer_digest != manifest["manifestDigest"].removeprefix("sha256:")
            )
            or (pointer_digest is None and pointer_identity != version)
        ):
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Installed release identity differs for {component['componentId']}.",
            )
        artifact_digest = _artifact_digest_from_manifest(manifest)
        if not _valid_digest(artifact_digest):
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Installed artifact digest is invalid for {component['componentId']}.",
            )
        receipt = self._read_active_receipt(component["componentId"])
        if receipt is None:
            receipt = self._read_release_receipt(
                component["componentId"], manifest["manifestDigest"]
            )
        if receipt is not None:
            self._validate_active_receipt(
                receipt,
                component_id=component["componentId"],
                manifest=manifest,
                artifact_digest=artifact_digest,
                pointer_identity=pointer_identity,
                bundle_identity=None,
            )
        return {
            "activeVersion": version,
            "manifest": manifest,
            "active": True,
            "identityAttested": receipt is not None,
            "releaseIdentity": manifest["manifestDigest"],
            "manifestDigest": manifest["manifestDigest"],
            "artifactDigest": artifact_digest,
            "pointerIdentity": pointer_identity,
            "bundleIdentity": None,
        }

    @staticmethod
    def _empty_installed(
        *, active_version: str | None = None, active: bool = False
    ) -> dict[str, Any]:
        return {
            "activeVersion": active_version,
            "manifest": None,
            "active": active,
            "identityAttested": False,
            "releaseIdentity": None,
            "manifestDigest": None,
            "artifactDigest": None,
            "pointerIdentity": None,
            "bundleIdentity": None,
        }

    def _authority_available(self) -> bool:
        try:
            info = self.authority_admin_socket.lstat()
        except OSError:
            return False
        mode = stat.S_IMODE(info.st_mode)
        return stat.S_ISSOCK(info.st_mode) and info.st_uid == 0 and not (mode & 0o007)

    def _authority_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self._authority_available():
            raise UpdateError(
                "AUTHORITY_NOT_MANAGED",
                "Authority's restricted local activation socket is unavailable.",
            )
        if not isinstance(request.get("action"), str) or request["action"] not in {
            "status",
            "validateArtifact",
            "activateArtifact",
        }:
            raise UpdateError("AUTHORITY_REQUEST_INVALID", "Authority action is not supported.")
        request_fields = {
            "status": {"action"},
            "validateArtifact": {"action", "planId", "planDigest", "artifactId"},
            "activateArtifact": {
                "action",
                "planId",
                "planDigest",
                "expectedGeneration",
                "artifactId",
            },
        }[request["action"]]
        if set(request) != request_fields:
            raise UpdateError(
                "AUTHORITY_REQUEST_INVALID", "Authority request contains an unexpected field."
            )
        if request["action"] != "status" and (
            not isinstance(request.get("planId"), str)
            or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", request["planId"]) is None
            or not _valid_digest(request.get("planDigest"))
            or not _valid_digest(request.get("artifactId"))
            or (
                request["action"] == "activateArtifact"
                and (
                    not isinstance(request.get("expectedGeneration"), int)
                    or isinstance(request.get("expectedGeneration"), bool)
                    or request["expectedGeneration"] < 0
                )
            )
        ):
            raise UpdateError(
                "AUTHORITY_REQUEST_INVALID", "Authority plan or artifact identity is invalid."
            )
        try:
            payload = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(15)
                connection.connect(str(self.authority_admin_socket))
                connection.sendall(payload)
                response = bytearray()
                while len(response) <= 1024 * 1024:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
                    if b"\n" in chunk:
                        break
            line, separator, rest = response.partition(b"\n")
            if not separator or rest.strip():
                raise UpdateError(
                    "AUTHORITY_PROTOCOL_ERROR", "Authority returned an incomplete JSONL response."
                )
            value = json.loads(line.decode("utf-8"))
        except UpdateError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TimeoutError) as error:
            raise UpdateError(
                "AUTHORITY_UNAVAILABLE", f"Authority admin request failed: {error}", retryable=True
            ) from error
        expected_status = {
            "status": "ok",
            "validateArtifact": "valid",
            "activateArtifact": "activated",
        }[request["action"]]
        if not isinstance(value, dict) or value.get("status") != expected_status:
            code = value.get("code") if isinstance(value, dict) else None
            message = value.get("message") if isinstance(value, dict) else None
            raise UpdateError(
                "AUTHORITY_REQUEST_REJECTED",
                str(message or code or "Authority rejected the restricted activation request."),
                retryable=bool(value.get("retryable")) if isinstance(value, dict) else False,
            )
        return value

    def _installed_component_directory(
        self, component_id: str, *, create: bool = False
    ) -> Path | None:
        if COMPONENT_ID_PATTERN.fullmatch(component_id) is None:
            raise UpdateError(
                "INVALID_COMPONENT", "Component ID is invalid for the installed receipt store."
            )
        root = self._private_state_directory("installed")
        directory = root / component_id
        if not directory.exists() and not directory.is_symlink():
            if not create:
                return None
            try:
                directory.mkdir(mode=0o700)
            except OSError as error:
                raise UpdateError(
                    "UNSAFE_STATE",
                    f"Cannot create installed identity directory for {component_id}: {error}",
                ) from error
        _verify_private_directory(directory)
        return directory

    def _read_active_receipt(self, component_id: str) -> dict[str, Any] | None:
        directory = self._installed_component_directory(component_id)
        if directory is None:
            return None
        active_path = directory / "active.json"
        if not active_path.exists() and not active_path.is_symlink():
            return None
        active = _read_object(active_path, f"{component_id} active release receipt")
        if (
            set(active) != {"schemaVersion", "componentId", "releaseIdentity", "bundleIdentity"}
            or active.get("schemaVersion") != 1
            or active.get("componentId") != component_id
            or not _valid_digest(active.get("releaseIdentity"))
        ):
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Installed active receipt is malformed for {component_id}.",
            )
        release_identity = active["releaseIdentity"]
        receipt = self._read_release_receipt(component_id, release_identity)
        if receipt is None:
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Installed release receipt is missing for {component_id}.",
            )
        if receipt.get("bundleIdentity") != active.get("bundleIdentity"):
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Installed active receipt bundle identity differs for {component_id}.",
            )
        return receipt

    def _read_release_receipt(
        self, component_id: str, release_identity: str
    ) -> dict[str, Any] | None:
        if not _valid_digest(release_identity):
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Installed release identity is invalid for {component_id}.",
            )
        directory = self._installed_component_directory(component_id)
        if directory is None:
            return None
        releases = directory / "releases"
        if not releases.exists() and not releases.is_symlink():
            return None
        _verify_private_directory(releases)
        receipt_path = releases / (release_identity.removeprefix("sha256:") + ".json")
        if not receipt_path.exists() and not receipt_path.is_symlink():
            return None
        receipt = _read_object(receipt_path, f"{component_id} verified release receipt")
        required = {
            "schemaVersion",
            "componentId",
            "releaseIdentity",
            "manifestDigest",
            "artifactDigest",
            "version",
            "bundleIdentity",
            "manifest",
        }
        manifest = receipt.get("manifest")
        if (
            set(receipt) != required
            or receipt.get("schemaVersion") != 1
            or receipt.get("componentId") != component_id
            or receipt.get("releaseIdentity") != release_identity
            or receipt.get("manifestDigest") != release_identity
            or not _valid_digest(receipt.get("artifactDigest"))
            or not isinstance(receipt.get("version"), str)
            or VERSION_PATTERN.fullmatch(receipt["version"]) is None
            or not isinstance(manifest, dict)
        ):
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Installed release receipt is malformed for {component_id}.",
            )
        self._validate_manifest_digest(manifest, release_identity)
        if (
            manifest.get("componentId") != component_id
            or manifest.get("version") != receipt["version"]
            or _artifact_digest_from_manifest(manifest) != receipt["artifactDigest"]
        ):
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Installed manifest does not match its receipt for {component_id}.",
            )
        return receipt

    def _validate_active_receipt(
        self,
        receipt: dict[str, Any],
        *,
        component_id: str,
        manifest: dict[str, Any],
        artifact_digest: str,
        pointer_identity: str,
        bundle_identity: str | None,
    ) -> None:
        if (
            receipt.get("manifest") != manifest
            or receipt.get("artifactDigest") != artifact_digest
            or receipt.get("bundleIdentity") != bundle_identity
            or (
                receipt.get("pointerIdentity") is not None
                and receipt.get("pointerIdentity") != pointer_identity
            )
        ):
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Installed {component_id} receipt does not match its active release pointer.",
            )

    def _write_release_receipt(self, item: dict[str, Any]) -> None:
        component_id = item["componentId"]
        manifest = item["manifest"]
        if (
            item.get("releaseIdentity") != item.get("manifestDigest")
            or not _valid_digest(item.get("releaseIdentity"))
            or item.get("artifactDigest") != _artifact_digest_from_manifest(manifest)
        ):
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Cannot persist an incomplete verified identity for {component_id}.",
            )
        self._validate_manifest_digest(manifest, item["releaseIdentity"])
        receipt_directory = self._installed_component_directory(component_id, create=True)
        assert receipt_directory is not None
        releases = receipt_directory / "releases"
        if not releases.exists():
            releases.mkdir(mode=0o700)
        _verify_private_directory(releases)
        receipt = {
            "schemaVersion": 1,
            "componentId": component_id,
            "releaseIdentity": item["releaseIdentity"],
            "manifestDigest": item["manifestDigest"],
            "artifactDigest": item["artifactDigest"],
            "version": item["version"],
            "bundleIdentity": item["bundleIdentity"],
            "manifest": manifest,
        }
        path = releases / (item["manifestDigest"].removeprefix("sha256:") + ".json")
        if path.exists() or path.is_symlink():
            existing = _read_object(path, f"{component_id} verified release receipt")
            if existing != receipt:
                raise UpdateError(
                    "RELEASE_RECEIPT_COLLISION",
                    f"Verified receipt identity collision for {component_id}.",
                )
            return
        _atomic_json(path, receipt)

    def _write_active_receipt(self, item: dict[str, Any]) -> None:
        directory = self._installed_component_directory(item["componentId"], create=True)
        assert directory is not None
        active_path = directory / "active.json"
        if active_path.is_symlink():
            raise UpdateError(
                "UNSAFE_STATE",
                f"Refusing a symlinked active identity receipt for {item['componentId']}.",
            )
        active = {
            "schemaVersion": 1,
            "componentId": item["componentId"],
            "releaseIdentity": item["releaseIdentity"],
            "bundleIdentity": item["bundleIdentity"],
        }
        _atomic_json(active_path, active)

    def _restore_active_receipt(self, component_id: str, previous: dict[str, Any]) -> None:
        if previous.get("identityAttested") is not True:
            self._clear_active_receipt(component_id)
            return
        if previous.get("manifestDigest") != previous.get("releaseIdentity") or not _valid_digest(
            previous.get("releaseIdentity")
        ):
            raise UpdateError(
                "TRANSACTION_IDENTITY_UNKNOWN",
                f"Previous {component_id} receipt identity is incomplete.",
            )
        directory = self._installed_component_directory(component_id)
        if directory is None:
            raise UpdateError(
                "TRANSACTION_IDENTITY_UNKNOWN", f"Previous {component_id} receipt is missing."
            )
        receipt = self._read_release_receipt(component_id, previous["releaseIdentity"])
        if receipt is None:
            raise UpdateError(
                "TRANSACTION_IDENTITY_UNKNOWN", f"Previous {component_id} receipt is missing."
            )
        if (
            receipt.get("releaseIdentity") != previous["releaseIdentity"]
            or receipt.get("artifactDigest") != previous.get("artifactDigest")
            or receipt.get("bundleIdentity") != previous.get("bundleIdentity")
            or receipt.get("version") != previous.get("version")
        ):
            raise UpdateError(
                "TRANSACTION_IDENTITY_UNKNOWN",
                f"Previous {component_id} receipt does not match the transaction journal.",
            )
        active = {
            "schemaVersion": 1,
            "componentId": component_id,
            "releaseIdentity": previous["releaseIdentity"],
            "bundleIdentity": previous.get("bundleIdentity"),
        }
        _atomic_json(directory / "active.json", active)

    def _clear_active_receipt(self, component_id: str) -> None:
        directory = self._installed_component_directory(component_id)
        if directory is None:
            return
        active_path = directory / "active.json"
        if active_path.is_symlink():
            raise UpdateError(
                "UNSAFE_STATE", f"Refusing a symlinked active identity receipt for {component_id}."
            )
        if active_path.exists():
            active_path.unlink()
            self._fsync_directory(directory)

    def _load_service_bundle(self) -> Any:
        candidates = [
            self.install_root / "scripts" / "service_bundle.py",
            self.install_root / "packaging" / "service_bundle.py",
            Path(__file__).with_name("service_bundle.py"),
        ]
        module_path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if module_path is None:
            raise UpdateError("SERVICE_BUNDLE_MISSING", "The Product bundle verifier is missing.")
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_cyrene_component_service_bundle", module_path
        )
        if spec is None or spec.loader is None:
            raise UpdateError("SERVICE_BUNDLE_MISSING", "Cannot load the Product bundle verifier.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _activity_catalog(self) -> tuple[dict[str, Any], list[str]]:
        catalog = _read_object(self.activity_catalog_path, "runtime activity source catalog")
        if (
            catalog.get("schema_version") != 1
            or not isinstance(catalog.get("generation"), int)
            or isinstance(catalog.get("generation"), bool)
            or catalog["generation"] < 1
        ):
            raise UpdateError(
                "GATE_UNKNOWN",
                "Runtime activity source catalog has an invalid schema or generation.",
                retryable=True,
            )
        sources = catalog.get("sources")
        if not isinstance(sources, list) or not sources:
            raise UpdateError(
                "GATE_UNKNOWN",
                "No installed Product activity sources are trusted; the updater will not assume the runtime is idle.",
                retryable=True,
            )
        ids: list[str] = []
        for source in sources:
            if not isinstance(source, dict) or set(source) != {
                "source_id",
                "uid",
                "gid",
                "source_token_sha256",
            }:
                raise UpdateError(
                    "GATE_UNKNOWN",
                    "Runtime activity source catalog contains an invalid source record.",
                    retryable=True,
                )
            source_id = source["source_id"]
            if (
                not isinstance(source_id, str)
                or re.fullmatch(r"[a-z0-9._-]{1,160}", source_id) is None
            ):
                raise UpdateError(
                    "GATE_UNKNOWN",
                    "Runtime activity source catalog contains an invalid source ID.",
                    retryable=True,
                )
            if (
                not isinstance(source["uid"], int)
                or isinstance(source["uid"], bool)
                or not isinstance(source["gid"], int)
                or isinstance(source["gid"], bool)
                or not isinstance(source["source_token_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", source["source_token_sha256"]) is None
            ):
                raise UpdateError(
                    "GATE_UNKNOWN",
                    f"Runtime activity source {source_id} has invalid trust metadata.",
                    retryable=True,
                )
            ids.append(source_id)
        if len(set(ids)) != len(ids) or ids != sorted(ids):
            raise UpdateError(
                "GATE_UNKNOWN",
                "Runtime activity source IDs must be unique and sorted.",
                retryable=True,
            )
        return catalog, ids

    def _broker_request(
        self, method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        catalog: dict[str, Any] | None = None
        sources: list[str] = []
        if method in {"GetUpdateReadiness", "BeginMaintenance"}:
            catalog, sources = self._activity_catalog()
        broker_path = self.broker_path or self._resolve_installed_broker()
        if not broker_path.is_file() or not os.access(broker_path, os.X_OK):
            raise UpdateError(
                "GATE_UNKNOWN",
                "The native runtime maintenance broker is unavailable; apply is fail-closed.",
                retryable=True,
            )
        request_id = request_id or "cyrene-update-" + uuid.uuid4().hex
        request_params = dict(params)
        if catalog is not None:
            request_params.setdefault("expected_catalog_generation", catalog["generation"])
            request_params.setdefault("expected_activity_sources", sources)
        payload = {
            "request_id": request_id,
            "method": method,
            "auth": {},
            "params": {
                **request_params,
            },
        }
        try:
            command = [str(broker_path), "request", "--socket", str(self.socket_path)]
            if method != "Health":
                command.append("--operator")
            completed = self.runner(
                command,
                input=json.dumps(payload, separators=(",", ":")) + "\n",
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UpdateError(
                "GATE_UNKNOWN", f"Cannot reach runtime maintenance broker: {error}", retryable=True
            ) from error
        if completed.returncode != 0:
            detail = (
                completed.stderr.strip()
                or completed.stdout.strip()
                or f"exit {completed.returncode}"
            )
            raise UpdateError(
                "GATE_UNKNOWN",
                f"Runtime maintenance broker rejected the request: {detail}",
                retryable=True,
            )
        lines = completed.stdout.splitlines()
        if len(lines) != 1:
            raise UpdateError(
                "GATE_UNKNOWN",
                "Runtime maintenance broker returned an invalid JSONL response.",
                retryable=True,
            )
        try:
            response = json.loads(lines[0])
        except json.JSONDecodeError as error:
            raise UpdateError(
                "GATE_UNKNOWN",
                "Runtime maintenance broker returned malformed JSON.",
                retryable=True,
            ) from error
        if not isinstance(response, dict) or response.get("request_id") != request_id:
            raise UpdateError(
                "GATE_UNKNOWN",
                "Runtime maintenance broker response identity did not match.",
                retryable=True,
            )
        if "error" in response:
            detail = response["error"]
            code = (
                detail.get("code", "GATE_UNKNOWN") if isinstance(detail, dict) else "GATE_UNKNOWN"
            )
            message = (
                detail.get("message", "Runtime readiness is unknown.")
                if isinstance(detail, dict)
                else "Runtime readiness is unknown."
            )
            refusal_codes = {
                "ACTIVE_TASKS",
                "IDLE_RUNTIME_REQUIRES_UNLOAD",
                "MAINTENANCE_ACTIVE",
                "UNKNOWN",
                "STALE_READINESS",
                "CATALOG_GENERATION_MISMATCH",
                "GATE_GENERATION_MISMATCH",
                "USER_CONFIRMATION_REQUIRED",
                "REQUEST_ID_PLAN_MISMATCH",
                "INVALID_REQUEST",
                "INVALID_ARGUMENT",
                "UNAUTHORIZED",
            }
            raise UpdateError(
                str(code),
                str(message),
                retryable=True,
                maintenance_not_acquired=method == "BeginMaintenance"
                and str(code) in refusal_codes,
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise UpdateError(
                "GATE_UNKNOWN",
                "Runtime maintenance broker response has no result object.",
                retryable=True,
            )
        return result

    def _resolve_installed_broker(self) -> Path:
        """Resolve the fixed active broker only when its install identities agree."""

        component = self.components.get(BROKER_COMPONENT_ID)
        if (
            not isinstance(component, dict)
            or component.get("componentId") != BROKER_COMPONENT_ID
            or component.get("kind") != "native-binary"
            or self.bootstrap_catalog_digest != TRUSTED_CATALOG_DIGEST
        ):
            raise UpdateError(
                "GATE_UNKNOWN", "The trusted maintenance broker catalog is unavailable."
            )

        # Reuse the updater's release, active-pointer, manifest, and install receipt checks.
        installed = self._installed(component)
        manifest = installed.get("manifest")
        if (
            installed.get("active") is not True
            or not isinstance(manifest, dict)
            or manifest.get("componentId") != BROKER_COMPONENT_ID
            or manifest.get("manifestDigest") != installed.get("manifestDigest")
            or manifest.get("version") != installed.get("activeVersion")
            or _artifact_kind_from_manifest(manifest) != "native-binary"
        ):
            raise UpdateError(
                "GATE_UNKNOWN", "The active maintenance broker identity is unverified."
            )

        artifact = manifest.get("artifact")
        files = artifact.get("files") if isinstance(artifact, dict) else None
        entrypoint = artifact.get("entrypoint") if isinstance(artifact, dict) else None
        if not isinstance(files, dict) or not isinstance(entrypoint, str):
            raise UpdateError(
                "GATE_UNKNOWN", "The active broker manifest has no trusted entrypoint."
            )
        relative = _safe_relative(entrypoint, field="broker.artifact.entrypoint")
        expected_digest = files.get(relative.as_posix())
        if not _valid_digest(expected_digest):
            raise UpdateError(
                "GATE_UNKNOWN", "The active broker entrypoint has no pinned file digest."
            )

        component_root = self.install_root / "components" / BROKER_COMPONENT_ID
        active = component_root / "active"
        try:
            active_info = active.lstat()
            if not stat.S_ISLNK(active_info.st_mode) or active_info.st_uid != 0:
                raise ValueError("active pointer is not a root-owned symlink")
            pointer = os.readlink(active)
            match = re.fullmatch(r"releases/([^/]+)", pointer)
            if match is None or match.group(1) != installed.get("pointerIdentity"):
                raise ValueError("active pointer differs from the verified receipt")
            release = component_root / pointer
            manifest_path = release / "component-manifest.json"
            self._verify_broker_directory_chain(release, entrypoint=relative)
            manifest_info = manifest_path.lstat()
            if (
                manifest_path.is_symlink()
                or not stat.S_ISREG(manifest_info.st_mode)
                or manifest_info.st_uid != 0
                or stat.S_IMODE(manifest_info.st_mode) & 0o022
            ):
                raise ValueError("installed manifest is not root-controlled")
            if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
                raise ValueError("installed manifest differs from the install receipt")
            binary = release.joinpath(*relative.parts)
            binary_info = binary.lstat()
            if (
                not stat.S_ISREG(binary_info.st_mode)
                or binary_info.st_uid != 0
                or binary_info.st_nlink != 1
                or stat.S_IMODE(binary_info.st_mode) & 0o022
                or not stat.S_IMODE(binary_info.st_mode) & 0o111
                or _file_digest(binary) != expected_digest
            ):
                raise ValueError("installed broker entrypoint differs from its manifest digest")
            if installed.get("identityAttested") is not True:
                # A completed first-install journal is only a recovery identity source
                # when the normal active/release receipt is unavailable. Newer releases
                # remain valid through their own verified install receipts.
                self._verify_broker_bootstrap_journal(manifest, installed, pointer)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise UpdateError(
                "GATE_UNKNOWN",
                f"The active maintenance broker failed install verification: {error}",
            ) from error
        return binary

    def _verify_broker_directory_chain(self, release: Path, *, entrypoint: PurePosixPath) -> None:
        """Require real root-owned, non-writable install directories and payload parents."""

        directories = [self.install_root, self.install_root / "components"]
        component_root = self.install_root / "components" / BROKER_COMPONENT_ID
        directories.extend((component_root, component_root / "releases", release))
        current = release
        for part in entrypoint.parts[:-1]:
            current /= part
            directories.append(current)
        if self.install_root == DEFAULT_INSTALL_ROOT:
            directories = [Path("/"), Path("/usr"), Path("/usr/lib"), *directories]
        for directory in directories:
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or directory.is_symlink()
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022
                or not stat.S_IMODE(info.st_mode) & 0o001
            ):
                raise ValueError(f"install directory is not root-controlled: {directory}")

    def _verify_broker_bootstrap_journal(
        self, manifest: dict[str, Any], installed: dict[str, Any], pointer: str
    ) -> None:
        """Bind the active broker receipt to the completed first-install journal."""

        journal_path = self.state_root / BROKER_BOOTSTRAP_JOURNAL
        journal_dir = journal_path.parent
        # The install-state ancestors are public system directories (0755 is valid);
        # only the updater state root and bootstrap journal directory are private.
        for directory in list(self.state_root.parents)[:3]:
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or directory.is_symlink()
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise ValueError("first-install journal ancestor is not root-controlled")
        for directory in (self.state_root, journal_dir):
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or directory.is_symlink()
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise ValueError("first-install journal directory is not private and root-owned")
        info = journal_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("first-install journal is not a private root-owned file")
        journal = _read_object(journal_path, "maintenance broker first-install journal")
        identity = journal.get("identity")
        artifact = manifest["artifact"]
        if (
            journal.get("schemaVersion") != 1
            or journal.get("phase") != "complete"
            or re.fullmatch(r"[0-9a-f]{64}", str(journal.get("planDigest"))) is None
            or journal.get("releaseIdentity") != pointer.removeprefix("releases/")
            or not isinstance(identity, dict)
            or identity.get("componentId") != BROKER_COMPONENT_ID
            or identity.get("version") != manifest.get("version")
            or identity.get("manifestDigest") != manifest.get("manifestDigest")
            or identity.get("artifactDigest") != artifact.get("sha256")
            or installed.get("artifactDigest") != identity.get("artifactDigest")
            or not any(
                target.get("id") == identity.get("targetId")
                and target.get("target") == manifest.get("target")
                for target in self.targets.values()
            )
            or not isinstance(identity.get("indexDigest"), str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", identity["indexDigest"]) is None
        ):
            raise ValueError("first-install journal does not bind the active broker receipt")

    def _readiness(self, component: dict[str, Any]) -> dict[str, Any]:
        target_kind = (
            "CORE_RUNTIME"
            if component.get("restart", {}).get("group") == "core-runtime"
            else "PACKAGE_ONLY"
        )
        return self._readiness_for(target_kind, requires_restart=True)

    def _readiness_for(
        self, target_kind: str, *, requires_restart: bool, force: bool = False
    ) -> dict[str, Any]:
        key = (target_kind, requires_restart)
        if not force and key in self._readiness_cache:
            return self._readiness_cache[key]
        try:
            activity_catalog, activity_sources = self._activity_catalog()
            result = self._broker_request(
                "GetUpdateReadiness",
                {
                    "target_kind": target_kind,
                    "requires_restart": requires_restart,
                    "expected_catalog_generation": activity_catalog["generation"],
                    "expected_activity_sources": activity_sources,
                },
            )
            if result.get("install_catalog_generation") != activity_catalog["generation"]:
                result = {
                    **result,
                    "status": "UNKNOWN",
                    "blocker_codes": [*result.get("blocker_codes", []), "ACTIVITY_CATALOG_CHANGED"],
                    "message": "Installed activity catalog changed during readiness.",
                }
        except UpdateError as error:
            result = {
                "status": "UNKNOWN",
                "gate_generation": None,
                "install_catalog_generation": None,
                "active_task_count": None,
                "active_tasks": [],
                "unknown_activity_sources": [],
                "active_worker_count": None,
                "active_allocation_count": None,
                "blocker_codes": [error.code],
                "requires_restart_confirmation": requires_restart,
                "message": str(error),
            }
        self._readiness_cache[key] = result
        return result

    @staticmethod
    def _public_gate(readiness: dict[str, Any]) -> dict[str, Any]:
        state_map = {
            "READY": "idle",
            "ACTIVE_TASKS": "busy",
            "UNKNOWN": "unknown",
            "IDLE_RUNTIME_REQUIRES_UNLOAD": "idle_runtime_requires_unload",
            "MAINTENANCE_ACTIVE": "maintenance_active",
        }
        blockers = [
            {"code": str(code), "message": str(code).replace("_", " ").lower()}
            for code in readiness.get("blocker_codes", [])
        ]
        if readiness.get("message"):
            blockers.append({"code": "BROKER_UNAVAILABLE", "message": readiness["message"]})
        result = {
            "state": state_map.get(readiness.get("status"), "unknown"),
            "gateGeneration": readiness.get("gate_generation"),
            "installCatalogGeneration": readiness.get("install_catalog_generation"),
            "activeTaskCount": readiness.get("active_task_count"),
            "activeTasks": [
                {
                    "sourceId": item.get("source_id", "unknown"),
                    "taskId": item.get("task_id", "unknown"),
                    "state": item.get("state", "unknown"),
                }
                for item in readiness.get("active_tasks", [])
                if isinstance(item, dict)
            ],
            "activeWorkerCount": readiness.get("active_worker_count"),
            "activeAllocationCount": readiness.get("active_allocation_count"),
            "inflightRuntimeAdmissionCount": readiness.get("inflight_runtime_admission_count"),
            "unknownActivitySources": readiness.get("unknown_activity_sources", []),
            "blockerCodes": readiness.get("blocker_codes", []),
            "requiresRestartConfirmation": bool(
                readiness.get("requires_restart_confirmation", False)
            ),
            "blockers": blockers,
        }
        return {key: value for key, value in result.items() if value is not None}

    @staticmethod
    def _require_ready(readiness: dict[str, Any], target_kind: str) -> None:
        status = readiness.get("status")
        if status == "READY":
            numeric_fields = (
                "gate_generation",
                "install_catalog_generation",
                "active_task_count",
                "active_worker_count",
                "active_allocation_count",
                "inflight_runtime_admission_count",
            )
            if any(
                not isinstance(readiness.get(field), int)
                or isinstance(readiness.get(field), bool)
                or readiness[field] < 0
                for field in numeric_fields
            ):
                raise UpdateError(
                    "GATE_UNKNOWN",
                    "Runtime readiness omitted valid counters or generations; apply is refused.",
                    retryable=True,
                )
            active = readiness.get("active_tasks")
            if not isinstance(active, list):
                raise UpdateError(
                    "GATE_UNKNOWN",
                    "Runtime readiness omitted the active task list; apply is refused.",
                    retryable=True,
                )
            if active or readiness["active_task_count"] != 0:
                raise UpdateError(
                    "ACTIVE_TASKS",
                    "Update refused while Product tasks are active. Finish or stop tasks in their owning Product service; Cyrene will not cancel or drain them.",
                )
            unknown_sources = readiness.get("unknown_activity_sources")
            if not isinstance(unknown_sources, list) or unknown_sources:
                raise UpdateError(
                    "GATE_UNKNOWN",
                    "Runtime activity sources are unknown; apply is refused.",
                    retryable=True,
                )
            if readiness["inflight_runtime_admission_count"] != 0:
                raise UpdateError(
                    "GATE_UNKNOWN",
                    "Runtime task admission is still in flight; wait for it to settle and check again.",
                    retryable=True,
                )
            if target_kind == "CORE_RUNTIME" and (
                readiness["active_worker_count"] != 0 or readiness["active_allocation_count"] != 0
            ):
                raise UpdateError(
                    "IDLE_RUNTIME_REQUIRES_UNLOAD",
                    "Runtime workers or allocations are still held. Unload the model from its Product, release workers/leases/allocations, then check again; the updater will not terminate them.",
                )
            if not isinstance(readiness.get("requires_restart_confirmation"), bool):
                raise UpdateError(
                    "GATE_UNKNOWN",
                    "Runtime readiness omitted the restart confirmation state; apply is refused.",
                    retryable=True,
                )
            return
        if status == "ACTIVE_TASKS":
            active = readiness.get("active_tasks", [])
            task_names = (
                ", ".join(
                    f"{item.get('source_id', 'unknown')}:{item.get('task_id', 'unknown')} ({item.get('state', 'active')})"
                    for item in active
                    if isinstance(item, dict)
                )
                or "active Product tasks"
            )
            raise UpdateError(
                "ACTIVE_TASKS",
                f"Update refused while {task_names} are active. Finish or stop tasks in their owning Product service; Cyrene will not cancel or drain them.",
            )
        if status == "IDLE_RUNTIME_REQUIRES_UNLOAD":
            raise UpdateError(
                "IDLE_RUNTIME_REQUIRES_UNLOAD",
                "Runtime workers or allocations are still held. Unload the model from its Product, release workers/leases/allocations, then check again; the updater will not terminate them.",
            )
        if status == "MAINTENANCE_ACTIVE":
            raise UpdateError(
                "MAINTENANCE_ACTIVE",
                "Another maintenance transaction already owns the update gate.",
                retryable=True,
            )
        raise UpdateError(
            "GATE_UNKNOWN",
            f"Runtime maintenance readiness is unknown ({target_kind}); apply is refused.",
            retryable=True,
        )

    def _channel_releases(
        self, publisher: dict[str, Any], channel: str, component: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        component_prefix = self._component_release_tag_prefix(component, channel)
        key = (
            publisher["repository"],
            channel,
            component["componentId"] if component_prefix else "",
        )
        if key in self._index_cache:
            return self._index_cache[key]
        releases_uri = publisher["releaseDiscovery"]["apiUri"]
        expected_api_uri = (
            f"https://api.github.com/repos/{publisher['repository']}/releases?per_page=100"
        )
        if releases_uri != expected_api_uri:
            raise UpdateError(
                "INVALID_CATALOG",
                f"Release discovery URL is not the fixed API for {publisher['repository']}.",
            )
        channel_cfg = self.catalog["channels"][channel]
        expected_prerelease = channel_cfg["releasePrerelease"]
        selected_release = None
        for page in range(1, 101):
            page_uri = releases_uri if page == 1 else f"{releases_uri}&page={page}"
            releases = self._get_json(page_uri)
            if not isinstance(releases, list):
                raise UpdateError(
                    "RELEASE_DISCOVERY_INVALID",
                    "GitHub Releases API did not return a release list.",
                    retryable=True,
                )
            selected_release = next(
                (
                    item
                    for item in releases
                    if isinstance(item, dict)
                    and item.get("draft") is False
                    and item.get("prerelease") is expected_prerelease
                    and (
                        component_prefix is None
                        or (
                            isinstance(item.get("tag_name"), str)
                            and item["tag_name"].startswith(component_prefix)
                        )
                    )
                ),
                None,
            )
            if selected_release is not None or len(releases) < 100:
                break
        if selected_release is None:
            raise UpdateError(
                "NO_RELEASE",
                f"No {channel} component release is published for {publisher['repository']}.",
                retryable=True,
            )
        assets = selected_release.get("assets")
        asset = (
            next(
                (
                    entry
                    for entry in assets
                    if isinstance(entry, dict)
                    and entry.get("name") == publisher["releaseDiscovery"]["indexAssetName"]
                ),
                None,
            )
            if isinstance(assets, list)
            else None
        )
        if asset is None or not isinstance(asset.get("browser_download_url"), str):
            raise UpdateError(
                "RELEASE_INDEX_MISSING",
                f"The selected {channel} release has no component index asset.",
                retryable=True,
            )
        index_uri = asset["browser_download_url"]
        self._require_github_asset_uri(index_uri, publisher["repository"])
        index_bytes = self._get_bytes(index_uri)
        try:
            index = json.loads(index_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError(
                "INVALID_RELEASE_INDEX", "The component release index is not valid UTF-8 JSON."
            ) from error
        self._validate_index(index, publisher, channel, selected_release, component)
        self._verify_attestation(
            index_bytes,
            subject_name=index["provenance"]["attestation"]["subjectName"],
            digest="sha256:" + hashlib.sha256(index_bytes).hexdigest(),
            repository=publisher["repository"],
            workflow=publisher["workflow"],
            source_ref=index["source"]["ref"],
            source_commit=index["source"]["commit"],
        )
        self._index_cache[key] = (index, index_uri)
        return index, index_uri

    def _validate_index(
        self,
        index: Any,
        publisher: dict[str, Any],
        channel: str,
        release: dict[str, Any],
        component: dict[str, Any],
    ) -> None:
        if (
            not isinstance(index, dict)
            or index.get("schemaVersion") != 1
            or index.get("repository") != publisher["repository"]
            or index.get("channel") != channel
        ):
            raise UpdateError(
                "INVALID_RELEASE_INDEX", "The release index identity or channel is inconsistent."
            )
        if index.get("indexDigest") != _digest_json(index, "indexDigest"):
            raise UpdateError("INDEX_DIGEST_MISMATCH", "The release index digest is invalid.")
        source = index.get("source")
        attestation = index.get("provenance", {}).get("attestation")
        allowed_refs = self.catalog["channels"][channel]["sourceRefs"]
        if (
            not isinstance(source, dict)
            or source.get("repository") != f"https://github.com/{publisher['repository']}"
            or source.get("ref") not in allowed_refs
            or COMMIT_PATTERN.fullmatch(str(source.get("commit", ""))) is None
        ):
            raise UpdateError(
                "UNTRUSTED_SOURCE",
                "The release index source is outside the trusted repository/ref pins.",
            )
        prefix = self._component_release_tag_prefix(component, channel) or (
            "preview-" if channel == "preview" else "stable-"
        )
        if release.get("tag_name") != prefix + source["commit"]:
            raise UpdateError(
                "UNTRUSTED_RELEASE_TAG",
                "The immutable release tag does not match the source commit.",
            )
        run = attestation.get("run") if isinstance(attestation, dict) else None
        if (
            not isinstance(attestation, dict)
            or attestation.get("kind") != "github-artifact-attestation"
            or attestation.get("repository") != publisher["repository"]
            or attestation.get("workflow") != publisher["workflow"]
            or attestation.get("predicateType") != "https://slsa.dev/provenance/v1"
            or attestation.get("subjectName") != publisher["releaseDiscovery"].get("indexAssetName")
            or not isinstance(run, dict)
            or not isinstance(run.get("id"), str)
            or re.fullmatch(r"[0-9]{1,30}", run["id"]) is None
            or not isinstance(run.get("attempt"), int)
            or isinstance(run.get("attempt"), bool)
            or run["attempt"] < 1
            or run.get("url")
            != f"https://github.com/{publisher['repository']}/actions/runs/{run['id']}/attempts/{run['attempt']}"
        ):
            raise UpdateError(
                "UNTRUSTED_WORKFLOW",
                "The index attestation identity differs from the trusted publisher workflow.",
            )
        if not isinstance(index.get("releases"), list):
            raise UpdateError("INVALID_RELEASE_INDEX", "The release index has no release list.")
        if not isinstance(index.get("compatibilityGroups", []), list):
            raise UpdateError(
                "INVALID_RELEASE_INDEX", "The release index compatibilityGroups field is invalid."
            )

    @staticmethod
    def _component_release_tag_prefix(component: dict[str, Any], channel: str) -> str | None:
        discovery = component.get("releaseDiscovery")
        if discovery is None:
            return None
        if not isinstance(discovery, dict) or set(discovery) != {"tagPrefixes"}:
            raise UpdateError(
                "INVALID_CATALOG",
                f"Component-specific release tag prefixes are invalid for {component.get('componentId')}.",
            )
        prefixes = discovery.get("tagPrefixes")
        prefix = prefixes.get(channel) if isinstance(prefixes, dict) else None
        expected = f"{channel}-{component.get('componentId')}-"
        if (
            not isinstance(prefixes, dict)
            or set(prefixes) != {"preview", "stable"}
            or prefix != expected
            or any(
                prefixes.get(item) != f"{item}-{component.get('componentId')}-"
                for item in ("stable", "preview")
            )
        ):
            raise UpdateError(
                "INVALID_CATALOG",
                f"Component-specific release tag prefixes are invalid for {component.get('componentId')}.",
            )
        return prefix

    def _candidate(
        self, component: dict[str, Any], target: dict[str, Any], channel: str
    ) -> Candidate:
        publisher = self.publishers.get(component["publisher"])
        if publisher is None:
            raise UpdateError(
                "INVALID_CATALOG",
                f"No trusted publisher is configured for {component['componentId']}.",
            )
        index, index_uri = self._channel_releases(publisher, channel, component)
        entries = [
            item
            for item in index.get("releases", [])
            if isinstance(item, dict)
            and item.get("componentId") == component["componentId"]
            and item.get("target") == target["target"]
        ]
        if len(entries) != 1:
            raise UpdateError(
                "TARGET_RELEASE_MISSING",
                f"Index has no unique {channel} manifest for {component['componentId']} at {target['id']}.",
                retryable=True,
            )
        entry = entries[0]
        self._require_github_asset_uri(entry.get("manifestUri"), publisher["repository"])
        manifest_bytes = self._get_bytes(entry["manifestUri"])
        try:
            manifest = json.loads(manifest_bytes, object_pairs_hook=_unique_json_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError(
                "INVALID_MANIFEST", f"Manifest for {component['componentId']} is invalid JSON."
            ) from error
        self._validate_manifest(manifest, entry, component, target, publisher, channel, index)
        artifact = manifest["artifact"]
        artifact_digest = artifact.get("digest", artifact.get("sha256"))
        return Candidate(
            component,
            manifest,
            entry["manifestDigest"],
            artifact_digest,
            entry["manifestUri"],
            index,
            index_uri,
            manifest_bytes,
        )

    def _validate_manifest(
        self,
        manifest: Any,
        entry: dict[str, Any],
        component: dict[str, Any],
        target: dict[str, Any],
        publisher: dict[str, Any],
        channel: str,
        index: dict[str, Any],
    ) -> None:
        required = {
            "schemaVersion",
            "releaseId",
            "componentId",
            "version",
            "channel",
            "target",
            "artifact",
            "dependencies",
            "restart",
            "source",
            "provenance",
            "manifestDigest",
        }
        schema_version = manifest.get("schemaVersion") if isinstance(manifest, dict) else None
        if isinstance(schema_version, bool) or schema_version not in (1, 2):
            raise UpdateError(
                "UNSUPPORTED_MANIFEST_VERSION",
                f"Manifest for {component['componentId']} uses an unsupported schema version.",
            )
        allowed = required | {"health", "compatibility"}
        if schema_version == 2:
            required |= {"protocolVersion", "contentDigest"}
            allowed |= {"protocolVersion", "contentDigest", "dataBundle"}
        if (
            not isinstance(manifest, dict)
            or set(manifest) - allowed
            or not required.issubset(manifest)
        ):
            raise UpdateError(
                "INVALID_MANIFEST",
                f"Manifest for {component['componentId']} has an invalid object shape.",
            )
        if (
            manifest.get("schemaVersion") != schema_version
            or manifest.get("componentId") != component["componentId"]
            or manifest.get("version") != entry.get("version")
            or manifest.get("target") != target["target"]
            or manifest.get("channel") != channel
        ):
            raise UpdateError(
                "MANIFEST_IDENTITY_MISMATCH",
                f"Manifest identity differs from its trusted index for {component['componentId']}.",
            )
        if manifest.get("manifestDigest") != _digest_json(manifest, "manifestDigest") or manifest[
            "manifestDigest"
        ] != entry.get("manifestDigest"):
            raise UpdateError(
                "MANIFEST_DIGEST_MISMATCH",
                f"Manifest JCS digest is invalid for {component['componentId']}.",
            )
        if VERSION_PATTERN.fullmatch(str(manifest.get("version", ""))) is None:
            raise UpdateError(
                "INVALID_MANIFEST", "Release version is not a safe immutable path segment."
            )
        if schema_version == 2:
            protocol_version = manifest.get("protocolVersion")
            if (
                not isinstance(protocol_version, str)
                or re.fullmatch(r"[a-z][a-z0-9._-]{0,127}", protocol_version) is None
            ):
                raise UpdateError("INVALID_MANIFEST", "Manifest protocolVersion is invalid.")
            expected_protocol = component.get("protocolVersion")
            group_id = component.get("compatibilityGroup")
            if group_id:
                group = next(
                    (
                        item
                        for item in self.catalog.get("compatibilityGroups", [])
                        if isinstance(item, dict) and item.get("groupId") == group_id
                    ),
                    None,
                )
                member = (
                    next(
                        (
                            item
                            for item in group.get("members", [])
                            if isinstance(item, dict)
                            and item.get("componentId") == component["componentId"]
                        ),
                        None,
                    )
                    if group
                    else None
                )
                member_protocol = (
                    member.get("protocolVersion") if isinstance(member, dict) else None
                )
                if schema_version == 2 and (
                    not isinstance(member_protocol, str)
                    or (expected_protocol is not None and expected_protocol != member_protocol)
                ):
                    raise UpdateError(
                        "INVALID_CATALOG",
                        f"V2 group member {component['componentId']} must have a matching explicit protocol pin.",
                    )
                expected_protocol = member_protocol or expected_protocol
            if not isinstance(expected_protocol, str) or protocol_version != expected_protocol:
                raise UpdateError(
                    "PROTOCOL_VERSION_UNSUPPORTED",
                    f"No trusted client support is pinned for {component['componentId']} protocol {protocol_version!r}.",
                )
            artifact_descriptor = manifest.get("artifact")
            artifact_content_digest = (
                artifact_descriptor.get("digest")
                if isinstance(artifact_descriptor, dict)
                and artifact_descriptor.get("kind") == "oci-image"
                else artifact_descriptor.get("sha256")
                if isinstance(artifact_descriptor, dict)
                else None
            )
            content_digest = manifest.get("contentDigest")
            if not _valid_digest(content_digest) or content_digest != artifact_content_digest:
                raise UpdateError(
                    "CONTENT_DIGEST_MISMATCH",
                    f"Manifest content digest is invalid for {component['componentId']}.",
                )
            compatibility_group = component.get("compatibilityGroup")
            if compatibility_group and not isinstance(manifest.get("compatibility"), dict):
                raise UpdateError(
                    "COMPATIBILITY_MISSING",
                    f"{component['componentId']} v2 release omits its trusted compatibility pins.",
                )
            elif compatibility_group:
                group = next(
                    (
                        item
                        for item in self.catalog.get("compatibilityGroups", [])
                        if item.get("groupId") == compatibility_group
                    ),
                    None,
                )
                compatibility = manifest["compatibility"]
                if (
                    group is None
                    or compatibility.get("groupId") != compatibility_group
                    or compatibility.get("groupVersion") != group.get("groupVersion")
                    or compatibility.get("wireApiVersion") != group.get("wireApiVersion")
                    or compatibility.get("contractApiVersion") != group.get("contractApiVersion")
                ):
                    raise UpdateError(
                        "COMPATIBILITY_GROUP_MISMATCH",
                        f"{component['componentId']} compatibility tuple differs from the trusted group.",
                    )
                if schema_version == 2 and (
                    not _trusted_contract_lock(group.get("contractLock"))
                    or compatibility.get("contractLock") != group.get("contractLock")
                ):
                    raise UpdateError(
                        "COMPATIBILITY_LOCK_UNTRUSTED",
                        f"{component['componentId']} contractLock differs from the trusted catalog pin.",
                    )
            data_bundle = manifest.get("dataBundle")
            artifact_kind = (
                manifest.get("artifact", {}).get("kind")
                if isinstance(manifest.get("artifact"), dict)
                else None
            )
            if artifact_kind == "data-bundle":
                if (
                    not isinstance(data_bundle, dict)
                    or set(data_bundle) != {"proofPath", "proofSha256"}
                    or data_bundle.get("proofPath") != "data-bundle-proof-v1.json"
                    or not _valid_digest(data_bundle.get("proofSha256"))
                ):
                    raise UpdateError(
                        "INVALID_DATA_BUNDLE_PROOF",
                        "V2 data-bundle manifest must pin the fixed proof path and digest.",
                    )
            elif data_bundle is not None:
                raise UpdateError(
                    "INVALID_MANIFEST",
                    "Only data-bundle artifacts may declare dataBundle proof metadata.",
                )
        self._validate_manifest_dependencies(
            manifest.get("dependencies"), component, require_version_range=schema_version == 2
        )
        source = manifest.get("source")
        index_source = index["source"]
        publisher_repo_url = f"https://github.com/{publisher['repository']}"
        allowed_refs = self.catalog["channels"][channel]["sourceRefs"]
        if (
            not isinstance(source, dict)
            or source.get("repository") != publisher_repo_url
            or source.get("ref") not in allowed_refs
            or source.get("ref") != index_source.get("ref")
            or source.get("commit") != index_source.get("commit")
        ):
            raise UpdateError(
                "UNTRUSTED_SOURCE",
                f"Manifest source for {component['componentId']} is not pinned by its attested index.",
            )
        attestation = manifest.get("provenance", {}).get("attestation")
        index_attestation = index["provenance"]["attestation"]
        if (
            not isinstance(attestation, dict)
            or attestation.get("kind") != "github-artifact-attestation"
            or attestation.get("repository") != publisher["repository"]
            or attestation.get("workflow") != publisher["workflow"]
            or attestation.get("predicateType") != "https://slsa.dev/provenance/v1"
            or attestation.get("run") != index_attestation.get("run")
        ):
            raise UpdateError(
                "UNTRUSTED_WORKFLOW",
                f"Manifest provenance for {component['componentId']} differs from its attested release run.",
            )
        artifact = manifest.get("artifact")
        if (
            not isinstance(artifact, dict)
            or artifact.get("kind") != target["artifactKind"]
            or artifact.get("kind") not in component.get("artifactKinds", [])
        ):
            raise UpdateError(
                "UNSUPPORTED_ARTIFACT",
                f"Artifact kind is not supported for {component['componentId']} at this target.",
            )
        if artifact["kind"] == "oci-image":
            raise UpdateError(
                "UNSUPPORTED_TARGET", "Linux native updater does not recreate OCI containers."
            )
        if (
            not _valid_digest(artifact.get("sha256"))
            or not isinstance(artifact.get("sizeBytes"), int)
            or isinstance(artifact.get("sizeBytes"), bool)
            or artifact["sizeBytes"] < 1
            or artifact["sizeBytes"] > MAX_SAFE_INTEGER
        ):
            raise UpdateError(
                "INVALID_MANIFEST",
                f"Artifact digest/size is invalid for {component['componentId']}.",
            )
        if artifact["kind"] == "native-binary":
            if (
                not isinstance(artifact.get("entrypoint"), str)
                or not isinstance(artifact.get("files"), dict)
                or not artifact["files"]
            ):
                raise UpdateError(
                    "INVALID_MANIFEST",
                    f"Native artifact payload map is invalid for {component['componentId']}.",
                )
        elif artifact["kind"] in {"python-bundle", "data-bundle"}:
            if (
                artifact.get("format") not in {"tar.gz", "tar.zst", "zip"}
                or not isinstance(artifact.get("files"), dict)
                or not artifact["files"]
                or (artifact["kind"] == "data-bundle" and schema_version != 2)
            ):
                raise UpdateError(
                    "INVALID_MANIFEST",
                    f"Python bundle payload map is invalid for {component['componentId']}.",
                )
            for name, file_digest in artifact["files"].items():
                _safe_relative(name, field="artifact.files path")
                if not _valid_digest(file_digest):
                    raise UpdateError(
                        "INVALID_MANIFEST",
                        f"Python bundle file digest is invalid for {component['componentId']}.",
                    )
        artifact_uri = artifact.get("uri")
        if not isinstance(artifact_uri, str):
            raise UpdateError(
                "INVALID_MANIFEST",
                f"Artifact download URI is missing for {component['componentId']}.",
            )
        self._require_github_asset_uri(artifact_uri, publisher["repository"])
        subject_name = manifest.get("provenance", {}).get("attestation", {}).get("subjectName")
        if subject_name != PurePosixPath(urllib.parse.urlsplit(artifact_uri).path).name:
            raise UpdateError(
                "UNTRUSTED_ATTESTATION_SUBJECT",
                f"Attestation subject differs from artifact basename for {component['componentId']}.",
            )
        restart = manifest.get("restart")
        expected_restart = component.get("restart", {})
        if (
            not isinstance(restart, dict)
            or restart.get("group") != expected_restart.get("group")
            or restart.get("unit", expected_restart.get("unit")) != expected_restart.get("unit")
        ):
            raise UpdateError(
                "RESTART_POLICY_MISMATCH",
                f"Manifest restart policy differs from the trusted catalog for {component['componentId']}.",
            )

    def _validate_manifest_dependencies(
        self, value: Any, component: dict[str, Any], *, require_version_range: bool = False
    ) -> None:
        """Require publisher dependencies to match the trusted component catalog exactly."""

        if not isinstance(value, list):
            raise UpdateError(
                "INVALID_MANIFEST_DEPENDENCIES",
                f"{component['componentId']} dependencies must be a list.",
            )
        trusted = component.get("dependencies", [])
        if not isinstance(trusted, list):
            raise UpdateError(
                "INVALID_CATALOG",
                f"Trusted dependencies are malformed for {component['componentId']}.",
            )

        def normalize(items: list[Any], *, source: str) -> list[dict[str, str]]:
            normalized: list[dict[str, str]] = []
            seen: set[str] = set()
            for item in items:
                expected_shapes = (
                    ({"componentId", "versionRange"},)
                    if require_version_range
                    else ({"componentId"}, {"componentId", "versionRange"})
                )
                if not isinstance(item, dict) or set(item) not in expected_shapes:
                    raise UpdateError(
                        "INVALID_MANIFEST_DEPENDENCIES",
                        f"{source} dependency record is malformed for {component['componentId']}.",
                    )
                dependency_id = item.get("componentId")
                if (
                    not isinstance(dependency_id, str)
                    or dependency_id not in self.components
                    or dependency_id in seen
                ):
                    raise UpdateError(
                        "INVALID_MANIFEST_DEPENDENCIES",
                        f"{source} references an unknown or duplicate dependency for {component['componentId']}.",
                    )
                seen.add(dependency_id)
                row = {"componentId": dependency_id}
                if "versionRange" in item:
                    if not isinstance(item["versionRange"], str) or not item["versionRange"]:
                        raise UpdateError(
                            "DEPENDENCY_RANGE_UNSUPPORTED",
                            f"{source} dependency range is empty for {component['componentId']}.",
                        )
                    _parse_supported_version_range(item["versionRange"])
                    row["versionRange"] = item["versionRange"]
                normalized.append(row)
            return sorted(normalized, key=lambda row: row["componentId"])

        if normalize(value, source="Manifest") != normalize(trusted, source="Catalog"):
            raise UpdateError(
                "DEPENDENCY_CATALOG_MISMATCH",
                f"Published dependencies differ from trusted catalog for {component['componentId']}.",
            )

    def _validate_runtime_dependencies(self, candidates: dict[str, Candidate]) -> None:
        """Check ranged runtime dependencies against installed or same-plan versions."""

        for component_id, candidate in candidates.items():
            for dependency in candidate.component.get("dependencies", []):
                version_range = (
                    dependency.get("versionRange") if isinstance(dependency, dict) else None
                )
                if version_range is None:
                    continue
                dependency_id = dependency["componentId"]
                dependency_component = self.components.get(dependency_id)
                if dependency_component is None:
                    raise UpdateError(
                        "INVALID_CATALOG",
                        f"Unknown trusted dependency {dependency_id!r} for {component_id}.",
                    )
                _parse_supported_version_range(version_range)
                if dependency_component.get("role") == "build-dependency":
                    continue
                planned = candidates.get(dependency_id)
                if planned is not None:
                    dependency_version = planned.manifest.get("version")
                else:
                    installed = self._installed(dependency_component)
                    dependency_version = installed.get("activeVersion")
                if dependency_version is None:
                    raise UpdateError(
                        "DEPENDENCY_NOT_INSTALLED",
                        f"{component_id} requires {dependency_id} {version_range}; install or include that component in the checked plan first.",
                    )
                if not _version_satisfies(dependency_version, version_range):
                    raise UpdateError(
                        "DEPENDENCY_VERSION_UNSATISFIED",
                        f"{component_id} requires {dependency_id} {version_range}, but the installed/planned version is {dependency_version}.",
                    )

    def _expand_compatibility_groups(
        self, candidates: dict[str, Candidate], channel: str
    ) -> dict[str, Candidate]:
        expanded = dict(candidates)
        group_catalogs = {
            item["groupId"]: item
            for item in self.catalog.get("compatibilityGroups", [])
            if isinstance(item, dict)
        }
        for candidate in list(candidates.values()):
            compatibility = candidate.manifest.get("compatibility")
            if compatibility is None:
                if candidate.component.get("compatibilityGroup"):
                    raise UpdateError(
                        "COMPATIBILITY_MISSING",
                        f"{candidate.component['componentId']} release omits its trusted compatibility pins.",
                    )
                continue
            group_id = compatibility.get("groupId")
            group = group_catalogs.get(group_id)
            if group is None or candidate.component.get("compatibilityGroup") != group_id:
                raise UpdateError(
                    "COMPATIBILITY_UNTRUSTED", f"Unknown compatibility group {group_id!r}."
                )
            if (
                compatibility.get("wireApiVersion") != group.get("wireApiVersion")
                or compatibility.get("contractApiVersion") != group.get("contractApiVersion")
                or (
                    candidate.manifest.get("schemaVersion") == 2
                    and compatibility.get("groupVersion") != group.get("groupVersion")
                )
            ):
                raise UpdateError(
                    "COMPATIBILITY_API_UNSUPPORTED",
                    f"No client support is pinned for {group_id} compatibility API.",
                )
            if candidate.manifest.get("schemaVersion") == 2:
                trusted_lock = group.get("contractLock")
                if (
                    not _trusted_contract_lock(trusted_lock)
                    or compatibility.get("contractLock") != trusted_lock
                ):
                    raise UpdateError(
                        "COMPATIBILITY_LOCK_UNTRUSTED",
                        f"{group_id} does not match its exact trusted protocol lock.",
                    )
            current_members: list[tuple[dict[str, Any], dict[str, Any]]] = []
            adoption_needed = False
            candidate_pin = (
                group.get("contractLock")
                if candidate.manifest.get("schemaVersion") == 2
                else compatibility["contractLock"]
            )
            members = group["members"]
            for member in members:
                member_component = self.components[member["componentId"]]
                member_target = self._target_for(member_component)
                if member_target is None:
                    if member.get("requiredForAdoption"):
                        raise UpdateError(
                            "COMPATIBILITY_MEMBER_UNSUPPORTED",
                            f"Required {group_id} member {member['componentId']} has no supported target.",
                        )
                    continue
                installed = self._installed(member_component)
                if not installed["active"]:
                    if member.get("requiredForAdoption"):
                        adoption_needed = True
                    continue
                installed_compat = (installed.get("manifest") or {}).get("compatibility")
                if (
                    (installed.get("manifest") or {}).get("schemaVersion")
                    != candidate.manifest.get("schemaVersion")
                    or not isinstance(installed_compat, dict)
                    or (
                        installed_compat.get("groupId") != group_id
                        or installed_compat.get("contractApiVersion")
                        != compatibility["contractApiVersion"]
                        or installed_compat.get("wireApiVersion") != compatibility["wireApiVersion"]
                        or (
                            candidate.manifest.get("schemaVersion") == 2
                            and installed_compat.get("groupVersion")
                            != compatibility.get("groupVersion")
                        )
                        or (
                            installed_compat.get("contractLock") != candidate_pin
                            if candidate.manifest.get("schemaVersion") == 2
                            else installed_compat.get("contractLock", {}).get("sha256")
                            != candidate_pin.get("sha256")
                        )
                    )
                ):
                    adoption_needed = True
                current_members.append((member_component, member_target))
            if not adoption_needed:
                continue

            required_ids = {
                member["componentId"]
                for member in members
                if member.get("requiredForAdoption")
                or self.components.get(member["componentId"], {}).get("kind") == "data-bundle"
            }
            required_ids = {
                component_id
                for component_id in required_ids
                if self._target_for(self.components[component_id]) is not None
            }
            include_ids = required_ids | {
                component["componentId"] for component, _ in current_members
            }
            for component_id in sorted(include_ids):
                member_component = self.components[component_id]
                member_target = self._target_for(member_component)
                if member_target is None:
                    raise UpdateError(
                        "COMPATIBILITY_MEMBER_UNSUPPORTED",
                        f"Required {group_id} member {component_id} has no supported target.",
                    )
                if component_id not in expanded:
                    # Compatibility groups may span repositories. Resolve each
                    # member through its own trusted publisher and attested index.
                    expanded[component_id] = self._candidate(
                        member_component, member_target, channel
                    )
                member_candidate = expanded[component_id]
                member_compat = member_candidate.manifest.get("compatibility")
                if (
                    not isinstance(member_compat, dict)
                    or member_compat.get("groupId") != group_id
                    or member_compat.get("contractApiVersion")
                    != compatibility["contractApiVersion"]
                    or member_compat.get("wireApiVersion") != compatibility["wireApiVersion"]
                    or member_compat.get("contractLock") != candidate_pin
                    or member_candidate.manifest.get("schemaVersion")
                    != candidate.manifest.get("schemaVersion")
                    or (
                        member_candidate.manifest.get("schemaVersion") == 2
                        and (
                            member_compat.get("groupVersion") != group.get("groupVersion")
                            or member_candidate.manifest.get("protocolVersion")
                            != member_component.get("protocolVersion")
                        )
                    )
                ):
                    raise UpdateError(
                        "COMPATIBILITY_GROUP_MISMATCH",
                        f"Member {component_id} does not share the exact {group_id} contract pins.",
                    )
        return expanded

    def _resolve_plan_candidates(self, plan: dict[str, Any], channel: str) -> dict[str, Candidate]:
        component_ids = [item["componentId"] for item in plan["components"]]
        candidates: dict[str, Candidate] = {}
        for component_id in component_ids:
            component = self.components[component_id]
            target = self._target_for(component)
            if target is None:
                raise UpdateError(
                    "UNSUPPORTED_TARGET",
                    f"Component {component_id} is no longer supported on this host.",
                )
            candidates[component_id] = self._candidate(component, target, channel)
        candidates = self._expand_compatibility_groups(candidates, channel)
        selected = {key: value for key, value in candidates.items() if key in set(component_ids)}
        rebuilt_components = [
            {
                "componentId": key,
                "version": item.manifest["version"],
                "manifestDigest": item.manifest_digest,
                "artifactDigest": item.manifest["artifact"].get(
                    "digest", item.manifest["artifact"].get("sha256")
                ),
                "restartGroup": item.component["restart"]["group"],
            }
            for key, item in sorted(selected.items())
        ]
        material = {
            "schemaVersion": 1,
            "channel": channel,
            "catalogGeneration": plan.get("catalogGeneration"),
            "catalogDigest": plan.get("catalogDigest"),
            "components": rebuilt_components,
        }
        if "sha256:" + hashlib.sha256(canonical_jcs(material)).hexdigest() != plan["planDigest"]:
            raise UpdateError(
                "PLAN_CHANGED",
                "Trusted release inputs changed since check; run check again.",
                retryable=True,
            )
        return selected

    def _validate_data_bundle_proof(
        self, candidate: Candidate, payload_root: Path, channel: str
    ) -> dict[str, Any]:
        """Verify every immutable source subject and its detached SLSA bundle."""
        manifest = candidate.manifest
        envelope = manifest.get("dataBundle")
        trust = self.catalog.get("dataBundleTrust")
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"proofPath", "proofSha256"}
            or envelope.get("proofPath") != "data-bundle-proof-v1.json"
            or not _valid_digest(envelope.get("proofSha256"))
            or not isinstance(trust, dict)
        ):
            raise UpdateError("INVALID_DATA_BUNDLE_PROOF", "Data-bundle proof envelope is invalid.")
        proof_path = payload_root.joinpath(
            *_safe_relative(envelope["proofPath"], field="dataBundle.proofPath").parts
        )
        if proof_path.is_symlink() or not proof_path.is_file():
            raise UpdateError(
                "INVALID_DATA_BUNDLE_PROOF",
                "The fixed data-bundle proof file is missing or unsafe.",
            )
        proof_bytes = proof_path.read_bytes()
        proof_digest = "sha256:" + hashlib.sha256(proof_bytes).hexdigest()
        if proof_digest != envelope["proofSha256"]:
            raise UpdateError(
                "DATA_BUNDLE_PROOF_DIGEST_MISMATCH", "Data-bundle proof digest is invalid."
            )
        try:
            proof = json.loads(proof_bytes.decode("utf-8"), object_pairs_hook=_unique_json_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError(
                "INVALID_DATA_BUNDLE_PROOF", "Data-bundle proof is not valid UTF-8 JSON."
            ) from error
        expected_proof_fields = {
            "schemaVersion",
            "protocolVersion",
            "contractApiVersion",
            "manifestPath",
            "manifestSha256",
            "policyPath",
            "policySha256",
            "policySchemaVersion",
            "owners",
            "policySource",
        }
        if (
            not isinstance(proof, dict)
            or set(proof) != expected_proof_fields
            or isinstance(proof.get("schemaVersion"), bool)
            or proof.get("schemaVersion") != 1
            or proof.get("protocolVersion") != trust.get("protocolVersion")
            or proof.get("protocolVersion") != manifest.get("protocolVersion")
            or proof.get("contractApiVersion") != trust.get("contractApiVersion")
            or proof.get("manifestPath") != trust.get("manifestPath")
        ):
            raise UpdateError(
                "INVALID_DATA_BUNDLE_PROOF", "Data-bundle proof identity or shape is invalid."
            )

        def read_subject(relative: Any, digest: Any, *, label: str) -> bytes:
            path = payload_root.joinpath(*_safe_relative(relative, field=label).parts)
            if path.is_symlink() or not path.is_file() or not _valid_digest(digest):
                raise UpdateError("INVALID_DATA_BUNDLE_PROOF", f"{label} is missing or unsafe.")
            data = path.read_bytes()
            if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
                raise UpdateError(
                    "DATA_BUNDLE_SOURCE_DIGEST_MISMATCH", f"{label} digest is invalid."
                )
            return data

        manifest_bytes = read_subject(
            proof.get("manifestPath"), proof.get("manifestSha256"), label="data bundle manifest"
        )
        try:
            manifest_value = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError(
                "INVALID_DATA_BUNDLE_PROOF", "The inner bundle manifest is invalid JSON."
            ) from error
        if not isinstance(manifest_value, dict):
            raise UpdateError(
                "INVALID_DATA_BUNDLE_PROOF", "The inner bundle manifest must be an object."
            )
        if manifest_value.get("formatVersion") != 2 or manifest_value.get(
            "wireApiVersion"
        ) != proof.get("protocolVersion"):
            raise UpdateError(
                "DATA_BUNDLE_MANIFEST_MISMATCH",
                "Inner contract-bundle manifest does not bind the declared Product wire protocol.",
            )

        policy_trust = trust.get("policySource")
        policy_source = proof.get("policySource")
        if (
            not isinstance(policy_trust, dict)
            or not isinstance(policy_source, dict)
            or set(policy_source) != {"source", "path", "sha256", "provenance"}
            or policy_source.get("path") != policy_trust.get("path")
            or policy_source.get("sha256") != proof.get("policySha256")
            or proof.get("policyPath") != "workspace-product-policy-v2.json"
            or proof.get("policySchemaVersion") != policy_trust.get("schemaVersion")
        ):
            raise UpdateError(
                "UNTRUSTED_POLICY_SOURCE",
                "Data-bundle policy source differs from the trusted Platform policy.",
            )
        policy_source_identity = policy_source.get("source")
        if (
            not isinstance(policy_source_identity, dict)
            or set(policy_source_identity) != {"repository", "ref", "commit"}
            or policy_source_identity.get("repository") != policy_trust.get("repository")
        ):
            raise UpdateError(
                "UNTRUSTED_POLICY_SOURCE", "Data-bundle policy source identity is not trusted."
            )
        policy_payload = read_subject(
            proof.get("policyPath"), proof.get("policySha256"), label="policy file"
        )
        try:
            policy_value = json.loads(policy_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError(
                "INVALID_POLICY", "Bundled workspace policy is not valid UTF-8 JSON."
            ) from error
        if not isinstance(policy_value, dict) or policy_value.get("schemaVersion") != proof.get(
            "policySchemaVersion"
        ):
            raise UpdateError(
                "INVALID_POLICY",
                "Bundled policy schema version differs from its trusted proof pin.",
            )
        owners = proof.get("owners")
        owner_trust = trust.get("owners")
        if not isinstance(owners, list) or not isinstance(owner_trust, list):
            raise UpdateError("INVALID_DATA_BUNDLE_PROOF", "Data-bundle owner list is invalid.")
        trusted_owners = {
            item.get("ownerId"): item for item in owner_trust if isinstance(item, dict)
        }
        owner_ids = [item.get("ownerId") for item in owners if isinstance(item, dict)]
        if (
            len(owner_ids) != len(owners)
            or len(set(owner_ids)) != len(owner_ids)
            or set(owner_ids) != set(trusted_owners)
        ):
            raise UpdateError(
                "DATA_BUNDLE_OWNERS_MISMATCH",
                "Proof does not contain each trusted owner exactly once.",
            )
        for owner in owners:
            expected_owner = trusted_owners[owner["ownerId"]]
            if (
                set(owner) != {"ownerId", "source", "catalogPath", "catalogSha256", "provenance"}
                or owner.get("catalogPath")
                != f"{expected_owner.get('repository')}/{expected_owner.get('catalogPath')}"
            ):
                raise UpdateError(
                    "DATA_BUNDLE_OWNER_MISMATCH",
                    f"Owner {owner['ownerId']} does not match trusted catalog identity.",
                )
            source = owner.get("source")
            if (
                not isinstance(source, dict)
                or set(source) != {"repository", "ref", "commit"}
                or source.get("repository") != expected_owner.get("repository")
            ):
                raise UpdateError(
                    "DATA_BUNDLE_OWNER_MISMATCH",
                    f"Owner {owner['ownerId']} source identity is not trusted.",
                )
            subject = read_subject(
                owner.get("catalogPath"),
                owner.get("catalogSha256"),
                label=f"owner {owner['ownerId']} catalog",
            )
            self._verify_data_bundle_subject(
                payload_root,
                subject,
                owner,
                label=f"owner {owner['ownerId']} catalog",
                channel=channel,
                expected_subject_path=(
                    expected_owner["repository"] + "/" + expected_owner["catalogPath"]
                ),
                expected_workflow=(
                    expected_owner["repository"] + PRODUCT_CONTRACT_ATTESTATION_WORKFLOW
                ),
            )
        self._validate_inner_bundle_files(
            manifest_value, proof, payload_root, policy_source, trusted_owners
        )
        self._verify_data_bundle_subject(
            payload_root,
            policy_payload,
            policy_source,
            label="policy source",
            channel=channel,
            expected_subject_path=(policy_trust["repository"] + "/" + policy_trust["path"]),
            expected_workflow=(policy_trust["repository"] + PRODUCT_POLICY_ATTESTATION_WORKFLOW),
        )
        return proof

    def _validate_inner_bundle_files(
        self,
        manifest: dict[str, Any],
        proof: dict[str, Any],
        payload_root: Path,
        policy_source: dict[str, Any],
        trusted_owners: dict[str, dict[str, Any]],
    ) -> None:
        """Bind Platform's short local bundle paths to trusted canonical sources.

        The Platform bundle format uses each checkout directory name as its
        inner repository/path prefix. Outer provenance remains fully qualified
        and is validated before this projection is checked.
        中文：仅将可信完整仓库名映射到包内短目录名，不改变外层来源证明。
        """

        proof_owners = {owner["ownerId"]: owner for owner in proof["owners"]}
        manifest_owners = manifest.get("owners")
        files = manifest.get("files")
        if not isinstance(manifest_owners, list) or not isinstance(files, list):
            raise UpdateError(
                "INVALID_DATA_BUNDLE_MANIFEST",
                "Inner bundle manifest owner/file lists are missing.",
            )
        seen_owners: set[str] = set()
        expected_files: dict[str, str] = {}
        for owner in manifest_owners:
            fields = {"ownerId", "repository", "sourceSha", "catalogPath", "catalogSha256"}
            if not isinstance(owner, dict) or set(owner) != fields:
                raise UpdateError(
                    "INVALID_DATA_BUNDLE_MANIFEST", "Inner bundle owner entry has an invalid shape."
                )
            owner_id = owner["ownerId"]
            if not isinstance(owner_id, str):
                raise UpdateError(
                    "INVALID_DATA_BUNDLE_MANIFEST", "Inner bundle owner identifier is invalid."
                )
            proof_owner = proof_owners.get(owner_id)
            trusted = trusted_owners.get(owner_id)
            trusted_repository = trusted.get("repository") if trusted else None
            trusted_catalog_path = trusted.get("catalogPath") if trusted else None
            repository_parts = (
                trusted_repository.split("/") if isinstance(trusted_repository, str) else []
            )
            if (
                len(repository_parts) != 2
                or repository_parts[0] != "DoHorizon-AI"
                or not repository_parts[1].startswith("Cyrene-")
                or trusted_catalog_path != "contracts/product/v2/catalog.json"
            ):
                raise UpdateError(
                    "DATA_BUNDLE_OWNER_MISMATCH",
                    f"Owner {owner_id!r} trusted source identity is invalid.",
                )
            local_repository = repository_parts[1]
            catalog_path = f"{local_repository}/{trusted_catalog_path}"
            if (
                owner_id in seen_owners
                or proof_owner is None
                or trusted is None
                or owner.get("repository") != local_repository
                or owner.get("sourceSha") != proof_owner["source"]["commit"]
                or owner.get("catalogPath") != catalog_path
                or owner.get("catalogSha256")
                != proof_owner["catalogSha256"].removeprefix("sha256:")
            ):
                raise UpdateError(
                    "DATA_BUNDLE_MANIFEST_MISMATCH",
                    f"Inner bundle owner {owner_id!r} differs from its verified proof.",
                )
            seen_owners.add(owner_id)
            expected_files[catalog_path] = owner["catalogSha256"].removeprefix("sha256:")
        if seen_owners != set(proof_owners):
            raise UpdateError(
                "DATA_BUNDLE_MANIFEST_MISMATCH", "Inner bundle manifest omits a proved owner."
            )
        expected_files["workspace-product-policy-v2.json"] = proof["policySha256"].removeprefix(
            "sha256:"
        )
        seen_files: dict[str, str] = {}
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                raise UpdateError(
                    "INVALID_DATA_BUNDLE_MANIFEST", "Inner bundle file entry has an invalid shape."
                )
            path, digest = item.get("path"), item.get("sha256")
            if (
                not isinstance(path, str)
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or path in seen_files
            ):
                raise UpdateError(
                    "INVALID_DATA_BUNDLE_MANIFEST", "Inner bundle file path or digest is invalid."
                )
            file_path = payload_root.joinpath(
                *_safe_relative(path, field="inner bundle file path").parts
            )
            if (
                file_path.is_symlink()
                or not file_path.is_file()
                or _file_digest(file_path) != "sha256:" + digest
            ):
                raise UpdateError(
                    "DATA_BUNDLE_FILE_MISMATCH", f"Inner bundle file {path!r} digest is invalid."
                )
            seen_files[path] = digest
        if any(seen_files.get(path) != digest for path, digest in expected_files.items()):
            raise UpdateError(
                "DATA_BUNDLE_MANIFEST_MISMATCH", "Inner manifest omits proved owner/policy files."
            )

    def _verify_data_bundle_subject(
        self,
        payload_root: Path,
        payload: bytes,
        record: dict[str, Any],
        *,
        label: str,
        channel: str,
        expected_subject_path: str,
        expected_workflow: str,
    ) -> None:
        source = record.get("source")
        provenance = record.get("provenance")
        attested = provenance.get("attestation") if isinstance(provenance, dict) else None
        publisher = (
            self.publishers.get(source.get("repository")) if isinstance(source, dict) else None
        )
        record_subject_path = record.get("catalogPath", record.get("path"))
        if record.get("catalogPath") is not None:
            subject_path = record_subject_path
        elif isinstance(source, dict):
            subject_path = f"{source.get('repository')}/{record_subject_path}"
        else:
            subject_path = None
        if (
            not isinstance(source, dict)
            or not isinstance(provenance, dict)
            or set(provenance)
            != {"attestation", "bundlePath", "bundleSha256", "subjectPath", "subjectDigest"}
            or publisher is None
            or source.get("ref")
            not in self.catalog.get("channels", {}).get(channel, {}).get("sourceRefs", [])
            or re.fullmatch(r"[0-9a-f]{40}", str(source.get("commit", ""))) is None
            or not isinstance(attested, dict)
            or not isinstance(subject_path, str)
            or subject_path != expected_subject_path
        ):
            raise UpdateError(
                "UNTRUSTED_DATA_BUNDLE_SOURCE", f"{label} source proof is incomplete or untrusted."
            )
        attestation = attested.get("attestation")
        _safe_relative(subject_path, field=f"{label} subject path")
        attestation_run = attestation.get("run") if isinstance(attestation, dict) else None
        if (
            not isinstance(attestation, dict)
            or set(attestation)
            - {"kind", "uri", "subjectName", "repository", "workflow", "predicateType", "run"}
            or attestation.get("kind") != "github-artifact-attestation"
            or attestation.get("repository") != source["repository"]
            or attestation.get("workflow") != expected_workflow
            or attestation.get("predicateType") != "https://slsa.dev/provenance/v1"
            or not {
                "kind",
                "subjectName",
                "repository",
                "workflow",
                "predicateType",
                "run",
            }.issubset(attestation)
            or attestation.get("subjectName") != PurePosixPath(subject_path).name
            or not isinstance(attestation_run, dict)
            or set(attestation_run) != {"id", "attempt", "url"}
            or not isinstance(attestation_run.get("id"), str)
            or re.fullmatch(r"[0-9]{1,30}", attestation_run["id"]) is None
            or not isinstance(attestation_run.get("attempt"), int)
            or isinstance(attestation_run.get("attempt"), bool)
            or attestation_run["attempt"] < 1
            or attestation_run.get("url")
            != f"https://github.com/{source['repository']}/actions/runs/{attestation.get('run', {}).get('id')}/attempts/{attestation.get('run', {}).get('attempt')}"
        ):
            raise UpdateError(
                "UNTRUSTED_DATA_BUNDLE_ATTESTATION", f"{label} attestation identity is not trusted."
            )
        bundle_path = payload_root.joinpath(
            *_safe_relative(
                provenance["bundlePath"], field=f"{label} attestation bundle path"
            ).parts
        )
        if (
            bundle_path.is_symlink()
            or not bundle_path.is_file()
            or not _valid_digest(provenance.get("bundleSha256"))
        ):
            raise UpdateError(
                "INVALID_DATA_BUNDLE_ATTESTATION",
                f"{label} attestation bundle is missing or unsafe.",
            )
        bundle_bytes = bundle_path.read_bytes()
        if "sha256:" + hashlib.sha256(bundle_bytes).hexdigest() != provenance["bundleSha256"]:
            raise UpdateError(
                "INVALID_DATA_BUNDLE_ATTESTATION", f"{label} attestation bundle digest is invalid."
            )
        declared_subject_path = provenance.get("subjectPath")
        if declared_subject_path != subject_path or provenance.get("subjectDigest") != record.get(
            "catalogSha256", record.get("sha256")
        ):
            raise UpdateError(
                "DATA_BUNDLE_SUBJECT_MISMATCH",
                f"{label} attestation does not bind the declared source file.",
            )
        self._verify_attestation(
            payload,
            subject_name=PurePosixPath(subject_path).name,
            digest=provenance["subjectDigest"],
            repository=source["repository"],
            workflow=expected_workflow,
            source_ref=source["ref"],
            source_commit=source["commit"],
            bundle_path=bundle_path,
        )

    def _stage_candidate(
        self,
        candidate: Candidate,
        plan_root: Path,
        plan_id: str | None = None,
        plan_digest: str | None = None,
    ) -> dict[str, Any]:
        manifest = candidate.manifest
        artifact = manifest["artifact"]
        filename = PurePosixPath(urllib.parse.urlsplit(artifact["uri"]).path).name
        if not filename or filename in {".", ".."}:
            raise UpdateError("INVALID_MANIFEST", "Artifact URI has no safe basename.")
        component_root = plan_root / candidate.component["componentId"]
        component_root.mkdir(mode=0o700)
        archive_path = component_root / filename
        payload = self._get_bytes(artifact["uri"])
        if (
            len(payload) != artifact["sizeBytes"]
            or "sha256:" + hashlib.sha256(payload).hexdigest() != artifact["sha256"]
        ):
            raise UpdateError(
                "ARTIFACT_DIGEST_MISMATCH",
                f"Downloaded artifact digest/size mismatch for {candidate.component['componentId']}.",
            )
        self._write_private_file(archive_path, payload)
        self._verify_attestation(
            payload,
            subject_name=candidate.manifest["provenance"]["attestation"]["subjectName"],
            digest=artifact["sha256"],
            repository=candidate.component["publisher"],
            workflow=self.publishers[candidate.component["publisher"]]["workflow"],
            source_ref=manifest["source"]["ref"],
            source_commit=manifest["source"]["commit"],
        )
        payload_root = component_root / "payload"
        if artifact["kind"] == "native-binary":
            self._extract_native(archive_path, payload_root, artifact)
            installed_path = self._install_native_release(candidate, payload_root)
        elif artifact["kind"] == "python-bundle":
            self._extract_tar(archive_path, payload_root, expected_files=artifact.get("files"))
            service = candidate.component.get("pythonBundleService")
            if not service:
                raise UpdateError(
                    "INVALID_CATALOG",
                    f"{candidate.component['componentId']} has no Product bundle service mapping.",
                )
            module = self._load_service_bundle()
            bundle_root = payload_root
            if (payload_root / service).is_dir():
                bundle_root = payload_root / service
            if (bundle_root / "manifest.json").is_file() is False:
                candidates = [
                    path for path in payload_root.rglob("manifest.json") if path.parent.is_dir()
                ]
                if len(candidates) == 1:
                    bundle_root = candidates[0].parent
            target_profile_id = next(
                (
                    item.get("targetId")
                    for item in candidate.component.get("targets", [])
                    if isinstance(item, dict)
                    and item.get("artifactKind") == "python-bundle"
                    and item.get("support") == "supported"
                    and self.targets.get(item.get("targetId"), {}).get("target")
                    == candidate.manifest.get("target")
                ),
                None,
            )
            if not isinstance(target_profile_id, str):
                raise UpdateError(
                    "UNSUPPORTED_TARGET",
                    f"No trusted Python target profile matches the release for {candidate.component['componentId']}.",
                )
            inner = module.validate_bundle(
                bundle_root,
                expected_service=service,
                expected_target_profile=target_profile_id,
                release_lock_path=self.release_lock_path,
            )
            if inner.get("schema_version") != 2:
                raise UpdateError(
                    "LEGACY_BUNDLE_NOT_STAGEABLE",
                    f"The {service} payload uses inner bundle schema v1. Install this helper to inspect or roll back existing releases, then build a new SDK-backed v2 bundle before staging an update.",
                )
            embedded_runtime_dependencies = inner.get("dependencies", {}).get("runtime", [])
            for dependency in candidate.component.get("dependencies", []):
                if not isinstance(dependency, dict) or "versionRange" not in dependency:
                    continue
                dependency_component = self.components.get(dependency["componentId"], {})
                if dependency_component.get("role") != "build-dependency":
                    continue
                embedded = next(
                    (
                        item
                        for item in embedded_runtime_dependencies
                        if item.get("component_id") == dependency["componentId"]
                    ),
                    None,
                )
                if embedded is None or not _version_satisfies(
                    embedded.get("version"), dependency["versionRange"]
                ):
                    raise UpdateError(
                        "BUILD_DEPENDENCY_UNSATISFIED",
                        f"Bundle for {candidate.component['componentId']} does not embed required build dependency {dependency['componentId']} {dependency['versionRange']}.",
                    )
            installed_path = module.stage_release(
                bundle_root,
                install_root=self.install_root,
                release_lock_path=self.release_lock_path,
            )
            bundle_identity = inner.get("artifact_digest")
            if (
                not isinstance(bundle_identity, str)
                or BUNDLE_ID_PATTERN.fullmatch(bundle_identity) is None
                or installed_path.name != inner.get("version")
            ):
                raise UpdateError(
                    "INVALID_BUNDLE", f"Installed {service} bundle pointer identity is invalid."
                )
        elif artifact["kind"] == "data-bundle":
            payload_root = self._extract_data_bundle_archive(archive_path, payload_root, artifact)
            proof = self._validate_data_bundle_proof(candidate, payload_root, manifest["channel"])
            installed_path = self._install_data_bundle(
                candidate,
                archive_path,
                payload_root,
                plan_id=plan_id,
                plan_digest=plan_digest,
            )
            bundle_identity = artifact["sha256"]
        else:
            raise UpdateError(
                "UNSUPPORTED_ARTIFACT",
                "Linux staging supports only native binary and Python bundle releases.",
            )
        if artifact["kind"] == "native-binary":
            bundle_identity = None
        item = {
            "componentId": candidate.component["componentId"],
            "version": manifest["version"],
            "manifestDigest": candidate.manifest_digest,
            "releaseIdentity": candidate.manifest_digest,
            "artifactDigest": artifact["sha256"],
            "restartGroup": candidate.component["restart"]["group"],
            "pointerIdentity": installed_path.name,
            "bundleIdentity": bundle_identity,
            "manifest": manifest,
            "releasePath": str(installed_path),
            "archivePath": str(archive_path),
        }
        if artifact["kind"] == "data-bundle":
            item["dataBundle"] = {
                "artifactId": artifact["sha256"],
                "proofSha256": manifest["dataBundle"]["proofSha256"],
                "proofSchemaVersion": proof["schemaVersion"],
            }
        self._write_release_receipt(item)
        return item

    def _extract_data_bundle_archive(
        self, archive: Path, destination: Path, artifact: dict[str, Any]
    ) -> Path:
        """Expand a bounded zstd tar without following links or trusting archive paths."""
        if shutil.which("zstd") is None:
            raise UpdateError(
                "DATA_BUNDLE_TOOL_MISSING", "The zstd utility is required to inspect a data bundle."
            )
        compressed = artifact.get("format")
        if compressed not in {"tar.zst", "tar.zstd"}:
            raise UpdateError("INVALID_ARTIFACT", "Data bundle must use the pinned tar.zst format.")
        if (
            artifact.get("sizeBytes") != archive.stat().st_size
            or archive.stat().st_size > 64 * 1024 * 1024
        ):
            raise UpdateError(
                "UNSAFE_ARTIFACT", "Data-bundle archive exceeds its compressed-size limit."
            )
        tar_path = archive.with_suffix(".tar")

        def limit_tar_output() -> None:
            import resource

            output_limit = 320 * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (output_limit, output_limit))

        try:
            with tar_path.open("xb") as output:
                completed = self.runner(
                    ["zstd", "--decompress", "--stdout", str(archive)],
                    stdout=output,
                    stderr=subprocess.PIPE,
                    timeout=180,
                    check=False,
                    preexec_fn=limit_tar_output,
                )
                output.flush()
                os.fsync(output.fileno())
            if completed.returncode != 0 or tar_path.stat().st_size > 320 * 1024 * 1024:
                raise UpdateError(
                    "INVALID_ARTIFACT",
                    "Cannot decompress data-bundle archive within its expanded-size limit.",
                )
            self._extract_tar(
                tar_path,
                destination,
                expected_files=artifact.get("files"),
                max_entries=1024,
                max_member_bytes=16 * 1024 * 1024,
                max_expanded_bytes=256 * 1024 * 1024,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UpdateError(
                "INVALID_ARTIFACT", f"Cannot safely decompress data bundle: {error}"
            ) from error
        finally:
            tar_path.unlink(missing_ok=True)
        return destination

    def _install_data_bundle(
        self,
        candidate: Candidate,
        archive_path: Path,
        payload_root: Path,
        *,
        plan_id: str | None = None,
        plan_digest: str | None = None,
    ) -> Path:
        """Install immutable bytes and their extracted view under Authority's fixed root."""
        artifact_id = candidate.manifest["artifact"]["sha256"]
        raw_hex = artifact_id.removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", raw_hex):
            raise UpdateError("INVALID_DATA_BUNDLE", "Data-bundle artifact identity is invalid.")
        archive_root = self.data_bundle_root / "archives"
        versions_root = self.data_bundle_root / "versions"
        metadata_root = self.data_bundle_root / "metadata"
        try:
            import grp

            authority_group_id = grp.getgrnam("cyrene-authority").gr_gid
            bundle_reader_group_id = grp.getgrnam("cyrene-product-bundle-reader").gr_gid
        except (ImportError, KeyError) as error:
            raise UpdateError(
                "AUTHORITY_GROUP_MISSING",
                "The cyrene-authority and cyrene-product-bundle-reader groups are required for data bundles.",
            ) from error
        group_by_directory = {
            archive_root: authority_group_id,
            versions_root: bundle_reader_group_id,
            metadata_root: authority_group_id,
        }
        try:
            root_info = self.data_bundle_root.lstat()
        except OSError as error:
            raise UpdateError(
                "UNSAFE_DATA_BUNDLE_ROOT",
                "Authority data-bundle root must be provisioned with restricted traversal ACLs.",
            ) from error
        if (
            self.data_bundle_root.is_symlink()
            or not stat.S_ISDIR(root_info.st_mode)
            or root_info.st_uid != 0
            or stat.S_IMODE(root_info.st_mode) & 0o022
        ):
            raise UpdateError("UNSAFE_DATA_BUNDLE_ROOT", "Authority data-bundle root is unsafe.")
        for directory, group_id in group_by_directory.items():
            directory.mkdir(mode=0o750, exist_ok=True)
            info = directory.lstat()
            if (
                directory.is_symlink()
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise UpdateError(
                    "UNSAFE_DATA_BUNDLE_ROOT", "Authority data-bundle root is unsafe."
                )
            os.chown(directory, 0, group_id)
            directory.chmod(0o750)
        archive_target = archive_root / f"sha256-{raw_hex}.tar.zst"
        version_target = versions_root / raw_hex
        metadata_directory = metadata_root / raw_hex
        metadata_directory.mkdir(mode=0o750, exist_ok=True)
        metadata_info = metadata_directory.lstat()
        if (
            metadata_directory.is_symlink()
            or not stat.S_ISDIR(metadata_info.st_mode)
            or metadata_info.st_uid != 0
            or stat.S_IMODE(metadata_info.st_mode) & 0o022
        ):
            raise UpdateError("UNSAFE_DATA_BUNDLE_ROOT", "Authority metadata path is unsafe.")
        os.chown(metadata_directory, 0, authority_group_id)
        metadata_directory.chmod(0o750)
        if (
            candidate.manifest_bytes is None
            or _digest_json(candidate.manifest, "manifestDigest") != candidate.manifest_digest
            or candidate.manifest.get("manifestDigest") != candidate.manifest_digest
        ):
            raise UpdateError(
                "INVALID_MANIFEST", "Data-bundle import requires its verified outer manifest bytes."
            )
        try:
            imported_manifest = json.loads(
                candidate.manifest_bytes, object_pairs_hook=_unique_json_object
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError(
                "INVALID_MANIFEST", "Verified data-bundle manifest bytes are invalid."
            ) from error
        if imported_manifest != candidate.manifest:
            raise UpdateError(
                "INVALID_MANIFEST", "Outer manifest bytes differ from the validated manifest."
            )
        metadata_path = metadata_directory / "component-manifest-v2.json"
        import_record_path = metadata_directory / "import-record-v1.json"
        import_record = {
            "schemaVersion": 1,
            "artifactId": artifact_id,
            "componentId": candidate.component["componentId"],
            "channel": candidate.manifest.get("channel"),
            "manifestDigest": candidate.manifest_digest,
            "manifestUri": candidate.manifest_uri,
            "indexUri": candidate.index_uri,
            "indexDigest": candidate.index.get("indexDigest"),
            "indexSource": candidate.index.get("source"),
            "publisherRepository": candidate.component["publisher"],
        }
        if metadata_path.exists() or metadata_path.is_symlink():
            self._validate_authority_metadata_file(metadata_path, authority_group_id)
            if metadata_path.read_bytes() != candidate.manifest_bytes:
                raise UpdateError(
                    "DATA_BUNDLE_COLLISION", "Existing outer manifest metadata differs."
                )
        else:
            self._write_authority_metadata(metadata_path, candidate.manifest_bytes)
        if import_record_path.exists() or import_record_path.is_symlink():
            self._validate_authority_metadata_file(import_record_path, authority_group_id)
            existing_record = _read_object(import_record_path, "existing data-bundle import record")
            if any(existing_record.get(key) != value for key, value in import_record.items()):
                raise UpdateError(
                    "DATA_BUNDLE_COLLISION", "Existing data-bundle import record differs."
                )
        else:
            self._write_authority_metadata(
                import_record_path,
                json.dumps(import_record, ensure_ascii=False, sort_keys=True, indent=2).encode()
                + b"\n",
            )
        if (
            not isinstance(plan_id, str)
            or PLAN_ID_PATTERN.fullmatch(plan_id) is None
            or not _valid_digest(plan_digest)
        ):
            raise UpdateError(
                "INVALID_PLAN", "Data-bundle import requires a digest-bound checked plan."
            )
        plan_refs = metadata_directory / "plans"
        plan_refs.mkdir(mode=0o750, exist_ok=True)
        plan_refs_info = plan_refs.lstat()
        if plan_refs.is_symlink() or not stat.S_ISDIR(plan_refs_info.st_mode):
            raise UpdateError("UNSAFE_DATA_BUNDLE_ROOT", "Authority plan metadata path is unsafe.")
        os.chown(plan_refs, 0, authority_group_id)
        plan_refs.chmod(0o750)
        plan_reference_path = plan_refs / f"{plan_id}.json"
        plan_reference = {
            "schemaVersion": 1,
            "planId": plan_id,
            "planDigest": plan_digest,
            "artifactId": artifact_id,
            "manifestDigest": candidate.manifest_digest,
            "proofSha256": candidate.manifest["dataBundle"]["proofSha256"],
        }
        plan_reference_bytes = (
            json.dumps(plan_reference, ensure_ascii=False, sort_keys=True, indent=2).encode()
            + b"\n"
        )
        if plan_reference_path.exists() or plan_reference_path.is_symlink():
            self._validate_authority_metadata_file(plan_reference_path, authority_group_id)
            existing_reference = _read_object(
                plan_reference_path, "existing data-bundle plan reference"
            )
            if existing_reference != plan_reference:
                raise UpdateError("DATA_BUNDLE_COLLISION", "Existing plan reference differs.")
        else:
            self._write_authority_metadata(plan_reference_path, plan_reference_bytes)
        if archive_target.exists() or archive_target.is_symlink():
            archive_info = archive_target.lstat()
            if (
                archive_target.is_symlink()
                or not stat.S_ISREG(archive_info.st_mode)
                or archive_info.st_uid != 0
                or archive_info.st_gid != authority_group_id
                or stat.S_IMODE(archive_info.st_mode) != 0o640
                or _file_digest(archive_target) != artifact_id
            ):
                raise UpdateError(
                    "DATA_BUNDLE_COLLISION",
                    "Existing content-addressed archive is unsafe or mismatched.",
                )
        else:
            temporary_archive = archive_root / f".{raw_hex}.{uuid.uuid4().hex}.tmp"
            descriptor = os.open(
                temporary_archive,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o640,
            )
            with os.fdopen(descriptor, "wb") as output, archive_path.open("rb") as source:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            try:
                import grp

                os.chown(temporary_archive, 0, grp.getgrnam("cyrene-authority").gr_gid)
                os.chmod(temporary_archive, 0o640)
            except (ImportError, KeyError) as error:
                temporary_archive.unlink(missing_ok=True)
                raise UpdateError(
                    "AUTHORITY_GROUP_MISSING",
                    "The cyrene-authority group is required for staged bundles.",
                ) from error
            os.replace(temporary_archive, archive_target)
            self._fsync_directory(archive_root)
        if version_target.exists() or version_target.is_symlink():
            version_info = version_target.lstat()
            if (
                version_target.is_symlink()
                or not stat.S_ISDIR(version_info.st_mode)
                or version_info.st_uid != 0
                or version_info.st_gid != bundle_reader_group_id
                or stat.S_IMODE(version_info.st_mode) != 0o750
            ):
                raise UpdateError(
                    "DATA_BUNDLE_COLLISION", "Existing data-bundle version path is unsafe."
                )
            proof = version_target / "data-bundle-proof-v1.json"
            if (
                proof.is_symlink()
                or not proof.is_file()
                or _file_digest(proof) != candidate.manifest["dataBundle"]["proofSha256"]
            ):
                raise UpdateError(
                    "DATA_BUNDLE_COLLISION", "Existing extracted data-bundle proof differs."
                )
            self._verify_existing_bundle_tree(version_target, payload_root, bundle_reader_group_id)
            return version_target
        temporary = versions_root / f".{raw_hex}.{uuid.uuid4().hex}.tmp"
        shutil.copytree(payload_root, temporary, symlinks=False)
        try:
            self._set_bundle_reader_permissions(temporary)
            os.replace(temporary, version_target)
            self._fsync_directory(versions_root)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return version_target

    @staticmethod
    def _verify_existing_bundle_tree(
        installed_root: Path, verified_root: Path, group_id: int
    ) -> None:
        """Require a reused content-addressed tree to exactly match newly verified payloads."""
        expected_files: dict[str, str] = {}
        expected_directories: set[str] = set()
        for current, directory_names, file_names in os.walk(verified_root, followlinks=False):
            current_path = Path(current)
            for name in directory_names:
                path = current_path / name
                if path.is_symlink() or not path.is_dir():
                    raise UpdateError(
                        "UNSAFE_DATA_BUNDLE", "Verified bundle has an unsafe directory."
                    )
                expected_directories.add(path.relative_to(verified_root).as_posix())
            for name in file_names:
                path = current_path / name
                if path.is_symlink() or not path.is_file():
                    raise UpdateError("UNSAFE_DATA_BUNDLE", "Verified bundle has an unsafe file.")
                expected_files[path.relative_to(verified_root).as_posix()] = _file_digest(path)

        actual_files: dict[str, str] = {}
        actual_directories: set[str] = set()
        for current, directory_names, file_names in os.walk(installed_root, followlinks=False):
            current_path = Path(current)
            info = current_path.lstat()
            if (
                current_path.is_symlink()
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or info.st_gid != group_id
                or stat.S_IMODE(info.st_mode) != 0o750
            ):
                raise UpdateError(
                    "UNSAFE_DATA_BUNDLE", "Installed bundle directory permissions are unsafe."
                )
            if current_path != installed_root:
                actual_directories.add(current_path.relative_to(installed_root).as_posix())
            for name in directory_names:
                path = current_path / name
                if path.is_symlink() or not path.is_dir():
                    raise UpdateError(
                        "UNSAFE_DATA_BUNDLE", "Installed bundle has an unsafe directory."
                    )
            for name in file_names:
                path = current_path / name
                info = path.lstat()
                if (
                    path.is_symlink()
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != 0
                    or info.st_gid != group_id
                    or stat.S_IMODE(info.st_mode) != 0o640
                ):
                    raise UpdateError(
                        "UNSAFE_DATA_BUNDLE", "Installed bundle file permissions are unsafe."
                    )
                actual_files[path.relative_to(installed_root).as_posix()] = _file_digest(path)
        if actual_files != expected_files or actual_directories != expected_directories:
            raise UpdateError(
                "DATA_BUNDLE_COLLISION",
                "Existing data-bundle tree differs from its verified archive.",
            )

    @staticmethod
    def _write_authority_metadata(path: Path, payload: bytes) -> None:
        """Atomically publish read-only import evidence for Authority's fixed-root reader."""
        try:
            import grp

            group_id = grp.getgrnam("cyrene-authority").gr_gid
        except (ImportError, KeyError) as error:
            raise UpdateError(
                "AUTHORITY_GROUP_MISSING",
                "The cyrene-authority group is required for data-bundle metadata.",
            ) from error
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o640,
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.chown(temporary, 0, group_id)
            os.chmod(temporary, 0o640)
            os.replace(temporary, path)
            ComponentUpdater._fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate_authority_metadata_file(path: Path, group_id: int) -> None:
        try:
            info = path.lstat()
        except OSError as error:
            raise UpdateError(
                "UNSAFE_DATA_BUNDLE_ROOT", "Authority metadata file is unavailable."
            ) from error
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != group_id
            or stat.S_IMODE(info.st_mode) != 0o640
        ):
            raise UpdateError(
                "UNSAFE_DATA_BUNDLE_ROOT", "Authority metadata file permissions are unsafe."
            )

    @staticmethod
    def _set_bundle_reader_permissions(root: Path) -> None:
        try:
            import grp

            group_id = grp.getgrnam("cyrene-product-bundle-reader").gr_gid
        except (ImportError, KeyError) as error:
            raise UpdateError(
                "AUTHORITY_GROUP_MISSING",
                "The cyrene-product-bundle-reader group is required for staged bundles.",
            ) from error
        for current, directories, files in os.walk(root, followlinks=False):
            directory = Path(current)
            if directory.is_symlink():
                raise UpdateError("UNSAFE_DATA_BUNDLE", "Extracted data bundle contains a symlink.")
            os.chown(directory, 0, group_id)
            directory.chmod(0o750)
            for name in directories + files:
                path = directory / name
                if path.is_symlink():
                    raise UpdateError(
                        "UNSAFE_DATA_BUNDLE", "Extracted data bundle contains a symlink."
                    )
                if path.is_dir():
                    os.chown(path, 0, group_id)
                    path.chmod(0o750)
                elif path.is_file():
                    os.chown(path, 0, group_id)
                    path.chmod(0o640)
                else:
                    raise UpdateError(
                        "UNSAFE_DATA_BUNDLE", "Extracted data bundle contains a special file."
                    )

    @staticmethod
    def _write_private_file(path: Path, value: bytes) -> None:
        descriptor = os.open(
            path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())

    def _extract_native(self, archive: Path, destination: Path, artifact: dict[str, Any]) -> None:
        expected = artifact["files"]
        self._extract_tar(archive, destination, expected_files=expected)
        entrypoint = _safe_relative(artifact["entrypoint"], field="artifact.entrypoint")
        binary = destination.joinpath(*entrypoint.parts)
        if not binary.is_file():
            raise UpdateError("INVALID_ARTIFACT", "Native artifact entrypoint is missing.")
        binary.chmod(0o755)
        self._normalize_payload(destination, entrypoint.as_posix())

    def _extract_tar(
        self,
        archive: Path,
        destination: Path,
        expected_files: dict[str, str] | None,
        *,
        max_entries: int | None = None,
        max_member_bytes: int | None = None,
        max_expanded_bytes: int | None = None,
    ) -> None:
        destination.mkdir(mode=0o700)
        found: dict[str, str] = {}
        seen_entries: set[str] = set()
        expanded_bytes = 0
        try:
            with tarfile.open(archive, "r:*") as tar:
                entry_count = 0
                for member in tar:
                    entry_count += 1
                    if max_entries is not None and entry_count > max_entries:
                        raise UpdateError(
                            "UNSAFE_ARTIFACT", "Archive contains too many files or directories."
                        )
                    raw_path = member.name
                    if member.isdir() and raw_path.endswith("/"):
                        raw_path = raw_path[:-1]
                    path = _safe_relative(raw_path, field="tar member")
                    normalized = path.as_posix()
                    if normalized in seen_entries:
                        raise UpdateError(
                            "UNSAFE_ARTIFACT", f"Archive contains a duplicate entry: {member.name}"
                        )
                    seen_entries.add(normalized)
                    target = destination.joinpath(*path.parts)
                    if member.isdir():
                        if member.size != 0:
                            raise UpdateError(
                                "UNSAFE_ARTIFACT",
                                f"Archive directory has unexpected content: {member.name}",
                            )
                        target.mkdir(parents=True, exist_ok=True, mode=0o755)
                        continue
                    if not member.isfile():
                        raise UpdateError(
                            "UNSAFE_ARTIFACT",
                            f"Archive contains a link or special file: {member.name}",
                        )
                    if member.size > (max_member_bytes or 2_000_000_000):
                        raise UpdateError(
                            "UNSAFE_ARTIFACT", f"Archive member is too large: {member.name}"
                        )
                    expanded_bytes += member.size
                    if max_expanded_bytes is not None and expanded_bytes > max_expanded_bytes:
                        raise UpdateError(
                            "UNSAFE_ARTIFACT", "Archive exceeds its expanded-size limit."
                        )
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    source = tar.extractfile(member)
                    if source is None:
                        raise UpdateError(
                            "INVALID_ARTIFACT", f"Archive member cannot be read: {member.name}"
                        )
                    with source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    found[path.as_posix()] = hashlib.sha256(target.read_bytes()).hexdigest()
        except (OSError, tarfile.TarError) as error:
            raise UpdateError(
                "INVALID_ARTIFACT", f"Cannot safely extract release archive: {error}"
            ) from error
        if expected_files is not None:
            wanted = {
                name: digest.removeprefix("sha256:") for name, digest in expected_files.items()
            }
            if found != wanted:
                raise UpdateError(
                    "PAYLOAD_FILE_DIGEST_MISMATCH",
                    "Native payload files do not match the manifest file map.",
                )

    @staticmethod
    def _normalize_payload(root: Path, entrypoint: str) -> None:
        for current, directory_names, file_names in os.walk(root, followlinks=False):
            current_path = Path(current)
            current_path.chmod(0o755)
            for name in directory_names:
                path = current_path / name
                if path.is_symlink() or not path.is_dir():
                    raise UpdateError(
                        "UNSAFE_ARTIFACT", f"Payload contains an unsafe directory: {path}"
                    )
                path.chmod(0o755)
            for name in file_names:
                path = current_path / name
                if path.is_symlink() or not path.is_file():
                    raise UpdateError("UNSAFE_ARTIFACT", f"Payload contains an unsafe file: {path}")
                path.chmod(0o755 if path.relative_to(root).as_posix() == entrypoint else 0o644)

    def _install_native_release(self, candidate: Candidate, payload_root: Path) -> Path:
        component_id = candidate.component["componentId"]
        version = candidate.manifest["version"]
        root = self.install_root / "components" / component_id
        manifest_digest = candidate.manifest_digest
        if not _valid_digest(manifest_digest):
            raise UpdateError(
                "INVALID_MANIFEST", f"Native {component_id} manifest digest is invalid."
            )
        release_identity = f"{version}--{manifest_digest.removeprefix('sha256:')}"
        release = root / "releases" / release_identity
        if release.exists():
            existing_manifest = _read_object(
                release / "component-manifest.json", "existing component manifest"
            )
            self._validate_manifest_digest(existing_manifest, candidate.manifest_digest)
            if existing_manifest != candidate.manifest:
                raise UpdateError(
                    "RELEASE_COLLISION",
                    f"Native release identity collision for {component_id} {version}.",
                )
            return release
        root.mkdir(parents=True, exist_ok=True, mode=0o755)
        (root / "releases").mkdir(exist_ok=True, mode=0o755)
        if payload_root.is_symlink() or not payload_root.is_dir():
            raise UpdateError(
                "UNSAFE_ARTIFACT", "Staged native payload root is not a real directory."
            )
        os.replace(payload_root, release)
        self._atomic_json_file(release / "component-manifest.json", candidate.manifest, mode=0o644)
        item = {
            "componentId": component_id,
            "version": version,
            "releaseIdentity": manifest_digest,
            "manifestDigest": manifest_digest,
            "artifactDigest": _artifact_digest_from_manifest(candidate.manifest),
            "bundleIdentity": None,
            "manifest": candidate.manifest,
        }
        self._write_release_receipt(item)
        return release

    @staticmethod
    def _atomic_json_file(path: Path, value: dict[str, Any], *, mode: int) -> None:
        temporary = path.with_name("." + path.name + ".tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)

    def _validate_manifest_digest(self, manifest: Any, expected: Any) -> None:
        if (
            not isinstance(manifest, dict)
            or not _valid_digest(expected)
            or manifest.get("manifestDigest") != expected
            or expected != _digest_json(manifest, "manifestDigest")
        ):
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE", "Component manifest JCS digest is invalid."
            )

    def _validate_staged_record(
        self,
        record: dict[str, Any],
        *,
        expected_plan_id: str,
        expected_plan_digest: str,
    ) -> None:
        if (
            record.get("schemaVersion") != 2
            or record.get("phase") != "staged"
            or not isinstance(record.get("components"), list)
        ):
            raise UpdateError("INVALID_STAGE", "Staged plan metadata has an unsupported schema.")
        plan = record.get("plan")
        if (
            not isinstance(plan, dict)
            or plan.get("planId") != expected_plan_id
            or plan.get("planDigest") != expected_plan_digest
            or plan.get("channel") != record.get("channel")
            or record.get("channel") not in {"stable", "preview"}
            or plan.get("phase") != "checked"
        ):
            raise UpdateError(
                "INVALID_STAGE", "Staged plan identity or channel differs from apply confirmation."
            )
        plan_components = plan.get("components")
        if not isinstance(plan_components, list) or not plan_components:
            raise UpdateError("INVALID_STAGE", "Staged plan has no checked component list.")
        if (
            plan.get("catalogGeneration") != self.catalog_generation
            or plan.get("catalogDigest") != self.catalog_digest
        ):
            raise UpdateError(
                "PLAN_CATALOG_CHANGED",
                "The staged plan was checked against a different catalog generation; check and stage again.",
            )
        plan_material = {
            "schemaVersion": 1,
            "channel": plan["channel"],
            "catalogGeneration": plan["catalogGeneration"],
            "catalogDigest": plan["catalogDigest"],
            "components": plan_components,
        }
        expected_digest = "sha256:" + hashlib.sha256(canonical_jcs(plan_material)).hexdigest()
        expected_id = "plan-" + expected_digest.split(":", 1)[1][:32]
        if expected_digest != expected_plan_digest or expected_id != expected_plan_id:
            raise UpdateError(
                "INVALID_STAGE", "Staged plan digest does not match its canonical contents."
            )
        if len(plan_components) != len(record["components"]):
            raise UpdateError(
                "INVALID_STAGE", "Staged component set differs from the checked plan."
            )

        planned_by_id: dict[str, dict[str, Any]] = {}
        plan_fields = {"componentId", "version", "manifestDigest", "artifactDigest", "restartGroup"}
        for planned in plan_components:
            if (
                not isinstance(planned, dict)
                or set(planned) != plan_fields
                or not isinstance(planned.get("componentId"), str)
                or not isinstance(planned.get("version"), str)
                or VERSION_PATTERN.fullmatch(planned["version"]) is None
                or not _valid_digest(planned.get("manifestDigest"))
                or not _valid_digest(planned.get("artifactDigest"))
                or not isinstance(planned.get("restartGroup"), str)
                or planned["componentId"] in planned_by_id
            ):
                raise UpdateError(
                    "INVALID_STAGE", "Checked plan component metadata is invalid or duplicated."
                )
            planned_by_id[planned["componentId"]] = planned

        staged_ids: set[str] = set()
        stage_root = self.state_root / "staged" / expected_plan_id
        for item in record["components"]:
            item_fields = plan_fields | {
                "manifest",
                "releasePath",
                "archivePath",
                "releaseIdentity",
                "pointerIdentity",
                "bundleIdentity",
            }
            if (
                isinstance(item, dict)
                and self.components.get(item.get("componentId"), {}).get("kind") == "data-bundle"
            ):
                item_fields.add("dataBundle")
            if (
                not isinstance(item, dict)
                or set(item) != item_fields
                or not isinstance(item.get("componentId"), str)
                or item.get("componentId") not in self.components
                or item["componentId"] in staged_ids
                or not isinstance(item.get("releasePath"), str)
                or not isinstance(item.get("archivePath"), str)
            ):
                raise UpdateError(
                    "INVALID_STAGE", "Staged component identity is invalid or duplicated."
                )
            component_id = item["componentId"]
            staged_ids.add(component_id)
            planned = planned_by_id.get(component_id)
            if planned is None or any(item.get(key) != planned.get(key) for key in plan_fields):
                raise UpdateError(
                    "INVALID_STAGE",
                    f"Staged metadata differs from the checked plan for {component_id}.",
                )
            manifest = item.get("manifest")
            self._validate_manifest_digest(manifest, item.get("manifestDigest"))
            if (
                manifest.get("componentId") != component_id
                or manifest.get("version") != item["version"]
                or item.get("releaseIdentity") != item.get("manifestDigest")
            ):
                raise UpdateError(
                    "INVALID_STAGE", f"Staged manifest identity differs for {component_id}."
                )
            artifact = manifest.get("artifact")
            artifact_digest = artifact.get("sha256") if isinstance(artifact, dict) else None
            if (
                not _valid_digest(artifact_digest)
                or item.get("artifactDigest") != artifact_digest
                or item.get("restartGroup")
                != self.components[component_id].get("restart", {}).get("group")
            ):
                raise UpdateError(
                    "INVALID_STAGE", f"Staged artifact identity differs for {component_id}."
                )

            component = self.components[component_id]
            service = component.get("pythonBundleService")
            if component.get("kind") == "data-bundle":
                artifact_descriptor = manifest.get("artifact")
                artifact_id = (
                    artifact_descriptor.get("sha256")
                    if isinstance(artifact_descriptor, dict)
                    else None
                )
                expected_release = (
                    self.data_bundle_root / "versions" / artifact_id.removeprefix("sha256:")
                    if _valid_digest(artifact_id)
                    else Path("/")
                )
                expected_pointer = (
                    artifact_id.removeprefix("sha256:") if _valid_digest(artifact_id) else None
                )
                bundle_record = item.get("dataBundle")
                if (
                    not isinstance(bundle_record, dict)
                    or set(bundle_record) != {"artifactId", "proofSha256", "proofSchemaVersion"}
                    or bundle_record.get("artifactId") != artifact_id
                    or bundle_record.get("proofSha256")
                    != manifest.get("dataBundle", {}).get("proofSha256")
                    or bundle_record.get("proofSchemaVersion") != 1
                    or item.get("bundleIdentity") != artifact_id
                ):
                    raise UpdateError(
                        "INVALID_STAGE", "Data-bundle staged identity differs from its manifest."
                    )
            elif service:
                bundle_identity = item.get("bundleIdentity")
                if (
                    not isinstance(bundle_identity, str)
                    or BUNDLE_ID_PATTERN.fullmatch(bundle_identity) is None
                ):
                    raise UpdateError(
                        "INVALID_STAGE",
                        f"Staged Python bundle identity is invalid for {component_id}.",
                    )
                expected_release = (
                    self.install_root / "services" / service / "releases" / bundle_identity
                )
            else:
                expected_pointer = (
                    f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
                )
                if item.get("bundleIdentity") is not None:
                    raise UpdateError(
                        "INVALID_STAGE",
                        f"Native component {component_id} has a Python bundle identity.",
                    )
                expected_release = (
                    self.install_root / "components" / component_id / "releases" / expected_pointer
                )
            if item.get("pointerIdentity") != expected_release.name:
                raise UpdateError(
                    "INVALID_STAGE", f"Staged pointer identity differs for {component_id}."
                )
            release_path = Path(item["releasePath"])
            if (
                release_path != expected_release
                or release_path.is_symlink()
                or not release_path.is_dir()
            ):
                raise UpdateError(
                    "INVALID_STAGE", f"Staged release is missing or unsafe for {component_id}."
                )
            allowed_release_root = (
                self.data_bundle_root
                if component.get("kind") == "data-bundle"
                else self.install_root
            )
            if not self._path_is_under(release_path, allowed_release_root):
                raise UpdateError(
                    "INVALID_STAGE", f"Staged release escaped its trusted root for {component_id}."
                )
            if component.get("kind") == "data-bundle":
                authority_archive = (
                    self.data_bundle_root
                    / "archives"
                    / f"sha256-{item['artifactDigest'].removeprefix('sha256:')}.tar.zst"
                )
                if (
                    authority_archive.is_symlink()
                    or not authority_archive.is_file()
                    or _file_digest(authority_archive) != item["artifactDigest"]
                ):
                    raise UpdateError(
                        "INVALID_STAGE",
                        "Authority's content-addressed bundle archive is missing or changed.",
                    )
                self._validate_data_bundle_proof(
                    Candidate(
                        component,
                        manifest,
                        item["manifestDigest"],
                        item["artifactDigest"],
                        "",
                        {},
                        "",
                    ),
                    release_path,
                    record["channel"],
                )
            elif service:
                try:
                    inner = self._load_service_bundle().validate_bundle(
                        release_path, expected_service=service
                    )
                except Exception as error:
                    raise UpdateError(
                        "INVALID_STAGE",
                        f"Staged Python bundle failed revalidation for {component_id}: {error}",
                    ) from error
                if (
                    inner.get("schema_version") != 2
                    or inner.get("version") != item["bundleIdentity"]
                    or inner.get("artifact_digest") != item["bundleIdentity"]
                ):
                    raise UpdateError(
                        "INVALID_STAGE",
                        f"Staged Python bundle identity differs for {component_id}.",
                    )
            else:
                local_manifest = _read_object(
                    release_path / "component-manifest.json",
                    f"staged {component_id} release manifest",
                )
                self._validate_manifest_digest(local_manifest, item["manifestDigest"])
                if local_manifest != manifest:
                    raise UpdateError(
                        "INVALID_STAGE", f"Staged native manifest changed for {component_id}."
                    )

            if component.get("kind") != "data-bundle":
                receipt = self._read_release_receipt(component_id, item["releaseIdentity"])
                if (
                    receipt is None
                    or receipt.get("manifest") != manifest
                    or receipt.get("artifactDigest") != item["artifactDigest"]
                    or receipt.get("version") != item["version"]
                    or receipt.get("bundleIdentity") != item.get("bundleIdentity")
                ):
                    raise UpdateError(
                        "INVALID_STAGE",
                        f"Verified release receipt is missing or inconsistent for {component_id}.",
                    )

            archive_path = Path(item["archivePath"])
            if (
                archive_path.parent != stage_root / component_id
                or archive_path.is_symlink()
                or not archive_path.is_file()
                or _file_digest(archive_path) != item["artifactDigest"]
            ):
                raise UpdateError(
                    "INVALID_STAGE", f"Staged archive is missing or unsafe for {component_id}."
                )

    @staticmethod
    def _path_is_under(path: Path, parent: Path) -> bool:
        try:
            path.resolve().relative_to(parent.resolve())
            return True
        except ValueError:
            return False

    def _capture_active_versions(self, components: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Capture actual pointer and verified release identities for rollback."""

        captured = []
        for item in components:
            component_id = item["componentId"]
            component = self.components[component_id]
            installed = self._installed(component)
            if component.get("kind") == "data-bundle" and installed.get("active") is True:
                artifact_id = installed.get("artifactDigest")
                captured.append(
                    {
                        "componentId": component_id,
                        "version": installed.get("activeVersion")
                        if installed.get("manifest")
                        else "0.0.0",
                        "releaseIdentity": installed.get("releaseIdentity")
                        if installed.get("manifest")
                        else None,
                        "manifestDigest": installed.get("manifestDigest")
                        if installed.get("manifest")
                        else None,
                        "artifactDigest": artifact_id,
                        "pointerIdentity": artifact_id.removeprefix("sha256:")
                        if _valid_digest(artifact_id)
                        else None,
                        "bundleIdentity": artifact_id,
                        "identityAttested": True,
                    }
                )
                continue
            captured.append(
                {
                    "componentId": component_id,
                    "version": installed.get("activeVersion"),
                    "releaseIdentity": installed.get("releaseIdentity"),
                    "manifestDigest": installed.get("manifestDigest"),
                    "artifactDigest": installed.get("artifactDigest"),
                    "pointerIdentity": installed.get("pointerIdentity"),
                    "bundleIdentity": installed.get("bundleIdentity"),
                    "identityAttested": installed.get("identityAttested") is True,
                }
            )
        return captured

    def _prepare_authority_activation(
        self, record: dict[str, Any], plan_id: str, plan_digest: str
    ) -> dict[str, Any] | None:
        data_bundle = next(
            (
                item
                for item in record["components"]
                if self.components[item["componentId"]].get("kind") == "data-bundle"
            ),
            None,
        )
        if data_bundle is None:
            return None
        status = self._authority_request({"action": "status"})
        generation = status.get("highestGeneration")
        current = status.get("activeArtifactId")
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
            or (current is not None and not _valid_digest(current))
        ):
            raise UpdateError(
                "AUTHORITY_STATUS_INVALID",
                "Authority status omitted a valid generation or artifact identity.",
            )
        data = data_bundle.get("dataBundle")
        artifact_id = data.get("artifactId") if isinstance(data, dict) else None
        if not _valid_digest(artifact_id):
            raise UpdateError("INVALID_STAGE", "Staged data-bundle artifact identity is invalid.")
        validation = self._authority_request(
            {
                "action": "validateArtifact",
                "planId": plan_id,
                "planDigest": plan_digest,
                "artifactId": artifact_id,
            }
        )
        if (
            validation.get("artifactId") != artifact_id
            or validation.get("planId") != plan_id
            or validation.get("planDigest") != plan_digest
            or validation.get("highestGeneration") != generation
            or validation.get("nextGeneration") != generation + 1
            or validation.get("currentGeneration") != status.get("currentGeneration")
        ):
            raise UpdateError(
                "AUTHORITY_PLAN_MISMATCH",
                "Authority validation response differs from the confirmed staged plan.",
            )
        return {
            "planId": plan_id,
            "planDigest": plan_digest,
            "artifactId": artifact_id,
            "expectedGeneration": generation,
            "previousArtifactId": current,
            "validationResponse": validation,
        }

    def _activate_authority_bundle(self, transaction: dict[str, Any]) -> None:
        activation = transaction.get("authorityActivation")
        if activation is None:
            return
        response = self._authority_request(
            {
                "action": "activateArtifact",
                "planId": activation["planId"],
                "planDigest": activation["planDigest"],
                "expectedGeneration": activation["expectedGeneration"],
                "artifactId": activation["artifactId"],
            }
        )
        status = self._authority_request({"action": "status"})
        current = status.get("activeArtifactId")
        generation = status.get("highestGeneration")
        if (
            current != activation["artifactId"]
            or not isinstance(generation, int)
            or generation != activation["expectedGeneration"] + 1
            or response.get("artifactId") != activation["artifactId"]
            or response.get("planId") != activation["planId"]
            or response.get("planDigest") != activation["planDigest"]
            or response.get("currentGeneration") != status.get("currentGeneration")
            or response.get("highestGeneration") != generation
            or response.get("activationEpoch") != status.get("activationEpoch")
        ):
            raise UpdateError(
                "AUTHORITY_ACTIVATION_UNCONFIRMED",
                "Authority did not durably activate the verified data bundle.",
                retryable=True,
            )
        activation["activationResponse"] = response
        activation["resultingGeneration"] = generation
        activation["resultingEpoch"] = status.get("activationEpoch")
        transaction_path = self.state_root / "transactions" / f"{transaction['planId']}.json"
        if transaction_path.is_file() and not transaction_path.is_symlink():
            _atomic_json(transaction_path, transaction)

    def _rollback_authority_activation(self, transaction: dict[str, Any]) -> None:
        activation = transaction.get("authorityActivation")
        if not isinstance(activation, dict):
            return
        status = self._authority_request({"action": "status"})
        current = status.get("activeArtifactId")
        if current == activation.get("previousArtifactId"):
            return
        if current != activation.get("artifactId"):
            raise UpdateError(
                "AUTHORITY_ROLLBACK_CONFLICT",
                "Authority active artifact changed outside this update transaction.",
            )
        previous_artifact = activation.get("previousArtifactId")
        if not _valid_digest(previous_artifact):
            raise UpdateError(
                "AUTHORITY_ROLLBACK_UNAVAILABLE",
                "Initial Authority activation has no previous artifact; its durable pointer cannot be rewound.",
            )
        origin_plan_id, origin_plan_digest = self._find_authority_artifact_plan_reference(
            previous_artifact
        )
        generation = status.get("highestGeneration")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise UpdateError(
                "AUTHORITY_STATUS_INVALID", "Authority generation is invalid during rollback."
            )
        rollback_material = {
            "schemaVersion": 1,
            "action": "rollback",
            "originPlanId": origin_plan_id,
            "originPlanDigest": origin_plan_digest,
            "artifactId": previous_artifact,
            "recoveryPlanId": transaction["planId"],
            "recoveryPlanDigest": transaction["planDigest"],
        }
        rollback_digest = "sha256:" + hashlib.sha256(canonical_jcs(rollback_material)).hexdigest()
        rollback_id = "plan-rollback-" + rollback_digest.split(":", 1)[1][:32]
        self._persist_authority_rollback_plan_reference(
            previous_artifact,
            plan_id=rollback_id,
            plan_digest=rollback_digest,
            origin_plan_id=origin_plan_id,
            origin_plan_digest=origin_plan_digest,
        )
        validation = self._authority_request(
            {
                "action": "validateArtifact",
                "planId": rollback_id,
                "planDigest": rollback_digest,
                "artifactId": previous_artifact,
            }
        )
        if (
            validation.get("planId") != rollback_id
            or validation.get("planDigest") != rollback_digest
            or validation.get("artifactId") != previous_artifact
            or validation.get("highestGeneration") != generation
            or validation.get("nextGeneration") != generation + 1
            or validation.get("currentGeneration") != status.get("currentGeneration")
        ):
            raise UpdateError(
                "AUTHORITY_PLAN_MISMATCH",
                "Authority rollback validation differs from the confirmed recovery plan.",
            )
        response = self._authority_request(
            {
                "action": "activateArtifact",
                "planId": rollback_id,
                "planDigest": rollback_digest,
                "expectedGeneration": generation,
                "artifactId": previous_artifact,
            }
        )
        after = self._authority_request({"action": "status"})
        if (
            after.get("activeArtifactId") != previous_artifact
            or not isinstance(after.get("highestGeneration"), int)
            or after["highestGeneration"] != generation + 1
            or response.get("planId") != rollback_id
            or response.get("planDigest") != rollback_digest
            or response.get("artifactId") != previous_artifact
            or response.get("currentGeneration") != after.get("currentGeneration")
            or response.get("highestGeneration") != after["highestGeneration"]
            or response.get("activationEpoch") != after.get("activationEpoch")
        ):
            raise UpdateError(
                "AUTHORITY_ROLLBACK_UNCONFIRMED",
                "Authority did not durably restore the previous artifact at a higher generation.",
            )
        activation["rollbackResponse"] = response
        activation["rollbackPlanId"] = rollback_id
        activation["rollbackPlanDigest"] = rollback_digest
        activation["rollbackGeneration"] = after["highestGeneration"]

    def _find_authority_artifact_plan_reference(self, artifact_id: str) -> tuple[str, str]:
        """Find a normal trusted plan reference that originally imported an artifact."""
        if not _valid_digest(artifact_id):
            raise UpdateError("INVALID_PLAN", "Authority artifact identity is invalid.")
        raw_hex = artifact_id.removeprefix("sha256:")
        metadata_directory = self.data_bundle_root / "metadata" / raw_hex
        plans_directory = metadata_directory / "plans"
        try:
            plans_info = plans_directory.lstat()
        except OSError as error:
            raise UpdateError(
                "AUTHORITY_ROLLBACK_UNAVAILABLE",
                "Previous Authority artifact has no imported plan references.",
            ) from error
        if (
            plans_directory.is_symlink()
            or not stat.S_ISDIR(plans_info.st_mode)
            or plans_info.st_uid != 0
            or stat.S_IMODE(plans_info.st_mode) & 0o022
        ):
            raise UpdateError("UNSAFE_DATA_BUNDLE_ROOT", "Authority plan metadata path is unsafe.")
        try:
            import grp

            authority_group_id = grp.getgrnam("cyrene-authority").gr_gid
        except (ImportError, KeyError) as error:
            raise UpdateError(
                "AUTHORITY_GROUP_MISSING", "The cyrene-authority group is required for rollback."
            ) from error

        manifest_path = metadata_directory / "component-manifest-v2.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise UpdateError(
                "AUTHORITY_ROLLBACK_UNAVAILABLE", "Previous outer manifest metadata is unavailable."
            )
        self._validate_authority_metadata_file(manifest_path, authority_group_id)
        try:
            manifest = json.loads(manifest_path.read_bytes(), object_pairs_hook=_unique_json_object)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError(
                "AUTHORITY_ROLLBACK_UNAVAILABLE", "Previous outer manifest metadata is invalid."
            ) from error
        if not isinstance(manifest, dict):
            raise UpdateError(
                "AUTHORITY_ROLLBACK_UNAVAILABLE", "Previous outer manifest metadata is invalid."
            )
        manifest_digest = manifest.get("manifestDigest")
        envelope = manifest.get("dataBundle")
        if (
            not _valid_digest(manifest_digest)
            or manifest_digest != _digest_json(manifest, "manifestDigest")
            or not isinstance(envelope, dict)
            or set(envelope) != {"proofPath", "proofSha256"}
            or not _valid_digest(envelope.get("proofSha256"))
        ):
            raise UpdateError(
                "AUTHORITY_ROLLBACK_UNAVAILABLE", "Previous outer manifest identity is invalid."
            )

        reference_fields = {
            "schemaVersion",
            "planId",
            "planDigest",
            "artifactId",
            "manifestDigest",
            "proofSha256",
        }
        for path in sorted(plans_directory.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            self._validate_authority_metadata_file(path, authority_group_id)
            reference = _read_object(path, "Authority artifact plan reference")
            if (
                set(reference) != reference_fields
                or reference.get("schemaVersion") != 1
                or reference.get("planId") != path.stem
                or PLAN_ID_PATTERN.fullmatch(path.stem) is None
                or not _valid_digest(reference.get("planDigest"))
                or reference.get("artifactId") != artifact_id
                or reference.get("manifestDigest") != manifest_digest
                or reference.get("proofSha256") != envelope["proofSha256"]
            ):
                continue
            return path.stem, reference["planDigest"]
        raise UpdateError(
            "AUTHORITY_ROLLBACK_UNAVAILABLE",
            "Previous Authority artifact has no matching normal plan reference.",
        )

    def _persist_authority_rollback_plan_reference(
        self,
        artifact_id: str,
        *,
        plan_id: str,
        plan_digest: str,
        origin_plan_id: str,
        origin_plan_digest: str,
    ) -> None:
        """Bind a monotonic rollback plan to an already verified immutable artifact."""
        if (
            not _valid_digest(artifact_id)
            or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", plan_id) is None
            or not _valid_digest(plan_digest)
            or PLAN_ID_PATTERN.fullmatch(origin_plan_id) is None
            or not _valid_digest(origin_plan_digest)
        ):
            raise UpdateError("INVALID_PLAN", "Authority rollback plan identity is invalid.")
        raw_hex = artifact_id.removeprefix("sha256:")
        metadata_directory = self.data_bundle_root / "metadata" / raw_hex
        manifest_path = metadata_directory / "component-manifest-v2.json"
        import_record_path = metadata_directory / "import-record-v1.json"
        for directory in (
            self.data_bundle_root,
            self.data_bundle_root / "metadata",
            metadata_directory,
        ):
            try:
                info = directory.lstat()
            except OSError as error:
                raise UpdateError(
                    "AUTHORITY_ROLLBACK_UNAVAILABLE",
                    "Previous Authority artifact import metadata is unavailable.",
                ) from error
            if (
                directory.is_symlink()
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise UpdateError(
                    "UNSAFE_DATA_BUNDLE_ROOT", "Previous Authority artifact metadata is unsafe."
                )
        if (
            manifest_path.is_symlink()
            or not manifest_path.is_file()
            or import_record_path.is_symlink()
            or not import_record_path.is_file()
        ):
            raise UpdateError(
                "AUTHORITY_ROLLBACK_UNAVAILABLE",
                "Previous Authority artifact has no imported outer manifest metadata.",
            )
        try:
            import grp

            authority_group_id = grp.getgrnam("cyrene-authority").gr_gid
        except (ImportError, KeyError) as error:
            raise UpdateError(
                "AUTHORITY_GROUP_MISSING", "The cyrene-authority group is required for rollback."
            ) from error
        self._validate_authority_metadata_file(manifest_path, authority_group_id)
        self._validate_authority_metadata_file(import_record_path, authority_group_id)
        manifest_bytes = manifest_path.read_bytes()
        try:
            manifest = json.loads(manifest_bytes, object_pairs_hook=_unique_json_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError(
                "AUTHORITY_ROLLBACK_UNAVAILABLE", "Previous outer manifest metadata is invalid."
            ) from error
        import_record = _read_object(import_record_path, "Authority artifact import record")
        envelope = manifest.get("dataBundle") if isinstance(manifest, dict) else None
        artifact = manifest.get("artifact") if isinstance(manifest, dict) else None
        if (
            not isinstance(manifest, dict)
            or manifest.get("schemaVersion") != 2
            or manifest.get("manifestDigest") != _digest_json(manifest, "manifestDigest")
            or manifest.get("contentDigest") != artifact_id
            or not isinstance(artifact, dict)
            or artifact.get("kind") != "data-bundle"
            or artifact.get("sha256") != artifact_id
            or not isinstance(envelope, dict)
            or set(envelope) != {"proofPath", "proofSha256"}
            or envelope.get("proofPath") != "data-bundle-proof-v1.json"
            or not _valid_digest(envelope.get("proofSha256"))
            or import_record.get("artifactId") != artifact_id
            or import_record.get("manifestDigest") != manifest.get("manifestDigest")
        ):
            raise UpdateError(
                "AUTHORITY_ROLLBACK_UNAVAILABLE",
                "Previous Authority artifact manifest identity is not trustworthy.",
            )
        proof_path = self.data_bundle_root / "versions" / raw_hex / envelope["proofPath"]
        for directory in (self.data_bundle_root / "versions", proof_path.parent):
            try:
                info = directory.lstat()
            except OSError as error:
                raise UpdateError(
                    "AUTHORITY_ROLLBACK_UNAVAILABLE",
                    "Previous Authority artifact version directory is unavailable.",
                ) from error
            if (
                directory.is_symlink()
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise UpdateError(
                    "UNSAFE_DATA_BUNDLE_ROOT", "Previous Authority artifact version path is unsafe."
                )
        if (
            proof_path.is_symlink()
            or not proof_path.is_file()
            or _file_digest(proof_path) != envelope["proofSha256"]
        ):
            raise UpdateError(
                "AUTHORITY_ROLLBACK_UNAVAILABLE",
                "Previous Authority artifact proof does not match its outer manifest.",
            )
        plans_directory = metadata_directory / "plans"
        plans_directory.mkdir(mode=0o750, exist_ok=True)
        info = plans_directory.lstat()
        if plans_directory.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
            raise UpdateError("UNSAFE_DATA_BUNDLE_ROOT", "Authority plan metadata path is unsafe.")
        os.chown(plans_directory, 0, authority_group_id)
        plans_directory.chmod(0o750)
        reference = {
            "schemaVersion": 1,
            "planId": plan_id,
            "planDigest": plan_digest,
            "artifactId": artifact_id,
            "manifestDigest": manifest["manifestDigest"],
            "proofSha256": envelope["proofSha256"],
            "action": "rollback",
            "originPlanId": origin_plan_id,
            "originPlanDigest": origin_plan_digest,
        }
        reference_path = plans_directory / f"{plan_id}.json"
        payload = (
            json.dumps(reference, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
        )
        if reference_path.exists() or reference_path.is_symlink():
            self._validate_authority_metadata_file(reference_path, authority_group_id)
            if (
                reference_path.is_symlink()
                or _read_object(reference_path, "rollback plan reference") != reference
            ):
                raise UpdateError("DATA_BUNDLE_COLLISION", "Rollback plan reference differs.")
        else:
            self._write_authority_metadata(reference_path, payload)

    def _begin_maintenance(self, transaction: dict[str, Any]) -> str:
        request_id = _maintenance_request_id(transaction)
        params = {
            "request_id": request_id,
            "target_kind": transaction["targetKind"],
            "requires_restart": True,
            "expected_catalog_generation": transaction["expectedCatalogGeneration"],
            "expected_activity_sources": transaction["expectedActivitySources"],
            "expected_gate_generation": transaction["expectedGateGeneration"],
            "user_confirmed_restart": True,
            "plan_id": transaction["planId"],
            "plan_digest": transaction["planDigest"],
            "component_artifact_digests": transaction["componentArtifactDigests"],
        }
        try:
            result = self._broker_request("BeginMaintenance", params, request_id=request_id)
        except UpdateError as error:
            if error.code in {
                "ACTIVE_TASKS",
                "IDLE_RUNTIME_REQUIRES_UNLOAD",
                "MAINTENANCE_ACTIVE",
                "UNKNOWN",
                "STALE_READINESS",
                "CATALOG_GENERATION_MISMATCH",
                "GATE_GENERATION_MISMATCH",
                "USER_CONFIRMATION_REQUIRED",
                "REQUEST_ID_PLAN_MISMATCH",
                "INVALID_REQUEST",
                "INVALID_ARGUMENT",
                "UNAUTHORIZED",
            }:
                error.maintenance_not_acquired = True
            raise
        status = result.get("status")
        token = result.get("maintenance_token")
        blocker_codes = result.get("blocker_codes")
        gate_generation = result.get("gate_generation")
        known_statuses = BEGIN_NO_TOKEN_STATUSES | {"READY"}
        if (
            not isinstance(status, str)
            or status not in known_statuses
            or "maintenance_token" not in result
            or not isinstance(gate_generation, int)
            or isinstance(gate_generation, bool)
            or gate_generation < 0
            or not isinstance(blocker_codes, list)
            or any(not isinstance(code, str) for code in blocker_codes)
        ):
            raise UpdateError(
                "GATE_UNKNOWN",
                "Maintenance broker returned an unknown or malformed BeginMaintenance result; the update transaction remains recoverable.",
                retryable=True,
            )

        if status in BEGIN_NO_TOKEN_STATUSES and token is None:
            if status == "STALE_READINESS":
                raise UpdateError(
                    "STALE_READINESS",
                    "A runtime task or admission changed readiness before the maintenance gate was acquired; retry the same staged plan after activity settles.",
                    retryable=True,
                    maintenance_not_acquired=True,
                )
            if status == "USER_CONFIRMATION_REQUIRED":
                raise UpdateError(
                    "USER_CONFIRMATION_REQUIRED",
                    "The maintenance broker requires explicit confirmation before restarting this component; confirm this exact staged plan and retry.",
                    maintenance_not_acquired=True,
                )
            try:
                self._require_ready(
                    {"status": status, "active_tasks": result.get("active_tasks", [])},
                    transaction["targetKind"],
                )
            except UpdateError as error:
                error.maintenance_not_acquired = True
                raise
            raise UpdateError(
                "GATE_UNKNOWN",
                "Maintenance broker refused the BeginMaintenance request without a token; the update transaction remains recoverable.",
                retryable=True,
            )

        if status != "MAINTENANCE_ACTIVE":
            raise UpdateError(
                "GATE_UNKNOWN",
                "Maintenance broker did not confirm gate ownership; the update transaction remains recoverable.",
                retryable=True,
            )
        if not isinstance(token, str) or len(token) < 32:
            raise UpdateError(
                "GATE_UNKNOWN",
                "Maintenance broker did not return a durable transaction token.",
                retryable=True,
            )
        return token

    def _clear_begin_pending(self, transaction: dict[str, Any], transaction_path: Path) -> None:
        """Remove only the local intent after the broker explicitly refused gate acquisition."""

        if not transaction_path.is_file() or transaction_path.is_symlink():
            return
        current = _read_object(transaction_path, "update transaction")
        if (
            current.get("phase") != "begin_pending"
            or current.get("planId") != transaction.get("planId")
            or current.get("planDigest") != transaction.get("planDigest")
        ):
            raise UpdateError(
                "TRANSACTION_CHANGED",
                "The updater journal changed while a maintenance request was refused.",
                retryable=True,
            )
        transaction_path.unlink()
        self._fsync_directory(transaction_path.parent)

    def _end_maintenance(self, transaction: dict[str, Any], *, outcome: str, healthy: bool) -> None:
        token = transaction.get("maintenanceToken")
        if not isinstance(token, str):
            token = self._begin_maintenance(transaction)
        transaction_id = _maintenance_request_id(transaction)
        request_id = f"cyrene-update-end-{transaction['planId']}-{outcome.lower()}"
        result = self._broker_request(
            "EndMaintenance",
            {
                "request_id": transaction_id,
                "target_kind": transaction["targetKind"],
                "maintenance_token": token,
                "outcome": outcome,
                "healthy": healthy,
            },
            request_id=request_id,
        )
        allowed = (
            {"READY", "SUCCESS", "ROLLED_BACK"} if healthy else {"MAINTENANCE_ACTIVE", "FAILED"}
        )
        if result.get("status") not in allowed:
            raise UpdateError(
                "GATE_END_FAILED",
                f"Maintenance broker did not release the gate: {result.get('status', 'unknown')}.",
                retryable=True,
            )

    def _activate_transaction(self, transaction: dict[str, Any]) -> None:
        for item in transaction["components"]:
            component = self.components[item["componentId"]]
            previous = next(
                old for old in transaction["previous"] if old["componentId"] == item["componentId"]
            )
            if component.get("kind") == "data-bundle":
                continue
            if component.get("pythonBundleService"):
                bundle = self._load_service_bundle()
                bundle_identity = item.get("bundleIdentity")
                if not _valid_digest(bundle_identity):
                    raise UpdateError(
                        "TRANSACTION_IDENTITY_UNKNOWN",
                        f"Staged Python bundle identity is missing for {item['componentId']}.",
                    )
                bundle.activate_release(
                    component["pythonBundleService"],
                    bundle_identity,
                    install_root=self.install_root,
                    expected_current_version=previous.get("bundleIdentity"),
                )
                self._write_active_receipt(item)
            else:
                pointer_identity = _native_release_pointer_identity(item)
                self._activate_native(
                    component["componentId"],
                    pointer_identity,
                    expected_current=previous.get("pointerIdentity"),
                )
                self._write_active_receipt(
                    {
                        **item,
                        "releaseIdentity": item.get("releaseIdentity", item.get("manifestDigest")),
                        "bundleIdentity": None,
                    }
                )

    def _activate_native(
        self, component_id: str, pointer_identity: str | None, *, expected_current: str | None
    ) -> None:
        root = self.install_root / "components" / component_id
        active = root / "active"
        current = self._active_native_pointer_identity(component_id)
        if current != expected_current:
            raise UpdateError(
                "ACTIVE_VERSION_CHANGED",
                f"Active {component_id} pointer changed since confirmation.",
                retryable=True,
            )
        if pointer_identity is not None:
            version, pointer_digest = _native_release_directory_identity(pointer_identity)
            if VERSION_PATTERN.fullmatch(version) is None or (
                pointer_digest is not None and len(pointer_digest) != 64
            ):
                raise UpdateError(
                    "TRANSACTION_IDENTITY_UNKNOWN",
                    f"Native {component_id} release pointer identity is invalid.",
                )
            release = root / "releases" / pointer_identity
            if release.is_symlink() or not release.is_dir():
                raise UpdateError(
                    "INVALID_INSTALLED_RELEASE",
                    f"Native {component_id} target release is missing or unsafe.",
                )
            manifest = _read_object(
                release / "component-manifest.json", f"{component_id} target release manifest"
            )
            expected_digest = (
                "sha256:" + pointer_digest
                if pointer_digest is not None
                else manifest.get("manifestDigest")
            )
            self._validate_manifest_digest(manifest, expected_digest)
            if manifest.get("componentId") != component_id or manifest.get("version") != version:
                raise UpdateError(
                    "INVALID_INSTALLED_RELEASE",
                    f"Native {component_id} pointer differs from its target manifest.",
                )
        temporary = root / f".active-{uuid.uuid4().hex}"
        try:
            if pointer_identity is None:
                self._remove_symlink_if_target(
                    active, f"releases/{current}" if current is not None else ""
                )
                return
            os.symlink(f"releases/{pointer_identity}", temporary)
            os.replace(temporary, active)
            self._fsync_directory(root)
        finally:
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()

    def _active_native_pointer_identity(self, component_id: str) -> str | None:
        active = self.install_root / "components" / component_id / "active"
        if not active.exists() and not active.is_symlink():
            return None
        if not active.is_symlink():
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE", f"Active pointer is not a symlink for {component_id}."
            )
        match = re.fullmatch(r"releases/([^/]+)", os.readlink(active))
        if match is None:
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Active pointer has an unsafe target for {component_id}.",
            )
        version, pointer_digest = _native_release_directory_identity(match.group(1))
        if VERSION_PATTERN.fullmatch(version) is None or (
            pointer_digest is not None and len(pointer_digest) != 64
        ):
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Active pointer identity is unsafe for {component_id}.",
            )
        return match.group(1)

    def _restart_order(self, transaction: dict[str, Any]) -> list[str]:
        restart_items: dict[str, tuple[int, int, str]] = {}
        if transaction["targetKind"] == "CORE_RUNTIME":
            for component in self.components.values():
                restart = component.get("restart", {})
                unit = restart.get("unit")
                if restart.get("group") == "core-runtime" and unit and self._unit_exists(component):
                    restart_items[unit] = (0, restart.get("order", 0), component["componentId"])
        for staged in transaction["components"]:
            component = self.components[staged["componentId"]]
            restart = component.get("restart", {})
            unit = restart.get("unit")
            if unit and restart.get("group") == "single-service":
                restart_items.setdefault(unit, (1, 0, component["componentId"]))
        return [unit for unit, _ in sorted(restart_items.items(), key=lambda item: item[1])]

    def _unit_exists(self, component: dict[str, Any]) -> bool:
        unit = self._catalog_matched_unit(component)
        if unit is None:
            return False
        for directory in self.systemd_unit_dirs:
            path = directory / unit
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                return False
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                return False
            return component.get("kind") != "native-binary" or self._unit_uses_component_runner(
                path, component["componentId"]
            )
        return False

    @staticmethod
    def _catalog_matched_unit(component: dict[str, Any]) -> str | None:
        unit = component.get("systemdUnit")
        restart = component.get("restart")
        restart_unit = restart.get("unit") if isinstance(restart, dict) else None
        if (
            not isinstance(unit, str)
            or unit != restart_unit
            or re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", unit) is None
        ):
            return None
        return unit

    @staticmethod
    def _unit_uses_component_runner(path: Path, component_id: str) -> bool:
        try:
            contents = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        for line in contents.splitlines():
            stripped = line.strip()
            if not stripped.startswith("ExecStart="):
                continue
            try:
                arguments = shlex.split(stripped.removeprefix("ExecStart="))
            except ValueError:
                return False
            if arguments[:3] == ["/usr/bin/cyrene", "component-run", component_id]:
                return True
        return False

    def _require_managed_services(self, staged_components: list[dict[str, Any]]) -> None:
        """Refuse activation before the gate when a selected service has no trusted host unit."""

        for staged in staged_components:
            component = self.components.get(staged.get("componentId"))
            if component is None or self._target_for(component) is None:
                raise UpdateError(
                    "SERVICE_NOT_MANAGED",
                    "The staged component has no supported, catalog-matched local service.",
                )
            if component.get("kind") == "data-bundle":
                if not self._authority_available():
                    raise UpdateError(
                        "AUTHORITY_NOT_MANAGED",
                        "Authority's restricted local activation socket is unavailable; data bundle remains staged.",
                    )
                continue
            if not self._unit_exists(component):
                raise UpdateError(
                    "SERVICE_NOT_MANAGED",
                    f"The OS target is available, but {component['componentId']} has no catalog-matched local systemd service; the plan remains staged.",
                )

    def _restart_transaction(self, transaction: dict[str, Any]) -> None:
        units = self._restart_order(transaction)
        for unit in units:
            self._run_systemctl("restart", unit)
            self._wait_unit_active(unit)

    def _health_transaction(self, transaction: dict[str, Any]) -> None:
        for item in transaction["components"]:
            manifest = item["manifest"]
            health = manifest.get("health")
            if not health:
                continue
            component = self.components[item["componentId"]]
            unit = component.get("systemdUnit") or component.get("restart", {}).get("unit")
            if health.get("kind") == "systemd-active" and unit:
                self._wait_unit_active(unit)
            elif health.get("kind") == "http":
                self._wait_http_health(health["port"], health["path"], item["componentId"])

    def _rollback_transaction(self, transaction: dict[str, Any]) -> tuple[bool, str]:
        try:
            for previous in transaction["previous"]:
                component = self.components[previous["componentId"]]
                if component.get("kind") == "data-bundle":
                    continue
                candidate = next(
                    item
                    for item in transaction["components"]
                    if item["componentId"] == previous["componentId"]
                )
                if component.get("pythonBundleService"):
                    active = (
                        self.install_root / "services" / component["pythonBundleService"] / "active"
                    )
                    current = self._active_python_pointer_identity(component["pythonBundleService"])
                    allowed_current = {
                        previous.get("bundleIdentity"),
                        candidate.get("bundleIdentity"),
                    }
                    if current not in allowed_current:
                        raise UpdateError(
                            "ROLLBACK_CONFLICT",
                            f"Active {previous['componentId']} bundle pointer is outside the transaction identities.",
                        )
                    expected_current = current
                    bundle_identity = previous.get("bundleIdentity")
                    if bundle_identity is None:
                        if current is not None:
                            self._remove_symlink_if_target(active, f"releases/{current}")
                    else:
                        self._load_service_bundle().activate_release(
                            component["pythonBundleService"],
                            bundle_identity,
                            install_root=self.install_root,
                            expected_current_version=expected_current,
                        )
                    self._restore_active_receipt(previous["componentId"], previous)
                else:
                    current = self._active_native_pointer_identity(previous["componentId"])
                    candidate_pointer = _native_release_pointer_identity(candidate)
                    if current not in {previous.get("pointerIdentity"), candidate_pointer}:
                        raise UpdateError(
                            "ROLLBACK_CONFLICT",
                            f"Active {previous['componentId']} native pointer is outside the transaction identities.",
                        )
                    self._activate_native(
                        previous["componentId"],
                        previous.get("pointerIdentity"),
                        expected_current=current,
                    )
                    self._restore_active_receipt(previous["componentId"], previous)
            self._restart_transaction(transaction)
            self._health_transaction(
                {
                    **transaction,
                    "components": [
                        {
                            **item,
                            "manifest": self._installed(self.components[item["componentId"]]).get(
                                "manifest"
                            )
                            or item["manifest"],
                        }
                        for item in transaction["components"]
                    ],
                }
            )
            self._rollback_authority_activation(transaction)
            return (
                True,
                "Prior active versions were restored and health-checked; gate may be released.",
            )
        except Exception as error:  # noqa: BLE001 - rollback must retain the gate for every unexpected failure.
            return False, f"Rollback could not be verified: {error}; maintenance gate remains held."

    def _active_python_pointer_identity(self, service: str) -> str | None:
        try:
            active = self._load_service_bundle().resolve_active_release(
                service, install_root=self.install_root
            )
            return active[1] if active is not None else None
        except Exception as error:
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Active {service} bundle pointer failed integrity validation: {error}",
            ) from error

    def _remove_symlink_if_target(self, path: Path, target: str) -> None:
        if path.is_symlink() and os.readlink(path) == target:
            path.unlink()
            self._fsync_directory(path.parent)
        elif path.exists() or path.is_symlink():
            raise UpdateError(
                "ROLLBACK_CONFLICT", f"Active pointer changed unexpectedly at {path}."
            )

    def _recover_transaction(
        self,
        transaction: dict[str, Any],
        transaction_path: Path,
        stage_path: Path,
        confirmation: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            transaction.get("planDigest") != confirmation["planDigest"]
            or transaction.get("planId") != confirmation["planId"]
        ):
            raise UpdateError(
                "TRANSACTION_MISMATCH",
                "An unresolved transaction exists with a different confirmed plan.",
            )
        self._validate_recovery_identity(transaction)
        if transaction.get("phase") == "succeeded":
            return self._applied_result(transaction)
        if transaction.get("phase") == "rolled_back":
            raise UpdateError(
                "APPLY_ROLLED_BACK",
                transaction.get("rollbackMessage", "Previous attempt rolled back."),
            )
        if transaction.get("phase") in {"success_end_pending", "rollback_end_pending"}:
            return self._recover_end_pending(transaction, transaction_path)
        if transaction.get("phase") == "begin_pending":
            try:
                transaction["maintenanceToken"] = self._begin_maintenance(transaction)
            except UpdateError as error:
                if error.maintenance_not_acquired:
                    self._clear_begin_pending(transaction, transaction_path)
                raise
            transaction["phase"] = "applying"
            _atomic_json(transaction_path, transaction)
        healthy, message = self._rollback_transaction(transaction)
        try:
            self._end_maintenance(
                transaction, outcome="ROLLED_BACK" if healthy else "FAILED", healthy=healthy
            )
        except UpdateError as error:
            transaction["phase"] = "rollback_required"
            transaction["recoveryError"] = str(error)
            _atomic_json(transaction_path, transaction)
            raise UpdateError(
                "ROLLBACK_GATE_HELD",
                f"Interrupted update recovery could not release maintenance: {error}",
                retryable=True,
            ) from error
        transaction["phase"] = "rolled_back" if healthy else "rollback_required"
        transaction["rollbackMessage"] = message
        transaction.pop("maintenanceToken", None)
        _atomic_json(transaction_path, transaction)
        if not healthy:
            raise UpdateError("ROLLBACK_UNHEALTHY", message, retryable=True)
        raise UpdateError(
            "INTERRUPTED_UPDATE_ROLLED_BACK",
            f"Interrupted transaction was rolled back. {message}; run check and stage again before retrying.",
        )

    def _validate_recovery_identity(self, transaction: dict[str, Any]) -> None:
        """Fail held when an unfinished journal lacks exact release identities."""

        components = transaction.get("components")
        previous = transaction.get("previous")
        if (
            transaction.get("schemaVersion") != 2
            or not isinstance(components, list)
            or not components
            or not isinstance(previous, list)
        ):
            raise UpdateError(
                "TRANSACTION_IDENTITY_UNKNOWN",
                "An unfinished legacy update journal has no verified pointer identities; maintenance remains held for operator recovery.",
                retryable=True,
            )
        component_ids: set[str] = set()
        for item in components:
            if not isinstance(item, dict) or not isinstance(item.get("componentId"), str):
                raise UpdateError(
                    "TRANSACTION_IDENTITY_UNKNOWN",
                    "Update journal contains an invalid candidate component identity.",
                    retryable=True,
                )
            component_id = item["componentId"]
            component = self.components.get(component_id)
            if (
                component is None
                or component_id in component_ids
                or not _valid_digest(item.get("manifestDigest"))
                or item.get("releaseIdentity") != item.get("manifestDigest")
                or not _valid_digest(item.get("artifactDigest"))
                or not isinstance(item.get("version"), str)
                or VERSION_PATTERN.fullmatch(item["version"]) is None
            ):
                raise UpdateError(
                    "TRANSACTION_IDENTITY_UNKNOWN",
                    f"Update journal candidate identity is incomplete for {component_id}.",
                    retryable=True,
                )
            component_ids.add(component_id)
            if component.get("kind") == "data-bundle":
                artifact_id = item.get("artifactDigest")
                if (
                    not _valid_digest(artifact_id)
                    or item.get("bundleIdentity") != artifact_id
                    or item.get("pointerIdentity") != artifact_id.removeprefix("sha256:")
                    or not isinstance(item.get("dataBundle"), dict)
                    or item["dataBundle"].get("artifactId") != artifact_id
                ):
                    raise UpdateError(
                        "TRANSACTION_IDENTITY_UNKNOWN",
                        f"Update journal data-bundle identity is incomplete for {component_id}.",
                        retryable=True,
                    )
            elif component.get("pythonBundleService"):
                bundle_identity = item.get("bundleIdentity")
                if (
                    not isinstance(bundle_identity, str)
                    or BUNDLE_ID_PATTERN.fullmatch(bundle_identity) is None
                    or item.get("pointerIdentity") != bundle_identity
                ):
                    raise UpdateError(
                        "TRANSACTION_IDENTITY_UNKNOWN",
                        f"Update journal Python pointer identity is incomplete for {component_id}.",
                        retryable=True,
                    )
            else:
                expected_pointer = (
                    f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
                )
                if (
                    item.get("bundleIdentity") is not None
                    or item.get("pointerIdentity") != expected_pointer
                ):
                    raise UpdateError(
                        "TRANSACTION_IDENTITY_UNKNOWN",
                        f"Update journal native pointer identity is incomplete for {component_id}.",
                        retryable=True,
                    )

        previous_ids: set[str] = set()
        previous_fields = {
            "componentId",
            "version",
            "releaseIdentity",
            "manifestDigest",
            "artifactDigest",
            "pointerIdentity",
            "bundleIdentity",
            "identityAttested",
        }
        for item in previous:
            if not isinstance(item, dict) or set(item) != previous_fields:
                raise UpdateError(
                    "TRANSACTION_IDENTITY_UNKNOWN",
                    "Update journal has an incomplete prior release identity; maintenance remains held.",
                    retryable=True,
                )
            component_id = item["componentId"]
            component = self.components.get(component_id)
            if (
                component_id not in component_ids
                or component is None
                or component_id in previous_ids
                or not isinstance(item.get("identityAttested"), bool)
            ):
                raise UpdateError(
                    "TRANSACTION_IDENTITY_UNKNOWN",
                    "Update journal prior component set or identity state is invalid.",
                    retryable=True,
                )
            previous_ids.add(component_id)
            if component.get("kind") == "data-bundle":
                if item.get("version") is None:
                    if any(
                        item.get(key) is not None
                        for key in (
                            "releaseIdentity",
                            "manifestDigest",
                            "artifactDigest",
                            "pointerIdentity",
                            "bundleIdentity",
                        )
                    ):
                        raise UpdateError(
                            "TRANSACTION_IDENTITY_UNKNOWN",
                            "Empty Authority snapshot identity has stray fields.",
                            retryable=True,
                        )
                elif item.get("releaseIdentity") is None:
                    artifact_id = item.get("artifactDigest")
                    if (
                        item.get("version") != "0.0.0"
                        or item.get("manifestDigest") is not None
                        or not _valid_digest(artifact_id)
                        or item.get("pointerIdentity") != artifact_id.removeprefix("sha256:")
                        or item.get("bundleIdentity") != artifact_id
                        or item.get("identityAttested") is not True
                    ):
                        raise UpdateError(
                            "TRANSACTION_IDENTITY_UNKNOWN",
                            "Prior Authority snapshot identity is invalid.",
                            retryable=True,
                        )
                elif (
                    not isinstance(item.get("version"), str)
                    or VERSION_PATTERN.fullmatch(item["version"]) is None
                    or not _valid_digest(item.get("releaseIdentity"))
                    or item.get("manifestDigest") != item.get("releaseIdentity")
                    or not _valid_digest(item.get("artifactDigest"))
                    or item.get("pointerIdentity") != item["artifactDigest"].removeprefix("sha256:")
                    or item.get("bundleIdentity") != item.get("artifactDigest")
                ):
                    raise UpdateError(
                        "TRANSACTION_IDENTITY_UNKNOWN",
                        "Prior Authority artifact identity is invalid.",
                        retryable=True,
                    )
                continue
            if item.get("version") is None:
                if any(
                    item.get(key) is not None
                    for key in (
                        "releaseIdentity",
                        "manifestDigest",
                        "artifactDigest",
                        "pointerIdentity",
                        "bundleIdentity",
                    )
                ):
                    raise UpdateError(
                        "TRANSACTION_IDENTITY_UNKNOWN",
                        f"Empty prior state has stray release identity for {component_id}.",
                        retryable=True,
                    )
                continue
            if (
                not isinstance(item.get("version"), str)
                or VERSION_PATTERN.fullmatch(item["version"]) is None
            ):
                raise UpdateError(
                    "TRANSACTION_IDENTITY_UNKNOWN",
                    f"Prior release version is invalid for {component_id}.",
                    retryable=True,
                )
            if component.get("pythonBundleService"):
                bundle_identity = item.get("bundleIdentity")
                if (
                    not isinstance(bundle_identity, str)
                    or BUNDLE_ID_PATTERN.fullmatch(bundle_identity) is None
                    or item.get("pointerIdentity") != bundle_identity
                ):
                    raise UpdateError(
                        "TRANSACTION_IDENTITY_UNKNOWN",
                        f"Prior Python pointer identity is incomplete for {component_id}.",
                        retryable=True,
                    )
            else:
                pointer = item.get("pointerIdentity")
                version, digest = (
                    _native_release_directory_identity(pointer)
                    if isinstance(pointer, str)
                    else ("", None)
                )
                if (
                    pointer is None
                    or version != item["version"]
                    or (digest is not None and item.get("manifestDigest") != "sha256:" + digest)
                ):
                    raise UpdateError(
                        "TRANSACTION_IDENTITY_UNKNOWN",
                        f"Prior native pointer identity is incomplete for {component_id}.",
                        retryable=True,
                    )
            if item["identityAttested"] and (
                not _valid_digest(item.get("releaseIdentity"))
                or item.get("manifestDigest") != item.get("releaseIdentity")
                or not _valid_digest(item.get("artifactDigest"))
            ):
                raise UpdateError(
                    "TRANSACTION_IDENTITY_UNKNOWN",
                    f"Prior verified receipt identity is incomplete for {component_id}.",
                    retryable=True,
                )
        if previous_ids != component_ids:
            raise UpdateError(
                "TRANSACTION_IDENTITY_UNKNOWN",
                "Update journal prior component set differs from the candidate set.",
                retryable=True,
            )

    def _recover_end_pending(
        self, transaction: dict[str, Any], transaction_path: Path
    ) -> dict[str, Any]:
        """Reconcile a durable EndMaintenance request without repeating a restart."""

        phase = transaction["phase"]
        readiness = self._readiness_for(
            transaction["targetKind"], requires_restart=True, force=True
        )
        gate_status = readiness.get("status")
        if gate_status not in {"READY", "MAINTENANCE_ACTIVE"}:
            raise UpdateError(
                "GATE_UNKNOWN",
                f"Cannot reconcile the durable maintenance completion while broker state is {gate_status!r}; journal and gate remain held.",
                retryable=True,
            )

        if phase == "success_end_pending":
            expected = {item["componentId"]: item for item in transaction["components"]}
            current = {
                item["componentId"]: item
                for item in self._capture_active_versions(transaction["components"])
            }
            if not self._same_release_identities(expected, current, require_attested=True):
                raise UpdateError(
                    "TRANSACTION_STATE_MISMATCH",
                    "Active releases differ from the healthy plan recorded before EndMaintenance; refusing to change service state during recovery.",
                    retryable=True,
                )
            self._health_transaction(transaction)
            if gate_status == "MAINTENANCE_ACTIVE":
                self._end_maintenance(transaction, outcome="SUCCESS", healthy=True)
            transaction["phase"] = "succeeded"
            transaction.pop("maintenanceToken", None)
            transaction.pop("recoveryError", None)
            _atomic_json(transaction_path, transaction)
            return self._applied_result(transaction)

        expected_previous = {item["componentId"]: item for item in transaction.get("previous", [])}
        current_previous = {
            item["componentId"]: item
            for item in self._capture_active_versions(transaction["components"])
        }
        if not self._same_release_identities(
            expected_previous, current_previous, require_attested=False
        ):
            raise UpdateError(
                "TRANSACTION_STATE_MISMATCH",
                "Restored releases differ from the rollback identities recorded before EndMaintenance; refusing to release the maintenance gate.",
                retryable=True,
            )
        if gate_status == "MAINTENANCE_ACTIVE":
            self._health_rollback_state(transaction)
            self._end_maintenance(transaction, outcome="ROLLED_BACK", healthy=True)
        else:
            self._health_rollback_state(transaction)
        transaction["phase"] = "rolled_back"
        transaction.pop("maintenanceToken", None)
        transaction.pop("recoveryError", None)
        _atomic_json(transaction_path, transaction)
        raise UpdateError(
            "INTERRUPTED_UPDATE_ROLLED_BACK",
            f"Interrupted update was rolled back and its maintenance completion was reconciled. {transaction.get('rollbackMessage', '')}",
        )

    def _health_rollback_state(self, transaction: dict[str, Any]) -> None:
        restored = []
        for item in transaction["components"]:
            installed = self._installed(self.components[item["componentId"]])
            if installed.get("manifest") is not None:
                restored.append({**item, "manifest": installed["manifest"]})
        if restored:
            self._health_transaction({**transaction, "components": restored})

    @staticmethod
    def _same_release_identities(
        expected: dict[str, dict[str, Any]],
        current: dict[str, dict[str, Any]],
        *,
        require_attested: bool,
    ) -> bool:
        if set(expected) != set(current):
            return False
        fields = (
            "version",
            "releaseIdentity",
            "manifestDigest",
            "artifactDigest",
            "pointerIdentity",
            "bundleIdentity",
        )
        for component_id, item in expected.items():
            active = current[component_id]
            if any(item.get(field) != active.get(field) for field in fields):
                return False
            if require_attested and active.get("identityAttested") is not True:
                return False
        return True

    def _applied_result(self, transaction: dict[str, Any]) -> dict[str, Any]:
        plan = {
            "planId": transaction["planId"],
            "planDigest": transaction["planDigest"],
            "channel": transaction.get("channel", DEFAULT_CHANNEL),
            "phase": "succeeded",
            "components": [
                {
                    "componentId": item["componentId"],
                    "version": item["version"],
                    "manifestDigest": item["manifestDigest"],
                    "artifactDigest": item["artifactDigest"],
                    "restartGroup": item["restartGroup"],
                }
                for item in transaction["components"]
            ],
        }
        rows = [
            self._result_component(
                self.components[item["componentId"]],
                self._readiness(self.components[item["componentId"]]),
                phase="current",
                update_available=False,
            )
            for item in transaction["components"]
        ]
        return {"status": "applied", "components": rows, "plan": plan, "plans": [plan]}

    def _staged_plans(self) -> list[dict[str, Any]]:
        root = self.state_root / "staged"
        if not root.is_dir() or root.is_symlink():
            return []
        plans = []
        for child in sorted(root.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            path = child / "stage.json"
            if not path.is_file() or path.is_symlink():
                continue
            record = _read_object(path, "staged update plan")
            if record.get("phase") == "staged" and isinstance(record.get("plan"), dict):
                plans.append({**record["plan"], "phase": "staged"})
        return plans

    def _get_json(self, uri: str) -> Any:
        try:
            return json.loads(self._get_bytes(uri).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError(
                "INVALID_HTTP_JSON", f"Response from {uri} is not valid JSON.", retryable=True
            ) from error

    def _get_bytes(self, uri: str) -> bytes:
        if not isinstance(uri, str) or urllib.parse.urlsplit(uri).scheme != "https":
            raise UpdateError("UNTRUSTED_URI", "Release assets must use HTTPS.")
        request = urllib.request.Request(
            uri,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, application/octet-stream",
            },
        )
        try:
            with self.opener(request, timeout=30) as response:
                final_host = urllib.parse.urlsplit(response.geturl()).hostname or ""
                if final_host not in {"github.com", "api.github.com"} and not final_host.endswith(
                    ".githubusercontent.com"
                ):
                    raise UpdateError(
                        "UNTRUSTED_REDIRECT",
                        f"GitHub asset redirected to an untrusted host: {final_host}.",
                    )
                data = response.read(2_000_000_001)
                if len(data) > 2_000_000_000:
                    raise UpdateError(
                        "ARTIFACT_TOO_LARGE", "Release asset exceeds the 2 GB safety limit."
                    )
                return data
        except UpdateError:
            raise
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            raise UpdateError(
                "NETWORK_ERROR", f"Cannot download trusted release asset: {error}", retryable=True
            ) from error

    @staticmethod
    def _require_github_asset_uri(uri: Any, repository: str) -> None:
        if not isinstance(uri, str):
            raise UpdateError("UNTRUSTED_URI", "Release index contains a non-string asset URI.")
        parts = urllib.parse.urlsplit(uri)
        if (
            parts.scheme != "https"
            or parts.hostname != "github.com"
            or not parts.path.startswith(f"/{repository}/releases/download/")
        ):
            raise UpdateError(
                "UNTRUSTED_URI", f"Release asset URI is outside the pinned repository {repository}."
            )

    def _verify_attestation(
        self,
        payload: bytes,
        *,
        subject_name: str,
        digest: str,
        repository: str,
        workflow: str,
        source_ref: str,
        source_commit: str,
        bundle_path: Path | None = None,
    ) -> None:
        if Path(repository).name == "" or not _valid_digest(digest):
            raise UpdateError("INVALID_ATTESTATION", "Attestation subject identity is invalid.")
        with tempfile.NamedTemporaryFile(
            prefix="cyrene-attest-", suffix="-" + Path(subject_name).name, delete=False
        ) as stream:
            stream.write(payload)
            temporary_path = Path(stream.name)
        try:
            if shutil.which("gh") is None:
                raise UpdateError(
                    "ATTESTATION_VERIFIER_MISSING",
                    "GitHub CLI (`gh`) is required to verify artifact attestations.",
                )
            arguments = [
                "gh",
                "attestation",
                "verify",
                str(temporary_path),
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
            if bundle_path is not None:
                if bundle_path.is_symlink() or not bundle_path.is_file():
                    raise UpdateError("INVALID_ATTESTATION", "Attestation bundle path is unsafe.")
                arguments.extend(["--bundle", str(bundle_path)])
            completed = self.runner(
                arguments,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if completed.returncode != 0:
                raise UpdateError(
                    "ATTESTATION_INVALID",
                    f"GitHub artifact attestation verification failed: {completed.stderr.strip() or completed.stdout.strip()}",
                )
            try:
                verification = json.loads(completed.stdout)
            except json.JSONDecodeError as error:
                raise UpdateError(
                    "ATTESTATION_INVALID", "GitHub CLI returned malformed attestation JSON."
                ) from error
            if not isinstance(verification, list):
                raise UpdateError(
                    "ATTESTATION_INVALID",
                    "GitHub CLI returned an unexpected verification result shape.",
                )
            raw_digest = digest.split(":", 1)[1]
            matched_subject = False
            for result in verification:
                if not isinstance(result, dict):
                    continue
                verification_result = result.get("verificationResult")
                statement = (
                    verification_result.get("statement")
                    if isinstance(verification_result, dict)
                    else None
                )
                if (
                    not isinstance(statement, dict)
                    or statement.get("predicateType") != "https://slsa.dev/provenance/v1"
                ):
                    continue
                subjects = statement.get("subject")
                if not isinstance(subjects, list):
                    continue
                if any(
                    isinstance(subject, dict)
                    and subject.get("name") == subject_name
                    and isinstance(subject.get("digest"), dict)
                    and subject["digest"].get("sha256") == raw_digest
                    for subject in subjects
                ):
                    matched_subject = True
                    break
            if not matched_subject:
                raise UpdateError(
                    "ATTESTATION_SUBJECT_MISMATCH",
                    "Verified SLSA statement does not name the expected subject and SHA-256 digest.",
                )
        except subprocess.TimeoutExpired as error:
            raise UpdateError(
                "ATTESTATION_TIMEOUT", "GitHub attestation verification timed out.", retryable=True
            ) from error
        finally:
            temporary_path.unlink(missing_ok=True)

    def _run_systemctl(self, operation: str, unit: str) -> None:
        try:
            completed = self.runner(
                ["systemctl", operation, unit],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UpdateError("SYSTEMD_FAILED", f"Cannot {operation} {unit}: {error}") from error
        if completed.returncode != 0:
            raise UpdateError(
                "SYSTEMD_FAILED",
                f"systemctl {operation} {unit} failed: {completed.stderr.strip() or completed.stdout.strip()}",
            )

    def _wait_unit_active(self, unit: str) -> None:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                completed = self.runner(
                    ["systemctl", "is-active", "--quiet", unit],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                completed = None
            if completed is not None and completed.returncode == 0:
                return
            time.sleep(1)
        raise UpdateError("HEALTH_CHECK_FAILED", f"{unit} did not become active within 90 seconds.")

    def _wait_http_health(self, port: int, path: str, component_id: str) -> None:
        deadline = time.monotonic() + 90
        url = f"http://127.0.0.1:{port}{path}"
        while time.monotonic() < deadline:
            try:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=2) as response:
                    if 200 <= response.status < 400:
                        return
            except (OSError, urllib.error.URLError, TimeoutError):
                time.sleep(1)
        raise UpdateError(
            "HEALTH_CHECK_FAILED",
            f"{component_id} did not pass its health endpoint {url} within 90 seconds.",
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def run_json_stdio(
    updater: ComponentUpdater, input_stream: Any = sys.stdin, output_stream: Any = sys.stdout
) -> int:
    """Read exactly one fixed JSON request and emit exactly one JSON envelope."""

    line = input_stream.readline(1_048_577)
    if not line or len(line) > 1_048_576 or input_stream.readline(1):
        envelope = {
            "protocolVersion": PROTOCOL_VERSION,
            "ok": False,
            "operation": "status",
            "error": {
                "code": "INVALID_REQUEST",
                "message": "Expected exactly one JSON request line (maximum 1 MiB).",
                "retryable": False,
            },
        }
    else:
        try:
            request = json.loads(line)
            envelope = updater.handle(request)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            envelope = {
                "protocolVersion": PROTOCOL_VERSION,
                "ok": False,
                "operation": "status",
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": f"Request must be valid UTF-8 JSON: {error}",
                    "retryable": False,
                },
            }
    output_stream.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n")
    output_stream.flush()
    return 0


def _validate_startup_arguments(arguments: Any, *, field: str) -> list[str]:
    """Validate fixed manifest or root-owned unit arguments before exec."""

    if not isinstance(arguments, (list, tuple)) or len(arguments) > 64:
        raise UpdateError("INVALID_STARTUP_ARGUMENTS", f"{field} must contain at most 64 strings.")
    validated: list[str] = []
    for argument in arguments:
        if (
            not isinstance(argument, str)
            or not argument
            or "\x00" in argument
            or len(argument.encode("utf-8")) > 512
        ):
            raise UpdateError("INVALID_STARTUP_ARGUMENTS", f"{field} contains an invalid argument.")
        validated.append(argument)
    return validated


def run_component(
    component_id: str,
    *,
    install_root: Path = DEFAULT_INSTALL_ROOT,
    startup_arguments: list[str] | tuple[str, ...] = (),
) -> int:
    """Execute the trusted active native release selected for one fixed component ID."""

    if COMPONENT_ID_PATTERN.fullmatch(component_id) is None:
        raise UpdateError("INVALID_COMPONENT", "Component ID is invalid.")
    root = install_root / "components" / component_id
    directories = (install_root, install_root / "components", root, root / "releases")
    for directory in directories:
        try:
            info = directory.lstat()
        except OSError as error:
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Active release directory is missing for {component_id}: {directory}.",
            ) from error
        if (
            not stat.S_ISDIR(info.st_mode)
            or directory.is_symlink()
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
            or not stat.S_IMODE(info.st_mode) & 0o001
            or not os.access(directory, os.X_OK)
        ):
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Active release directory is not root-owned, immutable, and traversable for {component_id}: {directory}.",
            )
    active = root / "active"
    if not active.is_symlink():
        raise UpdateError(
            "COMPONENT_NOT_INSTALLED",
            f"No active immutable release is installed for {component_id}.",
        )
    active_info = active.lstat()
    if active_info.st_uid != 0:
        raise UpdateError(
            "INVALID_INSTALLED_RELEASE", f"Active pointer is not root-owned for {component_id}."
        )
    match = re.fullmatch(r"releases/([^/]+)", os.readlink(active))
    if match is None:
        raise UpdateError(
            "INVALID_INSTALLED_RELEASE", f"Active release pointer is invalid for {component_id}."
        )
    pointer_identity = match.group(1)
    version, pointer_digest = _native_release_directory_identity(pointer_identity)
    if VERSION_PATTERN.fullmatch(version) is None:
        raise UpdateError(
            "INVALID_INSTALLED_RELEASE", f"Active release pointer is invalid for {component_id}."
        )
    release = root / "releases" / pointer_identity
    if release.is_symlink() or not release.is_dir():
        raise UpdateError(
            "INVALID_INSTALLED_RELEASE", f"Active release directory is unsafe for {component_id}."
        )
    release_info = release.lstat()
    if (
        release_info.st_uid != 0
        or stat.S_IMODE(release_info.st_mode) & 0o022
        or not stat.S_IMODE(release_info.st_mode) & 0o001
        or not os.access(release, os.X_OK)
    ):
        raise UpdateError(
            "INVALID_INSTALLED_RELEASE",
            f"Active release directory is not root-controlled and traversable for {component_id}.",
        )
    manifest_path = release / "component-manifest.json"
    if manifest_path.is_symlink():
        raise UpdateError(
            "INVALID_INSTALLED_RELEASE", f"Active release manifest is a symlink for {component_id}."
        )
    manifest_info = manifest_path.lstat()
    if (
        not stat.S_ISREG(manifest_info.st_mode)
        or manifest_info.st_uid != 0
        or stat.S_IMODE(manifest_info.st_mode) & 0o022
        or not stat.S_IMODE(manifest_info.st_mode) & 0o004
        or not os.access(manifest_path, os.R_OK)
    ):
        raise UpdateError(
            "INVALID_INSTALLED_RELEASE",
            f"Active release manifest is not root-owned and readable for {component_id}.",
        )
    manifest = _read_object(manifest_path, f"{component_id} release manifest")
    if manifest.get("manifestDigest") != _digest_json(manifest, "manifestDigest"):
        raise UpdateError(
            "INVALID_INSTALLED_RELEASE",
            f"Active release manifest digest is invalid for {component_id}.",
        )
    if (
        manifest.get("componentId") != component_id
        or manifest.get("version") != version
        or (
            pointer_digest is not None
            and manifest.get("manifestDigest") != "sha256:" + pointer_digest
        )
        or (pointer_digest is None and pointer_identity != version)
    ):
        raise UpdateError(
            "INVALID_INSTALLED_RELEASE",
            f"Active release pointer does not match its manifest for {component_id}.",
        )
    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("kind") != "native-binary":
        raise UpdateError(
            "INVALID_INSTALLED_RELEASE",
            f"Active release is not a native binary for {component_id}.",
        )
    relative = _safe_relative(artifact.get("entrypoint"), field="artifact.entrypoint")
    binary = release.joinpath(*relative.parts)
    if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise UpdateError(
            "INVALID_INSTALLED_RELEASE",
            f"Active entrypoint is missing or not executable for {component_id}.",
        )
    arguments = artifact.get("arguments", [])
    manifest_arguments = _validate_startup_arguments(
        arguments, field=f"{component_id} artifact.arguments"
    )
    unit_arguments = _validate_startup_arguments(
        startup_arguments, field="trusted systemd startup arguments"
    )
    environment = os.environ.copy()
    for variable in PRODUCT_CONTRACT_ROOT_ENV.values():
        environment.pop(variable, None)
    contract_env = PRODUCT_CONTRACT_ROOT_ENV.get(component_id)
    if contract_env is not None:
        contract_root = release / "share" / "cyrene" / "product-contracts"
        if contract_root.is_symlink() or not contract_root.is_dir():
            raise UpdateError(
                "INVALID_INSTALLED_RELEASE",
                f"Active release is missing its pinned Product contract directory for {component_id}.",
            )
        environment[contract_env] = str(contract_root)
    os.execve(str(binary), [str(binary), *manifest_arguments, *unit_arguments], environment)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Development entrypoint for standalone component update tests."""

    import argparse

    parser = argparse.ArgumentParser(prog="cyrene component-updates")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--activity-catalog", type=Path, default=DEFAULT_ACTIVITY_CATALOG)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--broker", type=Path)
    parser.add_argument("--install-root", type=Path, default=DEFAULT_INSTALL_ROOT)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    args = parser.parse_args(argv)
    updater = ComponentUpdater(
        catalog_path=args.catalog,
        activity_catalog_path=args.activity_catalog,
        socket_path=args.socket,
        broker_path=args.broker,
        install_root=args.install_root,
        state_root=args.state_root,
    )
    return run_json_stdio(updater)


if __name__ == "__main__":
    raise SystemExit(main())
