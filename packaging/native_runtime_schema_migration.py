"""Strict helpers for the signed native runtime schema-adoption transaction.

The updater derives legacy state shape from installed bytes and binds migration
proofs to its own confirmed CORE_RUNTIME transaction. The broker remains the
authority for complete event replay and state replacement.
模块只派生旧状态布局并绑定内部迁移证明；完整重放与原子迁移仍由受信 Broker 执行。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import time
from pathlib import Path
from typing import Any

SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SCHEMA1_PROFILES = frozenset(
    {
        "released-v1-no-binding-admissions",
        "experimental-v1-binding-admissions",
    }
)
NATIVE_GROUP_ID = "package-runtime-native-v1"
NATIVE_GROUP_MEMBERS = {
    "cyrene-runtime-maintenance": "cyrene.runtime-maintenance.broker.v1",
    "cyrene-kernel": "cyrene.runtime-maintenance.state.v2",
    "cy-package-runtime": "cy-package-runtime.control.v1",
}
_RELEVANT_UNIT_PREFIXES = ("cyrene-", "cy-package-", "cy-workspace-", "cy-")
_STATE_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "journal_sequence",
        "gate_generation",
        "install_catalog_generation",
        "maintenance",
        "completed_maintenances",
        "tasks",
        "runtime_admissions",
        "sources",
    }
)
_STATE_OPTIONAL_KEYS = frozenset({"core_bootstrap_eligible"})
_BINDING_MAP_KEYS = frozenset({"binding_operations", "completed_binding_operations"})
_JOURNAL_EVENT_FIELDS = {
    "catalog_configured": {"event", "generation"},
    "source_heartbeat": {"event", "source_id", "at_unix_ms"},
    "task_admitted": {"event", "admission"},
    "task_updated": {"event", "admission"},
    "task_completed": {"event", "source_id", "task_id"},
    "tasks_reconciled": {"event", "source_id", "active_tasks", "at_unix_ms"},
    "state_checkpoint": {"event", "state"},
    "runtime_admission_started": {"event", "token", "action"},
    "runtime_admission_ended": {"event", "token"},
    "runtime_admissions_recovered": {"event"},
    "binding_operation_admitted": {"event", "record"},
    "binding_operation_completed": {"event", "record"},
    "maintenance_began": {"event", "record"},
    "maintenance_ended": {
        "event",
        "request_id",
        "token",
        "outcome",
        "healthy",
        "unlocked",
        "ended_at_unix_ms",
    },
}


class MigrationError(ValueError):
    """A local schema-adoption input is incomplete or inconsistent."""


def _strict_json(data: bytes, *, description: str) -> Any:
    """Decode UTF-8 JSON without accepting duplicate object keys or non-finite numbers."""

    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise MigrationError(f"{description} contains a duplicate JSON key")
            value[key] = item
        return value

    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=object_from_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                MigrationError(f"{description} contains invalid number {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MigrationError(f"{description} is not valid UTF-8 JSON") from error


def _profile_for_state(state: Any, *, description: str) -> str:
    if not isinstance(state, dict):
        raise MigrationError(f"{description} must be an object")
    keys = set(state)
    if not _STATE_REQUIRED_KEYS.issubset(keys) or keys - (
        _STATE_REQUIRED_KEYS | _STATE_OPTIONAL_KEYS | _BINDING_MAP_KEYS
    ):
        raise MigrationError(f"{description} has missing or unknown fields")
    if type(state.get("schema_version")) is not int or state["schema_version"] != 1:
        raise MigrationError(f"{description} is not schema version 1")
    for field in ("journal_sequence", "gate_generation", "install_catalog_generation"):
        value = state.get(field)
        if type(value) is not int or value < 0:
            raise MigrationError(f"{description} has an invalid {field}")

    binding_maps_present = _BINDING_MAP_KEYS & keys
    if binding_maps_present not in (frozenset(), _BINDING_MAP_KEYS):
        raise MigrationError(f"{description} contains only one binding admission map")
    if binding_maps_present:
        if any(not isinstance(state.get(field), dict) for field in _BINDING_MAP_KEYS):
            raise MigrationError(f"{description} binding admission maps are malformed")
        return "experimental-v1-binding-admissions"
    return "released-v1-no-binding-admissions"


def derive_schema1_profile(state_bytes: bytes, journal_bytes: bytes) -> str:
    """Derive the legacy profile from a snapshot and every persisted checkpoint.

    This is a narrow identity check, not a replay implementation: the subsequent signed
    ``migrate-state`` command validates every event and performs the actual migration.
    Workspace只确定所有快照/检查点的profile一致，不代替Broker重放journal。
    """
    state = _strict_json(state_bytes, description="Runtime state snapshot")
    snapshot_profile = _profile_for_state(state, description="Runtime state snapshot")
    previous_sequence = -1
    profiles = [snapshot_profile]
    try:
        journal_text = journal_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MigrationError("Runtime journal is not valid UTF-8") from error
    if not journal_text:
        if state["journal_sequence"] != 0:
            raise MigrationError("Runtime journal is empty for a nonempty snapshot")
        return snapshot_profile
    if not journal_text.endswith("\n"):
        raise MigrationError("Runtime journal is missing its terminal newline")
    for line_number, line in enumerate(journal_text.splitlines(), start=1):
        if not line.strip():
            raise MigrationError(f"Runtime journal line {line_number} is empty")
        entry = _strict_json(
            line.encode("utf-8"), description=f"Runtime journal line {line_number}"
        )
        if not isinstance(entry, dict) or set(entry) != {"sequence", "event"}:
            raise MigrationError(f"Runtime journal line {line_number} has an invalid envelope")
        sequence = entry.get("sequence")
        event = entry.get("event")
        if type(sequence) is not int or sequence < 1 or not isinstance(event, dict):
            raise MigrationError(
                f"Runtime journal line {line_number} has invalid ordering or event data"
            )
        if previous_sequence >= 0 and sequence != previous_sequence + 1:
            raise MigrationError(f"Runtime journal line {line_number} has discontinuous sequence")
        kind = event.get("event")
        if not isinstance(kind, str) or kind not in _JOURNAL_EVENT_FIELDS:
            raise MigrationError(f"Runtime journal line {line_number} has an unknown event")
        if set(event) != _JOURNAL_EVENT_FIELDS[kind]:
            raise MigrationError(f"Runtime journal line {line_number} has an invalid event shape")
        previous_sequence = sequence
        if kind in {"binding_operation_admitted", "binding_operation_completed"} and (
            snapshot_profile == "released-v1-no-binding-admissions"
        ):
            raise MigrationError("Released schema-1 profile contains binding-operation events")
        if kind == "state_checkpoint":
            checkpoint_state = event.get("state")
            profile = _profile_for_state(
                checkpoint_state, description=f"Runtime journal checkpoint {line_number}"
            )
            if checkpoint_state.get("journal_sequence") != sequence:
                raise MigrationError(
                    f"Runtime journal checkpoint {line_number} sequence does not match its state"
                )
            profiles.append(profile)
    if len(set(profiles)) != 1:
        raise MigrationError("Runtime snapshot and journal checkpoints disagree on schema profile")
    if state["journal_sequence"] > previous_sequence:
        raise MigrationError("Runtime journal is truncated before the durable snapshot")
    return snapshot_profile


def read_schema1_profile(runtime_root: Path) -> str:
    """Read the fixed Broker snapshot and journal only from their shared-state inode."""
    runtime_root = Path(runtime_root)
    try:
        directory_info = runtime_root.lstat()
    except OSError as error:
        raise MigrationError(f"Runtime state directory is unavailable: {error}") from error
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or directory_info.st_uid != 0
        or stat.S_IMODE(directory_info.st_mode) != 0o2770
    ):
        raise MigrationError("Runtime state directory ownership or mode is unsafe")
    snapshot = _read_runtime_state_file(
        runtime_root / "maintenance-state.json",
        expected_gid=directory_info.st_gid,
        limit=16_000_000,
    )
    journal = _read_runtime_state_file(
        runtime_root / "maintenance-journal.jsonl",
        expected_gid=directory_info.st_gid,
        limit=256_000_000,
    )
    return derive_schema1_profile(snapshot, journal)


def _read_runtime_state_file(path: Path, *, expected_gid: int, limit: int) -> bytes:
    descriptor: int | None = None
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_gid != expected_gid
            or stat.S_IMODE(before.st_mode) != 0o660
            or before.st_nlink != 1
            or before.st_size > limit
        ):
            raise MigrationError(f"Runtime state file metadata is unsafe: {path.name}")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_uid != 0
            or opened.st_gid != expected_gid
            or stat.S_IMODE(opened.st_mode) != 0o660
            or opened.st_nlink != 1
        ):
            raise MigrationError(f"Runtime state file changed while opening: {path.name}")
        chunks = bytearray()
        while len(chunks) <= limit:
            block = os.read(descriptor, min(1024 * 1024, limit + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        if len(chunks) > limit:
            raise MigrationError(f"Runtime state file exceeds the size limit: {path.name}")
        return bytes(chunks)
    except MigrationError:
        raise
    except OSError as error:
        raise MigrationError(
            f"Cannot securely read runtime state file {path.name}: {error}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def create_migration_proof(transaction: dict[str, Any], profile: str) -> dict[str, Any]:
    """Create the exact broker proof from a confirmed updater transaction.

    ``profile`` must come from :func:`derive_schema1_profile`; callers cannot supply it in
    the public update request. The Broker still checks the proof against the active hold.
    """
    if profile not in SCHEMA1_PROFILES:
        raise MigrationError("Unsupported schema-1 migration profile")
    component_digests = transaction.get("componentArtifactDigests")
    if (
        transaction.get("targetKind") != "CORE_RUNTIME"
        or transaction.get("requiresRestart") is not True
        or transaction.get("userConfirmedRestart") is not True
        or not isinstance(transaction.get("maintenanceToken"), str)
        or not transaction["maintenanceToken"]
        or not isinstance(transaction.get("requestId"), str)
        or not isinstance(transaction.get("planId"), str)
        or not isinstance(transaction.get("planDigest"), str)
        or SHA256_PATTERN.fullmatch(transaction["planDigest"]) is None
        or not isinstance(component_digests, dict)
        or set(component_digests)
        != {"cyrene-kernel", "cyrene-runtime-maintenance", "cy-package-runtime"}
        or any(
            not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None
            for digest in component_digests.values()
        )
    ):
        raise MigrationError("Migration proof is not bound to a complete confirmed Core plan")
    for field in ("maintenanceGateGeneration", "expectedCatalogGeneration"):
        value = transaction.get(field)
        if type(value) is not int or value < 0:
            raise MigrationError(f"Migration transaction has an invalid {field}")
    return {
        "schema1_profile": profile,
        "request_id": transaction["requestId"],
        "maintenance_token": transaction["maintenanceToken"],
        "target_kind": "CORE_RUNTIME",
        "plan_id": transaction["planId"],
        "plan_digest": transaction["planDigest"],
        "component_artifact_digests": dict(component_digests),
        "expected_gate_generation": transaction["maintenanceGateGeneration"],
        "expected_catalog_generation": transaction["expectedCatalogGeneration"],
    }


def verify_active_native_payload(updater: Any, component_id: str) -> dict[str, Any]:
    """Recheck a receipt-bound active release and every file in its signed file map.

    Historical channel-index and SLSA verification is performed by the caller before
    this helper is used for migration authorization. This function protects the local
    active pointer, manifest receipt, directories, and payload bytes from drift.
    """
    component = updater.components.get(component_id)
    if not isinstance(component, dict):
        raise MigrationError(f"{component_id} is absent from the trusted component catalog")
    installed = updater._installed(component)
    manifest = installed.get("manifest")
    if (
        installed.get("active") is not True
        or installed.get("identityAttested") is not True
        or not isinstance(manifest, dict)
        or manifest.get("componentId") != component_id
        or manifest.get("manifestDigest") != installed.get("manifestDigest")
        or manifest.get("version") != installed.get("activeVersion")
        or not isinstance(manifest.get("artifact"), dict)
        or manifest["artifact"].get("kind") != "native-binary"
    ):
        raise MigrationError(f"{component_id} has no receipt-bound active native release")

    root = Path(updater.install_root) / "components" / component_id
    pointer = installed.get("pointerIdentity")
    if not isinstance(pointer, str):
        raise MigrationError(f"{component_id} active pointer identity is missing")
    release = root / "releases" / pointer
    for directory in (
        Path(updater.install_root),
        Path(updater.install_root) / "components",
        root,
        root / "releases",
        release,
    ):
        info = _lstat(directory, f"{component_id} release directory")
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise MigrationError(
                f"{component_id} release directory is not immutable root-owned data"
            )
    active_pointer = root / "active"
    info = _lstat(active_pointer, f"{component_id} active pointer")
    try:
        target = active_pointer.readlink().as_posix()
    except OSError as error:
        raise MigrationError(f"{component_id} active pointer cannot be read") from error
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or target != f"releases/{pointer}":
        raise MigrationError(f"{component_id} active pointer differs from its verified receipt")

    artifact = manifest["artifact"]
    files = artifact.get("files")
    entrypoint = artifact.get("entrypoint")
    if not isinstance(files, dict) or not files or not isinstance(entrypoint, str):
        raise MigrationError(f"{component_id} signed native file map is malformed")
    executable_files = artifact.get("executableFiles", [entrypoint])
    if (
        not isinstance(executable_files, list)
        or any(not isinstance(path, str) for path in executable_files)
        or len(set(executable_files)) != len(executable_files)
        or entrypoint not in executable_files
    ):
        raise MigrationError(f"{component_id} signed executable file list is malformed")

    for name, expected_digest in files.items():
        parts = name.split("/") if isinstance(name, str) else []
        if (
            not parts
            or any(part in {"", ".", ".."} for part in parts)
            or "\\" in name
            or not isinstance(expected_digest, str)
            or SHA256_PATTERN.fullmatch(expected_digest) is None
        ):
            raise MigrationError(f"{component_id} signed file map has an unsafe entry")
        parent = release
        for part in parts[:-1]:
            parent = parent / part
            info = _lstat(parent, f"{component_id} payload directory")
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise MigrationError(f"{component_id} payload directory is unsafe")
        path = release.joinpath(*parts)
        info = _lstat(path, f"{component_id} payload file")
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
            or info.st_nlink != 1
        ):
            raise MigrationError(f"{component_id} payload file is not immutable root-owned content")
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise MigrationError(f"{component_id} payload file cannot be read") from error
        if digest != expected_digest.removeprefix("sha256:"):
            raise MigrationError(f"{component_id} payload file differs from its signed digest")
        if name == entrypoint and not stat.S_IMODE(info.st_mode) & 0o111:
            raise MigrationError(f"{component_id} signed entrypoint is not executable")
    return installed


def verify_active_release_attested(updater: Any, component_id: str) -> dict[str, Any]:
    """Bind an installed native receipt to its immutable, officially attested index.

    The current catalog may describe a newer compatibility group than the active C9
    release. This checks the historical index and manifest bytes against the pinned
    publisher/workflow without interpreting the old group as a current update contract.
    """
    installed = verify_active_native_payload(updater, component_id)
    component = updater.components[component_id]
    manifest = installed["manifest"]
    channel = manifest.get("channel")
    if channel not in {"stable", "preview"}:
        raise MigrationError(f"{component_id} active channel is not supported")
    repository = component.get("publisher")
    publisher = updater.publishers.get(repository)
    if not isinstance(publisher, dict) or publisher.get("repository") != repository:
        raise MigrationError(f"{component_id} publisher is not pinned")
    source = manifest.get("source")
    source_commit = source.get("commit") if isinstance(source, dict) else None
    source_ref = source.get("ref") if isinstance(source, dict) else None
    if (
        not isinstance(source, dict)
        or source.get("repository") != f"https://github.com/{repository}"
        or not isinstance(source_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
        or source_ref not in updater.catalog["channels"][channel]["sourceRefs"]
    ):
        raise MigrationError(f"{component_id} active source is not pinned")

    prefix = updater._component_release_tag_prefix(component, channel) or (
        "preview-" if channel == "preview" else "stable-"
    )
    tag = prefix + source_commit
    release_uri = f"https://api.github.com/repos/{repository}/releases/tags/{tag}"
    release = updater._get_json(release_uri)
    if (
        not isinstance(release, dict)
        or release.get("tag_name") != tag
        or release.get("draft") is not False
        or release.get("prerelease") is not (channel == "preview")
        or release.get("immutable") is not True
    ):
        raise MigrationError(f"{component_id} active source release is not immutable")
    discovery = publisher.get("releaseDiscovery")
    index_asset_name = discovery.get("indexAssetName") if isinstance(discovery, dict) else None
    assets = release.get("assets")
    index_assets = (
        [
            asset
            for asset in assets
            if isinstance(asset, dict) and asset.get("name") == index_asset_name
        ]
        if isinstance(assets, list)
        else []
    )
    if len(index_assets) != 1:
        raise MigrationError(f"{component_id} source release has no unique signed index")
    index_uri = index_assets[0].get("browser_download_url")
    updater._require_github_asset_uri(index_uri, repository)
    index_bytes = updater._get_bytes(index_uri)
    index = _strict_json(index_bytes, description=f"{component_id} historical release index")
    updater._validate_index(index, publisher, channel, release, component)
    index_attestation = index.get("provenance", {}).get("attestation", {})
    updater._verify_attestation(
        index_bytes,
        subject_name=index_attestation.get("subjectName"),
        digest="sha256:" + hashlib.sha256(index_bytes).hexdigest(),
        repository=repository,
        workflow=publisher["workflow"],
        source_ref=source_ref,
        source_commit=source_commit,
    )

    target = updater._target_for(component)
    if not isinstance(target, dict) or manifest.get("target") != target.get("target"):
        raise MigrationError(f"{component_id} active target differs from this host")
    entries = [
        entry
        for entry in index.get("releases", [])
        if isinstance(entry, dict)
        and entry.get("componentId") == component_id
        and entry.get("target") == manifest.get("target")
    ]
    if (
        len(entries) != 1
        or entries[0].get("version") != manifest.get("version")
        or entries[0].get("manifestDigest") != installed.get("manifestDigest")
    ):
        raise MigrationError(f"{component_id} active receipt is absent from its signed index")
    entry = entries[0]
    updater._require_github_asset_uri(entry.get("manifestUri"), repository)
    manifest_bytes = updater._get_bytes(entry["manifestUri"])
    published_manifest = _strict_json(
        manifest_bytes, description=f"{component_id} historical manifest"
    )
    updater._validate_manifest_digest(published_manifest, installed["manifestDigest"])
    if published_manifest != manifest:
        raise MigrationError(f"{component_id} active manifest differs from its signed asset")
    manifest_source = manifest.get("source")
    provenance = manifest.get("provenance")
    attestation = provenance.get("attestation") if isinstance(provenance, dict) else None
    if (
        not isinstance(manifest_source, dict)
        or manifest_source.get("ref") != index.get("source", {}).get("ref")
        or manifest_source.get("commit") != index.get("source", {}).get("commit")
        or not isinstance(attestation, dict)
        or attestation.get("kind") != "github-artifact-attestation"
        or attestation.get("repository") != repository
        or attestation.get("workflow") != publisher.get("workflow")
        or attestation.get("predicateType") != "https://slsa.dev/provenance/v1"
        or attestation.get("run") != index_attestation.get("run")
    ):
        raise MigrationError(f"{component_id} manifest provenance differs from its signed index")
    return installed


def is_schema1_group_adoption(updater: Any, staged_record: dict[str, Any]) -> bool:
    """Recognize only the complete signed C10 group replacing the released C9 layout."""
    group = next(
        (
            item
            for item in updater.catalog.get("compatibilityGroups", [])
            if isinstance(item, dict) and item.get("groupId") == NATIVE_GROUP_ID
        ),
        None,
    )
    if group is None:
        return False
    expected_members = [
        {"componentId": component_id, "requiredForAdoption": True, "protocolVersion": protocol}
        for component_id, protocol in NATIVE_GROUP_MEMBERS.items()
    ]
    lock = group.get("contractLock")
    if (
        group.get("groupVersion") != "2"
        or group.get("contractApiVersion") != "0.1.0"
        or group.get("wireApiVersion") != "cyrene.runtime-maintenance.binding-operations.v1"
        or group.get("members") != expected_members
        or not isinstance(lock, dict)
        or lock.get("repository") != "DoHorizon-AI/Cyrene-Workspace"
        or lock.get("path") != "governance/package-runtime-protocols-v1.lock.json"
        or not isinstance(lock.get("commit"), str)
        or re.fullmatch(r"[0-9a-f]{40}", lock["commit"]) is None
        or not isinstance(lock.get("sha256"), str)
        or SHA256_PATTERN.fullmatch(lock["sha256"]) is None
    ):
        raise MigrationError("Trusted C10 compatibility group is malformed")

    staged_items = staged_record.get("components")
    if not isinstance(staged_items, list):
        raise MigrationError("Staged C10 Core adoption component list is malformed")
    staged = {item.get("componentId"): item for item in staged_items if isinstance(item, dict)}
    if set(staged) != set(NATIVE_GROUP_MEMBERS) or len(staged) != len(staged_items):
        return False
    for component_id, protocol in NATIVE_GROUP_MEMBERS.items():
        item = staged[component_id]
        manifest = item.get("manifest")
        compatibility = manifest.get("compatibility") if isinstance(manifest, dict) else None
        component = updater.components.get(component_id)
        member = next(entry for entry in expected_members if entry["componentId"] == component_id)
        if (
            not isinstance(component, dict)
            or component.get("compatibilityGroup") != NATIVE_GROUP_ID
            or component.get("protocolVersion") != protocol
            or not isinstance(manifest, dict)
            or type(manifest.get("schemaVersion")) is not int
            or manifest["schemaVersion"] != 2
            or manifest.get("componentId") != component_id
            or manifest.get("protocolVersion") != member["protocolVersion"]
            or not isinstance(compatibility, dict)
            or compatibility.get("groupId") != NATIVE_GROUP_ID
            or compatibility.get("groupVersion") != "2"
            or compatibility.get("contractApiVersion") != group["contractApiVersion"]
            or compatibility.get("wireApiVersion") != group["wireApiVersion"]
            or compatibility.get("contractLock") != group.get("contractLock")
        ):
            raise MigrationError(f"Staged C10 {component_id} does not match the trusted group")

    broker = updater._installed(updater.components["cyrene-runtime-maintenance"])
    kernel = updater._installed(updater.components["cyrene-kernel"])
    if not broker.get("active") and not kernel.get("active"):
        return False
    if not broker.get("active") or not kernel.get("active"):
        raise MigrationError("Installed C9 Core cohort is incomplete")
    legacy = []
    installed_versions = []
    for component_id, installed in (
        ("cyrene-runtime-maintenance", broker),
        ("cyrene-kernel", kernel),
    ):
        manifest = installed.get("manifest")
        if (
            installed.get("active") is not True
            or installed.get("identityAttested") is not True
            or not isinstance(manifest, dict)
        ):
            raise MigrationError(f"Installed {component_id} identity is unknown")
        if type(manifest.get("schemaVersion")) is not int or manifest.get("schemaVersion") not in {
            1,
            2,
        }:
            raise MigrationError(f"Installed {component_id} is not the released C9 layout")
        installed_versions.append(manifest["schemaVersion"])
        if manifest["schemaVersion"] == 1:
            if "compatibility" in manifest:
                raise MigrationError(f"Installed {component_id} is not the released C9 layout")
            legacy.append(component_id)
    if len(set(installed_versions)) != 1:
        raise MigrationError("Installed Kernel and broker use mixed release generations")
    if installed_versions == [2, 2]:
        return False
    for component_id in legacy:
        verify_active_release_attested(updater, component_id)

    package_runtime = updater._installed(updater.components["cy-package-runtime"])
    if package_runtime.get("active"):
        manifest = package_runtime.get("manifest")
        if (
            not isinstance(manifest, dict)
            or type(manifest.get("schemaVersion")) is not int
            or manifest["schemaVersion"] != 1
            or "compatibility" in manifest
        ):
            raise MigrationError("Installed Package Runtime is incompatible with schema adoption")
        verify_active_release_attested(updater, "cy-package-runtime")
    return True


def capture_unit_inventory(updater: Any, staged_component_ids: set[str]) -> list[dict[str, Any]]:
    """Capture only catalog-owned Cyrene services and reject unclassified units."""
    known: dict[str, dict[str, Any]] = {}
    for component in updater.components.values():
        unit = updater._catalog_matched_unit(component)
        if unit is None:
            continue
        if unit in known:
            raise MigrationError(f"Trusted catalog has duplicate unit owner for {unit}")
        known[unit] = component
    try:
        listed = updater.runner(
            [
                "systemctl",
                "list-unit-files",
                "--type=service",
                "--type=timer",
                "--no-legend",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, TimeoutError) as error:
        raise MigrationError(f"Cannot inventory installed Cyrene units: {error}") from error
    if listed.returncode != 0:
        raise MigrationError("Installed systemd unit inventory is unavailable")
    unit_states: dict[str, str] = {}
    for line in listed.stdout.splitlines():
        columns = line.split()
        if not columns:
            continue
        name = columns[0]
        if not name.startswith(_RELEVANT_UNIT_PREFIXES):
            continue
        unit_file_state = columns[1] if len(columns) > 1 else ""
        component = known.get(name)
        if component is None or not name.endswith(".service"):
            raise MigrationError(f"Unclassified Cyrene service or timer blocks migration: {name}")
        if unit_file_state not in {
            "enabled",
            "enabled-runtime",
            "disabled",
            "static",
            "indirect",
            "generated",
            "transient",
            "masked",
            "masked-runtime",
            "linked",
            "linked-runtime",
            "alias",
        }:
            raise MigrationError(f"Cyrene unit file state is unknown for {name}")
        component_id = component["componentId"]
        if component_id not in staged_component_ids:
            installed = updater._installed(component)
            if installed.get("active") is not True or installed.get("identityAttested") is not True:
                raise MigrationError(f"Cyrene unit has no verified installed receipt: {name}")
        if not updater._unit_exists(component):
            raise MigrationError(f"Cyrene unit bytes are not catalog-matched: {name}")
        if name in unit_states:
            raise MigrationError(f"Systemd listed a duplicate Cyrene unit: {name}")
        unit_states[name] = unit_file_state

    inventory: list[dict[str, Any]] = []
    for unit in sorted(unit_states):
        try:
            state = updater.runner(
                [
                    "systemctl",
                    "show",
                    unit,
                    "--property=ActiveState",
                    "--property=MainPID",
                    "--value",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, TimeoutError) as error:
            raise MigrationError(f"Cannot inspect Cyrene unit state: {unit}") from error
        values = state.stdout.splitlines()
        if state.returncode != 0 or len(values) != 2:
            raise MigrationError(f"Cyrene unit state is unknown: {unit}")
        active_state, pid_text = values
        if active_state not in {"active", "inactive", "failed"} or not pid_text.isdecimal():
            raise MigrationError(f"Cyrene unit state is transitional or malformed: {unit}")
        main_pid = int(pid_text)
        if (active_state == "active") != (main_pid > 0):
            raise MigrationError(f"Cyrene unit process identity is inconsistent: {unit}")
        inventory.append(
            {
                "unit": unit,
                "componentId": known[unit]["componentId"],
                "unitFileState": unit_states[unit],
                "active": active_state == "active",
            }
        )
    return inventory


def stop_unit_order(updater: Any, inventory: list[dict[str, Any]]) -> list[str]:
    """Stop clients first, reverse core order, and the broker last."""

    def key(item: dict[str, Any]) -> tuple[int, int, str]:
        component_id = item["componentId"]
        component = updater.components[component_id]
        restart = component.get("restart", {})
        if component_id == "cy-package-runtime":
            return (0, 0, item["unit"])
        if component_id == "cyrene-runtime-maintenance":
            return (4, 0, item["unit"])
        if restart.get("group") == "core-runtime":
            return (2, -int(restart.get("order", 0)), item["unit"])
        return (1, 0, item["unit"])

    return [item["unit"] for item in sorted(inventory, key=key) if item["active"]]


def start_core_unit_order(updater: Any, inventory: list[dict[str, Any]]) -> list[str]:
    """Start platform dependencies, then Broker, Kernel, agents, and Package Runtime."""
    desired = {
        item["unit"]
        for item in inventory
        if item["active"]
        and updater.components[item["componentId"]].get("restart", {}).get("group")
        == "core-runtime"
    }
    desired.update(
        updater._catalog_matched_unit(updater.components[component_id])
        for component_id in ("cyrene-runtime-maintenance", "cyrene-kernel", "cy-package-runtime")
    )
    inventory_units = {item["unit"] for item in inventory}
    if None in desired or not desired.issubset(inventory_units):
        raise MigrationError("Required C10 core service unit is not catalog-pinned")

    def key(unit: str) -> tuple[int, int, str]:
        component = updater.components[
            next(item["componentId"] for item in inventory if item["unit"] == unit)
        ]
        component_id = component["componentId"]
        if component_id == "cyrene-runtime-maintenance":
            return (25, 0, unit)
        if component_id == "cyrene-kernel":
            return (30, 0, unit)
        if component_id == "cy-package-runtime":
            return (35, 0, unit)
        return (int(component.get("restart", {}).get("order", 50)), 0, unit)

    return sorted(desired, key=key)


def stop_captured_units(updater: Any, inventory: list[dict[str, Any]]) -> None:
    """Stop the captured services in dependency-safe order and verify quiescence."""
    for unit in stop_unit_order(updater, inventory):
        updater._run_systemctl("stop", unit)
        _wait_unit_stopped(updater, unit)
    _assert_no_direct_state_writers(Path(getattr(updater, "proc_root", "/proc")))
    for path in (
        Path("/run/cyrene/runtime-maintenance.sock"),
        Path("/run/cyrene-package-runtime/control.sock"),
    ):
        _assert_unix_socket_not_listening(path)


def _wait_unit_stopped(updater: Any, unit: str) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            result = updater.runner(
                [
                    "systemctl",
                    "show",
                    unit,
                    "--property=ActiveState",
                    "--property=MainPID",
                    "--value",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, TimeoutError):
            result = None
        values = result.stdout.splitlines() if result is not None else []
        if result is not None and result.returncode == 0 and len(values) == 2:
            state, pid_text = values
            if state in {"inactive", "failed"} and pid_text == "0":
                return
            if state not in {"active", "activating", "deactivating"} or not pid_text.isdecimal():
                raise MigrationError(f"Stopped unit state is unknown: {unit}")
        time.sleep(0.25)
    raise MigrationError(f"Cyrene unit did not stop cleanly: {unit}")


def _assert_no_direct_state_writers(proc_root: Path) -> None:
    """Reject surviving Broker/Kernel executables, including direct CLI writers."""
    if not proc_root.is_dir() or proc_root.is_symlink():
        raise MigrationError("Process inventory is unavailable after stopping Core services")
    try:
        processes = tuple(proc_root.iterdir())
    except OSError as error:
        raise MigrationError(
            "Process inventory is unreadable after stopping Core services"
        ) from error
    writer_names = {"cyrene-runtime-maintenance", "cyrene-kernel"}
    for process in processes:
        if not process.name.isdecimal():
            continue
        try:
            state = (process / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()[0]
            if state in {"Z", "X"}:
                continue
            command = (process / "cmdline").read_bytes()
            if not command:
                continue
            executable = os.readlink(process / "exe")
            executable = executable.removesuffix(" (deleted)")
            metadata = os.stat(process / "exe")
            if not stat.S_ISREG(metadata.st_mode):
                raise MigrationError("Process executable identity is not a regular file")
            if Path(executable).name in writer_names:
                raise MigrationError("A direct Broker or Kernel process remains after unit stop")
        except FileNotFoundError:
            if not process.exists():
                continue
            raise MigrationError("Process changed during Core writer inventory") from None
        except PermissionError as error:
            raise MigrationError(
                "Process inventory is unreadable; writer state is UNKNOWN"
            ) from error
        except (OSError, IndexError) as error:
            raise MigrationError(
                "Process inventory is incomplete; writer state is UNKNOWN"
            ) from error


def _assert_unix_socket_not_listening(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise MigrationError(f"Cannot inspect Core control socket {path}: {error}") from error
    if path.is_symlink() or not stat.S_ISSOCK(info.st_mode):
        raise MigrationError(f"Core control endpoint has unexpected filesystem type: {path}")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(1.0)
        connection.connect(str(path))
    except ConnectionRefusedError:
        return
    except FileNotFoundError:
        return
    except OSError as error:
        raise MigrationError(f"Core control socket status is UNKNOWN: {path}") from error
    else:
        raise MigrationError(f"A Core control socket is still accepting connections: {path}")
    finally:
        connection.close()


def _lstat(path: Path, description: str) -> Any:
    try:
        return path.lstat()
    except OSError as error:
        raise MigrationError(f"Cannot inspect {description}: {error}") from error
