"""Generic Workspace helpers for the attested Platform Package Runtime.

The updater owns plans, maintenance holds, and durable transaction journals.
This module projects verified package identities into Platform requests and
brokers source-owned Package Runtime operations without adding local state.
中文：只封装已验证工作负载包与 Platform 正式 Package Runtime 接缝。
"""

from __future__ import annotations

import fcntl
import grp
import hashlib
import importlib.util
import json
import os
import pwd
import re
import select
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

DEFAULT_ACTIVITY_CATALOG_PATH = Path("/var/lib/cyrene/runtime/activity-sources.json")
DEFAULT_POLICY_PATH = Path("/etc/cyrene/runtime-package-sources.json")
DEFAULT_TOKEN_DIRECTORY = Path("/etc/cyrene/runtime-activity-source-tokens")
DEFAULT_PACKAGE_RUNTIME_SOCKET = Path("/run/cyrene-package-runtime/control.sock")
DEFAULT_MAINTENANCE_SOCKET = Path("/run/cyrene/runtime-maintenance.sock")
DEFAULT_CYRENE_COMMAND = Path("/usr/bin/cyrene")
DEFAULT_SDK_PYTHON = Path("/opt/cyrene/workload-operator/current/venv/bin/python")
MAX_FILE_BYTES = 1024 * 1024
MAX_IPC_BYTES = 256 * 1024
MAX_FRAME_BYTES = 64 * 1024
SOURCE_OPERATIONS = (
    "activate",
    "recover_binding",
    "deactivate",
    "runtime_status",
    "get_installation",
)
BINDING_OPERATION_PROTOCOL = "cyrene.runtime-maintenance.binding-operations.v1"
BROKER_OPERATIONS = ("activate", "deactivate", "recover")
RUNTIME_OPERATIONS = (
    "activate",
    "deactivate",
    "get_installation",
    "recover_binding",
    "runtime_status",
)
_PACKAGE_ID = re.compile(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*){1,7}\Z")
_COMPONENT_ID = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_SOURCE_ID = re.compile(r"[a-z][a-z0-9._-]{0,159}\Z")
_BINDING_ID = re.compile(r"[a-z][a-z0-9._-]{0,159}\Z")
_INSTALLATION_ID = re.compile(r"installation-[0-9a-f]{32}\Z")
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_TYPED_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RAW_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
# CPython may omit these Linux UAPI exports even when the host kernel supports seals.
# 即使宿主内核支持密封，CPython 也可能不导出这些 Linux UAPI 常量。
_LINUX_SEAL_FCNTL_VALUES = {
    "F_ADD_SEALS": 1033,
    "F_GET_SEALS": 1034,
    "F_SEAL_SEAL": 0x0001,
    "F_SEAL_SHRINK": 0x0002,
    "F_SEAL_GROW": 0x0004,
    "F_SEAL_WRITE": 0x0008,
}
_MUTATION_TO_BROKER = {
    "activate": "activate",
    "recover_binding": "recover",
    "deactivate": "deactivate",
}
_RUNTIME_TO_BROKER = {
    "activate": "activate",
    "recover_binding": "recover",
    "deactivate": "deactivate",
    "runtime_status": "runtime_status",
    "get_installation": "get_installation",
}


class WorkloadPackageRuntimeError(RuntimeError):
    """Raised when a verified workload cannot be projected safely to Platform."""


@dataclass(frozen=True)
class WorkloadSourceUpdate:
    """Broker catalog and Package Runtime policy projection for one workload."""

    source_arguments: tuple[str, ...]
    binding_scopes: dict[str, list[dict[str, Any]]]
    source_identity: dict[str, dict[str, Any]]
    runtime_bindings: dict[str, list[dict[str, Any]]]
    expected_generation: int
    changed: bool
    previous_policy_digest: str | None
    selected_binding_ids: tuple[dict[str, str], ...]
    component_artifact_digests: dict[str, str]
    required_hold_component_ids: tuple[str, ...]


def initialize_first_core_activity_catalog(
    *,
    maintenance: Mapping[str, Any],
    request_id: str,
    source_policy: Mapping[str, Any],
    selected_rows: Sequence[Mapping[str, Any]],
    source_principals: Mapping[str, Mapping[str, Any]],
    activity_catalog_path: Path = DEFAULT_ACTIVITY_CATALOG_PATH,
    policy_path: Path = DEFAULT_POLICY_PATH,
    token_directory: Path = DEFAULT_TOKEN_DIRECTORY,
    command: Path = DEFAULT_CYRENE_COMMAND,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Register one signed workload source under the fresh CoreBootstrap hold.

    First-Core initialization derives the source and its zero-binding scopes from
    the parent's signed ``sourcePolicy``. It only accepts the true generation-zero
    state. If Broker committed generation one before an interruption, recovery
    delegates to the existing source-update reconciler. It verifies the exact source,
    token, and ownership metadata, and only repairs the matching policy CAS window;
    it never reruns ``init-catalog`` or rotates the token.
    中文：仅在真实gen0状态下初始化签名工作负载来源，精确重试只读核验。
    """

    _require_root()
    _require_request_id(request_id)
    hold = _validate_policy_update_hold(maintenance)
    if (
        hold["target_kind"] != "CORE_RUNTIME"
        or hold["transaction_id"] != request_id
        or hold["expected_catalog_generation"] != 0
    ):
        raise WorkloadPackageRuntimeError("CoreBootstrap hold is not bound to generation zero")

    activity_catalog_path = Path(activity_catalog_path)
    policy_path = Path(policy_path)
    token_directory = Path(token_directory)
    if any(
        not path.is_absolute() for path in (activity_catalog_path, policy_path, token_directory)
    ):
        raise WorkloadPackageRuntimeError("First-Core Package Runtime paths must be absolute")
    _validate_policy_parent(policy_path.parent, _effective_uid())
    maintenance_gid = _maintenance_group_id()

    catalog_missing = False
    try:
        activity_catalog_path.lstat()
    except FileNotFoundError:
        catalog_missing = True
        activity_catalog = {"schema_version": 1, "generation": 0, "sources": []}
    except OSError as error:
        raise WorkloadPackageRuntimeError("Runtime activity catalog cannot be inspected") from error
    else:
        activity_catalog = _read_activity_catalog(activity_catalog_path)
    generation, sources = _catalog_identity(activity_catalog, allow_uninitialized=True)
    if generation not in {0, 1}:
        raise WorkloadPackageRuntimeError("First-Core activity catalog generation is unexpected")
    if generation == 0 and not catalog_missing:
        raise WorkloadPackageRuntimeError(
            "A generation-zero activity catalog file is not a fresh first-Core state"
        )
    if generation == 0 and sources:
        raise WorkloadPackageRuntimeError("Generation-zero Runtime catalog must be empty")

    policy_bytes = _read_policy_bytes_optional(
        policy_path,
        group_id=_runtime_group_id(),
        owner_id=_effective_uid(),
    )
    if generation == 0 and policy_bytes is not None:
        raise WorkloadPackageRuntimeError(
            "An uninitialized activity catalog cannot adopt an existing Runtime policy"
        )

    owner_source_id, policy_source_ids = _source_policy(source_policy)
    if len(policy_source_ids) != 1:
        raise WorkloadPackageRuntimeError(
            "First-Core workload source policy must declare exactly one source owner"
        )
    if not isinstance(source_principals, Mapping):
        raise WorkloadPackageRuntimeError("First-Core source principal map is malformed")
    owner_principal = _source_principal(source_principals.get(owner_source_id), owner_source_id)
    if owner_principal["tokenPath"] != token_directory / f"{owner_source_id}.token":
        raise WorkloadPackageRuntimeError(
            "First-Core source token path differs from the official token directory"
        )
    if not isinstance(selected_rows, Sequence) or isinstance(selected_rows, (str, bytes)):
        raise WorkloadPackageRuntimeError("First-Core selected plugin rows are malformed")
    selected = tuple(selected_rows)
    selected_component_ids: list[str] = []
    for row in selected:
        component_id = row.get("componentId") if isinstance(row, Mapping) else None
        if not isinstance(component_id, str) or _COMPONENT_ID.fullmatch(component_id) is None:
            raise WorkloadPackageRuntimeError("First-Core selected plugin identities are malformed")
        selected_component_ids.append(component_id)
    if len(set(selected_component_ids)) != len(selected_component_ids):
        raise WorkloadPackageRuntimeError("First-Core selected plugin identities are malformed")
    allow_empty_standalone_source = (
        not selected_component_ids
        and source_policy.get("mode") == "standaloneOperator"
        and owner_source_id == "cyrene-plugin-standalone-operator"
    )
    if (
        allow_empty_standalone_source
        and "cy-package-runtime" not in hold["component_artifact_digests"]
    ):
        raise WorkloadPackageRuntimeError(
            "Empty standalone source registration requires the held cy-package-runtime identity"
        )
    update = build_workload_source_update(
        source_policy=source_policy,
        selected_rows=selected,
        installation_records={},
        activity_catalog={"schema_version": 1, "generation": 0, "sources": []},
        source_principals=source_principals,
        runtime_policy=None,
        remove_component_ids=tuple(selected_component_ids),
        _allow_first_core_standalone_source=allow_empty_standalone_source,
    )
    if (
        update.expected_generation != 1
        or not update.changed
        or update.previous_policy_digest is not None
        or set(update.source_identity) != {owner_source_id}
        or update.binding_scopes != {owner_source_id: []}
        or update.runtime_bindings != {owner_source_id: []}
        or (not update.required_hold_component_ids and not allow_empty_standalone_source)
    ):
        raise WorkloadPackageRuntimeError(
            "First-Core source projection must register one owner with no package bindings"
        )
    held_digests = hold["component_artifact_digests"]
    if any(
        held_digests.get(component_id) != digest
        for component_id, digest in update.component_artifact_digests.items()
    ) or any(
        component_id not in held_digests for component_id in update.required_hold_component_ids
    ):
        raise WorkloadPackageRuntimeError("First-Core source projection is outside the held plan")

    if generation == 1:
        _verify_first_core_catalog_metadata(activity_catalog_path, maintenance_gid)
        result = reconcile_workload_source_update(
            update,
            maintenance=hold,
            activity_catalog_path=activity_catalog_path,
            policy_path=policy_path,
            token_directory=token_directory,
        )
        if result is None:
            raise WorkloadPackageRuntimeError(
                "Generation-one Runtime catalog did not match the committed source update"
            )
        if len(result.get("sourceIdentities", [])) < 1:
            raise WorkloadPackageRuntimeError("First-Core Runtime catalog has no workload source")
        return result

    result = apply_workload_source_update(
        update,
        maintenance=hold,
        request_id=request_id,
        expected_policy_digest=None,
        activity_catalog_path=activity_catalog_path,
        policy_path=policy_path,
        token_directory=token_directory,
        command=command,
        runner=runner,
        expected_catalog_gid=maintenance_gid,
    )
    _verify_first_core_catalog_metadata(activity_catalog_path, maintenance_gid)
    if len(result.get("sourceIdentities", [])) < 1:
        raise WorkloadPackageRuntimeError("First-Core Runtime catalog has no workload source")
    return result


def _verify_first_core_catalog_metadata(path: Path, maintenance_gid: int) -> None:
    """Require the canonical root:maintenance-group mode-0640 catalog."""

    try:
        info = path.lstat()
    except OSError as error:
        raise WorkloadPackageRuntimeError("First-Core activity catalog is unavailable") from error
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != maintenance_gid
        or stat.S_IMODE(info.st_mode) != 0o640
        or info.st_nlink != 1
    ):
        raise WorkloadPackageRuntimeError("First-Core activity catalog ownership is unsafe")


def _bootstrap_module() -> ModuleType:
    """Load the adjacent verified bootstrap implementation without copying it."""

    module_path = Path(__file__).with_name("native_package_runtime_bootstrap.py")
    name = "_cyrene_native_package_runtime_bootstrap_for_workload"
    existing = sys.modules.get(name)
    if isinstance(existing, ModuleType):
        return existing
    spec = importlib.util.spec_from_file_location(name, module_path)
    if spec is None or spec.loader is None:
        raise WorkloadPackageRuntimeError("Package Runtime bootstrap module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON names before any identity comparison."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkloadPackageRuntimeError("Package Runtime JSON repeats a field")
        result[key] = value
    return result


def _canonical_json(value: Any) -> bytes:
    """Encode one deterministic UTF-8 JSON document."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _require_root() -> None:
    """Require the host operator context used for Platform mutations."""

    if os.geteuid() != 0:
        raise WorkloadPackageRuntimeError("Package Runtime host mutation requires root")


def _require_text(value: Any, label: str) -> str:
    """Return one bounded, nonempty protocol string."""

    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or any(ord(ch) < 32 for ch in value)
    ):
        raise WorkloadPackageRuntimeError(f"{label} is invalid")
    return value


def _require_digest(value: Any, label: str, *, typed: bool = True) -> str:
    """Require one SHA-256 identity in the requested canonical form."""

    pattern = _TYPED_SHA256 if typed else _RAW_SHA256
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise WorkloadPackageRuntimeError(f"{label} is invalid")
    return value


def _require_request_id(value: Any, label: str = "request_id") -> str:
    """Require a caller-stable and path-safe operation identifier."""

    if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
        raise WorkloadPackageRuntimeError(f"{label} is invalid")
    return value


def _effective_uid() -> int:
    """Expose effective UID for narrow test substitution."""

    return os.geteuid()


def _runtime_group_id() -> int:
    """Resolve the installed Package Runtime group without a guessed numeric ID."""

    try:
        return grp.getgrnam("cyrene").gr_gid
    except KeyError as error:
        raise WorkloadPackageRuntimeError("Cyrene Package Runtime group is unavailable") from error


def _source_policy(policy: Any) -> tuple[str, tuple[str, ...]]:
    """Validate the signed Catalog's actualProduct or standaloneOperator scope."""

    if not isinstance(policy, Mapping) or set(policy) - {
        "mode",
        "productComponentIds",
        "productSources",
        "operations",
        "sourceId",
    }:
        raise WorkloadPackageRuntimeError("Catalog sourcePolicy has an unsupported shape")
    if policy.get("operations") != list(SOURCE_OPERATIONS):
        raise WorkloadPackageRuntimeError(
            "Catalog sourcePolicy operations differ from Platform SDK"
        )
    products = policy.get("productComponentIds")
    product_sources = policy.get("productSources")
    if (
        not isinstance(products, list)
        or any(
            not isinstance(item, str) or _COMPONENT_ID.fullmatch(item) is None for item in products
        )
        or len(products) != len(set(products))
        or not isinstance(product_sources, list)
    ):
        raise WorkloadPackageRuntimeError("Catalog Product source map is malformed")
    source_map: dict[str, str] = {}
    for row in product_sources:
        if not isinstance(row, Mapping) or set(row) != {"componentId", "sourceId"}:
            raise WorkloadPackageRuntimeError("Catalog Product source row is malformed")
        component_id = row.get("componentId")
        source_id = row.get("sourceId")
        if (
            not isinstance(component_id, str)
            or component_id not in products
            or not isinstance(source_id, str)
            or _SOURCE_ID.fullmatch(source_id) is None
            or component_id in source_map
            or source_id in source_map.values()
        ):
            raise WorkloadPackageRuntimeError("Catalog Product source identity is ambiguous")
        source_map[component_id] = source_id
    if set(source_map) != set(products):
        raise WorkloadPackageRuntimeError("Catalog Product source map does not cover its owners")
    if policy.get("mode") == "actualProduct":
        if not products or "sourceId" in policy:
            raise WorkloadPackageRuntimeError("actualProduct sourcePolicy is not bound to Products")
        # Platform binding IDs have one source owner. Multi-Product fan-out needs
        # a Catalog mapping that carries one owner binding per Product.
        if len(source_map) != 1:
            raise WorkloadPackageRuntimeError(
                "one Plugin binding cannot have multiple Product owners"
            )
        return next(iter(source_map.values())), tuple(sorted(source_map.values()))
    if policy.get("mode") == "standaloneOperator":
        source_id = policy.get("sourceId")
        if products or product_sources or source_id != "cyrene-plugin-standalone-operator":
            raise WorkloadPackageRuntimeError("standaloneOperator sourcePolicy is malformed")
        return source_id, (source_id,)
    raise WorkloadPackageRuntimeError("Catalog sourcePolicy mode is unsupported")


def _catalog_identity(
    catalog: Any, *, allow_uninitialized: bool = False
) -> tuple[int, dict[str, dict[str, Any]]]:
    """Validate the complete trusted activity-source catalog readback."""

    if not isinstance(catalog, Mapping) or set(catalog) != {
        "schema_version",
        "generation",
        "sources",
    }:
        raise WorkloadPackageRuntimeError("Runtime activity catalog has an unsupported shape")
    generation = catalog.get("generation")
    rows = catalog.get("sources")
    if (
        type(catalog.get("schema_version")) is not int
        or catalog["schema_version"] != 1
        or type(generation) is not int
        or not isinstance(rows, list)
    ):
        raise WorkloadPackageRuntimeError("Runtime activity catalog identity is invalid")
    if generation < 1 and not (allow_uninitialized and generation == 0 and not rows):
        raise WorkloadPackageRuntimeError("Runtime activity catalog generation is invalid")
    sources: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) not in (
            {"source_id", "uid", "gid", "source_token_sha256"},
            {"source_id", "uid", "gid", "source_token_sha256", "binding_scopes"},
        ):
            raise WorkloadPackageRuntimeError("Runtime activity source schema is invalid")
        source_id = row.get("source_id")
        if not isinstance(source_id, str) or _SOURCE_ID.fullmatch(source_id) is None:
            raise WorkloadPackageRuntimeError("Runtime activity source ID is invalid")
        if (
            type(row.get("uid")) is not int
            or row["uid"] <= 0
            or type(row.get("gid")) is not int
            or row["gid"] <= 0
            or not isinstance(row.get("source_token_sha256"), str)
            or _RAW_SHA256.fullmatch(row["source_token_sha256"]) is None
            or source_id in sources
        ):
            raise WorkloadPackageRuntimeError("Runtime activity source trust fields are invalid")
        scopes = row.get("binding_scopes", [])
        _validate_broker_scopes(scopes)
        sources[source_id] = {
            "uid": row["uid"],
            "gid": row["gid"],
            "source_token_sha256": row["source_token_sha256"],
            "binding_scopes": [dict(scope) for scope in scopes],
        }
    if list(sources) != sorted(sources):
        raise WorkloadPackageRuntimeError("Runtime activity source rows are not sorted")
    if generation == 0 and sources:
        raise WorkloadPackageRuntimeError("Uninitialized Runtime catalog cannot contain sources")
    return generation, sources


def _validate_broker_scopes(scopes: Any) -> None:
    """Validate Platform's three-operation source-binding scopes."""

    if not isinstance(scopes, list):
        raise WorkloadPackageRuntimeError("Runtime activity binding scopes are invalid")
    ids: list[str] = []
    for scope in scopes:
        if not isinstance(scope, Mapping) or set(scope) != {
            "binding_id",
            "package_id",
            "installation_ids",
            "operations",
        }:
            raise WorkloadPackageRuntimeError("Runtime activity binding scope is malformed")
        binding_id = scope.get("binding_id")
        package_id = scope.get("package_id")
        installations = scope.get("installation_ids")
        operations = scope.get("operations")
        if (
            not isinstance(binding_id, str)
            or _BINDING_ID.fullmatch(binding_id) is None
            or not isinstance(package_id, str)
            or _PACKAGE_ID.fullmatch(package_id) is None
            or not isinstance(installations, list)
            or not installations
            or any(
                not isinstance(item, str) or _INSTALLATION_ID.fullmatch(item) is None
                for item in installations
            )
            or installations != sorted(set(installations))
            or operations != list(BROKER_OPERATIONS)
        ):
            raise WorkloadPackageRuntimeError("Runtime activity binding scope identity is invalid")
        ids.append(binding_id)
    if ids != sorted(set(ids)):
        raise WorkloadPackageRuntimeError("Runtime activity binding IDs are ambiguous")


def _runtime_policy(policy: Any) -> dict[str, Any]:
    """Validate a generic Runtime source policy while preserving owner scopes."""

    if not isinstance(policy, Mapping) or set(policy) != {
        "schema_version",
        "generation",
        "sources",
    }:
        raise WorkloadPackageRuntimeError("Package Runtime source policy schema is invalid")
    generation = policy.get("generation")
    rows = policy.get("sources")
    if (
        type(policy.get("schema_version")) is not int
        or policy["schema_version"] != 1
        or type(generation) is not int
        or generation < 1
        or not isinstance(rows, list)
    ):
        raise WorkloadPackageRuntimeError("Package Runtime source policy identity is invalid")
    source_ids: list[str] = []
    all_bindings: set[str] = set()
    normalized_sources: list[dict[str, Any]] = []
    for source in rows:
        if not isinstance(source, Mapping) or set(source) != {
            "source_id",
            "uid",
            "gid",
            "source_token_sha256",
            "bindings",
        }:
            raise WorkloadPackageRuntimeError("Package Runtime source principal is malformed")
        source_id = source.get("source_id")
        uid, gid = source.get("uid"), source.get("gid")
        token_digest = source.get("source_token_sha256")
        bindings = source.get("bindings")
        if (
            not isinstance(source_id, str)
            or _SOURCE_ID.fullmatch(source_id) is None
            or type(uid) is not int
            or uid <= 0
            or type(gid) is not int
            or gid <= 0
            or not isinstance(token_digest, str)
            or _RAW_SHA256.fullmatch(token_digest) is None
            or not isinstance(bindings, list)
            or source_id in source_ids
        ):
            raise WorkloadPackageRuntimeError("Package Runtime source identity is invalid")
        source_ids.append(source_id)
        normalized_bindings: list[dict[str, Any]] = []
        binding_ids: list[str] = []
        for binding in bindings:
            if not isinstance(binding, Mapping) or set(binding) != {
                "binding_id",
                "package_id",
                "installation_ids",
                "operations",
            }:
                raise WorkloadPackageRuntimeError("Package Runtime binding row is malformed")
            binding_id = binding.get("binding_id")
            package_id = binding.get("package_id")
            installations = binding.get("installation_ids")
            operations = binding.get("operations")
            if (
                not isinstance(binding_id, str)
                or _BINDING_ID.fullmatch(binding_id) is None
                or not isinstance(package_id, str)
                or _PACKAGE_ID.fullmatch(package_id) is None
                or not isinstance(installations, list)
                or any(
                    not isinstance(item, str) or _INSTALLATION_ID.fullmatch(item) is None
                    for item in installations
                )
                or installations != sorted(set(installations))
                or not isinstance(operations, list)
                or any(item not in RUNTIME_OPERATIONS for item in operations)
                or operations != sorted(set(operations))
                or ("activate" in operations and not installations)
                or binding_id in all_bindings
            ):
                raise WorkloadPackageRuntimeError("Package Runtime binding identity is invalid")
            all_bindings.add(binding_id)
            binding_ids.append(binding_id)
            normalized_bindings.append(
                {
                    "binding_id": binding_id,
                    "package_id": package_id,
                    "installation_ids": list(installations),
                    "operations": list(operations),
                }
            )
        if binding_ids != sorted(binding_ids):
            raise WorkloadPackageRuntimeError("Package Runtime bindings are not sorted")
        normalized_sources.append(
            {
                "source_id": source_id,
                "uid": uid,
                "gid": gid,
                "source_token_sha256": token_digest,
                "bindings": normalized_bindings,
            }
        )
    if source_ids != sorted(source_ids):
        raise WorkloadPackageRuntimeError("Package Runtime source rows are not sorted")
    return {"schema_version": 1, "generation": generation, "sources": normalized_sources}


def _broker_projection(
    runtime_sources: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Project runtime operation scopes into the Broker's admitted mutations."""

    result: dict[str, list[dict[str, Any]]] = {}
    for source in runtime_sources:
        scopes: list[dict[str, Any]] = []
        for binding in source["bindings"]:
            operations = sorted(
                _MUTATION_TO_BROKER[operation]
                for operation in binding["operations"]
                if operation in _MUTATION_TO_BROKER
            )
            if operations:
                scopes.append(
                    {
                        "binding_id": binding["binding_id"],
                        "package_id": binding["package_id"],
                        "installation_ids": list(binding["installation_ids"]),
                        "operations": sorted(set(operations)),
                    }
                )
        scopes.sort(key=lambda row: row["binding_id"])
        result[source["source_id"]] = scopes
    return result


def _source_principal(value: Any, source_id: str) -> dict[str, Any]:
    """Validate the supplied account and credential path for one source."""

    if not isinstance(value, Mapping) or set(value) - {"uid", "gid", "tokenPath"}:
        raise WorkloadPackageRuntimeError(f"Source principal is malformed for {source_id}")
    uid, gid = value.get("uid"), value.get("gid")
    token_path = value.get("tokenPath")
    if type(uid) is not int or uid <= 0 or type(gid) is not int or gid <= 0:
        raise WorkloadPackageRuntimeError(f"Source UID/GID is invalid for {source_id}")
    if not isinstance(token_path, (str, os.PathLike)):
        raise WorkloadPackageRuntimeError(f"Source token path is missing for {source_id}")
    path = Path(token_path)
    if not path.is_absolute() or ".." in path.parts:
        raise WorkloadPackageRuntimeError(f"Source token path is unsafe for {source_id}")
    return {"uid": uid, "gid": gid, "tokenPath": path}


def candidate_from_workload_rows(
    selected_row: Mapping[str, Any], staged_row: Mapping[str, Any]
) -> Any:
    """Build a Platform candidate from the verified resolver and staged rows.

    The Catalog digest for a plugin archive and Platform's aggregate package
    digest are different identities. The latter is the Package Runtime hold
    artifact; the archive digest remains separately attached to its bytes.
    中文：安装身份使用描述符聚合digest，归档digest始终单独保留。
    """

    bootstrap = _bootstrap_module()
    if not isinstance(selected_row, Mapping) or not isinstance(staged_row, Mapping):
        raise WorkloadPackageRuntimeError("Selected and staged package rows are required")
    selected_id = selected_row.get("componentId")
    staged_id = staged_row.get("componentId")
    if (
        not isinstance(selected_id, str)
        or _COMPONENT_ID.fullmatch(selected_id) is None
        or staged_id != selected_id
        or selected_row.get("artifactKind") not in (None, "plugin-package")
        or staged_row.get("artifactKind") not in (None, "plugin-package")
    ):
        raise WorkloadPackageRuntimeError("Selected and staged component identities differ")
    _source_policy(selected_row.get("sourcePolicy"))
    package_id = _require_text(staged_row.get("packageId"), "staged.packageId")
    version = _require_text(staged_row.get("version"), "staged.version")
    capability = _require_text(staged_row.get("capabilityId"), "staged.capabilityId")
    if (
        _PACKAGE_ID.fullmatch(package_id) is None
        or _VERSION.fullmatch(version) is None
        or _PACKAGE_ID.fullmatch(capability) is None
        or selected_row.get("packageId", package_id) != package_id
        or selected_row.get("version") != version
        or selected_row.get("capabilityId", capability) != capability
        or not isinstance(selected_row.get("bindingId"), str)
        or _BINDING_ID.fullmatch(selected_row["bindingId"]) is None
        or selected_row.get("bindingId") != staged_row.get("bindingId")
        or selected_row.get("sourcePolicy") != staged_row.get("sourcePolicy")
        or selected_row.get("manifestDigest") != staged_row.get("manifestDigest")
    ):
        raise WorkloadPackageRuntimeError("Selected and staged package metadata differ")
    artifact_digest = _require_digest(
        staged_row.get("packageArtifactDigest"), "staged.packageArtifactDigest"
    )
    archive_digest = _require_digest(staged_row.get("archiveDigest"), "staged.archiveDigest")
    outer_digest = staged_row.get("digest")
    if outer_digest is not None and outer_digest != archive_digest:
        raise WorkloadPackageRuntimeError(
            "Catalog archive digest differs from staged archive identity"
        )
    staged_identity = staged_row.get("stagedIdentity")
    assets = staged_identity.get("assetPaths") if isinstance(staged_identity, Mapping) else None
    if not isinstance(assets, Mapping):
        raise WorkloadPackageRuntimeError("Verified staged package asset paths are missing")
    descriptor_path = Path(_require_text(assets.get("descriptor"), "descriptor path"))
    archive_path = Path(_require_text(assets.get("archive"), "archive path"))
    if not descriptor_path.is_absolute() or not archive_path.is_absolute():
        raise WorkloadPackageRuntimeError("Verified staged package paths must be absolute")
    candidate = bootstrap.WorkloadPackageCandidate(
        component_id=selected_id,
        package_id=package_id,
        package_version=version,
        capability=capability,
        interface_version="1",
        artifact_digest=artifact_digest,
        archive_digest=archive_digest,
        descriptor_digest=_require_digest(staged_row.get("descriptorDigest"), "descriptorDigest"),
        manifest_digest=_require_digest(staged_row.get("manifestDigest"), "manifestDigest"),
        dependency_lock_digest=_require_digest(
            staged_row.get("dependencyLockDigest"), "dependencyLockDigest"
        ),
        descriptor_path=descriptor_path,
        archive_path=archive_path,
    )
    return candidate


def install_workload_package(
    selected_row: Mapping[str, Any],
    staged_row: Mapping[str, Any],
    *,
    request_id: str,
    maintenance: Mapping[str, Any],
    staging_root: Path | None = None,
    command: Path | None = None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Install one staged plugin under the caller's current PACKAGE_ONLY hold."""

    _require_root()
    candidate = candidate_from_workload_rows(selected_row, staged_row)
    _validate_phase_identity(
        staged_row, request_id, maintenance, candidate.component_id, candidate.artifact_digest
    )
    bootstrap = _bootstrap_module()
    root = staging_root or bootstrap.PACKAGE_BOOTSTRAP_STAGE_ROOT
    try:
        _directory, _request_path, request = bootstrap.stage_workload_offline_install_request(
            candidate,
            request_id=request_id,
            maintenance=dict(maintenance),
            staging_root=root,
        )
        return bootstrap.run_workload_offline_install(
            candidate,
            request,
            command=command or bootstrap.PACKAGE_RUNTIME_COMMAND,
            runner=runner,
        )
    except WorkloadPackageRuntimeError:
        raise
    except Exception as error:
        raise WorkloadPackageRuntimeError(
            "Platform offline package install did not complete"
        ) from error


def uninstall_workload_package(
    installation: Mapping[str, Any],
    *,
    component_id: str,
    request_id: str,
    maintenance: Mapping[str, Any],
    staging_root: Path | None = None,
    command: Path | None = None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Uninstall one exact UDS-verified, already-deactivated installation."""

    _require_root()
    bootstrap = _bootstrap_module()
    if not isinstance(installation, Mapping):
        raise WorkloadPackageRuntimeError("Package Runtime uninstall identity is missing")
    if not isinstance(component_id, str) or _COMPONENT_ID.fullmatch(component_id) is None:
        raise WorkloadPackageRuntimeError("Package Runtime uninstall component ID is invalid")
    if set(installation) != {
        "record_version",
        "installation_id",
        "package_id",
        "package_version",
        "artifact_digest",
        "archive_digest",
        "capabilities",
        "state",
        "verification",
        "dependencies",
        "installed_at_unix_ms",
    }:
        raise WorkloadPackageRuntimeError(
            "Package Runtime uninstall requires the exact UDS InstallationRecord"
        )
    _validate_installation_record_identity(
        {
            "packageId": installation.get("package_id"),
            "version": installation.get("package_version"),
            "packageArtifactDigest": installation.get("artifact_digest"),
        },
        installation,
    )
    artifact_digest = installation.get("artifact_digest")
    _validate_phase_identity({}, request_id, maintenance, component_id, artifact_digest)
    verification = installation["verification"]
    uninstall_identity = {
        "component_id": component_id,
        "installation_id": installation["installation_id"],
        "package_id": installation["package_id"],
        "package_version": installation["package_version"],
        "artifact_digest": installation["artifact_digest"],
        "archive_digest": installation["archive_digest"],
        "descriptor_digest": verification["descriptor_digest"],
        "manifest_digest": verification["manifest_digest"],
        "dependency_lock_digest": verification["dependency_lock_digest"],
    }
    try:
        _directory, _request_path, request = bootstrap.stage_workload_offline_uninstall_request(
            request_id=request_id,
            maintenance=dict(maintenance),
            installation=uninstall_identity,
            staging_root=staging_root or bootstrap.PACKAGE_BOOTSTRAP_STAGE_ROOT,
        )
        result = bootstrap.run_workload_offline_uninstall(
            request,
            command=command or bootstrap.PACKAGE_RUNTIME_COMMAND,
            runner=runner,
        )
    except WorkloadPackageRuntimeError:
        raise
    except Exception as error:
        raise WorkloadPackageRuntimeError(
            "Platform offline package uninstall did not complete"
        ) from error
    return {"installation": result["installation"], "already_absent": result["already_absent"]}


def _validate_phase_identity(
    staged_row: Mapping[str, Any],
    request_id: str,
    maintenance: Mapping[str, Any],
    component_id: str,
    artifact_digest: Any,
) -> None:
    """Bind one unique operation request to the exact parent PACKAGE_ONLY plan."""

    _require_request_id(request_id)
    if not isinstance(maintenance, Mapping) or set(maintenance) != {
        "transaction_id",
        "maintenance_token",
        "target_kind",
        "plan_id",
        "plan_digest",
        "component_artifact_digests",
        "expected_gate_generation",
        "expected_catalog_generation",
    }:
        raise WorkloadPackageRuntimeError("PACKAGE_ONLY hold identity is malformed")
    try:
        _require_request_id(maintenance.get("transaction_id"), "maintenance.transaction_id")
        _require_text(maintenance.get("maintenance_token"), "maintenance token")
        _require_digest(maintenance.get("plan_digest"), "maintenance.plan_digest")
        _require_digest(artifact_digest, "package artifact digest")
    except WorkloadPackageRuntimeError:
        raise WorkloadPackageRuntimeError(
            "PACKAGE_ONLY hold identity differs from package"
        ) from None
    digest_map = maintenance.get("component_artifact_digests")
    if (
        maintenance.get("target_kind") != "PACKAGE_ONLY"
        or not isinstance(maintenance.get("plan_id"), str)
        or re.fullmatch(r"plan-[0-9a-f]{32}", maintenance["plan_id"]) is None
        or not isinstance(digest_map, Mapping)
        or digest_map.get(component_id) != artifact_digest
        or any(
            not isinstance(key, str)
            or _COMPONENT_ID.fullmatch(key) is None
            or not isinstance(value, str)
            or _TYPED_SHA256.fullmatch(value) is None
            for key, value in digest_map.items()
        )
        or type(maintenance.get("expected_gate_generation")) is not int
        or maintenance["expected_gate_generation"] < 1
        or type(maintenance.get("expected_catalog_generation")) is not int
        or maintenance["expected_catalog_generation"] < 1
    ):
        raise WorkloadPackageRuntimeError("PACKAGE_ONLY hold digest map or generation differs")
    if staged_row:
        identity = staged_row.get("stagedIdentity")
        if (
            not isinstance(identity, Mapping)
            or identity.get("planId") != maintenance.get("plan_id")
            or identity.get("planDigest") != maintenance.get("plan_digest")
        ):
            raise WorkloadPackageRuntimeError("Staged package is bound to a different parent plan")


def _runtime_policy_digest(policy: Mapping[str, Any] | None) -> str | None:
    """Return a typed digest for one already-validated policy value."""

    if policy is None:
        return None
    return "sha256:" + hashlib.sha256(_canonical_json(policy)).hexdigest()


def build_workload_source_update(
    *,
    source_policy: Mapping[str, Any],
    selected_rows: Sequence[Mapping[str, Any]],
    installation_records: Mapping[str, Mapping[str, Any]],
    activity_catalog: Mapping[str, Any],
    source_principals: Mapping[str, Mapping[str, Any]],
    runtime_policy: Mapping[str, Any] | None = None,
    remove_component_ids: Sequence[str] = (),
    _allow_first_core_standalone_source: bool = False,
) -> WorkloadSourceUpdate:
    """Project Catalog owners and actual InstallationRecords into both policies.

    Existing non-target scopes and runtime operation subsets are preserved. New
    package bindings use only explicit Catalog source and binding identities.
    中文：保留其他owner与其授权，只以签名映射更新目标绑定。
    """

    owner_source_id, policy_source_ids = _source_policy(source_policy)
    if len(policy_source_ids) != 1:
        raise WorkloadPackageRuntimeError("Workload source policy must resolve to one source owner")
    generation, current_sources = _catalog_identity(activity_catalog, allow_uninitialized=True)
    remove_ids = tuple(remove_component_ids)
    if len(set(remove_ids)) != len(remove_ids):
        raise WorkloadPackageRuntimeError("Package source removal list repeats a component")
    row_map: dict[str, Mapping[str, Any]] = {}
    component_artifact_digests: dict[str, str] = {}
    for row in selected_rows:
        if not isinstance(row, Mapping):
            raise WorkloadPackageRuntimeError("Selected plugin row is malformed")
        component_id = row.get("componentId")
        package_id = row.get("packageId")
        binding_id = row.get("bindingId")
        if (
            not isinstance(component_id, str)
            or _COMPONENT_ID.fullmatch(component_id) is None
            or not isinstance(package_id, str)
            or _PACKAGE_ID.fullmatch(package_id) is None
            or not isinstance(binding_id, str)
            or _BINDING_ID.fullmatch(binding_id) is None
            or component_id in row_map
        ):
            raise WorkloadPackageRuntimeError("Selected plugin owner identity is malformed")
        if row.get("sourcePolicy", source_policy) != dict(source_policy):
            raise WorkloadPackageRuntimeError("Selected plugin carries another sourcePolicy")
        row_map[component_id] = row
    if any(component_id not in row_map for component_id in remove_ids):
        raise WorkloadPackageRuntimeError("Package source removal has no exact selected owner")
    if set(installation_records) - set(row_map) or set(installation_records) & set(remove_ids):
        raise WorkloadPackageRuntimeError("Package installation and removal sets overlap")
    if set(row_map) != set(installation_records) | set(remove_ids):
        raise WorkloadPackageRuntimeError("Every selected package must be installed or removed")
    empty_first_core_standalone_source = (
        _allow_first_core_standalone_source
        and source_policy.get("mode") == "standaloneOperator"
        and owner_source_id == "cyrene-plugin-standalone-operator"
        and not row_map
        and not remove_ids
        and not installation_records
    )
    if _allow_first_core_standalone_source and not empty_first_core_standalone_source:
        raise WorkloadPackageRuntimeError(
            "Empty source registration is limited to the first-Core standalone operator"
        )

    principals: dict[str, dict[str, Any]] = {}
    expected_ids = set(current_sources) | {owner_source_id}
    if set(source_principals) != expected_ids:
        raise WorkloadPackageRuntimeError("Source principals do not cover the complete catalog")
    for source_id in sorted(expected_ids):
        current = current_sources.get(source_id)
        supplied = source_principals.get(source_id)
        if current is not None:
            principal = {"uid": current["uid"], "gid": current["gid"]}
            if supplied is not None:
                parsed = _source_principal(supplied, source_id)
                if parsed["uid"] != principal["uid"] or parsed["gid"] != principal["gid"]:
                    raise WorkloadPackageRuntimeError(
                        "Source principal differs from trusted catalog"
                    )
                principal["tokenPath"] = parsed["tokenPath"]
        else:
            parsed = _source_principal(supplied, source_id)
            principal = parsed
        if source_id == owner_source_id and "tokenPath" not in principal:
            parsed = _source_principal(supplied, source_id)
            principal["tokenPath"] = parsed["tokenPath"]
        principals[source_id] = principal

    old_policy: dict[str, Any] | None
    if runtime_policy is None:
        old_policy = None
        previous_digest = None
        if current_sources:
            raise WorkloadPackageRuntimeError(
                "Existing Runtime sources require their policy readback"
            )
    else:
        old_policy = _runtime_policy(runtime_policy)
        previous_digest = _runtime_policy_digest(old_policy)
        if old_policy["generation"] != generation:
            raise WorkloadPackageRuntimeError(
                "Runtime policy and activity catalog generations differ"
            )
        if {row["source_id"] for row in old_policy["sources"]} != set(current_sources):
            raise WorkloadPackageRuntimeError(
                "Runtime policy source set differs from activity catalog"
            )
        for policy_source in old_policy["sources"]:
            current = current_sources[policy_source["source_id"]]
            if any(
                policy_source[field] != current[field]
                for field in ("uid", "gid", "source_token_sha256")
            ):
                raise WorkloadPackageRuntimeError(
                    "Runtime source trust differs from activity catalog"
                )
        if _broker_projection(old_policy["sources"]) != {
            source_id: row["binding_scopes"] for source_id, row in current_sources.items()
        }:
            raise WorkloadPackageRuntimeError(
                "Runtime policy binding scopes differ from activity catalog"
            )

    runtime_by_source: dict[str, dict[str, dict[str, Any]]] = {
        source_id: {} for source_id in expected_ids
    }
    if old_policy is not None:
        for source in old_policy["sources"]:
            runtime_by_source[source["source_id"]] = {
                row["binding_id"]: dict(row) for row in source["bindings"]
            }
    scope_by_source: dict[str, dict[str, dict[str, Any]]] = {
        source_id: {row["binding_id"]: dict(row) for row in current["binding_scopes"]}
        for source_id, current in current_sources.items()
    }
    scope_by_source.setdefault(owner_source_id, {})
    runtime_by_source.setdefault(owner_source_id, {})
    selected_binding_ids: list[dict[str, str]] = []
    changed = owner_source_id not in current_sources

    # ── Phase 1: Remove requested bindings, then add exact installed identities ──
    # 第一阶段：仅按Catalog显式owner移除或追加真实安装身份。
    seen_target_binding_ids: set[str] = set()
    for component_id in sorted(row_map):
        row = row_map[component_id]
        binding_id = row["bindingId"]
        package_id = row["packageId"]
        if binding_id in seen_target_binding_ids:
            raise WorkloadPackageRuntimeError("Selected components reuse an owner binding ID")
        seen_target_binding_ids.add(binding_id)
        selected_binding_ids.append(
            {"componentId": component_id, "sourceId": owner_source_id, "bindingId": binding_id}
        )
        existing_scope = scope_by_source[owner_source_id].get(binding_id)
        existing_runtime = runtime_by_source[owner_source_id].get(binding_id)
        if existing_scope is not None and existing_scope["package_id"] != package_id:
            raise WorkloadPackageRuntimeError("Owner binding is assigned to another package")
        if existing_runtime is not None and existing_runtime["package_id"] != package_id:
            raise WorkloadPackageRuntimeError(
                "Runtime owner binding is assigned to another package"
            )
        if component_id in remove_ids:
            digest = row.get("packageArtifactDigest", row.get("artifactDigest"))
            component_artifact_digests[component_id] = _require_digest(
                digest, f"{component_id}.packageArtifactDigest"
            )
            if existing_scope is not None:
                del scope_by_source[owner_source_id][binding_id]
                changed = True
            runtime_by_source[owner_source_id].pop(binding_id, None)
            continue
        record = installation_records[component_id]
        _validate_installation_record_identity(row, record)
        component_artifact_digests[component_id] = record["artifact_digest"]
        installation_id = record["installation_id"]
        old_ids = list(existing_scope["installation_ids"]) if existing_scope is not None else []
        new_ids = sorted(set(old_ids) | {installation_id})
        wanted_scope = {
            "binding_id": binding_id,
            "package_id": package_id,
            "installation_ids": new_ids,
            "operations": list(BROKER_OPERATIONS),
        }
        if existing_scope != wanted_scope:
            scope_by_source[owner_source_id][binding_id] = wanted_scope
            changed = True
        old_runtime = existing_runtime or {}
        wanted_runtime = {
            "binding_id": binding_id,
            "package_id": package_id,
            "installation_ids": new_ids,
            "operations": list(RUNTIME_OPERATIONS),
        }
        if old_runtime != wanted_runtime:
            runtime_by_source[owner_source_id][binding_id] = wanted_runtime
            changed = True

    for source_id, bindings in scope_by_source.items():
        for binding_id in bindings:
            for other_source_id, other_bindings in scope_by_source.items():
                if other_source_id != source_id and binding_id in other_bindings:
                    raise WorkloadPackageRuntimeError(
                        "Binding ID is already owned by another source"
                    )
    # Preserve every non-target Broker scope's exact identity and runtime ops.
    desired_scopes = {
        source_id: sorted(bindings.values(), key=lambda item: item["binding_id"])
        for source_id, bindings in sorted(scope_by_source.items())
    }
    desired_runtime = {
        source_id: sorted(bindings.values(), key=lambda item: item["binding_id"])
        for source_id, bindings in sorted(runtime_by_source.items())
    }
    runtime_projection = [
        {
            "source_id": source_id,
            "uid": principals[source_id]["uid"],
            "gid": principals[source_id]["gid"],
            "source_token_sha256": current_sources[source_id]["source_token_sha256"]
            if source_id in current_sources
            else "0" * 64,
            "bindings": desired_runtime[source_id],
        }
        for source_id in sorted(expected_ids)
    ]
    desired_broker_projection = _broker_projection(runtime_projection)
    if desired_broker_projection != desired_scopes:
        raise WorkloadPackageRuntimeError("Projected Broker and Package Runtime scopes disagree")
    source_arguments = tuple(
        argument
        for source_id in sorted(expected_ids)
        for argument in (
            "--source",
            f"{source_id}={principals[source_id]['uid']}:{principals[source_id]['gid']}",
        )
    )
    source_identity = {
        source_id: {
            "uid": principals[source_id]["uid"],
            "gid": principals[source_id]["gid"],
            "source_token_sha256": current_sources[source_id]["source_token_sha256"]
            if source_id in current_sources
            else None,
        }
        for source_id in sorted(expected_ids)
    }
    required_hold_component_ids: tuple[str, ...] = ()
    if owner_source_id not in current_sources:
        product_component_ids = tuple(
            sorted(
                row["componentId"]
                for row in source_policy.get("productSources", [])
                if row["sourceId"] == owner_source_id
            )
        )
        required_hold_component_ids = product_component_ids or tuple(sorted(row_map))
        if not required_hold_component_ids and not empty_first_core_standalone_source:
            raise WorkloadPackageRuntimeError(
                "New source registration has no held component identity"
            )
    return WorkloadSourceUpdate(
        source_arguments=source_arguments,
        binding_scopes=desired_scopes,
        source_identity=source_identity,
        runtime_bindings=desired_runtime,
        expected_generation=generation + 1 if changed else generation,
        changed=changed,
        previous_policy_digest=previous_digest,
        selected_binding_ids=tuple(selected_binding_ids),
        component_artifact_digests=component_artifact_digests,
        required_hold_component_ids=required_hold_component_ids,
    )


def apply_workload_source_update(
    update: WorkloadSourceUpdate,
    *,
    maintenance: Mapping[str, Any],
    request_id: str,
    expected_policy_digest: str | None,
    activity_catalog_path: Path = DEFAULT_ACTIVITY_CATALOG_PATH,
    policy_path: Path = DEFAULT_POLICY_PATH,
    token_directory: Path = DEFAULT_TOKEN_DIRECTORY,
    command: Path = DEFAULT_CYRENE_COMMAND,
    runner: Any = subprocess.run,
    expected_catalog_gid: int | None = None,
) -> dict[str, Any]:
    """Commit Broker scopes and a CAS-protected Runtime policy inside one hold.

    When ``expected_catalog_gid`` is supplied, require the official Broker result
    to be root-owned, group-readable by that GID, and mode 0640 before the policy
    CAS can persist.
    """

    _require_root()
    _require_request_id(request_id)
    hold = _validate_policy_update_hold(maintenance)
    if expected_catalog_gid is not None and (
        type(expected_catalog_gid) is not int or expected_catalog_gid <= 0
    ):
        raise WorkloadPackageRuntimeError("Expected activity catalog GID is invalid")
    if expected_policy_digest != update.previous_policy_digest:
        raise WorkloadPackageRuntimeError(
            "Expected policy digest differs from the projected prior policy"
        )
    activity_catalog_path = Path(activity_catalog_path)
    policy_path = Path(policy_path)
    token_directory = Path(token_directory)
    if any(
        not path.is_absolute() for path in (activity_catalog_path, policy_path, token_directory)
    ):
        raise WorkloadPackageRuntimeError("Package Runtime update paths must be absolute")
    hold_digests = hold["component_artifact_digests"]
    if type(update.expected_generation) is not int or type(update.changed) is not bool:
        raise WorkloadPackageRuntimeError("Source update generation is invalid")
    previous_generation = update.expected_generation - (1 if update.changed else 0)
    if (
        update.expected_generation < 1
        or previous_generation < 0
        or (previous_generation == 0 and (update.expected_generation != 1 or not update.changed))
        or hold["expected_catalog_generation"] != previous_generation
    ):
        raise WorkloadPackageRuntimeError(
            "Source update generation differs from the held catalog generation"
        )
    if any(
        hold_digests.get(component_id) != digest
        for component_id, digest in update.component_artifact_digests.items()
    ):
        raise WorkloadPackageRuntimeError("Source update package digests are outside the held plan")
    if any(component_id not in hold_digests for component_id in update.required_hold_component_ids):
        raise WorkloadPackageRuntimeError("New source identity is outside the held plan")

    _validate_policy_parent(policy_path.parent, _effective_uid())
    try:
        activity_catalog_path.lstat()
    except FileNotFoundError:
        current_catalog = {"schema_version": 1, "generation": 0, "sources": []}
    else:
        current_catalog = _read_activity_catalog(activity_catalog_path)
    current_generation, _current_sources = _catalog_identity(
        current_catalog, allow_uninitialized=True
    )
    if current_generation != update.expected_generation - (1 if update.changed else 0):
        raise WorkloadPackageRuntimeError(
            "Runtime activity catalog changed after source projection"
        )
    current_policy_bytes = _read_policy_bytes_optional(
        policy_path,
        group_id=_runtime_group_id(),
        owner_id=_effective_uid(),
    )
    current_policy_digest = (
        "sha256:" + hashlib.sha256(current_policy_bytes).hexdigest()
        if current_policy_bytes is not None
        else None
    )
    if current_generation == 0 and current_policy_bytes is not None:
        raise WorkloadPackageRuntimeError(
            "An uninitialized activity catalog cannot replace an existing Package Runtime policy"
        )
    if current_policy_digest != expected_policy_digest:
        raise WorkloadPackageRuntimeError("Package Runtime policy changed after source projection")
    if current_policy_bytes is not None:
        current_policy = read_runtime_source_policy_generic(policy_path)
        _validate_runtime_policy_against_catalog(current_policy, current_catalog)

    bootstrap = _bootstrap_module()
    stage_root = bootstrap.PACKAGE_BOOTSTRAP_STAGE_ROOT
    request_directory = stage_root / request_id
    _ensure_private_dir(stage_root)
    _ensure_private_dir(request_directory, parent=stage_root)
    proof_path = request_directory / "maintenance-proof.json"
    scopes_path = request_directory / "binding-scopes.json"
    proof = {
        "request_id": hold["transaction_id"],
        "maintenance_token": hold["maintenance_token"],
        "plan_id": hold["plan_id"],
        "plan_digest": hold["plan_digest"],
        "component_artifact_digests": hold["component_artifact_digests"],
    }
    _write_private_json(proof_path, proof)
    _write_private_json(scopes_path, update.binding_scopes)
    catalog_gid = (
        expected_catalog_gid if expected_catalog_gid is not None else _maintenance_group_id()
    )

    # ── Phase 2: Ask the official broker helper to commit the exact scopes ──
    # 第二阶段：通过正式init-catalog在当前hold内登记来源与绑定范围。
    argv = [
        str(command),
        "component-run",
        "cyrene-runtime-maintenance",
        "--",
        "init-catalog",
        "--catalog",
        str(activity_catalog_path),
        "--token-dir",
        str(token_directory),
        "--catalog-gid",
        str(catalog_gid),
        "--binding-scopes-json",
        str(scopes_path),
        "--maintenance-proof-file",
        str(proof_path),
        *update.source_arguments,
    ]
    command_result = _run_json_command(
        argv,
        runner=runner,
        timeout=120,
        failure_message="Runtime activity catalog update failed",
    )
    catalog = _read_activity_catalog(activity_catalog_path)
    _validate_catalog_update_readback(
        catalog,
        command_result,
        update,
        token_directory=token_directory,
    )
    if expected_catalog_gid is not None:
        _verify_first_core_catalog_metadata(activity_catalog_path, expected_catalog_gid)

    # ── Phase 3: Rebuild policy with token hashes from broker readback, then CAS ──
    # 第三阶段：用Broker回读的token摘要重建policy，并对旧digest做原子CAS。
    new_policy = _policy_from_catalog_and_update(catalog, update)
    new_digest = write_runtime_source_policy_cas(
        new_policy,
        expected_prior_digest=expected_policy_digest,
        policy_path=policy_path,
    )
    readback_policy = read_runtime_source_policy_generic(policy_path)
    if readback_policy != new_policy or _runtime_policy_digest(readback_policy) != new_digest:
        raise WorkloadPackageRuntimeError("Package Runtime source policy readback differs")
    return {
        "catalogGeneration": catalog["generation"],
        "catalogDigest": _file_digest(activity_catalog_path),
        "policyGeneration": readback_policy["generation"],
        "policyDigest": new_digest,
        "sourceIdentities": [
            {"sourceId": row["source_id"], "uid": row["uid"], "gid": row["gid"]}
            for row in readback_policy["sources"]
        ],
        "bindings": [dict(row) for row in update.selected_binding_ids],
    }


def reconcile_workload_source_update(
    update: WorkloadSourceUpdate,
    *,
    maintenance: Mapping[str, Any],
    activity_catalog_path: Path = DEFAULT_ACTIVITY_CATALOG_PATH,
    policy_path: Path = DEFAULT_POLICY_PATH,
    token_directory: Path = DEFAULT_TOKEN_DIRECTORY,
) -> dict[str, Any] | None:
    """Finish or reject a source update interrupted after Broker commit.

    The caller must first persist ``update`` in its existing transaction journal
    before invoking ``apply_workload_source_update``.  Recovery accepts only the
    same active hold, complete artifact map, exact projected scopes, source
    principals, and the one expected Broker generation.  It repairs the local
    Runtime policy only while its bytes still have the journaled prior digest.

    Returns ``None`` while the Broker catalog is still at the pre-update
    generation, allowing the caller to run the normal held update.  Returns the
    verified update receipt when the Broker commit already happened and the
    Runtime policy was reconciled or already matches.  Any unrelated catalog or
    policy change fails closed.
    """

    _require_root()
    hold = _validate_policy_update_hold(maintenance)
    if not isinstance(update, WorkloadSourceUpdate):
        raise WorkloadPackageRuntimeError("Source update recovery identity is malformed")
    if type(update.expected_generation) is not int or update.expected_generation < 1:
        raise WorkloadPackageRuntimeError("Source update recovery generation is invalid")
    previous_generation = update.expected_generation - (1 if update.changed else 0)
    if previous_generation < 0 or type(update.changed) is not bool:
        raise WorkloadPackageRuntimeError("Source update prior generation is invalid")
    if (
        previous_generation == 0 and (update.expected_generation != 1 or not update.changed)
    ) or hold["expected_catalog_generation"] != previous_generation:
        raise WorkloadPackageRuntimeError(
            "Source update generation differs from the held catalog generation"
        )
    if not isinstance(update.binding_scopes, Mapping) or not isinstance(
        update.runtime_bindings, Mapping
    ):
        raise WorkloadPackageRuntimeError("Source update recovery scopes are malformed")
    source_ids = set(update.source_identity)
    selected_keys = [
        (row.get("componentId"), row.get("sourceId"), row.get("bindingId"))
        for row in update.selected_binding_ids
        if isinstance(row, Mapping)
    ]
    if (
        not source_ids
        or source_ids != set(update.binding_scopes)
        or source_ids != set(update.runtime_bindings)
        or len(selected_keys) != len(update.selected_binding_ids)
        or len(selected_keys) != len(set(selected_keys))
        or any(not all(isinstance(value, str) and value for value in key) for key in selected_keys)
        or _broker_projection(
            [
                {"source_id": source_id, "bindings": bindings}
                for source_id, bindings in update.runtime_bindings.items()
            ]
        )
        != update.binding_scopes
    ):
        raise WorkloadPackageRuntimeError("Source update recovery owner identities are incomplete")
    hold_digests = hold["component_artifact_digests"]
    if any(
        hold_digests.get(component_id) != digest
        for component_id, digest in update.component_artifact_digests.items()
    ) or any(
        component_id not in hold_digests for component_id in update.required_hold_component_ids
    ):
        raise WorkloadPackageRuntimeError("Source update recovery is outside the held artifact map")
    if update.previous_policy_digest is not None:
        _require_digest(update.previous_policy_digest, "prior Package Runtime policy digest")

    activity_catalog_path = Path(activity_catalog_path)
    policy_path = Path(policy_path)
    token_directory = Path(token_directory)
    if any(
        not path.is_absolute() for path in (activity_catalog_path, policy_path, token_directory)
    ):
        raise WorkloadPackageRuntimeError("Package Runtime recovery paths must be absolute")
    try:
        activity_catalog_path.lstat()
    except FileNotFoundError:
        if previous_generation == 0 and update.expected_generation == 1:
            current_catalog = {"schema_version": 1, "generation": 0, "sources": []}
        else:
            raise WorkloadPackageRuntimeError(
                "Runtime activity catalog is unavailable for recovery"
            )
    else:
        current_catalog = _read_activity_catalog(activity_catalog_path)
    generation, current_sources = _catalog_identity(current_catalog, allow_uninitialized=True)

    _validate_recovery_source_projection(update, hold, current_sources)
    if generation == previous_generation:
        current_bytes = _read_policy_bytes_optional(
            policy_path,
            group_id=_runtime_group_id(),
            owner_id=_effective_uid(),
        )
        current_digest = (
            "sha256:" + hashlib.sha256(current_bytes).hexdigest()
            if current_bytes is not None
            else None
        )
        if current_digest != update.previous_policy_digest:
            raise WorkloadPackageRuntimeError(
                "Package Runtime policy changed before Broker source commit"
            )
        return None
    if generation != update.expected_generation or not update.changed:
        raise WorkloadPackageRuntimeError(
            "Runtime activity catalog advanced to an unexpected generation"
        )

    _validate_recovery_catalog(update, current_sources, token_directory)
    desired_policy = _policy_from_catalog_and_update(current_catalog, update)
    policy_bytes = _read_policy_bytes_optional(
        policy_path,
        group_id=_runtime_group_id(),
        owner_id=_effective_uid(),
    )
    current_digest = (
        "sha256:" + hashlib.sha256(policy_bytes).hexdigest() if policy_bytes is not None else None
    )
    if policy_bytes is not None:
        current_policy = read_runtime_source_policy_generic(policy_path)
        try:
            _validate_runtime_policy_against_catalog(current_policy, current_catalog)
        except WorkloadPackageRuntimeError:
            # A policy one generation behind the newly committed Broker catalog
            # is the exact recoverable crash window; all other policy shapes are
            # still rejected by the digest/CAS checks below.
            if current_policy.get("generation") != previous_generation:
                raise
    else:
        current_policy = None

    desired_digest = _runtime_policy_digest(desired_policy)
    if current_policy == desired_policy:
        if current_digest != desired_digest:
            raise WorkloadPackageRuntimeError(
                "Committed Package Runtime policy bytes are not canonical"
            )
        new_digest = current_digest
    else:
        if current_digest != update.previous_policy_digest:
            raise WorkloadPackageRuntimeError(
                "Package Runtime policy changed outside the pending source update"
            )
        new_digest = write_runtime_source_policy_cas(
            desired_policy,
            expected_prior_digest=update.previous_policy_digest,
            policy_path=policy_path,
        )
    readback_policy = read_runtime_source_policy_generic(policy_path)
    if readback_policy != desired_policy or _runtime_policy_digest(readback_policy) != new_digest:
        raise WorkloadPackageRuntimeError("Reconciled Package Runtime policy readback differs")
    return {
        "catalogGeneration": generation,
        "catalogDigest": _file_digest(activity_catalog_path),
        "policyGeneration": readback_policy["generation"],
        "policyDigest": new_digest,
        "sourceIdentities": [
            {"sourceId": source_id, "uid": row["uid"], "gid": row["gid"]}
            for source_id, row in sorted(current_sources.items())
        ],
        "bindings": [dict(row) for row in update.selected_binding_ids],
    }


def _validate_recovery_source_projection(
    update: WorkloadSourceUpdate,
    hold: Mapping[str, Any],
    current_sources: Mapping[str, Mapping[str, Any]],
) -> None:
    """Bind durable source intent to the active hold and exact signed owners."""

    missing_new_sources: set[str] = set()
    for source_id, expected in update.source_identity.items():
        if not isinstance(expected, Mapping) or set(expected) != {
            "uid",
            "gid",
            "source_token_sha256",
        }:
            raise WorkloadPackageRuntimeError("Source update principal identity is malformed")
        if (
            type(expected.get("uid")) is not int
            or expected["uid"] <= 0
            or type(expected.get("gid")) is not int
            or expected["gid"] <= 0
            or (
                expected.get("source_token_sha256") is not None
                and _RAW_SHA256.fullmatch(str(expected["source_token_sha256"])) is None
            )
        ):
            raise WorkloadPackageRuntimeError("Source update principal trust fields are malformed")
        if source_id not in current_sources and expected.get("source_token_sha256") is None:
            missing_new_sources.add(source_id)
    if set(current_sources) - set(update.source_identity) or set(current_sources) != (
        set(update.source_identity) - missing_new_sources
    ):
        raise WorkloadPackageRuntimeError(
            "Runtime activity source set differs from recovery intent"
        )
    for source_id, expected in update.source_identity.items():
        actual = current_sources.get(source_id)
        if actual is None:
            continue
        if actual.get("uid") != expected.get("uid") or actual.get("gid") != expected.get("gid"):
            raise WorkloadPackageRuntimeError(
                "Runtime activity source principal differs from recovery intent"
            )
        old_token_digest = expected.get("source_token_sha256")
        if old_token_digest is not None and actual.get("source_token_sha256") != old_token_digest:
            raise WorkloadPackageRuntimeError("Runtime activity source token identity changed")

    if any(
        not isinstance(component_id, str)
        or hold["component_artifact_digests"].get(component_id) != digest
        for component_id, digest in update.component_artifact_digests.items()
    ) or any(
        not isinstance(component_id, str) or component_id not in hold["component_artifact_digests"]
        for component_id in update.required_hold_component_ids
    ):
        raise WorkloadPackageRuntimeError("Source update component digest differs from held plan")


def _validate_recovery_catalog(
    update: WorkloadSourceUpdate,
    current_sources: Mapping[str, Mapping[str, Any]],
    token_directory: Path,
) -> None:
    """Require exact committed Broker scopes and source-token readback."""

    if set(current_sources) != set(update.binding_scopes):
        raise WorkloadPackageRuntimeError(
            "Committed Broker source set differs from recovery intent"
        )
    for source_id, actual in current_sources.items():
        expected = update.source_identity[source_id]
        if (
            actual.get("uid") != expected.get("uid")
            or actual.get("gid") != expected.get("gid")
            or actual.get("binding_scopes") != update.binding_scopes[source_id]
        ):
            raise WorkloadPackageRuntimeError("Committed Broker scopes differ from recovery intent")
        expected_token_digest = expected.get("source_token_sha256")
        if (
            expected_token_digest is not None
            and actual.get("source_token_sha256") != expected_token_digest
        ):
            raise WorkloadPackageRuntimeError(
                "Committed Broker token identity differs from recovery intent"
            )
        _verify_token_file(token_directory / f"{source_id}.token", actual["source_token_sha256"])


def _validate_policy_update_hold(maintenance: Mapping[str, Any]) -> dict[str, Any]:
    """Validate fields needed to bind init-catalog to the current hold."""

    if not isinstance(maintenance, Mapping) or set(maintenance) != {
        "transaction_id",
        "maintenance_token",
        "target_kind",
        "plan_id",
        "plan_digest",
        "component_artifact_digests",
        "expected_gate_generation",
        "expected_catalog_generation",
    }:
        raise WorkloadPackageRuntimeError("PACKAGE_ONLY hold identity is malformed")
    _require_request_id(maintenance.get("transaction_id"), "maintenance.transaction_id")
    _require_text(maintenance.get("maintenance_token"), "maintenance token")
    _require_digest(maintenance.get("plan_digest"), "maintenance.plan_digest")
    digest_map = maintenance.get("component_artifact_digests")
    if (
        maintenance.get("target_kind") not in {"CORE_RUNTIME", "PACKAGE_ONLY"}
        or not isinstance(maintenance.get("plan_id"), str)
        or re.fullmatch(r"plan-[0-9a-f]{32}", maintenance["plan_id"]) is None
        or not isinstance(digest_map, Mapping)
        or not digest_map
        or any(
            not isinstance(component_id, str)
            or _COMPONENT_ID.fullmatch(component_id) is None
            or not isinstance(digest, str)
            or _TYPED_SHA256.fullmatch(digest) is None
            for component_id, digest in digest_map.items()
        )
        or type(maintenance.get("expected_gate_generation")) is not int
        or maintenance["expected_gate_generation"] < 1
        or type(maintenance.get("expected_catalog_generation")) is not int
        or maintenance["expected_catalog_generation"] < 0
    ):
        raise WorkloadPackageRuntimeError("PACKAGE_ONLY hold echo is invalid")
    return dict(maintenance)


def _maintenance_group_id() -> int:
    """Resolve the official helper's trusted maintenance group."""

    try:
        return grp.getgrnam("cyrene-runtime-maintenance").gr_gid
    except KeyError as error:
        raise WorkloadPackageRuntimeError("Runtime Maintenance group is unavailable") from error


def _ensure_private_dir(path: Path, *, parent: Path | None = None) -> None:
    """Create or validate one root-only request directory."""

    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise WorkloadPackageRuntimeError("Package Runtime request directory is unsafe")
        return
    if parent is not None:
        _ensure_private_dir(parent)
    path.mkdir(mode=0o700)
    os.chown(path, 0, 0)
    os.chmod(path, 0o700)


def _write_private_json(path: Path, value: Any) -> None:
    """Write one canonical root-only JSON input without replacing prior bytes."""

    payload = _canonical_json(value)
    if len(payload) > MAX_FRAME_BYTES:
        raise WorkloadPackageRuntimeError("Package Runtime private request is too large")
    if path.exists() or path.is_symlink():
        existing = _read_private_json(path)
        if existing != value:
            raise WorkloadPackageRuntimeError("Package Runtime retry request differs")
        return
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise


def _read_private_json(path: Path) -> Any:
    """Read one root-only request file without following links."""

    try:
        before = path.lstat()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                path.is_symlink()
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != 0
                or opened.st_gid != 0
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_nlink != 1
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_size > MAX_FRAME_BYTES
            ):
                raise WorkloadPackageRuntimeError("Package Runtime private request is unsafe")
            raw = stream.read(MAX_FRAME_BYTES + 1)
    except OSError as error:
        raise WorkloadPackageRuntimeError(
            "Package Runtime private request is unavailable"
        ) from error
    if len(raw) > MAX_FRAME_BYTES:
        raise WorkloadPackageRuntimeError("Package Runtime private request is too large")
    try:
        return json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkloadPackageRuntimeError("Package Runtime private request is malformed") from error


def _run_json_command(
    argv: Sequence[str], *, runner: Any, timeout: int, failure_message: str
) -> dict[str, Any]:
    """Run one fixed official command and accept exactly one JSON response."""

    try:
        completed = runner(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={
                "LANG": "C.UTF-8",
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            },
            cwd="/",
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkloadPackageRuntimeError(failure_message) from error
    lines = completed.stdout.splitlines()
    if completed.returncode != 0 or len(lines) != 1:
        raise WorkloadPackageRuntimeError(failure_message)
    try:
        value = json.loads(lines[0], object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkloadPackageRuntimeError(f"{failure_message}: response is malformed") from error
    if not isinstance(value, dict):
        raise WorkloadPackageRuntimeError(f"{failure_message}: response is not an object")
    return value


def _read_owned_json(path: Path, *, group_id: int | None = None) -> tuple[dict[str, Any], bytes]:
    """Read one protected root-controlled JSON file through a no-follow descriptor."""

    try:
        before = path.lstat()
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                path.is_symlink()
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != _effective_uid()
                or (group_id is not None and opened.st_gid != group_id)
                or stat.S_IMODE(opened.st_mode) & 0o022
                or opened.st_nlink != 1
                or opened.st_size > MAX_FILE_BYTES
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
            ):
                raise WorkloadPackageRuntimeError(
                    "Protected Package Runtime file metadata is unsafe"
                )
            raw = stream.read(MAX_FILE_BYTES + 1)
    except OSError as error:
        raise WorkloadPackageRuntimeError(
            "Protected Package Runtime file is unavailable"
        ) from error
    if len(raw) > MAX_FILE_BYTES:
        raise WorkloadPackageRuntimeError("Protected Package Runtime file is too large")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkloadPackageRuntimeError("Protected Package Runtime JSON is malformed") from error
    if not isinstance(value, dict):
        raise WorkloadPackageRuntimeError("Protected Package Runtime JSON is not an object")
    return value, raw


def _read_activity_catalog(path: Path) -> dict[str, Any]:
    """Read and validate the current activity catalog after init-catalog."""

    value, _raw = _read_owned_json(path)
    _catalog_identity(value)
    return value


def _validate_catalog_update_readback(
    catalog: Mapping[str, Any],
    command_result: Mapping[str, Any],
    update: WorkloadSourceUpdate,
    *,
    token_directory: Path,
) -> None:
    """Require exact Broker generation, source identities, scopes, and tokens."""

    generation, sources = _catalog_identity(catalog)
    if (
        generation != update.expected_generation
        or set(sources) != set(update.source_identity)
        or set(command_result) != {"schema_version", "generation", "sources"}
        or type(command_result.get("schema_version")) is not int
        or command_result.get("schema_version") != 1
        or command_result.get("generation") != generation
        or type(command_result.get("generation")) is not int
        or not isinstance(command_result.get("sources"), list)
    ):
        raise WorkloadPackageRuntimeError(
            "Runtime activity catalog response differs from the held update"
        )
    result_rows = command_result["sources"]
    if len(result_rows) != len(sources):
        raise WorkloadPackageRuntimeError("Runtime activity catalog source receipt is incomplete")
    for (source_id, actual), receipt in zip(sorted(sources.items()), result_rows, strict=True):
        expected = update.source_identity[source_id]
        if (
            actual["uid"] != expected["uid"]
            or actual["gid"] != expected["gid"]
            or actual["binding_scopes"] != update.binding_scopes[source_id]
            or (
                expected["source_token_sha256"] is not None
                and actual["source_token_sha256"] != expected["source_token_sha256"]
            )
            or not isinstance(receipt, Mapping)
            or set(receipt) != {"source_id", "token_file", "binding_scope_count"}
            or receipt.get("source_id") != source_id
            or receipt.get("token_file") != str(token_directory / f"{source_id}.token")
            or receipt.get("binding_scope_count") != len(update.binding_scopes[source_id])
            or type(receipt.get("binding_scope_count")) is not int
        ):
            raise WorkloadPackageRuntimeError("Runtime activity catalog source readback differs")
        token_path = token_directory / f"{source_id}.token"
        _verify_token_file(token_path, actual["source_token_sha256"])


def _verify_token_file(path: Path, expected_sha256: str) -> None:
    """Check one root-only 0400 token against the activity catalog hash."""

    expected = _require_digest(expected_sha256, "source_token_sha256", typed=False)
    try:
        before = path.lstat()
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                path.is_symlink()
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != _effective_uid()
                or opened.st_gid != _effective_uid()
                or stat.S_IMODE(opened.st_mode) != 0o400
                or opened.st_nlink != 1
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_size < 1
                or opened.st_size > 4096
            ):
                raise WorkloadPackageRuntimeError("Activity source token metadata is unsafe")
            token = stream.read(4097)
    except OSError as error:
        raise WorkloadPackageRuntimeError("Activity source token is unavailable") from error
    if len(token) > 4096:
        raise WorkloadPackageRuntimeError("Activity source token is too large")
    try:
        normalized = token.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise WorkloadPackageRuntimeError("Activity source token is malformed") from error
    if (
        not normalized
        or any(character.isspace() or ord(character) < 0x21 for character in normalized)
        or hashlib.sha256(normalized.encode("utf-8")).hexdigest() != expected
    ):
        raise WorkloadPackageRuntimeError("Activity source token digest differs")


def _policy_from_catalog_and_update(
    catalog: Mapping[str, Any], update: WorkloadSourceUpdate
) -> dict[str, Any]:
    """Join projected bindings to token hashes read from the committed catalog."""

    generation, sources = _catalog_identity(catalog)
    policy_sources = [
        {
            "source_id": source_id,
            "uid": source["uid"],
            "gid": source["gid"],
            "source_token_sha256": source["source_token_sha256"],
            "bindings": update.runtime_bindings[source_id],
        }
        for source_id, source in sorted(sources.items())
    ]
    policy = _runtime_policy(
        {"schema_version": 1, "generation": generation, "sources": policy_sources}
    )
    if _broker_projection(policy["sources"]) != {
        source_id: row["binding_scopes"] for source_id, row in sources.items()
    }:
        raise WorkloadPackageRuntimeError("Committed catalog and Runtime policy scopes disagree")
    return policy


def _validate_installation_record_identity(
    row: Mapping[str, Any], record: Mapping[str, Any]
) -> None:
    """Check the real Platform InstallationRecord before owner-scope mutation."""

    fields = {
        "record_version",
        "installation_id",
        "package_id",
        "package_version",
        "artifact_digest",
        "archive_digest",
        "capabilities",
        "state",
        "verification",
        "dependencies",
        "installed_at_unix_ms",
    }
    if not isinstance(record, Mapping) or set(record) != fields:
        raise WorkloadPackageRuntimeError("Platform InstallationRecord is malformed")
    package_id = row.get("packageId")
    if (
        type(record.get("record_version")) is not int
        or record.get("record_version") != 1
        or record.get("package_id") != package_id
        or _PACKAGE_ID.fullmatch(str(package_id)) is None
        or record.get("package_version") != row.get("version")
        or _VERSION.fullmatch(str(record.get("package_version"))) is None
        or record.get("state") != "INSTALLED"
        or type(record.get("installed_at_unix_ms")) is not int
        or record["installed_at_unix_ms"] < 1
        or _INSTALLATION_ID.fullmatch(str(record.get("installation_id"))) is None
        or any(
            not isinstance(record.get(field), str) or _TYPED_SHA256.fullmatch(record[field]) is None
            for field in ("artifact_digest", "archive_digest")
        )
    ):
        raise WorkloadPackageRuntimeError(
            "Platform InstallationRecord differs from selected package"
        )
    artifact = row.get("packageArtifactDigest")
    if artifact is not None and record["artifact_digest"] != artifact:
        raise WorkloadPackageRuntimeError(
            "Platform aggregate artifact digest differs from selected package"
        )
    verification = record.get("verification")
    dependencies = record.get("dependencies")
    if (
        not isinstance(verification, Mapping)
        or set(verification)
        != {
            "verifier",
            "verified_at_unix_ms",
            "artifact_digest",
            "archive_digest",
            "descriptor_digest",
            "manifest_digest",
            "dependency_lock_digest",
        }
        or not isinstance(dependencies, Mapping)
        or set(dependencies)
        != {
            "preparer",
            "prepared_at_unix_ms",
            "lock_digest",
            "runtime_digest",
            "runtime_executable",
        }
        or not isinstance(verification.get("verifier"), str)
        or not verification["verifier"]
        or type(verification.get("verified_at_unix_ms")) is not int
        or verification["verified_at_unix_ms"] < 1
        or verification.get("artifact_digest") != record["artifact_digest"]
        or verification.get("archive_digest") != record["archive_digest"]
        or any(
            not isinstance(verification.get(field), str)
            or _TYPED_SHA256.fullmatch(verification[field]) is None
            for field in (
                "artifact_digest",
                "archive_digest",
                "descriptor_digest",
                "manifest_digest",
                "dependency_lock_digest",
            )
        )
        or not isinstance(dependencies.get("preparer"), str)
        or not dependencies["preparer"]
        or type(dependencies.get("prepared_at_unix_ms")) is not int
        or dependencies["prepared_at_unix_ms"] < 1
        or dependencies.get("lock_digest") != verification.get("dependency_lock_digest")
        or not isinstance(dependencies.get("runtime_digest"), str)
        or _TYPED_SHA256.fullmatch(dependencies["runtime_digest"]) is None
        or (
            dependencies.get("runtime_executable") is not None
            and (
                not isinstance(dependencies["runtime_executable"], str)
                or not Path(dependencies["runtime_executable"]).is_absolute()
            )
        )
    ):
        raise WorkloadPackageRuntimeError("Platform InstallationRecord verification is incomplete")
    capabilities = record.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or capabilities != sorted(set(capabilities))
        or any(
            not isinstance(value, str) or _PACKAGE_ID.fullmatch(value) is None
            for value in capabilities
        )
    ):
        raise WorkloadPackageRuntimeError("Platform InstallationRecord capabilities are invalid")
    capability = row.get("capabilityId")
    if capability is not None and capabilities != [capability]:
        raise WorkloadPackageRuntimeError(
            "Platform InstallationRecord capability differs from selected package"
        )
    derived = (
        "installation-"
        + hashlib.sha256(
            f"{record['package_id']}\0{record['package_version']}\0{record['artifact_digest']}".encode()
        ).hexdigest()[:32]
    )
    if record["installation_id"] != derived:
        raise WorkloadPackageRuntimeError(
            "Platform InstallationId is not derived from its identity"
        )


def _validate_runtime_policy_against_catalog(
    policy: Mapping[str, Any], catalog: Mapping[str, Any]
) -> dict[str, Any]:
    """Authenticate source principals and binding scopes before any SDK call."""

    normalized = _runtime_policy(policy)
    generation, sources = _catalog_identity(catalog)
    if normalized["generation"] != generation or set(sources) != {
        row["source_id"] for row in normalized["sources"]
    }:
        raise WorkloadPackageRuntimeError("Runtime source policy is not at the catalog generation")
    for source in normalized["sources"]:
        current = sources[source["source_id"]]
        if any(source[field] != current[field] for field in ("uid", "gid", "source_token_sha256")):
            raise WorkloadPackageRuntimeError(
                "Runtime source policy principal differs from Broker catalog"
            )
    projected = _broker_projection(normalized["sources"])
    if projected != {source_id: row["binding_scopes"] for source_id, row in sources.items()}:
        raise WorkloadPackageRuntimeError("Runtime source policy scopes differ from Broker catalog")
    return normalized


def _validate_owner_call(
    *,
    operation: str,
    source_id: str,
    uid: int,
    gid: int,
    binding_id: str,
    package_id: str,
    installation_ids: Sequence[str],
    catalog_generation: int,
    activity_catalog: Mapping[str, Any],
    runtime_policy: Mapping[str, Any],
    source_principals: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """Validate exact source, owner, operation, and installation allowlists."""

    if operation not in SOURCE_OPERATIONS:
        raise WorkloadPackageRuntimeError(
            "Package Runtime operation is not in the signed source policy"
        )
    if (
        _SOURCE_ID.fullmatch(source_id) is None
        or type(uid) is not int
        or uid <= 0
        or type(gid) is not int
        or gid <= 0
        or _BINDING_ID.fullmatch(binding_id) is None
        or _PACKAGE_ID.fullmatch(package_id) is None
        or type(catalog_generation) is not int
        or catalog_generation < 1
    ):
        raise WorkloadPackageRuntimeError("Package Runtime operation identity is invalid")
    generation, catalog_sources = _catalog_identity(activity_catalog)
    policy = _validate_runtime_policy_against_catalog(runtime_policy, activity_catalog)
    if generation != catalog_generation:
        raise WorkloadPackageRuntimeError("Package Runtime catalog generation is stale")
    activity_source = catalog_sources.get(source_id)
    policy_source = next((row for row in policy["sources"] if row["source_id"] == source_id), None)
    principal_value = source_principals.get(source_id)
    principal = _source_principal(principal_value, source_id)
    if (
        activity_source is None
        or policy_source is None
        or activity_source["uid"] != uid
        or activity_source["gid"] != gid
        or principal["uid"] != uid
        or principal["gid"] != gid
        or policy_source["uid"] != uid
        or policy_source["gid"] != gid
    ):
        raise WorkloadPackageRuntimeError(
            "Package Runtime caller UID/GID differs from registered source"
        )
    policy_binding = next(
        (row for row in policy_source["bindings"] if row["binding_id"] == binding_id), None
    )
    if (
        policy_binding is None
        or policy_binding["package_id"] != package_id
        or operation not in policy_binding["operations"]
    ):
        raise WorkloadPackageRuntimeError("Package Runtime operation is outside the owner policy")
    requested_ids = list(installation_ids)
    if (
        not requested_ids
        or requested_ids != sorted(set(requested_ids))
        or any(
            not isinstance(item, str) or _INSTALLATION_ID.fullmatch(item) is None
            for item in requested_ids
        )
        or not set(requested_ids).issubset(policy_binding["installation_ids"])
    ):
        raise WorkloadPackageRuntimeError(
            "Package Runtime installation IDs exceed the owner policy"
        )
    scope = next(
        (row for row in activity_source["binding_scopes"] if row["binding_id"] == binding_id), None
    )
    if (
        scope is None
        or scope["package_id"] != package_id
        or not set(requested_ids).issubset(scope["installation_ids"])
    ):
        raise WorkloadPackageRuntimeError(
            "Broker binding scope differs from the Runtime owner policy"
        )
    try:
        account = pwd.getpwuid(uid)
        group = grp.getgrgid(gid)
    except KeyError as error:
        raise WorkloadPackageRuntimeError(
            "Registered Package Runtime account is unavailable"
        ) from error
    if account.pw_uid != uid or group.gr_gid != gid:
        raise WorkloadPackageRuntimeError("Registered Package Runtime account identity changed")
    token_path = principal["tokenPath"]
    if token_path.name != f"{source_id}.token":
        raise WorkloadPackageRuntimeError(
            "Source token path does not match the registered source ID"
        )
    _verify_token_file(token_path, activity_source["source_token_sha256"])
    return policy_source, policy_binding, token_path


def run_package_binding_operation(
    *,
    operation: str,
    source_id: str,
    uid: int,
    gid: int,
    token_path: Path,
    binding_id: str,
    package_id: str,
    installation_ids: Sequence[str],
    catalog_generation: int,
    request_id: str,
    sdk_python: Path = DEFAULT_SDK_PYTHON,
    activity_catalog: Mapping[str, Any],
    runtime_policy: Mapping[str, Any],
    source_principals: Mapping[str, Mapping[str, Any]],
    persist_intent: Callable[[str, Mapping[str, Any]], None] | None = None,
    persist_outcome: Callable[[Mapping[str, Any] | None, Mapping[str, Any]], None] | None = None,
    persist_reconcile: Callable[
        [Mapping[str, Any] | None, Mapping[str, Any], Mapping[str, Any]], None
    ]
    | None = None,
    socket_path: Path = DEFAULT_PACKAGE_RUNTIME_SOCKET,
    maintenance_socket_path: Path = DEFAULT_MAINTENANCE_SOCKET,
    timeout: float = 30.0,
    process_factory: Any = subprocess.Popen,
) -> dict[str, Any]:
    """Run one authenticated SDK operation as the registered source principal.

    Root validates and opens the protected token, then gives the SDK child only
    an inherited anonymous descriptor. Owner callbacks execute in this root
    process over a private socket; the child cannot Complete until each callback
    has durably acknowledged the parent journal. 中文：先持久化owner意图和结果，再允许SDK完成admission。
    """

    _require_root()
    _require_request_id(request_id)
    _validate_owner_call(
        operation=operation,
        source_id=source_id,
        uid=uid,
        gid=gid,
        binding_id=binding_id,
        package_id=package_id,
        installation_ids=installation_ids,
        catalog_generation=catalog_generation,
        activity_catalog=activity_catalog,
        runtime_policy=runtime_policy,
        source_principals=source_principals,
    )
    principal = _source_principal(source_principals[source_id], source_id)
    if Path(token_path) != principal["tokenPath"]:
        raise WorkloadPackageRuntimeError(
            "SDK token path differs from the registered source principal"
        )
    if operation in ("activate", "recover_binding", "deactivate"):
        if not all(
            callable(callback) for callback in (persist_intent, persist_outcome, persist_reconcile)
        ):
            raise WorkloadPackageRuntimeError(
                "Durable owner callbacks are required for binding mutations"
            )
        if len(installation_ids) != 1:
            raise WorkloadPackageRuntimeError(
                "One exact installation ID is required per binding mutation"
            )
    elif persist_intent is not None or persist_outcome is not None or persist_reconcile is not None:
        raise WorkloadPackageRuntimeError(
            "Read-only SDK operations do not accept mutation callbacks"
        )
    python_path = Path(sdk_python)
    _validate_sdk_python(python_path)
    if (
        not socket_path.is_absolute()
        or not maintenance_socket_path.is_absolute()
        or timeout <= 0
        or timeout > 120
    ):
        raise WorkloadPackageRuntimeError("Package Runtime SDK endpoint or timeout is invalid")
    try:
        user = pwd.getpwuid(uid)
        group = grp.getgrgid(gid)
    except KeyError as error:
        raise WorkloadPackageRuntimeError("Registered source account is unavailable") from error
    if user.pw_uid == 0 or group.gr_gid == 0:
        raise WorkloadPackageRuntimeError("Package Runtime sources cannot run as root")

    expected_token_hash = _catalog_identity(activity_catalog)[1][source_id]["source_token_sha256"]
    token = _read_token(token_path, expected_token_hash)
    token_fd = _create_token_memfd(token, uid=uid, gid=gid)
    parent_channel, child_channel = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    parent_channel.setblocking(False)
    driver_request = {
        "operation": operation,
        "source_id": source_id,
        "source_uid": uid,
        "source_gid": gid,
        "binding_id": binding_id,
        "package_id": package_id,
        "installation_ids": list(installation_ids),
        "catalog_generation": catalog_generation,
        "request_id": request_id,
        "socket_path": str(socket_path),
        "maintenance_socket_path": str(maintenance_socket_path),
    }
    driver_argv = [
        str(python_path),
        "-I",
        "-c",
        _SDK_DRIVER_PROGRAM,
        str(child_channel.fileno()),
        str(token_fd),
        _canonical_json(driver_request).decode("utf-8"),
    ]
    environment = {"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"}

    def drop_privileges() -> None:
        """Discard root groups and switch the SDK child to the exact source peer."""

        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
        os.umask(0o077)

    child: Any = None
    try:
        child = process_factory(
            driver_argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd="/",
            env=environment,
            close_fds=True,
            pass_fds=(child_channel.fileno(), token_fd),
            preexec_fn=drop_privileges,
        )
    except (OSError, subprocess.SubprocessError) as error:
        parent_channel.close()
        child_channel.close()
        os.close(token_fd)
        raise WorkloadPackageRuntimeError(
            "Source-owned Package Runtime SDK could not start"
        ) from error
    child_channel.close()
    os.close(token_fd)
    buffered = bytearray()
    read_result: dict[str, Any] | None = None
    got_read_result = False
    summary: dict[str, Any] | None = None
    identity_checked = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            if time.monotonic() >= deadline:
                child.kill()
                raise WorkloadPackageRuntimeError("Source-owned Package Runtime SDK timed out")
            ready, _, _ = select.select(
                [parent_channel], [], [], min(0.1, deadline - time.monotonic())
            )
            if ready:
                chunk = parent_channel.recv(MAX_IPC_BYTES - len(buffered) + 1)
                if not chunk:
                    break
                buffered.extend(chunk)
                if len(buffered) > MAX_IPC_BYTES:
                    child.kill()
                    raise WorkloadPackageRuntimeError(
                        "Source-owned SDK callback frame is too large"
                    )
                while b"\n" in buffered:
                    line, _, remainder = buffered.partition(b"\n")
                    buffered[:] = remainder
                    message = _parse_sdk_frame(line)
                    kind = message.get("kind")
                    if kind == "identity":
                        if (
                            identity_checked
                            or message.get("uid") != uid
                            or message.get("euid") != uid
                            or message.get("gid") != gid
                            or message.get("egid") != gid
                            or message.get("groups") != []
                        ):
                            raise WorkloadPackageRuntimeError(
                                "SDK peer credentials differ from registered source"
                            )
                        _verify_process_identity(child.pid, uid=uid, gid=gid)
                        identity_checked = True
                        parent_channel.sendall(b'{"ok":true}\n')
                    elif kind == "callback":
                        if not identity_checked:
                            raise WorkloadPackageRuntimeError(
                                "SDK callback arrived before peer identity proof"
                            )
                        _dispatch_durable_callback(
                            message,
                            expected_request_id=request_id,
                            source_id=source_id,
                            catalog_generation=catalog_generation,
                            operation=operation,
                            binding_id=binding_id,
                            package_id=package_id,
                            installation_id=installation_ids[0],
                            persist_intent=persist_intent,
                            persist_outcome=persist_outcome,
                            persist_reconcile=persist_reconcile,
                        )
                        parent_channel.sendall(b'{"ok":true}\n')
                    elif kind == "read_result":
                        value = message.get("value")
                        if (
                            not identity_checked
                            or got_read_result
                            or (value is not None and not isinstance(value, dict))
                        ):
                            raise WorkloadPackageRuntimeError(
                                "SDK returned duplicate or invalid readback"
                            )
                        read_result = value
                        got_read_result = True
                        parent_channel.sendall(b'{"ok":true}\n')
                    elif kind == "summary":
                        if (
                            not identity_checked
                            or summary is not None
                            or not isinstance(message.get("value"), dict)
                        ):
                            raise WorkloadPackageRuntimeError(
                                "SDK returned duplicate or invalid summary"
                            )
                        summary = message["value"]
                        parent_channel.sendall(b'{"ok":true}\n')
                    elif kind == "error":
                        raise WorkloadPackageRuntimeError(
                            "Source-owned Package Runtime SDK operation failed"
                        )
                    else:
                        raise WorkloadPackageRuntimeError(
                            "Source-owned SDK callback type is unsupported"
                        )
            if child.poll() is not None:
                # Drain any final frame before leaving; the child may have closed after it.
                ready, _, _ = select.select([parent_channel], [], [], 0)
                if not ready:
                    break
        return_code = child.wait(timeout=max(0.1, deadline - time.monotonic()))
        stdout = child.stdout.read(MAX_FRAME_BYTES + 1) if child.stdout is not None else b""
        if return_code != 0 or len(stdout) > MAX_FRAME_BYTES or not identity_checked or buffered:
            raise WorkloadPackageRuntimeError("Source-owned Package Runtime SDK operation failed")
        result_frame: Any = None
        if stdout:
            try:
                result_frame = json.loads(stdout, object_pairs_hook=_unique_object)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise WorkloadPackageRuntimeError("Source-owned SDK result is malformed") from error
        if (
            not isinstance(result_frame, dict)
            or set(result_frame) != {"ok", "kind", "value"}
            or result_frame.get("ok") is not True
            or result_frame.get("kind") != "complete"
            or result_frame.get("value") != {"requestId": request_id}
        ):
            raise WorkloadPackageRuntimeError("Source-owned SDK result is not successful")
        if operation == "get_installation":
            if not got_read_result or (
                read_result is not None and not isinstance(read_result, dict)
            ):
                raise WorkloadPackageRuntimeError(
                    "Package Runtime installation readback is missing"
                )
            if read_result is not None:
                if read_result.get("installation_id") != installation_ids[0]:
                    raise WorkloadPackageRuntimeError(
                        "Package Runtime returned another installation"
                    )
                _validate_installation_record_identity(
                    {
                        "packageId": read_result.get("package_id"),
                        "version": read_result.get("package_version"),
                        "packageArtifactDigest": read_result.get("artifact_digest"),
                    },
                    read_result,
                )
            return {"operation": operation, "requestId": request_id, "installation": read_result}
        if operation == "runtime_status":
            if read_result is None:
                return {"operation": operation, "requestId": request_id, "status": None}
            sanitized = _safe_runtime_status(read_result)
            if (
                sanitized["binding_id"] != binding_id
                or sanitized["installation_id"] not in installation_ids
            ):
                raise WorkloadPackageRuntimeError(
                    "Package Runtime status exceeds the requested binding scope"
                )
            return {
                "operation": operation,
                "requestId": request_id,
                "status": sanitized,
            }
        if not isinstance(summary, dict):
            raise WorkloadPackageRuntimeError("Package Runtime mutation summary is missing")
        _validate_sdk_summary(summary, binding_id=binding_id, installation_id=installation_ids[0])
        return {"operation": operation, "requestId": request_id, **summary}
    except WorkloadPackageRuntimeError:
        if child.poll() is None:
            child.kill()
        child.wait()
        raise
    except (OSError, subprocess.SubprocessError) as error:
        if child.poll() is None:
            child.kill()
        child.wait()
        raise WorkloadPackageRuntimeError(
            "Source-owned Package Runtime SDK bridge failed"
        ) from error
    except Exception as error:
        if child.poll() is None:
            child.kill()
        child.wait()
        raise WorkloadPackageRuntimeError("Parent journal callback or SDK bridge failed") from error
    finally:
        parent_channel.close()
        if child.stdout is not None:
            child.stdout.close()


def _validate_binding_operation_receipt(
    receipt: Mapping[str, Any],
    *,
    request_id: str,
    source_id: str,
    catalog_generation: int,
    binding_id: str,
    package_id: str,
    installation_id: str,
    operation: str,
) -> None:
    """Bind callback receipt fields to the exact SDK request and source scope."""

    expected = {
        "request_id",
        "source_id",
        "protocol_version",
        "catalog_generation",
        "scope",
        "operation_token",
        "gate_generation",
        "already_in_flight",
        "already_completed",
    }
    scope = receipt.get("scope") if isinstance(receipt, Mapping) else None
    if (
        not isinstance(receipt, Mapping)
        or set(receipt) != expected
        or receipt.get("request_id") != request_id
        or receipt.get("source_id") != source_id
        or receipt.get("protocol_version") != BINDING_OPERATION_PROTOCOL
        or type(receipt.get("catalog_generation")) is not int
        or receipt.get("catalog_generation") != catalog_generation
        or not isinstance(receipt.get("operation_token"), str)
        or not receipt["operation_token"]
        or len(receipt["operation_token"]) > 4096
        or any(ord(character) < 32 for character in receipt["operation_token"])
        or type(receipt.get("gate_generation")) is not int
        or receipt["gate_generation"] < 1
        or type(receipt.get("already_in_flight")) is not bool
        or type(receipt.get("already_completed")) is not bool
        or (receipt["already_in_flight"] and receipt["already_completed"])
        or not isinstance(scope, Mapping)
        or dict(scope)
        != {
            "binding_id": binding_id,
            "package_id": package_id,
            "installation_id": installation_id,
            "operation": operation,
        }
    ):
        raise WorkloadPackageRuntimeError(
            "SDK binding-operation receipt differs from the requested scope"
        )


def _binding_receipt_identity(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Return journal-safe receipt identity without its bearer operation token."""

    return {
        key: receipt[key]
        for key in (
            "request_id",
            "source_id",
            "protocol_version",
            "catalog_generation",
            "scope",
            "gate_generation",
            "already_in_flight",
            "already_completed",
        )
    }


def _validate_sdk_summary(
    summary: Mapping[str, Any], *, binding_id: str, installation_id: str
) -> None:
    """Validate the redacted result frame returned after durable SDK callbacks."""

    if (
        not isinstance(summary, Mapping)
        or set(summary)
        != {"bindingId", "installationId", "generation", "state", "failureCode", "reconciled"}
        or summary.get("bindingId") != binding_id
        or summary.get("installationId") != installation_id
        or type(summary.get("reconciled")) is not bool
        or summary.get("state") not in {"RUNNING", "STOPPED", "FAILED"}
        or (
            summary.get("generation") is not None
            and (type(summary["generation"]) is not int or summary["generation"] < 1)
        )
        or (
            summary.get("generation") is None
            and not (summary.get("reconciled") is True and summary.get("state") == "STOPPED")
        )
        or (
            summary.get("failureCode") is not None
            and (
                not isinstance(summary["failureCode"], str)
                or not summary["failureCode"]
                or any(ord(character) < 32 for character in summary["failureCode"])
            )
        )
    ):
        raise WorkloadPackageRuntimeError("SDK mutation summary differs from its admitted binding")


def _verify_process_identity(pid: int, *, uid: int, gid: int) -> None:
    """Read Linux's process credentials while the source child awaits its ACK."""

    if type(pid) is not int or pid <= 0:
        raise WorkloadPackageRuntimeError("SDK child process identity is unavailable")
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
    except (OSError, UnicodeError) as error:
        raise WorkloadPackageRuntimeError(
            "SDK child process credentials cannot be verified"
        ) from error
    fields: dict[str, list[str]] = {}
    for line in status.splitlines():
        name, separator, value = line.partition(":")
        if separator and name in {"Uid", "Gid", "Groups"}:
            fields[name] = value.split()
    try:
        process_uids = [int(value) for value in fields["Uid"]]
        process_gids = [int(value) for value in fields["Gid"]]
        supplementary_groups = [int(value) for value in fields["Groups"]]
    except (KeyError, ValueError) as error:
        raise WorkloadPackageRuntimeError("SDK child process credentials are malformed") from error
    if process_uids != [uid] * 4 or process_gids != [gid] * 4 or supplementary_groups:
        raise WorkloadPackageRuntimeError(
            "SDK child process is not isolated to its registered source"
        )


def _dispatch_durable_callback(
    message: Mapping[str, Any],
    *,
    expected_request_id: str,
    source_id: str,
    catalog_generation: int,
    operation: str,
    binding_id: str,
    package_id: str,
    installation_id: str,
    persist_intent: Callable[..., Any] | None,
    persist_outcome: Callable[..., Any] | None,
    persist_reconcile: Callable[..., Any] | None,
) -> None:
    """Run one parent-owned journal callback synchronously before SDK ack."""

    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        raise WorkloadPackageRuntimeError("SDK durable callback payload is malformed")

    def persist(callback: Callable[..., Any], *args: Any) -> None:
        try:
            result = callback(*args)
        except Exception:  # noqa: BLE001 - redact arbitrary parent journal failures before SDK ack.
            raise WorkloadPackageRuntimeError("Parent journal callback failed") from None
        if result is not None:
            raise WorkloadPackageRuntimeError(
                "Parent journal callback did not complete synchronously"
            )

    event = payload.get("event")
    if event == "intent" and callable(persist_intent):
        request_id = payload.get("requestId")
        scope = payload.get("scope")
        expected_scope_operation = "recover" if operation == "recover_binding" else operation
        if (
            request_id != expected_request_id
            or not isinstance(scope, Mapping)
            or dict(scope)
            != {
                "binding_id": binding_id,
                "package_id": package_id,
                "installation_id": installation_id,
                "operation": expected_scope_operation,
            }
        ):
            raise WorkloadPackageRuntimeError("SDK operation intent is malformed")
        persist(persist_intent, request_id, dict(scope))
    elif event == "outcome" and callable(persist_outcome):
        status = payload.get("status")
        receipt = payload.get("receipt")
        expected_scope_operation = "recover" if operation == "recover_binding" else operation
        if not isinstance(status, Mapping) or not isinstance(receipt, Mapping):
            raise WorkloadPackageRuntimeError("SDK operation outcome is malformed")
        _validate_binding_operation_receipt(
            receipt,
            request_id=expected_request_id,
            source_id=source_id,
            catalog_generation=catalog_generation,
            binding_id=binding_id,
            package_id=package_id,
            installation_id=installation_id,
            operation=expected_scope_operation,
        )
        safe_status = _safe_runtime_status(status)
        if (
            safe_status["binding_id"] != binding_id
            or safe_status["installation_id"] != installation_id
        ):
            raise WorkloadPackageRuntimeError(
                "SDK operation status differs from its admitted scope"
            )
        persist(persist_outcome, safe_status, _binding_receipt_identity(receipt))
    elif event == "reconcile" and callable(persist_reconcile):
        status = payload.get("status")
        installation = payload.get("installation")
        receipt = payload.get("receipt")
        if (
            (status is not None and not isinstance(status, Mapping))
            or not isinstance(installation, Mapping)
            or not isinstance(receipt, Mapping)
        ):
            raise WorkloadPackageRuntimeError("SDK reconciliation evidence is malformed")
        _validate_binding_operation_receipt(
            receipt,
            request_id=expected_request_id,
            source_id=source_id,
            catalog_generation=catalog_generation,
            binding_id=binding_id,
            package_id=package_id,
            installation_id=installation_id,
            operation="recover" if operation == "recover_binding" else operation,
        )
        if installation.get("installation_id") != installation_id:
            raise WorkloadPackageRuntimeError(
                "SDK reconciliation installation differs from its scope"
            )
        _validate_installation_record_identity(
            {
                "packageId": package_id,
                "version": installation.get("package_version"),
                "packageArtifactDigest": installation.get("artifact_digest"),
            },
            installation,
        )
        if status is not None:
            safe_status = _safe_runtime_status(status)
            if (
                safe_status["binding_id"] != binding_id
                or safe_status["installation_id"] != installation_id
            ):
                raise WorkloadPackageRuntimeError(
                    "SDK reconciliation status differs from its scope"
                )
        persist(
            persist_reconcile,
            _safe_runtime_status(status) if status is not None else None,
            dict(installation),
            _binding_receipt_identity(receipt),
        )
    else:
        raise WorkloadPackageRuntimeError("SDK requested an unconfigured durable callback")


def _parse_sdk_frame(raw: bytes) -> dict[str, Any]:
    """Decode one bounded private SDK callback frame."""

    if not raw or len(raw) > MAX_FRAME_BYTES:
        raise WorkloadPackageRuntimeError("Source-owned SDK callback frame is invalid")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkloadPackageRuntimeError("Source-owned SDK callback frame is malformed") from error
    if not isinstance(value, dict):
        raise WorkloadPackageRuntimeError("Source-owned SDK callback frame is not an object")
    return value


def _validate_sdk_python(path: Path) -> None:
    """Require an immutable root-owned executable interpreter path."""

    if not path.is_absolute() or ".." in path.parts:
        raise WorkloadPackageRuntimeError("SDK Python path must be normalized and absolute")
    _validate_sdk_path_components(path, allow_final_symlink=True)
    try:
        executable = path.resolve(strict=True)
    except OSError as error:
        raise WorkloadPackageRuntimeError("Attested SDK Python is unavailable") from error
    if executable != path:
        _validate_sdk_path_components(executable, allow_final_symlink=False)
    try:
        info = executable.lstat()
    except OSError as error:
        raise WorkloadPackageRuntimeError("Attested SDK Python is unavailable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or not info.st_mode & 0o100
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & 0o022
        or info.st_nlink != 1
    ):
        raise WorkloadPackageRuntimeError("Attested SDK Python executable metadata is unsafe")


def _validate_sdk_path_components(path: Path, *, allow_final_symlink: bool) -> None:
    """Require every interpreter path component to remain root-controlled."""

    current = Path(path.anchor)
    for index, part in enumerate(path.parts[1:]):
        current = current / part
        try:
            info = current.lstat()
        except OSError as error:
            raise WorkloadPackageRuntimeError("Attested SDK Python is unavailable") from error
        is_final = index == len(path.parts[1:]) - 1
        if stat.S_ISLNK(info.st_mode):
            if not (is_final and allow_final_symlink and info.st_uid == 0):
                raise WorkloadPackageRuntimeError(
                    "Attested SDK Python path contains an unsafe symlink"
                )
            continue
        if not is_final and not stat.S_ISDIR(info.st_mode):
            raise WorkloadPackageRuntimeError("Attested SDK Python path has a non-directory parent")
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise WorkloadPackageRuntimeError("Attested SDK Python path is not root-controlled")


def _read_token(path: Path, expected_digest: str) -> str:
    """Read the token only after no-follow metadata and hash validation."""

    _verify_token_file(path, expected_digest)
    try:
        raw = path.read_bytes()
        token = raw.decode("utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise WorkloadPackageRuntimeError("Activity source token cannot be read") from error
    if not token:
        raise WorkloadPackageRuntimeError("Activity source token is empty")
    return token


def _create_token_memfd(token: str, *, uid: int | None = None, gid: int | None = None) -> int:
    """Put one verified token into an anonymous sealed descriptor for the child."""

    if (
        sys.platform != "linux"
        or not hasattr(os, "memfd_create")
        or not hasattr(os, "MFD_ALLOW_SEALING")
    ):
        raise WorkloadPackageRuntimeError("Linux anonymous token descriptors are unavailable")
    import fcntl as _fcntl

    seal_constants = {
        name: getattr(_fcntl, name, value) for name, value in _LINUX_SEAL_FCNTL_VALUES.items()
    }
    try:
        descriptor = os.memfd_create(
            "cyrene-activity-token",
            os.MFD_ALLOW_SEALING | getattr(os, "MFD_CLOEXEC", 0),
        )
        encoded = token.encode("utf-8")
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("sealed token descriptor write made no progress")
            remaining = remaining[written:]
        os.fchown(
            descriptor,
            os.geteuid() if uid is None else uid,
            os.getegid() if gid is None else gid,
        )
        os.fchmod(descriptor, 0o400)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.set_inheritable(descriptor, True)
        seals = (
            seal_constants["F_SEAL_SEAL"]
            | seal_constants["F_SEAL_SHRINK"]
            | seal_constants["F_SEAL_GROW"]
            | seal_constants["F_SEAL_WRITE"]
        )
        _fcntl.fcntl(descriptor, seal_constants["F_ADD_SEALS"], seals)
        applied_seals = _fcntl.fcntl(descriptor, seal_constants["F_GET_SEALS"])
        if applied_seals & seals != seals:
            raise OSError("kernel did not apply all required token descriptor seals")
        return descriptor
    except OSError as error:
        if "descriptor" in locals():
            os.close(descriptor)
        raise WorkloadPackageRuntimeError(
            "Activity token could not be placed in a sealed descriptor"
        ) from error


def _safe_runtime_status(status: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a full SDK RuntimeStatus and return its nonsecret identity fields."""

    fields = {
        "binding_id",
        "installation_id",
        "generation",
        "state",
        "failure_code",
        "failure_message",
        "connection_ref",
    }
    if (
        not isinstance(status, Mapping)
        or not fields.issubset(status)
        or not isinstance(status.get("binding_id"), str)
        or _BINDING_ID.fullmatch(status["binding_id"]) is None
        or not isinstance(status.get("installation_id"), str)
        or _INSTALLATION_ID.fullmatch(status["installation_id"]) is None
        or type(status.get("generation")) is not int
        or status["generation"] < 1
        or status.get("state") not in {"RUNNING", "STOPPED", "FAILED"}
        or any(
            value is not None
            and (not isinstance(value, str) or not value or any(ord(ch) < 32 for ch in value))
            for value in (
                status.get("failure_code"),
                status.get("failure_message"),
                status.get("connection_ref"),
            )
        )
    ):
        raise WorkloadPackageRuntimeError("Package Runtime returned an invalid RuntimeStatus")
    return {
        key: status[key]
        for key in (
            "binding_id",
            "installation_id",
            "generation",
            "state",
            "failure_code",
            "connection_ref",
        )
    }


def _validate_installation_record_identity_for_hint(
    record: Mapping[str, Any], hint: Mapping[str, Any]
) -> dict[str, Any]:
    """Match every authoritative Package Runtime identity field to the local hint."""

    required_hint_fields = {
        "componentId",
        "packageId",
        "installed",
        "installationId",
        "version",
        "releaseId",
        "manifestUri",
        "manifestDigest",
        "manifestAssetDigest",
        "digest",
        "targetId",
        "indexIdentity",
        "publisherIdentity",
        "attestationRef",
        "descriptorDigest",
        "archiveDigest",
        "dependencyLockDigest",
    }
    if not required_hint_fields.issubset(hint):
        raise WorkloadPackageRuntimeError("Package Runtime installation hint is incomplete")
    for field in (
        "manifestDigest",
        "manifestAssetDigest",
        "digest",
        "descriptorDigest",
        "archiveDigest",
        "dependencyLockDigest",
    ):
        if not isinstance(hint.get(field), str) or _TYPED_SHA256.fullmatch(hint[field]) is None:
            raise WorkloadPackageRuntimeError("Package Runtime installation hint digest is invalid")
    hinted_artifact_digest = hint.get("packageArtifactDigest", hint.get("artifactDigest"))
    if (
        not isinstance(hinted_artifact_digest, str)
        or _TYPED_SHA256.fullmatch(hinted_artifact_digest) is None
    ):
        raise WorkloadPackageRuntimeError(
            "Package Runtime installation hint artifact digest is invalid"
        )
    for field in ("releaseId", "manifestUri", "targetId"):
        if not isinstance(hint.get(field), str) or not hint[field]:
            raise WorkloadPackageRuntimeError(
                "Package Runtime installation hint provenance is invalid"
            )
    for field in ("indexIdentity", "publisherIdentity", "attestationRef"):
        if not isinstance(hint.get(field), Mapping) or not hint[field]:
            raise WorkloadPackageRuntimeError(
                "Package Runtime installation hint provenance is incomplete"
            )
    verification = record.get("verification")
    dependencies = record.get("dependencies")
    if not isinstance(verification, Mapping) or not isinstance(dependencies, Mapping):
        raise WorkloadPackageRuntimeError("Package Runtime installation readback is incomplete")
    expected = {
        "componentId": hint.get("componentId"),
        "packageId": hint.get("packageId"),
        "installationId": hint.get("installationId"),
        "version": hint.get("version"),
        "digest": hint.get("digest"),
        "artifactDigest": hinted_artifact_digest,
        "archiveDigest": hint.get("archiveDigest"),
        "descriptorDigest": hint.get("descriptorDigest"),
        "manifestDigest": hint.get("manifestDigest"),
        "dependencyLockDigest": hint.get("dependencyLockDigest"),
    }
    actual = {
        "componentId": hint.get("componentId"),
        "packageId": record.get("package_id"),
        "installationId": record.get("installation_id"),
        "version": record.get("package_version"),
        "digest": record.get("archive_digest"),
        "artifactDigest": record.get("artifact_digest"),
        "archiveDigest": record.get("archive_digest"),
        "descriptorDigest": verification.get("descriptor_digest"),
        "manifestDigest": verification.get("manifest_digest"),
        "dependencyLockDigest": verification.get("dependency_lock_digest"),
    }
    if hint.get("installed") is not True or expected != actual:
        raise WorkloadPackageRuntimeError(
            "Local plugin receipt differs from authenticated Package Runtime"
        )
    _validate_installation_record_identity(
        {
            "packageId": hint["packageId"],
            "version": hint["version"],
            "packageArtifactDigest": actual["artifactDigest"],
        },
        record,
    )
    inventory = {
        field: hint[field]
        for field in (
            "componentId",
            "packageId",
            "installationId",
            "version",
            "releaseId",
            "manifestUri",
            "manifestDigest",
            "manifestAssetDigest",
            "digest",
            "targetId",
            "indexIdentity",
            "publisherIdentity",
            "attestationRef",
            "descriptorDigest",
            "archiveDigest",
            "dependencyLockDigest",
        )
    }
    inventory.update(
        {
            "installed": True,
            "packageArtifactDigest": record["artifact_digest"],
            "installationId": record["installation_id"],
        }
    )
    return inventory


def read_workload_package_inventory(
    *,
    selected_rows: Sequence[Mapping[str, Any]],
    source_principals: Mapping[str, Mapping[str, Any]],
    sdk_python: Path,
    activity_catalog_path: Path = DEFAULT_ACTIVITY_CATALOG_PATH,
    policy_path: Path = DEFAULT_POLICY_PATH,
    token_directory: Path = DEFAULT_TOKEN_DIRECTORY,
    installation_hints: Mapping[str, Mapping[str, Any]],
    socket_path: Path = DEFAULT_PACKAGE_RUNTIME_SOCKET,
    maintenance_socket_path: Path = DEFAULT_MAINTENANCE_SOCKET,
    request_id_factory: Callable[[str, str], str] | None = None,
    process_factory: Any = subprocess.Popen,
) -> dict[str, Any]:
    """Read installed plugin identity and active owner use from authenticated UDS.

    Local updater receipts are treated only as hints. A component is returned as
    installed only when every installation field matches the Package Runtime
    record reached through the registered source UID/GID and root token.
    中文：本地receipt不能代替UDS真实安装记录。
    """

    _require_root()
    catalog = _read_activity_catalog(Path(activity_catalog_path))
    policy_value = read_runtime_source_policy_generic(Path(policy_path))
    policy = _validate_runtime_policy_against_catalog(policy_value, catalog)
    if not isinstance(installation_hints, Mapping):
        raise WorkloadPackageRuntimeError("Package Runtime installation hints are malformed")
    package_rows: dict[str, Mapping[str, Any]] = {}
    for row in selected_rows:
        if not isinstance(row, Mapping):
            raise WorkloadPackageRuntimeError("Selected plugin inventory row is malformed")
        component_id = row.get("componentId")
        package_id = row.get("packageId")
        binding_id = row.get("bindingId")
        if (
            not isinstance(component_id, str)
            or _COMPONENT_ID.fullmatch(component_id) is None
            or not isinstance(package_id, str)
            or _PACKAGE_ID.fullmatch(package_id) is None
            or not isinstance(binding_id, str)
            or _BINDING_ID.fullmatch(binding_id) is None
        ):
            raise WorkloadPackageRuntimeError("Selected plugin inventory identity is invalid")
        _source_policy(row.get("sourcePolicy"))
        if component_id in package_rows:
            raise WorkloadPackageRuntimeError("Selected plugin inventory repeats a component")
        package_rows[component_id] = row
    components: dict[str, dict[str, Any]] = {}
    installation_records: dict[str, dict[str, Any]] = {}
    source_bindings: list[dict[str, Any]] = []
    request_factory = request_id_factory or _inventory_request_id

    # ── Phase 1: Query exact selected Package Runtime installations ──
    # 第一阶段：按精确owner scope读取安装记录并与本地hint逐字段核对。
    for component_id, row in sorted(package_rows.items()):
        source_id, _source_ids = _source_policy(row["sourcePolicy"])
        source = next((item for item in policy["sources"] if item["source_id"] == source_id), None)
        hint = installation_hints.get(component_id)
        if source is None:
            if isinstance(hint, Mapping) and hint.get("installed") is True:
                raise WorkloadPackageRuntimeError(
                    "Installed Package Runtime owner source is not registered"
                )
            continue
        binding_id = row["bindingId"]
        binding = next(
            (item for item in source["bindings"] if item["binding_id"] == binding_id), None
        )
        if (
            binding is None
            or binding["package_id"] != row["packageId"]
            or not isinstance(hint, Mapping)
            or hint.get("componentId") != component_id
            or hint.get("packageId") != row["packageId"]
        ):
            if isinstance(hint, Mapping) and hint.get("installed") is True:
                raise WorkloadPackageRuntimeError(
                    "Installed Package Runtime owner binding is not registered"
                )
            continue
        installation_id = hint.get("installationId")
        if (
            not isinstance(installation_id, str)
            or installation_id not in binding["installation_ids"]
        ):
            continue
        principal = _source_principal(source_principals.get(source_id), source_id)
        result = run_package_binding_operation(
            operation="get_installation",
            source_id=source_id,
            uid=principal["uid"],
            gid=principal["gid"],
            token_path=principal["tokenPath"],
            binding_id=binding_id,
            package_id=row["packageId"],
            installation_ids=[installation_id],
            catalog_generation=policy["generation"],
            request_id=request_factory(component_id, "installation"),
            sdk_python=sdk_python,
            activity_catalog=catalog,
            runtime_policy=policy,
            source_principals=source_principals,
            socket_path=socket_path,
            maintenance_socket_path=maintenance_socket_path,
            process_factory=process_factory,
        )
        installation = result["installation"]
        if installation is None:
            continue
        try:
            components[component_id] = _validate_installation_record_identity_for_hint(
                installation, hint
            )
            installation_records[component_id] = dict(installation)
        except WorkloadPackageRuntimeError:
            # A mismatched local receipt must stay absent from resolver inventory.
            continue

    # ── Phase 2: Read every registered owner, including owners outside this plan ──
    # 第二阶段：扫描所有已登记source，卸载检查不能漏掉其他owner。
    for source in policy["sources"]:
        source_id = source["source_id"]
        principal = _source_principal(source_principals.get(source_id), source_id)
        _verify_token_file(principal["tokenPath"], source["source_token_sha256"])
        for binding in source["bindings"]:
            result = run_package_binding_operation(
                operation="runtime_status",
                source_id=source_id,
                uid=principal["uid"],
                gid=principal["gid"],
                token_path=principal["tokenPath"],
                binding_id=binding["binding_id"],
                package_id=binding["package_id"],
                installation_ids=binding["installation_ids"],
                catalog_generation=policy["generation"],
                request_id=request_factory(binding["binding_id"], "status"),
                sdk_python=sdk_python,
                activity_catalog=catalog,
                runtime_policy=policy,
                source_principals=source_principals,
                socket_path=socket_path,
                maintenance_socket_path=maintenance_socket_path,
                process_factory=process_factory,
            )
            status = result.get("status")
            active_id = None
            state = None
            failure_code = None
            if isinstance(status, Mapping):
                if status.get("binding_id") != binding["binding_id"]:
                    raise WorkloadPackageRuntimeError("Runtime status belongs to another binding")
                state = status.get("state")
                failure_code = status.get("failure_code")
                candidate_id = status.get("installation_id")
                if candidate_id not in binding["installation_ids"]:
                    raise WorkloadPackageRuntimeError(
                        "Runtime status installation exceeds owner scope"
                    )
                if state == "RUNNING":
                    active_id = candidate_id
            source_bindings.append(
                {
                    "sourceId": source_id,
                    "bindingId": binding["binding_id"],
                    "packageId": binding["package_id"],
                    "installationIds": list(binding["installation_ids"]),
                    "activeInstallationId": active_id,
                    "state": state,
                    "failureCode": failure_code,
                }
            )
    return {
        "components": components,
        "installationRecords": installation_records,
        "sourceBindings": source_bindings,
    }


def _inventory_request_id(component_or_binding_id: str, action: str) -> str:
    """Derive a unique safe read ID without logging source credentials."""

    digest = hashlib.sha256(
        f"{component_or_binding_id}\0{action}\0{os.urandom(16).hex()}".encode()
    ).hexdigest()[:32]
    return f"cyrene-workload-inventory-{digest}"


def read_runtime_source_policy_generic(path: Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    """Read the fixed root:cyrene policy with generic multi-owner validation."""

    group_id = _runtime_group_id()
    value, _raw = _read_owned_json(Path(path), group_id=group_id)
    return _runtime_policy(value)


def write_runtime_source_policy_cas(
    policy: Mapping[str, Any],
    *,
    expected_prior_digest: str | None,
    policy_path: Path = DEFAULT_POLICY_PATH,
) -> str:
    """Atomically replace Runtime policy only when its current digest matches."""

    _require_root()
    target = Path(policy_path)
    if not target.is_absolute() or ".." in target.parts:
        raise WorkloadPackageRuntimeError("Package Runtime policy path is unsafe")
    normalized = _runtime_policy(policy)
    if expected_prior_digest is not None:
        _require_digest(expected_prior_digest, "expected policy digest")
    content = _canonical_json(normalized)
    if len(content) > MAX_FILE_BYTES:
        raise WorkloadPackageRuntimeError("Package Runtime source policy is too large")
    owner_id = _effective_uid()
    group_id = _runtime_group_id()
    _validate_policy_parent(target.parent, owner_id)
    lock_path = target.parent / f".{target.name}.lock"
    lock_descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        lock_info = os.fstat(lock_descriptor)
        if (
            not stat.S_ISREG(lock_info.st_mode)
            or lock_info.st_uid != owner_id
            or stat.S_IMODE(lock_info.st_mode) != 0o600
            or lock_info.st_nlink != 1
        ):
            raise WorkloadPackageRuntimeError("Package Runtime policy CAS lock is unsafe")
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        old_bytes = _read_policy_bytes_optional(target, group_id=group_id, owner_id=owner_id)
        old_digest = (
            "sha256:" + hashlib.sha256(old_bytes).hexdigest() if old_bytes is not None else None
        )
        if old_digest != expected_prior_digest:
            raise WorkloadPackageRuntimeError("Package Runtime policy CAS expected digest is stale")
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary_path = Path(temporary_name)
        replaced = False
        try:
            os.fchmod(descriptor, 0o440)
            os.fchown(descriptor, owner_id, group_id)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # Recheck under the lock immediately before the atomic replacement.
            latest = _read_policy_bytes_optional(target, group_id=group_id, owner_id=owner_id)
            latest_digest = (
                "sha256:" + hashlib.sha256(latest).hexdigest() if latest is not None else None
            )
            if latest_digest != expected_prior_digest:
                raise WorkloadPackageRuntimeError(
                    "Package Runtime policy CAS lost a concurrent update"
                )
            os.replace(temporary_path, target)
            replaced = True
            directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            if replaced:
                try:
                    _restore_policy_bytes(target, old_bytes, owner_id=owner_id, group_id=group_id)
                except Exception as restore_error:
                    raise WorkloadPackageRuntimeError(
                        "Package Runtime source policy update failed and rollback could not be verified"
                    ) from restore_error
            raise
        try:
            readback = _read_policy_bytes_optional(target, group_id=group_id, owner_id=owner_id)
        except Exception:
            _restore_policy_bytes(target, old_bytes, owner_id=owner_id, group_id=group_id)
            raise
        if readback != content:
            # Restore the prior bytes when readback fails; the caller keeps its hold active.
            _restore_policy_bytes(target, old_bytes, owner_id=owner_id, group_id=group_id)
            raise WorkloadPackageRuntimeError("Package Runtime source policy readback failed")
        return "sha256:" + hashlib.sha256(readback).hexdigest()
    except OSError as error:
        raise WorkloadPackageRuntimeError("Package Runtime policy CAS failed") from error
    finally:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)


def _validate_policy_parent(path: Path, owner_id: int) -> None:
    """Require a root-controlled, non-writable policy directory."""

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except OSError as error:
            raise WorkloadPackageRuntimeError(
                "Package Runtime policy directory is unavailable"
            ) from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WorkloadPackageRuntimeError("Package Runtime policy directory is unsafe")
        if info.st_uid != owner_id or stat.S_IMODE(info.st_mode) & 0o022:
            raise WorkloadPackageRuntimeError(
                "Package Runtime policy directory is not root-controlled"
            )


def _read_policy_bytes_optional(path: Path, *, group_id: int, owner_id: int) -> bytes | None:
    """Read current policy bytes safely, or return None only when absent."""

    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                path.is_symlink()
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != owner_id
                or opened.st_gid != group_id
                or stat.S_IMODE(opened.st_mode) != 0o440
                or opened.st_nlink != 1
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_size > MAX_FILE_BYTES
            ):
                raise WorkloadPackageRuntimeError("Package Runtime policy file metadata is unsafe")
            raw = stream.read(MAX_FILE_BYTES + 1)
    except OSError as error:
        raise WorkloadPackageRuntimeError("Package Runtime policy file is unavailable") from error
    if len(raw) > MAX_FILE_BYTES:
        raise WorkloadPackageRuntimeError("Package Runtime policy file is too large")
    return raw


def _restore_policy_bytes(path: Path, old: bytes | None, *, owner_id: int, group_id: int) -> None:
    """Restore the prior atomic policy value after a failed readback check."""

    if old is None:
        path.unlink(missing_ok=True)
    else:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.restore.", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o440)
            os.fchown(descriptor, owner_id, group_id)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(old)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _file_digest(path: Path) -> str:
    """Hash one already-protected file for the parent transaction receipt."""

    _value, raw = _read_owned_json(path)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


_SDK_DRIVER_PROGRAM = r"""import json, os, socket, sys
from cyrene_runtime_maintenance import PackageRuntimeClient, PackageRuntimeError

channel_fd = int(sys.argv[1])
token_fd = int(sys.argv[2])
request = json.loads(sys.argv[3])
channel = socket.socket(fileno=channel_fd)

def receive_ack():
    line = bytearray()
    while len(line) <= 65536:
        block = channel.recv(1)
        if not block:
            raise RuntimeError("journal callback channel closed")
        if block == b"\n":
            break
        line.extend(block)
    if len(line) > 65536 or json.loads(line).get("ok") is not True:
        raise RuntimeError("parent journal callback rejected")

def send(value):
    encoded = json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(encoded) > 65536:
        raise RuntimeError("SDK callback data exceeds bound")
    channel.sendall(encoded)
    receive_ack()

def scope_value(scope):
    return {
        "binding_id": scope.binding_id,
        "package_id": scope.package_id,
        "installation_id": scope.installation_id,
        "operation": scope.operation,
    }

def receipt_value(receipt):
    return {
        "request_id": receipt.request_id,
        "source_id": receipt.source_id,
        "protocol_version": receipt.protocol_version,
        "catalog_generation": receipt.catalog_generation,
        "scope": scope_value(receipt.scope),
        "operation_token": receipt.operation_token,
        "gate_generation": receipt.gate_generation,
        "already_in_flight": receipt.already_in_flight,
        "already_completed": receipt.already_completed,
    }

def intent(request_id, scope):
    send({"kind": "callback", "payload": {"event": "intent", "requestId": request_id, "scope": scope_value(scope)}})

def outcome(status, receipt):
    send({"kind": "callback", "payload": {"event": "outcome", "status": dict(status) if status is not None else None, "receipt": receipt_value(receipt)}})

def reconcile(status, installation, receipt):
    send({"kind": "callback", "payload": {"event": "reconcile", "status": dict(status) if status is not None else None, "installation": dict(installation), "receipt": receipt_value(receipt)}})

def summary(status, reconciled=False):
    value = {
        "bindingId": status.get("binding_id"),
        "installationId": status.get("installation_id"),
        "generation": status.get("generation"),
        "state": status.get("state"),
        "failureCode": status.get("failure_code"),
        "reconciled": reconciled,
    }
    send({"kind": "summary", "value": value})

try:
    if (
        os.getuid() != request["source_uid"]
        or os.geteuid() != request["source_uid"]
        or os.getgid() != request["source_gid"]
        or os.getegid() != request["source_gid"]
        or os.getgroups() != []
    ):
        raise RuntimeError("source process credentials differ")
    send({
        "kind": "identity",
        "uid": os.getuid(), "euid": os.geteuid(),
        "gid": os.getgid(), "egid": os.getegid(), "groups": os.getgroups(),
    })
    os.lseek(token_fd, 0, os.SEEK_SET)
    token_path = "/proc/self/fd/" + str(token_fd)
    client = PackageRuntimeClient.from_source_secret(
        request["source_id"], token_path,
        socket_path=request["socket_path"],
        catalog_generation=request["catalog_generation"],
        maintenance_socket_path=request["maintenance_socket_path"],
    )
    op = request["operation"]
    binding_id = request["binding_id"]
    package_id = request["package_id"]
    installation_ids = request["installation_ids"]
    if op == "get_installation":
        try:
            value = client.get_installation(installation_ids[0])
        except PackageRuntimeError as error:
            if error.code == "INSTALLATION_NOT_FOUND":
                value = None
            else:
                raise
        send({"kind": "read_result", "value": value})
    elif op == "runtime_status":
        try:
            value = client.runtime_status(binding_id)
        except PackageRuntimeError as error:
            if error.code == "BINDING_NOT_FOUND":
                value = None
            else:
                raise
        send({"kind": "read_result", "value": value})
    else:
        installation_id = installation_ids[0]
        try:
            if op == "activate":
                value = client.activate_and_persist(
                    binding_id, installation_id, package_id=package_id,
                    persist_intent=intent, persist=outcome, request_id=request["request_id"],
                )
            elif op == "recover_binding":
                value = client.recover_and_persist(
                    binding_id, package_id=package_id, installation_id=installation_id,
                    persist_intent=intent, persist=outcome, request_id=request["request_id"],
                )
            elif op == "deactivate":
                value = client.deactivate_and_persist(
                    binding_id, package_id=package_id, installation_id=installation_id,
                    persist_intent=intent, persist=outcome, request_id=request["request_id"],
                )
            else:
                raise ValueError("unsupported SDK operation")
            summary(value)
        except PackageRuntimeError as error:
            receipt = error.pending_binding_operation
            if receipt is None:
                raise
            recovered = client.reconcile_binding_operation_and_persist(receipt, reconcile=reconcile)
            summary(recovered.runtime_status or {"binding_id": binding_id, "installation_id": installation_id, "state": "STOPPED"}, True)
    print(json.dumps({"ok": True, "kind": "complete", "value": {"requestId": request["request_id"]}}, separators=(",", ":")))
except Exception as error:
    code = getattr(error, "code", "PACKAGE_RUNTIME_OPERATION_FAILED")
    if not isinstance(code, str) or not code.isascii() or not code.replace("_", "").isalnum():
        code = "PACKAGE_RUNTIME_OPERATION_FAILED"
    print(json.dumps({"ok": False, "kind": "error", "value": {"code": code}}, separators=(",", ":")))
    raise SystemExit(1)
"""


__all__ = [
    "WorkloadPackageRuntimeError",
    "WorkloadSourceUpdate",
    "apply_workload_source_update",
    "build_workload_source_update",
    "candidate_from_workload_rows",
    "install_workload_package",
    "read_runtime_source_policy_generic",
    "read_workload_package_inventory",
    "reconcile_workload_source_update",
    "run_package_binding_operation",
    "uninstall_workload_package",
    "write_runtime_source_policy_cas",
]
