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
COMPONENT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
DIGEST_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")
BUNDLE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
SEMVER3_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
PLAN_ID_PATTERN = re.compile(r"^plan-[0-9a-f]{32}$")
MAX_SAFE_INTEGER = 9_007_199_254_740_991
INSTALLED_CATALOG = Path("/usr/share/cyrene/component-catalog-v1.json")
DEFAULT_CATALOG = (
    INSTALLED_CATALOG
    if INSTALLED_CATALOG.is_file()
    else Path(__file__).resolve().parents[1] / "governance" / "component-catalog-v1.json"
)
DEFAULT_ACTIVITY_CATALOG = Path("/var/lib/cyrene/runtime/activity-sources.json")
DEFAULT_SOCKET = Path("/run/cyrene/runtime-maintenance.sock")
DEFAULT_BROKER = Path("/usr/bin/cyrene-runtime-maintenance")
DEFAULT_INSTALL_ROOT = Path("/usr/lib/cyrene")
DEFAULT_STATE_ROOT = Path("/var/lib/cyrene-updates")
DEFAULT_CHANNEL = "stable"
TRUSTED_CATALOG_DIGEST = "sha256:248a9a3b27f3d1daa4c0a6fdc405c612fd492483bb4157ff46b6d2836ffd0d35"
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
            raise UpdateError("INVALID_JSON", "Component JSON must not contain floating-point values.")
        if isinstance(item, str):
            return _jcs_string(item)
        if isinstance(item, list):
            return "[" + ",".join(encode(value) for value in item) + "]"
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise UpdateError("INVALID_JSON", "Canonical JSON object keys must be strings.")
            keys = sorted(item, key=lambda key: key.encode("utf-16be"))
            return "{" + ",".join(_jcs_string(key) + ":" + encode(item[key]) for key in keys) + "}"
        raise UpdateError("INVALID_JSON", f"Unsupported canonical JSON value: {type(item).__name__}.")

    return encode(value).encode("utf-8")


def _digest_json(value: dict[str, Any], field: str) -> str:
    material = dict(value)
    material.pop(field, None)
    return "sha256:" + hashlib.sha256(canonical_jcs(material)).hexdigest()


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and DIGEST_PATTERN.fullmatch(value) is not None


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
        raise UpdateError("INVALID_TRANSACTION", "Maintenance requestId does not match the confirmed plan.")
    transaction["requestId"] = request_id
    return request_id


def _parse_supported_version_range(value: Any) -> tuple[str, tuple[int, int, int], tuple[int, int, int] | None]:
    """Parse the deliberately small SemVer requirement subset in the catalog."""

    if not isinstance(value, str):
        raise UpdateError("DEPENDENCY_RANGE_UNSUPPORTED", "A component dependency has no supported versionRange.")
    exact = re.fullmatch(r"=([0-9]+\.[0-9]+\.[0-9]+)", value)
    if exact:
        match = SEMVER3_PATTERN.fullmatch(exact.group(1))
        if match is None:
            raise UpdateError("DEPENDENCY_RANGE_UNSUPPORTED", f"Unsupported component versionRange {value!r}.")
        return "exact", tuple(map(int, match.groups())), None
    interval = re.fullmatch(r">=([0-9]+\.[0-9]+\.[0-9]+), <([0-9]+\.[0-9]+\.[0-9]+)", value)
    if interval:
        lower_match = SEMVER3_PATTERN.fullmatch(interval.group(1))
        upper_match = SEMVER3_PATTERN.fullmatch(interval.group(2))
        if lower_match is None or upper_match is None:
            raise UpdateError("DEPENDENCY_RANGE_UNSUPPORTED", f"Unsupported component versionRange {value!r}.")
        lower = tuple(map(int, lower_match.groups()))
        upper = tuple(map(int, upper_match.groups()))
        if lower >= upper:
            raise UpdateError("DEPENDENCY_RANGE_UNSUPPORTED", f"Invalid component versionRange {value!r}.")
        return "interval", lower, upper
    raise UpdateError("DEPENDENCY_RANGE_UNSUPPORTED", f"Unsupported component versionRange {value!r}.")


def _version_satisfies(version: Any, version_range: Any) -> bool:
    kind, lower, upper = _parse_supported_version_range(version_range)
    if not isinstance(version, str):
        return False
    match = SEMVER3_PATTERN.fullmatch(version)
    if match is None:
        return False
    parsed = tuple(map(int, match.groups()))
    return parsed == lower if kind == "exact" else (parsed >= lower and upper is not None and parsed < upper)


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
        if VERSION_PATTERN.fullmatch(version) and (pointer_digest is None or len(pointer_digest) == 64):
            return pointer_identity
    version = item.get("version")
    manifest_digest = item.get("manifestDigest")
    if not isinstance(version, str) or VERSION_PATTERN.fullmatch(version) is None or not _valid_digest(manifest_digest):
        raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", "Native release has no verified pointer identity.")
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
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
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


def _verify_private_directory(path: Path) -> None:
    """Require a real, current-user-owned directory with no group/other access."""

    absolute = path.absolute()
    try:
        if absolute.resolve(strict=True) != absolute:
            raise UpdateError("UNSAFE_STATE", f"Refusing a symlinked update state directory: {path}")
        info = absolute.lstat()
    except OSError as error:
        raise UpdateError("UNSAFE_STATE", f"Cannot inspect update state directory {path}: {error}") from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
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
        broker_path: Path = DEFAULT_BROKER,
        install_root: Path = DEFAULT_INSTALL_ROOT,
        state_root: Path = DEFAULT_STATE_ROOT,
        trusted_catalog_digest: str | None = TRUSTED_CATALOG_DIGEST,
        opener: Callable[..., Any] = urllib.request.urlopen,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        systemd_unit_dirs: tuple[Path, ...] | None = None,
    ) -> None:
        self.catalog_path = Path(catalog_path)
        self.activity_catalog_path = Path(activity_catalog_path)
        self.socket_path = Path(socket_path)
        self.broker_path = Path(broker_path)
        self.install_root = Path(install_root)
        self.state_root = Path(state_root)
        self.opener = opener
        self.runner = runner
        self.systemd_unit_dirs = tuple(Path(path) for path in systemd_unit_dirs) if systemd_unit_dirs is not None else (
            Path("/etc/systemd/system"),
            Path("/lib/systemd/system"),
            Path("/usr/lib/systemd/system"),
        )
        if self.catalog_path.is_symlink():
            raise UpdateError("UNSAFE_CATALOG", "The trusted component catalog path must not be a symbolic link.")
        catalog_info = self.catalog_path.lstat()
        if not stat.S_ISREG(catalog_info.st_mode):
            raise UpdateError("UNSAFE_CATALOG", "The trusted component catalog must be a regular file.")
        if self.catalog_path == INSTALLED_CATALOG and (
            catalog_info.st_uid != 0 or stat.S_IMODE(catalog_info.st_mode) & 0o022
        ):
            raise UpdateError("UNSAFE_CATALOG", "The installed component catalog must be root-owned and not group/world writable.")
        catalog_bytes = self.catalog_path.read_bytes()
        catalog_digest = "sha256:" + hashlib.sha256(catalog_bytes).hexdigest()
        if trusted_catalog_digest is not None and catalog_digest != trusted_catalog_digest:
            raise UpdateError("CATALOG_DIGEST_MISMATCH", "The installed component catalog does not match its compiled authority pin.")
        try:
            catalog_value = json.loads(catalog_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError("INVALID_LOCAL_STATE", f"Cannot read component catalog: {error}") from error
        if not isinstance(catalog_value, dict):
            raise UpdateError("INVALID_CATALOG", "The component catalog must be a JSON object.")
        self.catalog = catalog_value
        if self.catalog.get("schemaVersion") != 1 or not isinstance(self.catalog.get("components"), list):
            raise UpdateError("INVALID_CATALOG", "The installed component catalog has an unsupported schema.")
        self.components = {
            item.get("componentId"): item
            for item in self.catalog["components"]
            if isinstance(item, dict) and isinstance(item.get("componentId"), str)
        }
        self.targets = {item["id"]: item for item in self.catalog.get("targets", []) if isinstance(item, dict) and isinstance(item.get("id"), str)}
        self.publishers = {item["repository"]: item for item in self.catalog.get("publishers", []) if isinstance(item, dict) and isinstance(item.get("repository"), str)}
        self._index_cache: dict[tuple[str, str], tuple[dict[str, Any], str]] = {}
        self._readiness_cache: dict[tuple[str, bool], dict[str, Any]] = {}

    def _ensure_state_root(self) -> Path:
        """Create or validate the private root-owned updater journal directory."""

        try:
            self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as error:
            raise UpdateError("UNSAFE_STATE", f"Cannot prepare update state directory: {error}") from error
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
            raise UpdateError("UNSAFE_STATE", f"Cannot prepare update state directory {directory}: {error}") from error
        _verify_private_directory(directory)
        return directory

    def _require_authorized_process(self) -> None:
        if self.state_root == DEFAULT_STATE_ROOT and not _running_as_root():
            raise UpdateError("PRIVILEGE_REQUIRED", "The fixed Linux update helper must run with its root-authorized OS identity.")

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
            raise UpdateError("UNSAFE_STATE", f"Cannot open updater transaction lock: {error}") from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise UpdateError("UNSAFE_STATE", "Updater transaction lock is not a private regular file.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise UpdateError("UPDATE_IN_PROGRESS", "Another updater process is staging or applying a component plan.", retryable=True) from error
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
        envelope_operation = operation if isinstance(operation, str) and operation in {"status", "check", "stage", "apply"} else "status"
        try:
            if not isinstance(request, dict) or request.get("protocolVersion") != PROTOCOL_VERSION:
                raise UpdateError("INVALID_REQUEST", "Unsupported component update protocol version.")
            fields_by_operation = {
                "status": {"protocolVersion", "operation"},
                "check": {"protocolVersion", "operation", "componentIds", "channel"},
                "stage": {"protocolVersion", "operation", "planId", "planDigest", "channel"},
                "apply": {"protocolVersion", "operation", "planId", "planDigest", "confirmation", "channel"},
            }
            if operation not in fields_by_operation or set(request) - fields_by_operation[operation]:
                raise UpdateError("INVALID_REQUEST", "The request contains an unsupported operation or field.")
            self._require_authorized_process()
            if operation == "status":
                result = self.status()
            elif operation == "check":
                result = self.check(request.get("componentIds"), channel=request.get("channel"))
            elif operation == "stage":
                result = self.stage(request.get("planId"), request.get("planDigest"), channel=request.get("channel"))
            else:
                result = self.apply(
                    request.get("planId"),
                    request.get("planDigest"),
                    request.get("confirmation"),
                    channel=request.get("channel"),
                )
            return {"protocolVersion": PROTOCOL_VERSION, "ok": True, "operation": envelope_operation, "result": result}
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
            and all(isinstance(value, str) and COMPONENT_ID_PATTERN.fullmatch(value) for value in component_ids)
            and len(set(component_ids)) == len(component_ids)
        ):
            selected = component_ids
        else:
            raise UpdateError("INVALID_REQUEST", "componentIds must be a unique, non-empty component ID list.")
        unknown = sorted(set(selected) - set(self.components))
        if unknown:
            raise UpdateError("INVALID_COMPONENT", f"Unknown trusted component IDs: {', '.join(unknown)}")

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
            plan_components.append({
                "componentId": component_id,
                "version": candidate.manifest["version"],
                "manifestDigest": candidate.manifest_digest,
                "artifactDigest": artifact.get("digest", artifact.get("sha256")),
                "restartGroup": candidate.component["restart"]["group"],
            })

        plan = None
        if plan_components:
            digest_material = {
                "schemaVersion": 1,
                "channel": channel,
                "components": plan_components,
            }
            plan_digest = _digest_json(digest_material, "planDigest")
            plan_id = "plan-" + plan_digest.split(":", 1)[1][:32]
            plan = {
                "planId": plan_id,
                "planDigest": plan_digest,
                "channel": channel,
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
                gate = self._readiness(component) if include_readiness else {
                    "status": "UNKNOWN",
                    "blocker_codes": ["READINESS_NOT_QUERIED"],
                    "message": "Check plan generation does not acquire the maintenance gate.",
                }
                row = self._result_component(component, gate, phase="unknown", update_available=False)
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
            gate = self._readiness(component) if include_readiness else {
                "status": "UNKNOWN",
                "blocker_codes": ["READINESS_NOT_QUERIED"],
                "message": "Check plan generation does not acquire the maintenance gate.",
            }
            rows.append(self._result_component(
                component,
                gate,
                phase="available" if is_available else "current",
                available_version=candidate.manifest["version"] if is_available else None,
                update_available=is_available,
            ))
        result: dict[str, Any] = {"status": "checked", "components": rows, "plans": [plan] if plan else []}
        if plan:
            result["plan"] = plan
            plan_directory = self._private_state_directory("plans")
            _atomic_json(plan_directory / f"{plan['planId']}.json", plan)
        return result

    def stage(self, plan_id: Any, plan_digest: Any, *, channel: Any = None) -> dict[str, Any]:
        self._require_authorized_process()
        with self._exclusive_update_lock():
            return self._stage_locked(plan_id, plan_digest, channel=channel)

    def _stage_locked(self, plan_id: Any, plan_digest: Any, *, channel: Any = None) -> dict[str, Any]:
        """Download, verify, and install immutable payloads without gate or activation."""

        self._ensure_state_root()
        self._validate_plan_identity(plan_id, plan_digest)
        stored_plan = _read_object(self.state_root / "plans" / f"{plan_id}.json", "checked update plan")
        if stored_plan.get("planDigest") != plan_digest:
            raise UpdateError("PLAN_CHANGED", "The latest checked plan no longer matches this digest; run check again.", retryable=True)
        channel = self._resolve_channel(channel if channel is not None else stored_plan.get("channel"))
        if stored_plan.get("channel") != channel:
            raise UpdateError("PLAN_CHANNEL_MISMATCH", "Stage channel does not match the checked, digest-bound plan.")
        component_ids = [item.get("componentId") for item in stored_plan.get("components", []) if isinstance(item, dict)]
        checked = self.check(component_ids, channel=channel, include_readiness=False)
        plan = self._find_plan(checked, plan_id, plan_digest)
        candidate_map = self._resolve_plan_candidates(plan, channel)
        staged_root = self._private_state_directory("staged")
        plan_root = staged_root / plan_id
        if plan_root.is_symlink():
            raise UpdateError("UNSAFE_STATE", f"Refusing a symlinked staged plan directory: {plan_root}")
        if plan_root.exists():
            shutil.rmtree(plan_root)
        plan_root.mkdir(parents=True, mode=0o700)
        staged_components: list[dict[str, Any]] = []
        try:
            for plan_component in plan["components"]:
                candidate = candidate_map[plan_component["componentId"]]
                staged_components.append(self._stage_candidate(candidate, plan_root))
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
        staged_rows = [self._result_component(
            self.components[item["componentId"]],
            {"status": "UNKNOWN", "blocker_codes": ["READINESS_NOT_QUERIED"], "message": "Staging does not query or acquire the maintenance gate."},
            phase="staged",
            available_version=item["version"],
            staged={"plan": staged_plan, "component": item},
        ) for item in staged_components]
        return {"status": "staged", "components": staged_rows, "plan": staged_plan, "plans": [staged_plan]}

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

        self._validate_plan_identity(plan_id, plan_digest)
        if (
            not isinstance(confirmation, dict)
            or set(confirmation) != {"planId", "planDigest", "confirmed"}
            or confirmation.get("confirmed") is not True
            or confirmation.get("planId") != plan_id
            or confirmation.get("planDigest") != plan_digest
        ):
            raise UpdateError("CONFIRMATION_MISMATCH", "Apply requires confirmation bound to this exact planId and planDigest.")
        stage_path = self.state_root / "staged" / plan_id / "stage.json"
        record = _read_object(stage_path, "staged update plan")
        if record.get("phase") != "staged" or record.get("plan", {}).get("planDigest") != plan_digest:
            raise UpdateError("PLAN_NOT_STAGED", "This exact plan is not staged. Stage it again before apply.")
        stored_channel = record.get("channel")
        if stored_channel not in {"stable", "preview"} or (
            channel is not None and self._resolve_channel(channel) != stored_channel
        ):
            raise UpdateError("PLAN_CHANNEL_MISMATCH", "Apply channel does not match the staged, digest-bound plan.")
        self._validate_staged_record(
            record,
            expected_plan_id=plan_id,
            expected_plan_digest=plan_digest,
        )
        transaction_path = self._private_state_directory("transactions") / f"{plan_id}.json"
        existing = _read_object(transaction_path, "update transaction") if transaction_path.exists() else None
        if existing and existing.get("phase") not in {"succeeded", "rolled_back"}:
            return self._recover_transaction(existing, transaction_path, stage_path, confirmation)
        self._require_managed_services(record["components"])
        if not _running_as_root():
            raise UpdateError("PRIVILEGE_REQUIRED", "Applying staged components requires the root-owned local update helper.")

        target_kind = "CORE_RUNTIME" if any(
            self.components[item["componentId"]]["restart"]["group"] == "core-runtime"
            for item in record["components"]
        ) else "PACKAGE_ONLY"
        readiness = self._readiness_for(target_kind, requires_restart=True, force=True)
        self._require_ready(readiness, target_kind)
        gate_catalog, gate_sources = self._activity_catalog()
        if readiness.get("install_catalog_generation") != gate_catalog["generation"]:
            raise UpdateError("GATE_UNKNOWN", "Installed activity catalog changed during readiness; check again before applying.", retryable=True)
        artifact_digests = {
            item["componentId"]: item["artifactDigest"]
            for item in record["components"]
        }
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
        except Exception as failure:
            healthy_rollback, rollback_message = self._rollback_transaction(transaction)
            transaction["phase"] = "rollback_end_pending" if healthy_rollback else "rollback_required"
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
            raise UpdateError("INVALID_CATALOG", f"The trusted catalog has no {channel} channel policy.")
        return channel

    def _validate_plan_identity(self, plan_id: Any, plan_digest: Any) -> None:
        if not isinstance(plan_id, str) or PLAN_ID_PATTERN.fullmatch(plan_id) is None or not _valid_digest(plan_digest):
            raise UpdateError("INVALID_PLAN", "planId or sha256 planDigest is invalid.")

    @staticmethod
    def _find_plan(result: dict[str, Any], plan_id: str, plan_digest: str) -> dict[str, Any]:
        plan = next((item for item in result.get("plans", []) if item.get("planId") == plan_id), None)
        if plan is None or plan.get("planDigest") != plan_digest:
            raise UpdateError("PLAN_CHANGED", "The latest trusted release set no longer matches this plan; run check again.", retryable=True)
        return plan

    def _host_components(self) -> list[dict[str, Any]]:
        return sorted(self.components.values(), key=lambda item: item["componentId"])

    def _target_for(self, component: dict[str, Any]) -> dict[str, Any] | None:
        try:
            host = platform.freedesktop_os_release()
        except OSError:
            return None
        if host.get("ID") != "ubuntu" or host.get("VERSION_ID") != "24.04":
            return None
        host_arch = platform.machine().lower()
        architecture = "x86_64" if host_arch in {"x86_64", "amd64"} else "aarch64" if host_arch in {"aarch64", "arm64"} else host_arch
        for entry in component.get("targets", []):
            target = self.targets.get(entry.get("targetId"))
            if target is None or entry.get("support") != "supported":
                continue
            spec = target.get("target", {})
            if spec.get("os") != "linux" or spec.get("architecture") != architecture:
                continue
            if spec.get("distribution") != "ubuntu" or spec.get("distributionVersion") != "24.04":
                continue
            if entry.get("artifactKind") == "python-bundle" and spec.get("runtime") != "python:3.12":
                continue
            if entry.get("artifactKind") == "python-bundle" and sys.version_info[:2] != (3, 12):
                continue
            if entry.get("artifactKind") == "native-binary" and spec.get("runtime") != "systemd":
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
            "gate": {"state": "unknown", "activeTasks": [], "unknownActivitySources": [], "blockerCodes": ["UNSUPPORTED_TARGET"], "blockers": [{"code": "UNSUPPORTED_TARGET", "message": "This component has no supported Linux 24.04 target in the trusted catalog."}]},
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
        service_managed = target is not None and self._unit_exists(component)
        if target is not None and phase == "staged" and gate.get("status") == "READY" and service_managed:
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
            result["blockers"] = [{
                "code": "SERVICE_NOT_MANAGED",
                "message": "The OS target is available, but no catalog-matched local systemd service is installed.",
            }]
        return {key: value for key, value in result.items() if value is not None}

    def _installed(self, component: dict[str, Any]) -> dict[str, Any]:
        service = component.get("pythonBundleService")
        if service:
            try:
                module = self._load_service_bundle()
                active = module.resolve_active_release(service, install_root=self.install_root)
                if active is None:
                    if self._read_active_receipt(component["componentId"]) is not None:
                        raise UpdateError("INVALID_INSTALLED_RELEASE", f"Installed identity receipt exists without an active {service} bundle.")
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
                    raise UpdateError("INVALID_INSTALLED_RELEASE", f"Installed {component['componentId']} receipt does not match the active bundle pointer.")
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
                raise UpdateError("INVALID_INSTALLED_RELEASE", f"Installed {component['componentId']} release failed integrity validation: {error}") from error
        root = self.install_root / "components" / component["componentId"]
        active = root / "active"
        if not active.exists() and not active.is_symlink():
            if self._read_active_receipt(component["componentId"]) is not None:
                raise UpdateError("INVALID_INSTALLED_RELEASE", f"Installed identity receipt exists without an active {component['componentId']} release.")
            return self._empty_installed()
        info = active.lstat()
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active pointer is unsafe for {component['componentId']}.")
        target = os.readlink(active)
        match = re.fullmatch(r"releases/([^/]+)", target)
        if not match:
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active pointer has an unsafe target for {component['componentId']}.")
        pointer_identity = match.group(1)
        version, pointer_digest = _native_release_directory_identity(pointer_identity)
        if VERSION_PATTERN.fullmatch(version) is None:
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active pointer has an unsafe release identity for {component['componentId']}.")
        release = root / "releases" / pointer_identity
        if release.is_symlink() or not release.is_dir():
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active release directory is unsafe for {component['componentId']}.")
        manifest = _read_object(release / "component-manifest.json", f"installed {component['componentId']} manifest")
        self._validate_manifest_digest(manifest, manifest.get("manifestDigest"))
        if (
            manifest.get("componentId") != component["componentId"]
            or manifest.get("version") != version
            or _artifact_kind_from_manifest(manifest) != "native-binary"
            or (pointer_digest is not None and pointer_digest != manifest["manifestDigest"].removeprefix("sha256:"))
            or (pointer_digest is None and pointer_identity != version)
        ):
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Installed release identity differs for {component['componentId']}.")
        artifact_digest = _artifact_digest_from_manifest(manifest)
        if not _valid_digest(artifact_digest):
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Installed artifact digest is invalid for {component['componentId']}.")
        receipt = self._read_active_receipt(component["componentId"])
        if receipt is None:
            receipt = self._read_release_receipt(component["componentId"], manifest["manifestDigest"])
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
    def _empty_installed(*, active_version: str | None = None, active: bool = False) -> dict[str, Any]:
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

    def _installed_component_directory(self, component_id: str, *, create: bool = False) -> Path | None:
        if COMPONENT_ID_PATTERN.fullmatch(component_id) is None:
            raise UpdateError("INVALID_COMPONENT", "Component ID is invalid for the installed receipt store.")
        root = self._private_state_directory("installed")
        directory = root / component_id
        if not directory.exists() and not directory.is_symlink():
            if not create:
                return None
            try:
                directory.mkdir(mode=0o700)
            except OSError as error:
                raise UpdateError("UNSAFE_STATE", f"Cannot create installed identity directory for {component_id}: {error}") from error
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
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Installed active receipt is malformed for {component_id}.")
        release_identity = active["releaseIdentity"]
        receipt = self._read_release_receipt(component_id, release_identity)
        if receipt is None:
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Installed release receipt is missing for {component_id}.")
        if receipt.get("bundleIdentity") != active.get("bundleIdentity"):
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Installed active receipt bundle identity differs for {component_id}.")
        return receipt

    def _read_release_receipt(self, component_id: str, release_identity: str) -> dict[str, Any] | None:
        if not _valid_digest(release_identity):
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Installed release identity is invalid for {component_id}.")
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
            "schemaVersion", "componentId", "releaseIdentity", "manifestDigest",
            "artifactDigest", "version", "bundleIdentity", "manifest",
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
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Installed release receipt is malformed for {component_id}.")
        self._validate_manifest_digest(manifest, release_identity)
        if (
            manifest.get("componentId") != component_id
            or manifest.get("version") != receipt["version"]
            or _artifact_digest_from_manifest(manifest) != receipt["artifactDigest"]
        ):
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Installed manifest does not match its receipt for {component_id}.")
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
            or (receipt.get("pointerIdentity") is not None and receipt.get("pointerIdentity") != pointer_identity)
        ):
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Installed {component_id} receipt does not match its active release pointer.")

    def _write_release_receipt(self, item: dict[str, Any]) -> None:
        component_id = item["componentId"]
        manifest = item["manifest"]
        if (
            item.get("releaseIdentity") != item.get("manifestDigest")
            or not _valid_digest(item.get("releaseIdentity"))
            or item.get("artifactDigest") != _artifact_digest_from_manifest(manifest)
        ):
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Cannot persist an incomplete verified identity for {component_id}.")
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
                raise UpdateError("RELEASE_RECEIPT_COLLISION", f"Verified receipt identity collision for {component_id}.")
            return
        _atomic_json(path, receipt)

    def _write_active_receipt(self, item: dict[str, Any]) -> None:
        directory = self._installed_component_directory(item["componentId"], create=True)
        assert directory is not None
        active_path = directory / "active.json"
        if active_path.is_symlink():
            raise UpdateError("UNSAFE_STATE", f"Refusing a symlinked active identity receipt for {item['componentId']}.")
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
        if (
            previous.get("manifestDigest") != previous.get("releaseIdentity")
            or not _valid_digest(previous.get("releaseIdentity"))
        ):
            raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", f"Previous {component_id} receipt identity is incomplete.")
        directory = self._installed_component_directory(component_id)
        if directory is None:
            raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", f"Previous {component_id} receipt is missing.")
        receipt = self._read_release_receipt(component_id, previous["releaseIdentity"])
        if receipt is None:
            raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", f"Previous {component_id} receipt is missing.")
        if (
            receipt.get("releaseIdentity") != previous["releaseIdentity"]
            or receipt.get("artifactDigest") != previous.get("artifactDigest")
            or receipt.get("bundleIdentity") != previous.get("bundleIdentity")
            or receipt.get("version") != previous.get("version")
        ):
            raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", f"Previous {component_id} receipt does not match the transaction journal.")
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
            raise UpdateError("UNSAFE_STATE", f"Refusing a symlinked active identity receipt for {component_id}.")
        if active_path.exists():
            active_path.unlink()
            self._fsync_directory(directory)

    def _load_service_bundle(self) -> Any:
        candidates = [self.install_root / "scripts" / "service_bundle.py", self.install_root / "packaging" / "service_bundle.py", Path(__file__).with_name("service_bundle.py")]
        module_path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if module_path is None:
            raise UpdateError("SERVICE_BUNDLE_MISSING", "The Product bundle verifier is missing.")
        import importlib.util

        spec = importlib.util.spec_from_file_location("_cyrene_component_service_bundle", module_path)
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
            raise UpdateError("GATE_UNKNOWN", "Runtime activity source catalog has an invalid schema or generation.", retryable=True)
        sources = catalog.get("sources")
        if not isinstance(sources, list) or not sources:
            raise UpdateError("GATE_UNKNOWN", "No installed Product activity sources are trusted; the updater will not assume the runtime is idle.", retryable=True)
        ids: list[str] = []
        for source in sources:
            if not isinstance(source, dict) or set(source) != {"source_id", "uid", "gid", "source_token_sha256"}:
                raise UpdateError("GATE_UNKNOWN", "Runtime activity source catalog contains an invalid source record.", retryable=True)
            source_id = source["source_id"]
            if not isinstance(source_id, str) or re.fullmatch(r"[a-z0-9._-]{1,160}", source_id) is None:
                raise UpdateError("GATE_UNKNOWN", "Runtime activity source catalog contains an invalid source ID.", retryable=True)
            if (
                not isinstance(source["uid"], int)
                or isinstance(source["uid"], bool)
                or not isinstance(source["gid"], int)
                or isinstance(source["gid"], bool)
                or not isinstance(source["source_token_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", source["source_token_sha256"]) is None
            ):
                raise UpdateError("GATE_UNKNOWN", f"Runtime activity source {source_id} has invalid trust metadata.", retryable=True)
            ids.append(source_id)
        if len(set(ids)) != len(ids) or ids != sorted(ids):
            raise UpdateError("GATE_UNKNOWN", "Runtime activity source IDs must be unique and sorted.", retryable=True)
        return catalog, ids

    def _broker_request(self, method: str, params: dict[str, Any], *, request_id: str | None = None) -> dict[str, Any]:
        catalog: dict[str, Any] | None = None
        sources: list[str] = []
        if method in {"GetUpdateReadiness", "BeginMaintenance"}:
            catalog, sources = self._activity_catalog()
        if not self.broker_path.is_file() or not os.access(self.broker_path, os.X_OK):
            raise UpdateError("GATE_UNKNOWN", "The native runtime maintenance broker is unavailable; apply is fail-closed.", retryable=True)
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
            completed = self.runner(
                [str(self.broker_path), "request", "--socket", str(self.socket_path), "--operator"],
                input=json.dumps(payload, separators=(",", ":")) + "\n",
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UpdateError("GATE_UNKNOWN", f"Cannot reach runtime maintenance broker: {error}", retryable=True) from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
            raise UpdateError("GATE_UNKNOWN", f"Runtime maintenance broker rejected the request: {detail}", retryable=True)
        lines = completed.stdout.splitlines()
        if len(lines) != 1:
            raise UpdateError("GATE_UNKNOWN", "Runtime maintenance broker returned an invalid JSONL response.", retryable=True)
        try:
            response = json.loads(lines[0])
        except json.JSONDecodeError as error:
            raise UpdateError("GATE_UNKNOWN", "Runtime maintenance broker returned malformed JSON.", retryable=True) from error
        if not isinstance(response, dict) or response.get("request_id") != request_id:
            raise UpdateError("GATE_UNKNOWN", "Runtime maintenance broker response identity did not match.", retryable=True)
        if "error" in response:
            detail = response["error"]
            code = detail.get("code", "GATE_UNKNOWN") if isinstance(detail, dict) else "GATE_UNKNOWN"
            message = detail.get("message", "Runtime readiness is unknown.") if isinstance(detail, dict) else "Runtime readiness is unknown."
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
                maintenance_not_acquired=method == "BeginMaintenance" and str(code) in refusal_codes,
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise UpdateError("GATE_UNKNOWN", "Runtime maintenance broker response has no result object.", retryable=True)
        return result

    def _readiness(self, component: dict[str, Any]) -> dict[str, Any]:
        target_kind = "CORE_RUNTIME" if component.get("restart", {}).get("group") == "core-runtime" else "PACKAGE_ONLY"
        return self._readiness_for(target_kind, requires_restart=True)

    def _readiness_for(self, target_kind: str, *, requires_restart: bool, force: bool = False) -> dict[str, Any]:
        key = (target_kind, requires_restart)
        if not force and key in self._readiness_cache:
            return self._readiness_cache[key]
        try:
            activity_catalog, activity_sources = self._activity_catalog()
            result = self._broker_request("GetUpdateReadiness", {
                "target_kind": target_kind,
                "requires_restart": requires_restart,
                "expected_catalog_generation": activity_catalog["generation"],
                "expected_activity_sources": activity_sources,
            })
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
        blockers = [{"code": str(code), "message": str(code).replace("_", " ").lower()} for code in readiness.get("blocker_codes", [])]
        if readiness.get("message"):
            blockers.append({"code": "BROKER_UNAVAILABLE", "message": readiness["message"]})
        result = {
            "state": state_map.get(readiness.get("status"), "unknown"),
            "gateGeneration": readiness.get("gate_generation"),
            "installCatalogGeneration": readiness.get("install_catalog_generation"),
            "activeTaskCount": readiness.get("active_task_count"),
            "activeTasks": [
                {"sourceId": item.get("source_id", "unknown"), "taskId": item.get("task_id", "unknown"), "state": item.get("state", "unknown")}
                for item in readiness.get("active_tasks", []) if isinstance(item, dict)
            ],
            "activeWorkerCount": readiness.get("active_worker_count"),
            "activeAllocationCount": readiness.get("active_allocation_count"),
            "inflightRuntimeAdmissionCount": readiness.get("inflight_runtime_admission_count"),
            "unknownActivitySources": readiness.get("unknown_activity_sources", []),
            "blockerCodes": readiness.get("blocker_codes", []),
            "requiresRestartConfirmation": bool(readiness.get("requires_restart_confirmation", False)),
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
                not isinstance(readiness.get(field), int) or isinstance(readiness.get(field), bool) or readiness[field] < 0
                for field in numeric_fields
            ):
                raise UpdateError("GATE_UNKNOWN", "Runtime readiness omitted valid counters or generations; apply is refused.", retryable=True)
            active = readiness.get("active_tasks")
            if not isinstance(active, list):
                raise UpdateError("GATE_UNKNOWN", "Runtime readiness omitted the active task list; apply is refused.", retryable=True)
            if active or readiness["active_task_count"] != 0:
                raise UpdateError("ACTIVE_TASKS", "Update refused while Product tasks are active. Finish or stop tasks in their owning Product service; Cyrene will not cancel or drain them.")
            unknown_sources = readiness.get("unknown_activity_sources")
            if not isinstance(unknown_sources, list) or unknown_sources:
                raise UpdateError("GATE_UNKNOWN", "Runtime activity sources are unknown; apply is refused.", retryable=True)
            if readiness["inflight_runtime_admission_count"] != 0:
                raise UpdateError("GATE_UNKNOWN", "Runtime task admission is still in flight; wait for it to settle and check again.", retryable=True)
            if target_kind == "CORE_RUNTIME" and (
                readiness["active_worker_count"] != 0 or readiness["active_allocation_count"] != 0
            ):
                raise UpdateError("IDLE_RUNTIME_REQUIRES_UNLOAD", "Runtime workers or allocations are still held. Unload the model from its Product, release workers/leases/allocations, then check again; the updater will not terminate them.")
            if not isinstance(readiness.get("requires_restart_confirmation"), bool):
                raise UpdateError("GATE_UNKNOWN", "Runtime readiness omitted the restart confirmation state; apply is refused.", retryable=True)
            return
        if status == "ACTIVE_TASKS":
            active = readiness.get("active_tasks", [])
            task_names = ", ".join(
                f"{item.get('source_id', 'unknown')}:{item.get('task_id', 'unknown')} ({item.get('state', 'active')})"
                for item in active if isinstance(item, dict)
            ) or "active Product tasks"
            raise UpdateError("ACTIVE_TASKS", f"Update refused while {task_names} are active. Finish or stop tasks in their owning Product service; Cyrene will not cancel or drain them.")
        if status == "IDLE_RUNTIME_REQUIRES_UNLOAD":
            raise UpdateError("IDLE_RUNTIME_REQUIRES_UNLOAD", "Runtime workers or allocations are still held. Unload the model from its Product, release workers/leases/allocations, then check again; the updater will not terminate them.")
        if status == "MAINTENANCE_ACTIVE":
            raise UpdateError("MAINTENANCE_ACTIVE", "Another maintenance transaction already owns the update gate.", retryable=True)
        raise UpdateError("GATE_UNKNOWN", f"Runtime maintenance readiness is unknown ({target_kind}); apply is refused.", retryable=True)

    def _channel_releases(self, publisher: dict[str, Any], channel: str) -> tuple[dict[str, Any], str]:
        key = (publisher["repository"], channel)
        if key in self._index_cache:
            return self._index_cache[key]
        releases_uri = publisher["releaseDiscovery"]["apiUri"]
        expected_api_uri = f"https://api.github.com/repos/{publisher['repository']}/releases?per_page=100"
        if releases_uri != expected_api_uri:
            raise UpdateError("INVALID_CATALOG", f"Release discovery URL is not the fixed API for {publisher['repository']}.")
        releases = self._get_json(releases_uri)
        if not isinstance(releases, list):
            raise UpdateError("RELEASE_DISCOVERY_INVALID", "GitHub Releases API did not return a release list.", retryable=True)
        channel_cfg = self.catalog["channels"][channel]
        expected_prerelease = channel_cfg["releasePrerelease"]
        selected_release = next((
            item for item in releases
            if isinstance(item, dict)
            and item.get("draft") is False
            and item.get("prerelease") is expected_prerelease
        ), None)
        if selected_release is None:
            raise UpdateError("NO_RELEASE", f"No {channel} component release is published for {publisher['repository']}.", retryable=True)
        assets = selected_release.get("assets")
        asset = next((entry for entry in assets if isinstance(entry, dict) and entry.get("name") == publisher["releaseDiscovery"]["indexAssetName"]), None) if isinstance(assets, list) else None
        if asset is None or not isinstance(asset.get("browser_download_url"), str):
            raise UpdateError("RELEASE_INDEX_MISSING", f"The selected {channel} release has no component index asset.", retryable=True)
        index_uri = asset["browser_download_url"]
        self._require_github_asset_uri(index_uri, publisher["repository"])
        index_bytes = self._get_bytes(index_uri)
        try:
            index = json.loads(index_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError("INVALID_RELEASE_INDEX", "The component release index is not valid UTF-8 JSON.") from error
        self._validate_index(index, publisher, channel, selected_release)
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

    def _validate_index(self, index: Any, publisher: dict[str, Any], channel: str, release: dict[str, Any]) -> None:
        if not isinstance(index, dict) or index.get("schemaVersion") != 1 or index.get("repository") != publisher["repository"] or index.get("channel") != channel:
            raise UpdateError("INVALID_RELEASE_INDEX", "The release index identity or channel is inconsistent.")
        if index.get("indexDigest") != _digest_json(index, "indexDigest"):
            raise UpdateError("INDEX_DIGEST_MISMATCH", "The release index digest is invalid.")
        source = index.get("source")
        attestation = index.get("provenance", {}).get("attestation")
        allowed_refs = self.catalog["channels"][channel]["sourceRefs"]
        if not isinstance(source, dict) or source.get("repository") != f"https://github.com/{publisher['repository']}" or source.get("ref") not in allowed_refs or COMMIT_PATTERN.fullmatch(str(source.get("commit", ""))) is None:
            raise UpdateError("UNTRUSTED_SOURCE", "The release index source is outside the trusted repository/ref pins.")
        prefix = "preview-" if channel == "preview" else "stable-"
        if release.get("tag_name") != prefix + source["commit"]:
            raise UpdateError("UNTRUSTED_RELEASE_TAG", "The immutable release tag does not match the source commit.")
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
            or run.get("url") != f"https://github.com/{publisher['repository']}/actions/runs/{run['id']}/attempts/{run['attempt']}"
        ):
            raise UpdateError("UNTRUSTED_WORKFLOW", "The index attestation identity differs from the trusted publisher workflow.")
        if not isinstance(index.get("releases"), list):
            raise UpdateError("INVALID_RELEASE_INDEX", "The release index has no release list.")
        if not isinstance(index.get("compatibilityGroups", []), list):
            raise UpdateError("INVALID_RELEASE_INDEX", "The release index compatibilityGroups field is invalid.")

    def _candidate(self, component: dict[str, Any], target: dict[str, Any], channel: str) -> Candidate:
        publisher = self.publishers.get(component["publisher"])
        if publisher is None:
            raise UpdateError("INVALID_CATALOG", f"No trusted publisher is configured for {component['componentId']}.")
        index, index_uri = self._channel_releases(publisher, channel)
        entries = [
            item for item in index.get("releases", [])
            if isinstance(item, dict) and item.get("componentId") == component["componentId"] and item.get("target") == target["target"]
        ]
        if len(entries) != 1:
            raise UpdateError("TARGET_RELEASE_MISSING", f"Index has no unique {channel} manifest for {component['componentId']} at {target['id']}.", retryable=True)
        entry = entries[0]
        self._require_github_asset_uri(entry.get("manifestUri"), publisher["repository"])
        manifest_bytes = self._get_bytes(entry["manifestUri"])
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UpdateError("INVALID_MANIFEST", f"Manifest for {component['componentId']} is invalid JSON.") from error
        self._validate_manifest(manifest, entry, component, target, publisher, channel, index)
        artifact = manifest["artifact"]
        artifact_digest = artifact.get("digest", artifact.get("sha256"))
        return Candidate(component, manifest, entry["manifestDigest"], artifact_digest, entry["manifestUri"], index, index_uri)

    def _validate_manifest(self, manifest: Any, entry: dict[str, Any], component: dict[str, Any], target: dict[str, Any], publisher: dict[str, Any], channel: str, index: dict[str, Any]) -> None:
        required = {"schemaVersion", "releaseId", "componentId", "version", "channel", "target", "artifact", "dependencies", "restart", "source", "provenance", "manifestDigest"}
        allowed = required | {"health", "compatibility"}
        if not isinstance(manifest, dict) or set(manifest) - allowed or not required.issubset(manifest):
            raise UpdateError("INVALID_MANIFEST", f"Manifest for {component['componentId']} has an invalid object shape.")
        if manifest.get("schemaVersion") != 1 or manifest.get("componentId") != component["componentId"] or manifest.get("version") != entry.get("version") or manifest.get("target") != target["target"] or manifest.get("channel") != channel:
            raise UpdateError("MANIFEST_IDENTITY_MISMATCH", f"Manifest identity differs from its trusted index for {component['componentId']}.")
        if manifest.get("manifestDigest") != _digest_json(manifest, "manifestDigest") or manifest["manifestDigest"] != entry.get("manifestDigest"):
            raise UpdateError("MANIFEST_DIGEST_MISMATCH", f"Manifest JCS digest is invalid for {component['componentId']}.")
        if VERSION_PATTERN.fullmatch(str(manifest.get("version", ""))) is None:
            raise UpdateError("INVALID_MANIFEST", "Release version is not a safe immutable path segment.")
        self._validate_manifest_dependencies(manifest.get("dependencies"), component)
        source = manifest.get("source")
        index_source = index["source"]
        publisher_repo_url = f"https://github.com/{publisher['repository']}"
        allowed_refs = self.catalog["channels"][channel]["sourceRefs"]
        if not isinstance(source, dict) or source.get("repository") != publisher_repo_url or source.get("ref") not in allowed_refs or source.get("ref") != index_source.get("ref") or source.get("commit") != index_source.get("commit"):
            raise UpdateError("UNTRUSTED_SOURCE", f"Manifest source for {component['componentId']} is not pinned by its attested index.")
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
            raise UpdateError("UNTRUSTED_WORKFLOW", f"Manifest provenance for {component['componentId']} differs from its attested release run.")
        artifact = manifest.get("artifact")
        if not isinstance(artifact, dict) or artifact.get("kind") != target["artifactKind"] or artifact.get("kind") not in component.get("artifactKinds", []):
            raise UpdateError("UNSUPPORTED_ARTIFACT", f"Artifact kind is not supported for {component['componentId']} at this target.")
        if artifact["kind"] == "oci-image":
            raise UpdateError("UNSUPPORTED_TARGET", "Linux native updater does not recreate OCI containers.")
        if not _valid_digest(artifact.get("sha256")) or not isinstance(artifact.get("sizeBytes"), int) or artifact["sizeBytes"] < 1:
            raise UpdateError("INVALID_MANIFEST", f"Artifact digest/size is invalid for {component['componentId']}.")
        if artifact["kind"] == "native-binary":
            if not isinstance(artifact.get("entrypoint"), str) or not isinstance(artifact.get("files"), dict) or not artifact["files"]:
                raise UpdateError("INVALID_MANIFEST", f"Native artifact payload map is invalid for {component['componentId']}.")
        elif artifact["kind"] == "python-bundle":
            if artifact.get("format") not in {"tar.gz", "tar.zst", "zip"} or not isinstance(artifact.get("files"), dict) or not artifact["files"]:
                raise UpdateError("INVALID_MANIFEST", f"Python bundle payload map is invalid for {component['componentId']}.")
            for name, file_digest in artifact["files"].items():
                _safe_relative(name, field="artifact.files path")
                if not _valid_digest(file_digest):
                    raise UpdateError("INVALID_MANIFEST", f"Python bundle file digest is invalid for {component['componentId']}.")
        artifact_uri = artifact.get("uri")
        if not isinstance(artifact_uri, str):
            raise UpdateError("INVALID_MANIFEST", f"Artifact download URI is missing for {component['componentId']}.")
        self._require_github_asset_uri(artifact_uri, publisher["repository"])
        subject_name = manifest.get("provenance", {}).get("attestation", {}).get("subjectName")
        if subject_name != PurePosixPath(urllib.parse.urlsplit(artifact_uri).path).name:
            raise UpdateError("UNTRUSTED_ATTESTATION_SUBJECT", f"Attestation subject differs from artifact basename for {component['componentId']}.")
        restart = manifest.get("restart")
        expected_restart = component.get("restart", {})
        if not isinstance(restart, dict) or restart.get("group") != expected_restart.get("group") or restart.get("unit", expected_restart.get("unit")) != expected_restart.get("unit"):
            raise UpdateError("RESTART_POLICY_MISMATCH", f"Manifest restart policy differs from the trusted catalog for {component['componentId']}.")

    def _validate_manifest_dependencies(self, value: Any, component: dict[str, Any]) -> None:
        """Require publisher dependencies to match the trusted component catalog exactly."""

        if not isinstance(value, list):
            raise UpdateError("INVALID_MANIFEST_DEPENDENCIES", f"{component['componentId']} dependencies must be a list.")
        trusted = component.get("dependencies", [])
        if not isinstance(trusted, list):
            raise UpdateError("INVALID_CATALOG", f"Trusted dependencies are malformed for {component['componentId']}.")

        def normalize(items: list[Any], *, source: str) -> list[dict[str, str]]:
            normalized: list[dict[str, str]] = []
            seen: set[str] = set()
            for item in items:
                if not isinstance(item, dict) or set(item) not in ({"componentId"}, {"componentId", "versionRange"}):
                    raise UpdateError("INVALID_MANIFEST_DEPENDENCIES", f"{source} dependency record is malformed for {component['componentId']}.")
                dependency_id = item.get("componentId")
                if not isinstance(dependency_id, str) or dependency_id not in self.components or dependency_id in seen:
                    raise UpdateError("INVALID_MANIFEST_DEPENDENCIES", f"{source} references an unknown or duplicate dependency for {component['componentId']}.")
                seen.add(dependency_id)
                row = {"componentId": dependency_id}
                if "versionRange" in item:
                    _parse_supported_version_range(item["versionRange"])
                    row["versionRange"] = item["versionRange"]
                normalized.append(row)
            return sorted(normalized, key=lambda row: row["componentId"])

        if normalize(value, source="Manifest") != normalize(trusted, source="Catalog"):
            raise UpdateError("DEPENDENCY_CATALOG_MISMATCH", f"Published dependencies differ from trusted catalog for {component['componentId']}.")

    def _validate_runtime_dependencies(self, candidates: dict[str, Candidate]) -> None:
        """Check ranged runtime dependencies against installed or same-plan versions."""

        for component_id, candidate in candidates.items():
            for dependency in candidate.component.get("dependencies", []):
                version_range = dependency.get("versionRange") if isinstance(dependency, dict) else None
                if version_range is None:
                    continue
                dependency_id = dependency["componentId"]
                dependency_component = self.components.get(dependency_id)
                if dependency_component is None:
                    raise UpdateError("INVALID_CATALOG", f"Unknown trusted dependency {dependency_id!r} for {component_id}.")
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
                    raise UpdateError("DEPENDENCY_NOT_INSTALLED", f"{component_id} requires {dependency_id} {version_range}; install or include that component in the checked plan first.")
                if not _version_satisfies(dependency_version, version_range):
                    raise UpdateError("DEPENDENCY_VERSION_UNSATISFIED", f"{component_id} requires {dependency_id} {version_range}, but the installed/planned version is {dependency_version}.")

    def _expand_compatibility_groups(self, candidates: dict[str, Candidate], channel: str) -> dict[str, Candidate]:
        expanded = dict(candidates)
        group_catalogs = {item["groupId"]: item for item in self.catalog.get("compatibilityGroups", []) if isinstance(item, dict)}
        for candidate in list(candidates.values()):
            compatibility = candidate.manifest.get("compatibility")
            if compatibility is None:
                if candidate.component.get("compatibilityGroup"):
                    raise UpdateError("COMPATIBILITY_MISSING", f"{candidate.component['componentId']} release omits its trusted compatibility pins.")
                continue
            group_id = compatibility.get("groupId")
            group = group_catalogs.get(group_id)
            if group is None or candidate.component.get("compatibilityGroup") != group_id:
                raise UpdateError("COMPATIBILITY_UNTRUSTED", f"Unknown compatibility group {group_id!r}.")
            if compatibility.get("wireApiVersion") != group.get("wireApiVersion") or compatibility.get("contractApiVersion") != group.get("contractApiVersion"):
                raise UpdateError("COMPATIBILITY_API_UNSUPPORTED", f"No client support is pinned for {group_id} compatibility API.")
            current_members: list[tuple[dict[str, Any], dict[str, Any]]] = []
            adoption_needed = False
            candidate_pin = compatibility["contractLock"]
            members = group["members"]
            for member in members:
                member_component = self.components[member["componentId"]]
                member_target = self._target_for(member_component)
                if member_target is None:
                    if member.get("requiredForAdoption"):
                        raise UpdateError("COMPATIBILITY_MEMBER_UNSUPPORTED", f"Required {group_id} member {member['componentId']} has no supported target.")
                    continue
                installed = self._installed(member_component)
                if not installed["active"]:
                    if member.get("requiredForAdoption"):
                        adoption_needed = True
                    continue
                installed_compat = (installed.get("manifest") or {}).get("compatibility")
                if not isinstance(installed_compat, dict) or (
                    installed_compat.get("groupId") != group_id
                    or installed_compat.get("contractApiVersion") != compatibility["contractApiVersion"]
                    or installed_compat.get("wireApiVersion") != compatibility["wireApiVersion"]
                    or installed_compat.get("contractLock", {}).get("sha256") != candidate_pin.get("sha256")
                ):
                    adoption_needed = True
                current_members.append((member_component, member_target))
            if not adoption_needed:
                continue

            required_ids = {member["componentId"] for member in members if member.get("requiredForAdoption")}
            include_ids = required_ids | {component["componentId"] for component, _ in current_members}
            index_group = next((item for item in candidate.index.get("compatibilityGroups", []) if item.get("groupId") == group_id), None)
            if not isinstance(index_group, dict):
                raise UpdateError("COMPATIBILITY_GROUP_INCOMPLETE", f"Release index omits compatibility group {group_id}.")
            if (
                index_group.get("contractApiVersion") != compatibility["contractApiVersion"]
                or index_group.get("wireApiVersion") != compatibility["wireApiVersion"]
                or index_group.get("contractLock") != candidate_pin
            ):
                raise UpdateError("COMPATIBILITY_GROUP_MISMATCH", f"Release index compatibility pins differ for {group_id}.")
            index_members = index_group.get("members")
            if not isinstance(index_members, list):
                raise UpdateError("COMPATIBILITY_GROUP_INCOMPLETE", f"Release index has no member pins for {group_id}.")
            pinned_ids = {item.get("componentId") for item in index_members if isinstance(item, dict)}
            if not include_ids.issubset(pinned_ids):
                missing = sorted(include_ids - pinned_ids)
                raise UpdateError("COMPATIBILITY_GROUP_INCOMPLETE", f"Compatibility group {group_id} is missing releases for {', '.join(missing)}.")
            for component_id in sorted(include_ids):
                member_component = self.components[component_id]
                member_target = self._target_for(member_component)
                assert member_target is not None
                if component_id not in expanded:
                    expanded[component_id] = self._candidate_from_index(candidate, member_component, member_target, channel)
                member_candidate = expanded[component_id]
                member_compat = member_candidate.manifest.get("compatibility")
                if not isinstance(member_compat, dict) or member_compat.get("groupId") != group_id or member_compat.get("contractApiVersion") != compatibility["contractApiVersion"] or member_compat.get("wireApiVersion") != compatibility["wireApiVersion"] or member_compat.get("contractLock") != candidate_pin:
                    raise UpdateError("COMPATIBILITY_GROUP_MISMATCH", f"Member {component_id} does not share the exact {group_id} contract pins.")
                exact_index_member = next((item for item in index_members if item.get("componentId") == component_id and item.get("target") == member_target["target"]), None)
                if exact_index_member is None or exact_index_member.get("version") != member_candidate.manifest["version"] or exact_index_member.get("manifestDigest") != member_candidate.manifest_digest:
                    raise UpdateError("COMPATIBILITY_MEMBER_PIN_MISMATCH", f"Index member digest does not match {component_id} release manifest.")
        return expanded

    def _candidate_from_index(self, owner: Candidate, component: dict[str, Any], target: dict[str, Any], channel: str) -> Candidate:
        publisher = self.publishers[component["publisher"]]
        if publisher["repository"] != owner.index["repository"]:
            raise UpdateError("COMPATIBILITY_GROUP_CROSS_REPOSITORY", "Compatibility group members must be published by one trusted repository index.")
        entries = [item for item in owner.index["releases"] if isinstance(item, dict) and item.get("componentId") == component["componentId"] and item.get("target") == target["target"]]
        if len(entries) != 1:
            raise UpdateError("COMPATIBILITY_GROUP_INCOMPLETE", f"Release index has no unique manifest for group member {component['componentId']}.")
        entry = entries[0]
        self._require_github_asset_uri(entry.get("manifestUri"), publisher["repository"])
        raw = self._get_bytes(entry["manifestUri"])
        manifest = json.loads(raw)
        digest = manifest.get("manifestDigest") if isinstance(manifest, dict) else None
        if digest != entry.get("manifestDigest") or digest != _digest_json(manifest, "manifestDigest"):
            raise UpdateError("MANIFEST_DIGEST_MISMATCH", f"Digest mismatch for compatibility member {component['componentId']}.")
        self._validate_manifest(manifest, entry, component, target, publisher, channel, owner.index)
        artifact_digest = manifest["artifact"].get("digest", manifest["artifact"].get("sha256"))
        return Candidate(component, manifest, digest, artifact_digest, entry["manifestUri"], owner.index, owner.index_uri)

    def _resolve_plan_candidates(self, plan: dict[str, Any], channel: str) -> dict[str, Candidate]:
        component_ids = [item["componentId"] for item in plan["components"]]
        candidates: dict[str, Candidate] = {}
        for component_id in component_ids:
            component = self.components[component_id]
            target = self._target_for(component)
            if target is None:
                raise UpdateError("UNSUPPORTED_TARGET", f"Component {component_id} is no longer supported on this host.")
            candidates[component_id] = self._candidate(component, target, channel)
        candidates = self._expand_compatibility_groups(candidates, channel)
        selected = {key: value for key, value in candidates.items() if key in set(component_ids)}
        rebuilt_components = [{"componentId": key, "version": item.manifest["version"], "manifestDigest": item.manifest_digest, "artifactDigest": item.manifest["artifact"].get("digest", item.manifest["artifact"].get("sha256")), "restartGroup": item.component["restart"]["group"]} for key, item in sorted(selected.items())]
        material = {"schemaVersion": 1, "channel": channel, "components": rebuilt_components}
        if "sha256:" + hashlib.sha256(canonical_jcs(material)).hexdigest() != plan["planDigest"]:
            raise UpdateError("PLAN_CHANGED", "Trusted release inputs changed since check; run check again.", retryable=True)
        return selected

    def _stage_candidate(self, candidate: Candidate, plan_root: Path) -> dict[str, Any]:
        manifest = candidate.manifest
        artifact = manifest["artifact"]
        filename = PurePosixPath(urllib.parse.urlsplit(artifact["uri"]).path).name
        if not filename or filename in {".", ".."}:
            raise UpdateError("INVALID_MANIFEST", "Artifact URI has no safe basename.")
        component_root = plan_root / candidate.component["componentId"]
        component_root.mkdir(mode=0o700)
        archive_path = component_root / filename
        payload = self._get_bytes(artifact["uri"])
        if len(payload) != artifact["sizeBytes"] or "sha256:" + hashlib.sha256(payload).hexdigest() != artifact["sha256"]:
            raise UpdateError("ARTIFACT_DIGEST_MISMATCH", f"Downloaded artifact digest/size mismatch for {candidate.component['componentId']}.")
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
                raise UpdateError("INVALID_CATALOG", f"{candidate.component['componentId']} has no Product bundle service mapping.")
            module = self._load_service_bundle()
            bundle_root = payload_root
            if (payload_root / service).is_dir():
                bundle_root = payload_root / service
            if (bundle_root / "manifest.json").is_file() is False:
                candidates = [path for path in payload_root.rglob("manifest.json") if path.parent.is_dir()]
                if len(candidates) == 1:
                    bundle_root = candidates[0].parent
            inner = module.validate_bundle(bundle_root, expected_service=service)
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
                embedded = next((item for item in embedded_runtime_dependencies if item.get("component_id") == dependency["componentId"]), None)
                if embedded is None or not _version_satisfies(embedded.get("version"), dependency["versionRange"]):
                    raise UpdateError("BUILD_DEPENDENCY_UNSATISFIED", f"Bundle for {candidate.component['componentId']} does not embed required build dependency {dependency['componentId']} {dependency['versionRange']}.")
            installed_path = module.stage_release(bundle_root, install_root=self.install_root)
            bundle_identity = inner.get("artifact_digest")
            if not isinstance(bundle_identity, str) or BUNDLE_ID_PATTERN.fullmatch(bundle_identity) is None or installed_path.name != inner.get("version"):
                raise UpdateError("INVALID_BUNDLE", f"Installed {service} bundle pointer identity is invalid.")
        else:
            raise UpdateError("UNSUPPORTED_ARTIFACT", "Linux staging supports only native binary and Python bundle releases.")
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
        self._write_release_receipt(item)
        return item

    @staticmethod
    def _write_private_file(path: Path, value: bytes) -> None:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
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

    def _extract_tar(self, archive: Path, destination: Path, expected_files: dict[str, str] | None) -> None:
        destination.mkdir(mode=0o700)
        found: dict[str, str] = {}
        try:
            with tarfile.open(archive, "r:*") as tar:
                for member in tar.getmembers():
                    path = _safe_relative(member.name.rstrip("/"), field="tar member")
                    target = destination.joinpath(*path.parts)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True, mode=0o755)
                        continue
                    if not member.isfile():
                        raise UpdateError("UNSAFE_ARTIFACT", f"Archive contains a link or special file: {member.name}")
                    if member.size > 2_000_000_000:
                        raise UpdateError("UNSAFE_ARTIFACT", f"Archive member is too large: {member.name}")
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    source = tar.extractfile(member)
                    if source is None:
                        raise UpdateError("INVALID_ARTIFACT", f"Archive member cannot be read: {member.name}")
                    with source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    found[path.as_posix()] = hashlib.sha256(target.read_bytes()).hexdigest()
        except (OSError, tarfile.TarError) as error:
            raise UpdateError("INVALID_ARTIFACT", f"Cannot safely extract release archive: {error}") from error
        if expected_files is not None:
            wanted = {name: digest.removeprefix("sha256:") for name, digest in expected_files.items()}
            if found != wanted:
                raise UpdateError("PAYLOAD_FILE_DIGEST_MISMATCH", "Native payload files do not match the manifest file map.")

    @staticmethod
    def _normalize_payload(root: Path, entrypoint: str) -> None:
        for current, directory_names, file_names in os.walk(root, followlinks=False):
            current_path = Path(current)
            current_path.chmod(0o755)
            for name in directory_names:
                path = current_path / name
                if path.is_symlink() or not path.is_dir():
                    raise UpdateError("UNSAFE_ARTIFACT", f"Payload contains an unsafe directory: {path}")
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
            raise UpdateError("INVALID_MANIFEST", f"Native {component_id} manifest digest is invalid.")
        release_identity = f"{version}--{manifest_digest.removeprefix('sha256:')}"
        release = root / "releases" / release_identity
        if release.exists():
            existing_manifest = _read_object(release / "component-manifest.json", "existing component manifest")
            self._validate_manifest_digest(existing_manifest, candidate.manifest_digest)
            if existing_manifest != candidate.manifest:
                raise UpdateError("RELEASE_COLLISION", f"Native release identity collision for {component_id} {version}.")
            return release
        root.mkdir(parents=True, exist_ok=True, mode=0o755)
        (root / "releases").mkdir(exist_ok=True, mode=0o755)
        if payload_root.is_symlink() or not payload_root.is_dir():
            raise UpdateError("UNSAFE_ARTIFACT", "Staged native payload root is not a real directory.")
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
            raise UpdateError("INVALID_INSTALLED_RELEASE", "Component manifest JCS digest is invalid.")

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
            raise UpdateError("INVALID_STAGE", "Staged plan identity or channel differs from apply confirmation.")
        plan_components = plan.get("components")
        if not isinstance(plan_components, list) or not plan_components:
            raise UpdateError("INVALID_STAGE", "Staged plan has no checked component list.")
        plan_material = {
            "schemaVersion": 1,
            "channel": plan["channel"],
            "components": plan_components,
        }
        expected_digest = "sha256:" + hashlib.sha256(canonical_jcs(plan_material)).hexdigest()
        expected_id = "plan-" + expected_digest.split(":", 1)[1][:32]
        if expected_digest != expected_plan_digest or expected_id != expected_plan_id:
            raise UpdateError("INVALID_STAGE", "Staged plan digest does not match its canonical contents.")
        if len(plan_components) != len(record["components"]):
            raise UpdateError("INVALID_STAGE", "Staged component set differs from the checked plan.")

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
                raise UpdateError("INVALID_STAGE", "Checked plan component metadata is invalid or duplicated.")
            planned_by_id[planned["componentId"]] = planned

        staged_ids: set[str] = set()
        stage_root = self.state_root / "staged" / expected_plan_id
        for item in record["components"]:
            item_fields = plan_fields | {
                "manifest", "releasePath", "archivePath", "releaseIdentity",
                "pointerIdentity", "bundleIdentity",
            }
            if (
                not isinstance(item, dict)
                or set(item) != item_fields
                or not isinstance(item.get("componentId"), str)
                or item.get("componentId") not in self.components
                or item["componentId"] in staged_ids
                or not isinstance(item.get("releasePath"), str)
                or not isinstance(item.get("archivePath"), str)
            ):
                raise UpdateError("INVALID_STAGE", "Staged component identity is invalid or duplicated.")
            component_id = item["componentId"]
            staged_ids.add(component_id)
            planned = planned_by_id.get(component_id)
            if planned is None or any(item.get(key) != planned.get(key) for key in plan_fields):
                raise UpdateError("INVALID_STAGE", f"Staged metadata differs from the checked plan for {component_id}.")
            manifest = item.get("manifest")
            self._validate_manifest_digest(manifest, item.get("manifestDigest"))
            if (
                manifest.get("componentId") != component_id
                or manifest.get("version") != item["version"]
                or item.get("releaseIdentity") != item.get("manifestDigest")
            ):
                raise UpdateError("INVALID_STAGE", f"Staged manifest identity differs for {component_id}.")
            artifact = manifest.get("artifact")
            artifact_digest = artifact.get("sha256") if isinstance(artifact, dict) else None
            if (
                not _valid_digest(artifact_digest)
                or item.get("artifactDigest") != artifact_digest
                or item.get("restartGroup") != self.components[component_id].get("restart", {}).get("group")
            ):
                raise UpdateError("INVALID_STAGE", f"Staged artifact identity differs for {component_id}.")

            component = self.components[component_id]
            service = component.get("pythonBundleService")
            if service:
                bundle_identity = item.get("bundleIdentity")
                if not isinstance(bundle_identity, str) or BUNDLE_ID_PATTERN.fullmatch(bundle_identity) is None:
                    raise UpdateError("INVALID_STAGE", f"Staged Python bundle identity is invalid for {component_id}.")
                expected_release = self.install_root / "services" / service / "releases" / bundle_identity
            else:
                expected_pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
                if item.get("bundleIdentity") is not None:
                    raise UpdateError("INVALID_STAGE", f"Native component {component_id} has a Python bundle identity.")
                expected_release = self.install_root / "components" / component_id / "releases" / expected_pointer
            if item.get("pointerIdentity") != expected_release.name:
                raise UpdateError("INVALID_STAGE", f"Staged pointer identity differs for {component_id}.")
            release_path = Path(item["releasePath"])
            if release_path != expected_release or release_path.is_symlink() or not release_path.is_dir():
                raise UpdateError("INVALID_STAGE", f"Staged release is missing or unsafe for {component_id}.")
            if not self._path_is_under(release_path, self.install_root):
                raise UpdateError("INVALID_STAGE", f"Staged release escaped the install root for {component_id}.")
            if service:
                try:
                    inner = self._load_service_bundle().validate_bundle(
                        release_path, expected_service=service
                    )
                except Exception as error:
                    raise UpdateError("INVALID_STAGE", f"Staged Python bundle failed revalidation for {component_id}: {error}") from error
                if (
                    inner.get("schema_version") != 2
                    or inner.get("version") != item["bundleIdentity"]
                    or inner.get("artifact_digest") != item["bundleIdentity"]
                ):
                    raise UpdateError("INVALID_STAGE", f"Staged Python bundle identity differs for {component_id}.")
            else:
                local_manifest = _read_object(
                    release_path / "component-manifest.json",
                    f"staged {component_id} release manifest",
                )
                self._validate_manifest_digest(local_manifest, item["manifestDigest"])
                if local_manifest != manifest:
                    raise UpdateError("INVALID_STAGE", f"Staged native manifest changed for {component_id}.")

            receipt = self._read_release_receipt(component_id, item["releaseIdentity"])
            if (
                receipt is None
                or receipt.get("manifest") != manifest
                or receipt.get("artifactDigest") != item["artifactDigest"]
                or receipt.get("version") != item["version"]
                or receipt.get("bundleIdentity") != item.get("bundleIdentity")
            ):
                raise UpdateError("INVALID_STAGE", f"Verified release receipt is missing or inconsistent for {component_id}.")

            archive_path = Path(item["archivePath"])
            if (
                archive_path.parent != stage_root / component_id
                or archive_path.is_symlink()
                or not archive_path.is_file()
            ):
                raise UpdateError("INVALID_STAGE", f"Staged archive is missing or unsafe for {component_id}.")

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
            installed = self._installed(self.components[component_id])
            captured.append({
                "componentId": component_id,
                "version": installed.get("activeVersion"),
                "releaseIdentity": installed.get("releaseIdentity"),
                "manifestDigest": installed.get("manifestDigest"),
                "artifactDigest": installed.get("artifactDigest"),
                "pointerIdentity": installed.get("pointerIdentity"),
                "bundleIdentity": installed.get("bundleIdentity"),
                "identityAttested": installed.get("identityAttested") is True,
            })
        return captured

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
            raise UpdateError("GATE_UNKNOWN", "Maintenance broker did not return a durable transaction token.", retryable=True)
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
            raise UpdateError("TRANSACTION_CHANGED", "The updater journal changed while a maintenance request was refused.", retryable=True)
        transaction_path.unlink()
        self._fsync_directory(transaction_path.parent)

    def _end_maintenance(self, transaction: dict[str, Any], *, outcome: str, healthy: bool) -> None:
        token = transaction.get("maintenanceToken")
        if not isinstance(token, str):
            token = self._begin_maintenance(transaction)
        transaction_id = _maintenance_request_id(transaction)
        request_id = f"cyrene-update-end-{transaction['planId']}-{outcome.lower()}"
        result = self._broker_request("EndMaintenance", {
            "request_id": transaction_id,
            "target_kind": transaction["targetKind"],
            "maintenance_token": token,
            "outcome": outcome,
            "healthy": healthy,
        }, request_id=request_id)
        allowed = {"READY", "SUCCESS", "ROLLED_BACK"} if healthy else {"MAINTENANCE_ACTIVE", "FAILED"}
        if result.get("status") not in allowed:
            raise UpdateError("GATE_END_FAILED", f"Maintenance broker did not release the gate: {result.get('status', 'unknown')}.", retryable=True)

    def _activate_transaction(self, transaction: dict[str, Any]) -> None:
        for item in transaction["components"]:
            component = self.components[item["componentId"]]
            previous = next(old for old in transaction["previous"] if old["componentId"] == item["componentId"])
            if component.get("pythonBundleService"):
                bundle = self._load_service_bundle()
                bundle_identity = item.get("bundleIdentity")
                if not _valid_digest(bundle_identity):
                    raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", f"Staged Python bundle identity is missing for {item['componentId']}.")
                bundle.activate_release(
                    component["pythonBundleService"],
                    bundle_identity,
                    install_root=self.install_root,
                    expected_current_version=previous.get("bundleIdentity"),
                )
                self._write_active_receipt(item)
            else:
                pointer_identity = _native_release_pointer_identity(item)
                self._activate_native(component["componentId"], pointer_identity, expected_current=previous.get("pointerIdentity"))
                self._write_active_receipt({
                    **item,
                    "releaseIdentity": item.get("releaseIdentity", item.get("manifestDigest")),
                    "bundleIdentity": None,
                })

    def _activate_native(self, component_id: str, pointer_identity: str | None, *, expected_current: str | None) -> None:
        root = self.install_root / "components" / component_id
        active = root / "active"
        current = self._active_native_pointer_identity(component_id)
        if current != expected_current:
            raise UpdateError("ACTIVE_VERSION_CHANGED", f"Active {component_id} pointer changed since confirmation.", retryable=True)
        if pointer_identity is not None:
            version, pointer_digest = _native_release_directory_identity(pointer_identity)
            if VERSION_PATTERN.fullmatch(version) is None or (pointer_digest is not None and len(pointer_digest) != 64):
                raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", f"Native {component_id} release pointer identity is invalid.")
            release = root / "releases" / pointer_identity
            if release.is_symlink() or not release.is_dir():
                raise UpdateError("INVALID_INSTALLED_RELEASE", f"Native {component_id} target release is missing or unsafe.")
            manifest = _read_object(release / "component-manifest.json", f"{component_id} target release manifest")
            expected_digest = "sha256:" + pointer_digest if pointer_digest is not None else manifest.get("manifestDigest")
            self._validate_manifest_digest(manifest, expected_digest)
            if manifest.get("componentId") != component_id or manifest.get("version") != version:
                raise UpdateError("INVALID_INSTALLED_RELEASE", f"Native {component_id} pointer differs from its target manifest.")
        temporary = root / f".active-{uuid.uuid4().hex}"
        try:
            if pointer_identity is None:
                self._remove_symlink_if_target(active, f"releases/{current}" if current is not None else "")
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
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active pointer is not a symlink for {component_id}.")
        match = re.fullmatch(r"releases/([^/]+)", os.readlink(active))
        if match is None:
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active pointer has an unsafe target for {component_id}.")
        version, pointer_digest = _native_release_directory_identity(match.group(1))
        if VERSION_PATTERN.fullmatch(version) is None or (pointer_digest is not None and len(pointer_digest) != 64):
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active pointer identity is unsafe for {component_id}.")
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
                raise UpdateError("SERVICE_NOT_MANAGED", "The staged component has no supported, catalog-matched local service.")
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
                candidate = next(item for item in transaction["components"] if item["componentId"] == previous["componentId"])
                if component.get("pythonBundleService"):
                    active = self.install_root / "services" / component["pythonBundleService"] / "active"
                    current = self._active_python_pointer_identity(component["pythonBundleService"])
                    allowed_current = {previous.get("bundleIdentity"), candidate.get("bundleIdentity")}
                    if current not in allowed_current:
                        raise UpdateError("ROLLBACK_CONFLICT", f"Active {previous['componentId']} bundle pointer is outside the transaction identities.")
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
                        raise UpdateError("ROLLBACK_CONFLICT", f"Active {previous['componentId']} native pointer is outside the transaction identities.")
                    self._activate_native(
                        previous["componentId"],
                        previous.get("pointerIdentity"),
                        expected_current=current,
                    )
                    self._restore_active_receipt(previous["componentId"], previous)
            self._restart_transaction(transaction)
            self._health_transaction({**transaction, "components": [
                {**item, "manifest": self._installed(self.components[item["componentId"]]).get("manifest") or item["manifest"]}
                for item in transaction["components"]
            ]})
            return True, "Prior active versions were restored and health-checked; gate may be released."
        except Exception as error:  # noqa: BLE001 - rollback must retain the gate for every unexpected failure.
            return False, f"Rollback could not be verified: {error}; maintenance gate remains held."

    def _active_python_pointer_identity(self, service: str) -> str | None:
        try:
            active = self._load_service_bundle().resolve_active_release(service, install_root=self.install_root)
            return active[1] if active is not None else None
        except Exception as error:
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active {service} bundle pointer failed integrity validation: {error}") from error

    def _remove_symlink_if_target(self, path: Path, target: str) -> None:
        if path.is_symlink() and os.readlink(path) == target:
            path.unlink()
            self._fsync_directory(path.parent)
        elif path.exists() or path.is_symlink():
            raise UpdateError("ROLLBACK_CONFLICT", f"Active pointer changed unexpectedly at {path}.")

    def _recover_transaction(self, transaction: dict[str, Any], transaction_path: Path, stage_path: Path, confirmation: dict[str, Any]) -> dict[str, Any]:
        if transaction.get("planDigest") != confirmation["planDigest"] or transaction.get("planId") != confirmation["planId"]:
            raise UpdateError("TRANSACTION_MISMATCH", "An unresolved transaction exists with a different confirmed plan.")
        self._validate_recovery_identity(transaction)
        if transaction.get("phase") == "succeeded":
            return self._applied_result(transaction)
        if transaction.get("phase") == "rolled_back":
            raise UpdateError("APPLY_ROLLED_BACK", transaction.get("rollbackMessage", "Previous attempt rolled back."))
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
            self._end_maintenance(transaction, outcome="ROLLED_BACK" if healthy else "FAILED", healthy=healthy)
        except UpdateError as error:
            transaction["phase"] = "rollback_required"
            transaction["recoveryError"] = str(error)
            _atomic_json(transaction_path, transaction)
            raise UpdateError("ROLLBACK_GATE_HELD", f"Interrupted update recovery could not release maintenance: {error}", retryable=True) from error
        transaction["phase"] = "rolled_back" if healthy else "rollback_required"
        transaction["rollbackMessage"] = message
        transaction.pop("maintenanceToken", None)
        _atomic_json(transaction_path, transaction)
        if not healthy:
            raise UpdateError("ROLLBACK_UNHEALTHY", message, retryable=True)
        raise UpdateError("INTERRUPTED_UPDATE_ROLLED_BACK", f"Interrupted transaction was rolled back. {message}; run check and stage again before retrying.")

    def _validate_recovery_identity(self, transaction: dict[str, Any]) -> None:
        """Fail held when an unfinished journal lacks exact release identities."""

        components = transaction.get("components")
        previous = transaction.get("previous")
        if transaction.get("schemaVersion") != 2 or not isinstance(components, list) or not components or not isinstance(previous, list):
            raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", "An unfinished legacy update journal has no verified pointer identities; maintenance remains held for operator recovery.", retryable=True)
        component_ids: set[str] = set()
        for item in components:
            if not isinstance(item, dict) or not isinstance(item.get("componentId"), str):
                raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", "Update journal contains an invalid candidate component identity.", retryable=True)
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
                raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", f"Update journal candidate identity is incomplete for {component_id}.", retryable=True)
            component_ids.add(component_id)
            if component.get("pythonBundleService"):
                bundle_identity = item.get("bundleIdentity")
                if not isinstance(bundle_identity, str) or BUNDLE_ID_PATTERN.fullmatch(bundle_identity) is None or item.get("pointerIdentity") != bundle_identity:
                    raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", f"Update journal Python pointer identity is incomplete for {component_id}.", retryable=True)
            else:
                expected_pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
                if item.get("bundleIdentity") is not None or item.get("pointerIdentity") != expected_pointer:
                    raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", f"Update journal native pointer identity is incomplete for {component_id}.", retryable=True)

        previous_ids: set[str] = set()
        previous_fields = {
            "componentId", "version", "releaseIdentity", "manifestDigest", "artifactDigest",
            "pointerIdentity", "bundleIdentity", "identityAttested",
        }
        for item in previous:
            if not isinstance(item, dict) or set(item) != previous_fields:
                raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", "Update journal has an incomplete prior release identity; maintenance remains held.", retryable=True)
            component_id = item["componentId"]
            component = self.components.get(component_id)
            if component_id not in component_ids or component is None or component_id in previous_ids or not isinstance(item.get("identityAttested"), bool):
                raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", "Update journal prior component set or identity state is invalid.", retryable=True)
            previous_ids.add(component_id)
            if item.get("version") is None:
                if any(item.get(key) is not None for key in ("releaseIdentity", "manifestDigest", "artifactDigest", "pointerIdentity", "bundleIdentity")):
                    raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", f"Empty prior state has stray release identity for {component_id}.", retryable=True)
                continue
            if not isinstance(item.get("version"), str) or VERSION_PATTERN.fullmatch(item["version"]) is None:
                raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", f"Prior release version is invalid for {component_id}.", retryable=True)
            if component.get("pythonBundleService"):
                bundle_identity = item.get("bundleIdentity")
                if not isinstance(bundle_identity, str) or BUNDLE_ID_PATTERN.fullmatch(bundle_identity) is None or item.get("pointerIdentity") != bundle_identity:
                    raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", f"Prior Python pointer identity is incomplete for {component_id}.", retryable=True)
            else:
                pointer = item.get("pointerIdentity")
                version, digest = _native_release_directory_identity(pointer) if isinstance(pointer, str) else ("", None)
                if pointer is None or version != item["version"] or (digest is not None and item.get("manifestDigest") != "sha256:" + digest):
                    raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", f"Prior native pointer identity is incomplete for {component_id}.", retryable=True)
            if item["identityAttested"] and (
                not _valid_digest(item.get("releaseIdentity"))
                or item.get("manifestDigest") != item.get("releaseIdentity")
                or not _valid_digest(item.get("artifactDigest"))
            ):
                raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", f"Prior verified receipt identity is incomplete for {component_id}.", retryable=True)
        if previous_ids != component_ids:
            raise UpdateError("TRANSACTION_IDENTITY_UNKNOWN", "Update journal prior component set differs from the candidate set.", retryable=True)

    def _recover_end_pending(self, transaction: dict[str, Any], transaction_path: Path) -> dict[str, Any]:
        """Reconcile a durable EndMaintenance request without repeating a restart."""

        phase = transaction["phase"]
        readiness = self._readiness_for(transaction["targetKind"], requires_restart=True, force=True)
        gate_status = readiness.get("status")
        if gate_status not in {"READY", "MAINTENANCE_ACTIVE"}:
            raise UpdateError(
                "GATE_UNKNOWN",
                f"Cannot reconcile the durable maintenance completion while broker state is {gate_status!r}; journal and gate remain held.",
                retryable=True,
            )

        if phase == "success_end_pending":
            expected = {
                item["componentId"]: item for item in transaction["components"]
            }
            current = {item["componentId"]: item for item in self._capture_active_versions(transaction["components"])}
            if not self._same_release_identities(expected, current, require_attested=True):
                raise UpdateError("TRANSACTION_STATE_MISMATCH", "Active releases differ from the healthy plan recorded before EndMaintenance; refusing to change service state during recovery.", retryable=True)
            self._health_transaction(transaction)
            if gate_status == "MAINTENANCE_ACTIVE":
                self._end_maintenance(transaction, outcome="SUCCESS", healthy=True)
            transaction["phase"] = "succeeded"
            transaction.pop("maintenanceToken", None)
            transaction.pop("recoveryError", None)
            _atomic_json(transaction_path, transaction)
            return self._applied_result(transaction)

        expected_previous = {item["componentId"]: item for item in transaction.get("previous", [])}
        current_previous = {item["componentId"]: item for item in self._capture_active_versions(transaction["components"])}
        if not self._same_release_identities(expected_previous, current_previous, require_attested=False):
            raise UpdateError("TRANSACTION_STATE_MISMATCH", "Restored releases differ from the rollback identities recorded before EndMaintenance; refusing to release the maintenance gate.", retryable=True)
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
        fields = ("version", "releaseIdentity", "manifestDigest", "artifactDigest", "pointerIdentity", "bundleIdentity")
        for component_id, item in expected.items():
            active = current[component_id]
            if any(item.get(field) != active.get(field) for field in fields):
                return False
            if require_attested and active.get("identityAttested") is not True:
                return False
        return True

    def _applied_result(self, transaction: dict[str, Any]) -> dict[str, Any]:
        plan = {"planId": transaction["planId"], "planDigest": transaction["planDigest"], "channel": transaction.get("channel", DEFAULT_CHANNEL), "phase": "succeeded", "components": [
            {"componentId": item["componentId"], "version": item["version"], "manifestDigest": item["manifestDigest"], "artifactDigest": item["artifactDigest"], "restartGroup": item["restartGroup"]}
            for item in transaction["components"]
        ]}
        rows = [self._result_component(self.components[item["componentId"]], self._readiness(self.components[item["componentId"]]), phase="current", update_available=False) for item in transaction["components"]]
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
            raise UpdateError("INVALID_HTTP_JSON", f"Response from {uri} is not valid JSON.", retryable=True) from error

    def _get_bytes(self, uri: str) -> bytes:
        if not isinstance(uri, str) or urllib.parse.urlsplit(uri).scheme != "https":
            raise UpdateError("UNTRUSTED_URI", "Release assets must use HTTPS.")
        request = urllib.request.Request(uri, headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/octet-stream"})
        try:
            with self.opener(request, timeout=30) as response:
                final_host = urllib.parse.urlsplit(response.geturl()).hostname or ""
                if final_host not in {"github.com", "api.github.com"} and not final_host.endswith(".githubusercontent.com"):
                    raise UpdateError("UNTRUSTED_REDIRECT", f"GitHub asset redirected to an untrusted host: {final_host}.")
                data = response.read(2_000_000_001)
                if len(data) > 2_000_000_000:
                    raise UpdateError("ARTIFACT_TOO_LARGE", "Release asset exceeds the 2 GB safety limit.")
                return data
        except UpdateError:
            raise
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            raise UpdateError("NETWORK_ERROR", f"Cannot download trusted release asset: {error}", retryable=True) from error

    @staticmethod
    def _require_github_asset_uri(uri: Any, repository: str) -> None:
        if not isinstance(uri, str):
            raise UpdateError("UNTRUSTED_URI", "Release index contains a non-string asset URI.")
        parts = urllib.parse.urlsplit(uri)
        if parts.scheme != "https" or parts.hostname != "github.com" or not parts.path.startswith(f"/{repository}/releases/download/"):
            raise UpdateError("UNTRUSTED_URI", f"Release asset URI is outside the pinned repository {repository}.")

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
    ) -> None:
        if Path(repository).name == "" or not _valid_digest(digest):
            raise UpdateError("INVALID_ATTESTATION", "Attestation subject identity is invalid.")
        with tempfile.NamedTemporaryFile(prefix="cyrene-attest-", suffix="-" + Path(subject_name).name, delete=False) as stream:
            stream.write(payload)
            temporary_path = Path(stream.name)
        try:
            if shutil.which("gh") is None:
                raise UpdateError("ATTESTATION_VERIFIER_MISSING", "GitHub CLI (`gh`) is required to verify artifact attestations.")
            completed = self.runner([
                "gh", "attestation", "verify", str(temporary_path),
                "--repo", repository,
                "--signer-workflow", workflow,
                "--source-ref", source_ref,
                "--source-digest", source_commit,
                "--predicate-type", "https://slsa.dev/provenance/v1",
                "--cert-oidc-issuer", "https://token.actions.githubusercontent.com",
                "--format", "json",
            ], capture_output=True, text=True, timeout=60, check=False)
            if completed.returncode != 0:
                raise UpdateError("ATTESTATION_INVALID", f"GitHub artifact attestation verification failed: {completed.stderr.strip() or completed.stdout.strip()}")
            try:
                verification = json.loads(completed.stdout)
            except json.JSONDecodeError as error:
                raise UpdateError("ATTESTATION_INVALID", "GitHub CLI returned malformed attestation JSON.") from error
            if not isinstance(verification, list):
                raise UpdateError("ATTESTATION_INVALID", "GitHub CLI returned an unexpected verification result shape.")
            raw_digest = digest.split(":", 1)[1]
            matched_subject = False
            for result in verification:
                if not isinstance(result, dict):
                    continue
                verification_result = result.get("verificationResult")
                statement = verification_result.get("statement") if isinstance(verification_result, dict) else None
                if not isinstance(statement, dict) or statement.get("predicateType") != "https://slsa.dev/provenance/v1":
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
                raise UpdateError("ATTESTATION_SUBJECT_MISMATCH", "Verified SLSA statement does not name the expected subject and SHA-256 digest.")
        except subprocess.TimeoutExpired as error:
            raise UpdateError("ATTESTATION_TIMEOUT", "GitHub attestation verification timed out.", retryable=True) from error
        finally:
            temporary_path.unlink(missing_ok=True)

    def _run_systemctl(self, operation: str, unit: str) -> None:
        try:
            completed = self.runner(["systemctl", operation, unit], capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UpdateError("SYSTEMD_FAILED", f"Cannot {operation} {unit}: {error}") from error
        if completed.returncode != 0:
            raise UpdateError("SYSTEMD_FAILED", f"systemctl {operation} {unit} failed: {completed.stderr.strip() or completed.stdout.strip()}")

    def _wait_unit_active(self, unit: str) -> None:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                completed = self.runner(["systemctl", "is-active", "--quiet", unit], capture_output=True, text=True, timeout=5, check=False)
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
        raise UpdateError("HEALTH_CHECK_FAILED", f"{component_id} did not pass its health endpoint {url} within 90 seconds.")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def run_json_stdio(updater: ComponentUpdater, input_stream: Any = sys.stdin, output_stream: Any = sys.stdout) -> int:
    """Read exactly one fixed JSON request and emit exactly one JSON envelope."""

    line = input_stream.readline(1_048_577)
    if not line or len(line) > 1_048_576 or input_stream.readline(1):
        envelope = {
            "protocolVersion": PROTOCOL_VERSION,
            "ok": False,
            "operation": "status",
            "error": {"code": "INVALID_REQUEST", "message": "Expected exactly one JSON request line (maximum 1 MiB).", "retryable": False},
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
                "error": {"code": "INVALID_REQUEST", "message": f"Request must be valid UTF-8 JSON: {error}", "retryable": False},
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
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active release directory is missing for {component_id}: {directory}.") from error
        if (
            not stat.S_ISDIR(info.st_mode)
            or directory.is_symlink()
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
            or not stat.S_IMODE(info.st_mode) & 0o001
            or not os.access(directory, os.X_OK)
        ):
            raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active release directory is not root-owned, immutable, and traversable for {component_id}: {directory}.")
    active = root / "active"
    if not active.is_symlink():
        raise UpdateError("COMPONENT_NOT_INSTALLED", f"No active immutable release is installed for {component_id}.")
    active_info = active.lstat()
    if active_info.st_uid != 0:
        raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active pointer is not root-owned for {component_id}.")
    match = re.fullmatch(r"releases/([^/]+)", os.readlink(active))
    if match is None:
        raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active release pointer is invalid for {component_id}.")
    pointer_identity = match.group(1)
    version, pointer_digest = _native_release_directory_identity(pointer_identity)
    if VERSION_PATTERN.fullmatch(version) is None:
        raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active release pointer is invalid for {component_id}.")
    release = root / "releases" / pointer_identity
    if release.is_symlink() or not release.is_dir():
        raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active release directory is unsafe for {component_id}.")
    release_info = release.lstat()
    if (
        release_info.st_uid != 0
        or stat.S_IMODE(release_info.st_mode) & 0o022
        or not stat.S_IMODE(release_info.st_mode) & 0o001
        or not os.access(release, os.X_OK)
    ):
        raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active release directory is not root-controlled and traversable for {component_id}.")
    manifest_path = release / "component-manifest.json"
    if manifest_path.is_symlink():
        raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active release manifest is a symlink for {component_id}.")
    manifest_info = manifest_path.lstat()
    if (
        not stat.S_ISREG(manifest_info.st_mode)
        or manifest_info.st_uid != 0
        or stat.S_IMODE(manifest_info.st_mode) & 0o022
        or not stat.S_IMODE(manifest_info.st_mode) & 0o004
        or not os.access(manifest_path, os.R_OK)
    ):
        raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active release manifest is not root-owned and readable for {component_id}.")
    manifest = _read_object(manifest_path, f"{component_id} release manifest")
    if manifest.get("manifestDigest") != _digest_json(manifest, "manifestDigest"):
        raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active release manifest digest is invalid for {component_id}.")
    if (
        manifest.get("componentId") != component_id
        or manifest.get("version") != version
        or (pointer_digest is not None and manifest.get("manifestDigest") != "sha256:" + pointer_digest)
        or (pointer_digest is None and pointer_identity != version)
    ):
        raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active release pointer does not match its manifest for {component_id}.")
    artifact = manifest.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("kind") != "native-binary":
        raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active release is not a native binary for {component_id}.")
    relative = _safe_relative(artifact.get("entrypoint"), field="artifact.entrypoint")
    binary = release.joinpath(*relative.parts)
    if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise UpdateError("INVALID_INSTALLED_RELEASE", f"Active entrypoint is missing or not executable for {component_id}.")
    arguments = artifact.get("arguments", [])
    manifest_arguments = _validate_startup_arguments(arguments, field=f"{component_id} artifact.arguments")
    unit_arguments = _validate_startup_arguments(startup_arguments, field="trusted systemd startup arguments")
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
    parser.add_argument("--broker", type=Path, default=DEFAULT_BROKER)
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
