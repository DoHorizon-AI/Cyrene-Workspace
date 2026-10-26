"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 stage_transition.py                                                   │
│  Module: tooling.deploy.teams_cloud.stage_transition                     │
│  Role: Plan and apply one durable Teams cloud runtime transition.         │
│                                                                          │
│  模块职责：计划并执行可恢复的 Teams 云运行时正式迁移事务。                │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import preflight
import runtime_update
from build_arm64 import BuildError

PLAN_SCHEMA = "cyrene-teams-stage-transition-plan-v1"
JOURNAL_SCHEMA = "cyrene-teams-stage-transition-journal-v1"
SELECTION_SCHEMA = "cyrene-teams-runtime-selection-v2"
DEFAULT_JOURNAL_DIR = Path("/var/lib/cyrene/stage-transition")
PHASES = {"provider-bootstrap", "issued-connector-activation"}
NEW_SERVICES = {"huggingface", "cloud-management-phase2"}
SERVICE_ORDER = runtime_update.STAGE_TRANSITION_TARGET_ORDER
SERVICE_SET = set(SERVICE_ORDER)
PROJECT_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}\Z")
FULL_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_ID_RE = runtime_update.IMAGE_ID_RE
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
SELECTION_PATH_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
DATABASE_PATH = "/var/lib/cyrene-navigator/persistence.db"
DATABASE_VOLUME_TARGET = "/var/lib/cyrene-navigator"
CODEBUDDY_HOME_TARGET = "/home/navigator/.codebuddy"
MEMORY_RESERVE_DEFAULT_MIB = 1024


class StageTransitionError(ValueError):
    """A staged runtime transition is malformed, unsafe, or not yet recoverable."""


def _utc_now() -> str:
    """Return the canonical UTC timestamp used in private transaction records."""

    return datetime.now(UTC).isoformat()


def _canonical_json(value: Any) -> bytes:
    """Serialize JSON deterministically for the immutable plan and selection digests."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _sha256_bytes(value: bytes) -> str:
    """Hash one byte string without returning its contents."""

    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    """Hash one regular non-symlink plan input without serializing its contents."""

    try:
        metadata = path.lstat()
    except OSError as error:
        raise StageTransitionError(f"stage input is unavailable: {path.name}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise StageTransitionError(f"stage input is not a regular file: {path.name}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise StageTransitionError(f"stage input could not be read: {path.name}") from error
    return digest.hexdigest()


def _file_record(path: Path, label: str) -> dict[str, str]:
    """Bind a single external input to its normalized path and SHA-256."""

    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise StageTransitionError(f"{label} is unavailable") from error
    return {"path": str(resolved), "sha256": _sha256_file(resolved)}


def _file_records(paths: list[Path], label: str) -> list[dict[str, str]]:
    """Bind an ordered set of Compose or environment inputs to their digests."""

    if not paths:
        raise StageTransitionError(f"at least one {label} is required")
    return [_file_record(path, label) for path in paths]


def _directory_manifest(path: Path, label: str) -> list[dict[str, Any]]:
    """Fingerprint every regular file in one private directory without storing contents."""

    resolved = _validate_private_directory(
        path,
        label,
        expected_uid=10001,
    )
    records: list[dict[str, Any]] = []
    for child in sorted(resolved.rglob("*")):
        try:
            metadata = child.lstat()
        except OSError as error:
            raise StageTransitionError(f"{label} directory changed while inventorying") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise StageTransitionError(f"{label} directory contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            if metadata.st_uid != 10001 or metadata.st_mode & 0o077:
                raise StageTransitionError(f"{label} subdirectory must be private UID 10001")
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 10001
            or metadata.st_mode & 0o077
        ):
            raise StageTransitionError(f"{label} contains a non-private regular file")
        records.append(
            {
                "relativePath": str(child.relative_to(resolved)),
                "sizeBytes": metadata.st_size,
                "mode": stat.S_IMODE(metadata.st_mode),
                "sha256": _sha256_file(child),
            }
        )
    return records


def _secret_manifest(compose: dict[str, Any], label: str) -> list[dict[str, Any]]:
    """Fingerprint only explicit Compose secret sources, never walk the whole secrets directory."""

    secrets = compose.get("secrets", {})
    if not isinstance(secrets, dict):
        raise StageTransitionError(f"{label} Compose secret inventory is malformed")
    records: list[dict[str, Any]] = []
    for name, row in sorted(secrets.items()):
        file_text = row.get("file") if isinstance(row, dict) else None
        if not isinstance(name, str) or not isinstance(file_text, str):
            raise StageTransitionError(f"{label} Compose secret source is malformed")
        path = Path(file_text)
        record = _file_record(path, f"{label} secret {name}")
        metadata = path.stat()
        records.append(
            {
                "name": name,
                **record,
                "sizeBytes": metadata.st_size,
                "mode": stat.S_IMODE(metadata.st_mode),
                "uid": metadata.st_uid,
            }
        )
    return records


def _verify_secret_manifest(records: Any, label: str) -> None:
    """Recheck exact Compose secret path, content digest, owner, mode, and size."""

    if not isinstance(records, list):
        raise StageTransitionError(f"{label} secret fingerprint is malformed")
    for row in records:
        if not isinstance(row, dict) or set(row) != {
            "name",
            "path",
            "sha256",
            "sizeBytes",
            "mode",
            "uid",
        }:
            raise StageTransitionError(f"{label} secret fingerprint is malformed")
        path = Path(row["path"])
        try:
            metadata = path.stat()
        except OSError as error:
            raise StageTransitionError(
                f"{label} secret {row.get('name')} disappeared after planning"
            ) from error
        if (
            str(path.resolve(strict=True)) != row["path"]
            or metadata.st_size != row["sizeBytes"]
            or stat.S_IMODE(metadata.st_mode) != row["mode"]
            or metadata.st_uid != row["uid"]
            or _sha256_file(path) != row["sha256"]
        ):
            raise StageTransitionError(f"{label} secret {row.get('name')} changed after planning")


def _read_json(path: Path, label: str, *, maximum_bytes: int = 16 * 1024 * 1024) -> dict[str, Any]:
    """Read one bounded duplicate-key-free JSON object without echoing values."""

    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
            raise StageTransitionError(f"{label} is not a bounded regular file")
        raw = path.read_bytes()
        if len(raw) > maximum_bytes:
            raise StageTransitionError(f"{label} exceeds the input size limit")
        value = json.loads(
            raw,
            object_pairs_hook=preflight._unique_json_object,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        if isinstance(error, StageTransitionError):
            raise
        raise StageTransitionError(f"{label} is invalid JSON") from error
    if not isinstance(value, dict):
        raise StageTransitionError(f"{label} must be a JSON object")
    return value


def _runtime_service_inventory(compose: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Select active long-running services from the exact rendered profile set."""

    services = compose.get("services")
    if not isinstance(services, dict):
        raise StageTransitionError("rendered Compose has no service inventory")
    active_profiles = {"public-ingress", "cloud-phase2", "runpod"}
    result: dict[str, dict[str, Any]] = {}
    for name, service in services.items():
        if not isinstance(name, str) or not isinstance(service, dict):
            raise StageTransitionError("rendered Compose service inventory is malformed")
        if service.get("restart") == "no":
            continue
        profiles = service.get("profiles", [])
        if not isinstance(profiles, list) or any(not isinstance(item, str) for item in profiles):
            raise StageTransitionError(f"Compose profiles are malformed for {name}")
        if profiles and not active_profiles.intersection(profiles):
            continue
        if name == "cloud-management-phase2" and "runpod" not in profiles:
            raise StageTransitionError(
                "the staged Cloud Management service must use the RunPod profile"
            )
        result[name] = service
    if "navigator" not in result or "navigator-persistence" not in result:
        raise StageTransitionError("Navigator and its Persistence service are required")
    return result


def _lock_image_ids(lock: dict[str, Any]) -> dict[str, str]:
    """Reuse the canonical runtime-update image-lock validator."""

    try:
        return runtime_update._locked_image_ids(lock)
    except runtime_update.RuntimeUpdateError as error:
        raise StageTransitionError("active or candidate image lock is invalid") from error


def _service_image(compose: dict[str, Any], image_ids: dict[str, str], name: str) -> dict[str, str]:
    """Resolve one Compose service to its exact locked immutable image reference and ID."""

    service = compose.get("services", {}).get(name)
    image_ref = service.get("image") if isinstance(service, dict) else None
    image_id = image_ids.get(image_ref) if isinstance(image_ref, str) else None
    if (
        not isinstance(image_ref, str)
        or not isinstance(image_id, str)
        or not IMAGE_ID_RE.fullmatch(image_id)
    ):
        raise StageTransitionError(f"{name} image does not resolve to an exact locked image")
    return {"imageRef": image_ref, "imageId": image_id}


def _mount_rows(service: dict[str, Any], field: str) -> list[dict[str, Any]]:
    """Return validated Compose mount rows for one service field."""

    rows = service.get(field, [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise StageTransitionError(f"Compose {field} list is malformed")
    return rows


def _volume_source_for_target(service: dict[str, Any], target: str, *, kind: str) -> str:
    """Require exactly one named volume or bind mount at a fixed container target."""

    matches = [
        row
        for row in _mount_rows(service, "volumes")
        if row.get("target") == target and row.get("type") == kind
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("source"), str):
        raise StageTransitionError(f"required {kind} mount {target} is missing or ambiguous")
    return matches[0]["source"]


def _validate_durable_mounts(active: dict[str, Any], candidate: dict[str, Any]) -> None:
    """Keep the Navigator database volume and CodeBuddy profile at the same host paths."""

    for compose in (active, candidate):
        services = compose.get("services", {})
        persistence = services.get("navigator-persistence")
        navigator = services.get("navigator")
        if not isinstance(persistence, dict) or not isinstance(navigator, dict):
            raise StageTransitionError("Navigator Persistence or executor service is missing")
        if (
            _volume_source_for_target(persistence, DATABASE_VOLUME_TARGET, kind="volume")
            != "navigator-sessions"
        ):
            raise StageTransitionError(
                "Navigator Persistence must keep the navigator-sessions volume"
            )
        if _volume_source_for_target(navigator, DATABASE_VOLUME_TARGET, kind="volume") != (
            "navigator-sessions"
        ):
            raise StageTransitionError("Navigator executor must keep the navigator-sessions volume")
        environment = persistence.get("environment", {})
        if environment.get("NAVIGATOR_DATABASE_PATH") != DATABASE_PATH:
            raise StageTransitionError("Navigator Persistence database path changed")
    active_home = _volume_source_for_target(
        active["services"]["navigator"], CODEBUDDY_HOME_TARGET, kind="bind"
    )
    candidate_home = _volume_source_for_target(
        candidate["services"]["navigator"], CODEBUDDY_HOME_TARGET, kind="bind"
    )
    if active_home != candidate_home:
        raise StageTransitionError("the service-owned CodeBuddy profile path must remain stable")


def _mounted_file_digests(compose: dict[str, Any], service_name: str) -> dict[str, str]:
    """Return private content digests for one service's Compose config and secret files."""

    service = compose.get("services", {}).get(service_name)
    if not isinstance(service, dict):
        return {}
    result: dict[str, str] = {}
    for field, collection in (
        ("configs", compose.get("configs", {})),
        ("secrets", compose.get("secrets", {})),
    ):
        if not isinstance(collection, dict):
            raise StageTransitionError(f"Compose {field} inventory is malformed")
        for row in _mount_rows(service, field):
            source_name = row.get("source")
            source = collection.get(source_name) if isinstance(source_name, str) else None
            file_path = source.get("file") if isinstance(source, dict) else None
            if not isinstance(source_name, str) or not isinstance(file_path, str):
                raise StageTransitionError(f"{service_name} mounts an unresolved {field} file")
            result[f"{field}:{source_name}"] = _sha256_file(Path(file_path))
    return result


def _validate_phase_topology(
    *,
    phase: str,
    active: dict[str, Any],
    candidate: dict[str, Any],
    active_lock: dict[str, Any],
    candidate_lock: dict[str, Any],
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Enforce the exact service delta, retained single-executor boundary, and image phase."""

    if phase not in PHASES:
        raise StageTransitionError(
            "phase must be provider-bootstrap or issued-connector-activation"
        )
    if active.get("name") != candidate.get("name"):
        raise StageTransitionError("active and candidate Compose project names differ")
    project = active.get("name")
    if not isinstance(project, str) or PROJECT_RE.fullmatch(project) is None:
        raise StageTransitionError("Compose project name is malformed")
    _validate_durable_mounts(active, candidate)
    _validate_compose_definition_deltas(active, candidate)
    old_services = _runtime_service_inventory(active)
    new_services = _runtime_service_inventory(candidate)
    if phase == "provider-bootstrap":
        added = set(new_services) - set(old_services)
        removed = set(old_services) - set(new_services)
        if added != NEW_SERVICES or removed:
            raise StageTransitionError(
                "provider bootstrap must add only Hugging Face and Cloud Management without removing services"
            )
    elif set(new_services) != set(old_services):
        raise StageTransitionError(
            "issued connector activation must not add or remove runtime services"
        )
    if sum(name == "navigator" for name in new_services) != 1:
        raise StageTransitionError(
            "the transition must retain exactly one Navigator executor service"
        )

    active_ids = _lock_image_ids(active_lock)
    candidate_ids = _lock_image_ids(candidate_lock)
    try:
        active_rows = runtime_update._image_rows_by_service(active_lock)
        candidate_rows = runtime_update._image_rows_by_service(candidate_lock)
    except runtime_update.RuntimeUpdateError as error:
        raise StageTransitionError(
            "active or candidate built image inventory is malformed"
        ) from error
    mutable_lock_services = {
        "navigator-persistence",
        "account-center",
        "huggingface-provider",
        "cloud-management",
        "exchange",
        "dsh-executor",
    }
    if not set(active_rows) <= set(candidate_rows):
        raise StageTransitionError("candidate image lock removes a previously locked build service")
    for service_name, row in active_rows.items():
        if service_name not in mutable_lock_services and candidate_rows[service_name] != row:
            raise StageTransitionError(f"candidate changes non-target built image {service_name}")
    if not (set(candidate_rows) - set(active_rows)) <= {"huggingface-provider", "cloud-management"}:
        raise StageTransitionError("candidate adds an image outside the HF/cloud provider set")
    if active_lock.get("externalImages") != candidate_lock.get("externalImages"):
        raise StageTransitionError("candidate changes non-target external image locks")
    target_images: dict[str, dict[str, Any]] = {}
    changed: set[str] = set()
    for name, candidate_service in new_services.items():
        candidate_image = _service_image(candidate, candidate_ids, name)
        old_service = old_services.get(name)
        old_image = _service_image(active, active_ids, name) if old_service is not None else None
        if phase == "issued-connector-activation" and (
            old_image is None or old_image != candidate_image
        ):
            raise StageTransitionError(
                "issued connector activation must keep every image ID unchanged"
            )
        config_changed = old_service is None or old_service != candidate_service
        mounted_files_changed = old_service is not None and _mounted_file_digests(
            active, name
        ) != _mounted_file_digests(candidate, name)
        image_changed = old_image is None or old_image != candidate_image
        if config_changed or mounted_files_changed or image_changed:
            if name not in SERVICE_SET:
                raise StageTransitionError(f"candidate changes non-target runtime service {name}")
            changed.add(name)
        target_images[name] = {
            "active": old_image,
            "candidate": candidate_image,
            "added": old_service is None,
            "configChanged": config_changed or mounted_files_changed,
        }

    for name, old_service in old_services.items():
        if name not in new_services:
            continue
        if name not in SERVICE_SET and old_service != new_services[name]:
            raise StageTransitionError(f"candidate changes non-target runtime service {name}")
    if phase == "provider-bootstrap":
        persistence = target_images.get("navigator-persistence")
        if not isinstance(persistence, dict) or persistence["active"] == persistence["candidate"]:
            raise StageTransitionError(
                "provider bootstrap must include the locked Persistence image update"
            )
    ordered_targets = [name for name in SERVICE_ORDER if name in changed]
    if not ordered_targets or "navigator" not in ordered_targets:
        raise StageTransitionError(
            "transition must update Navigator after its connector catalog/config change"
        )
    if phase == "provider-bootstrap" and not NEW_SERVICES <= set(ordered_targets):
        raise StageTransitionError(
            "provider bootstrap must include both newly locked provider services"
        )
    return ordered_targets, target_images


def _expected_mount_identity(compose: dict[str, Any], service_name: str) -> list[dict[str, Any]]:
    """Resolve file-backed and named-volume mounts for one rendered Compose service."""

    services = compose.get("services", {})
    service = services.get(service_name) if isinstance(services, dict) else None
    if not isinstance(service, dict):
        raise StageTransitionError(
            f"Compose service {service_name} is unavailable for mount identity"
        )
    result: list[dict[str, Any]] = []
    for row in _mount_rows(service, "volumes"):
        mount_type = row.get("type")
        source = row.get("source")
        target = row.get("target")
        if mount_type == "bind":
            if not isinstance(source, str) or not isinstance(target, str):
                raise StageTransitionError(f"{service_name} bind mount is malformed")
            try:
                resolved_source = str(Path(source).resolve(strict=True))
            except OSError as error:
                raise StageTransitionError(
                    f"{service_name} bind mount source is unavailable"
                ) from error
            read_only = row.get("read_only") is True
            source_metadata = Path(resolved_source).stat()
            if not (stat.S_ISDIR(source_metadata.st_mode) or stat.S_ISREG(source_metadata.st_mode)):
                raise StageTransitionError(f"{service_name} bind source is not a file or directory")
            identity = {
                "type": "bind",
                "source": resolved_source,
                "target": target,
                "readOnly": read_only,
                "sourceKind": "directory" if stat.S_ISDIR(source_metadata.st_mode) else "file",
                "sourceUid": source_metadata.st_uid,
                "sourceMode": stat.S_IMODE(source_metadata.st_mode),
            }
            resolved_path = Path(resolved_source)
            if read_only and resolved_path.is_file():
                identity["contentSha256"] = _sha256_file(resolved_path)
            elif read_only and stat.S_ISDIR(source_metadata.st_mode):
                directory_contents = _directory_manifest(
                    resolved_path, "Teams authorization config"
                )
                identity["contentSha256"] = _sha256_bytes(_canonical_json(directory_contents))
            result.append(identity)
        elif mount_type == "volume":
            if not isinstance(source, str) or not isinstance(target, str):
                raise StageTransitionError(f"{service_name} named-volume mount is malformed")
            volume_row = compose.get("volumes", {}).get(source, {})
            volume_name = volume_row.get("name") if isinstance(volume_row, dict) else None
            actual_name = (
                volume_name
                if isinstance(volume_name, str) and volume_name
                else f"{compose['name']}_{source}"
            )
            result.append(
                {
                    "type": "volume",
                    "source": actual_name,
                    "target": target,
                    "readOnly": row.get("read_only") is True,
                }
            )
        elif mount_type is not None:
            raise StageTransitionError(f"{service_name} has an unsupported mount type")
    for field in ("configs", "secrets"):
        definitions = compose.get(field, {})
        if not isinstance(definitions, dict):
            raise StageTransitionError(f"Compose {field} inventory is malformed")
        for row in _mount_rows(service, field):
            name = row.get("source")
            target = row.get("target")
            definition = definitions.get(name) if isinstance(name, str) else None
            source = definition.get("file") if isinstance(definition, dict) else None
            if not isinstance(source, str) or not isinstance(target, str):
                raise StageTransitionError(f"{service_name} {field} mount is unresolved")
            try:
                resolved_source = str(Path(source).resolve(strict=True))
            except OSError as error:
                raise StageTransitionError(f"{service_name} {field} file is unavailable") from error
            source_metadata = Path(resolved_source).stat()
            result.append(
                {
                    "type": "bind",
                    "source": resolved_source,
                    "target": target,
                    "readOnly": True,
                    "sourceKind": "directory" if stat.S_ISDIR(source_metadata.st_mode) else "file",
                    "sourceUid": source_metadata.st_uid,
                    "sourceMode": stat.S_IMODE(source_metadata.st_mode),
                    "contentSha256": _sha256_file(Path(resolved_source)),
                }
            )
    return sorted(result, key=lambda row: (row["target"], row["type"], row["source"]))


def _runtime_config_identity_for_compose(
    compose: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Fingerprint static inputs and pin stateful mounts for each selected runtime service."""

    return {
        name: _expected_mount_identity(compose, name)
        for name in sorted(_runtime_service_inventory(compose))
    }


def _candidate_runtime_config_identity(
    *,
    active_identity: dict[str, list[dict[str, Any]]],
    candidate_compose: dict[str, Any],
    target_services: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """Use candidate mounts only for recreated services and retain live mounts elsewhere."""

    candidate_services = set(_runtime_service_inventory(candidate_compose))
    if not target_services <= candidate_services:
        raise StageTransitionError("target mount identity names a missing candidate service")
    if not candidate_services - target_services <= set(active_identity):
        raise StageTransitionError("untargeted candidate service has no active mount identity")
    identity = {
        name: (
            _expected_mount_identity(candidate_compose, name)
            if name in target_services
            else active_identity[name]
        )
        for name in sorted(candidate_services)
    }
    return _validate_runtime_config_identity(
        identity,
        expected_services=candidate_services,
        label="candidate runtime configuration identity",
    )


def _validate_runtime_config_identity(
    value: Any,
    *,
    expected_services: set[str],
    label: str,
) -> dict[str, list[dict[str, Any]]]:
    """Validate a closed service-to-mount identity map without accepting opaque fields."""

    if not isinstance(value, dict) or set(value) != expected_services:
        raise StageTransitionError(f"{label} service inventory is malformed")
    result: dict[str, list[dict[str, Any]]] = {}
    for service_name, mounts in value.items():
        if not isinstance(service_name, str) or not isinstance(mounts, list):
            raise StageTransitionError(f"{label} mount inventory is malformed")
        validated: list[dict[str, Any]] = []
        targets: set[str] = set()
        for row in mounts:
            if not isinstance(row, dict):
                raise StageTransitionError(f"{label} mount record is malformed")
            mount_type = row.get("type")
            common = {"type", "source", "target", "readOnly"}
            metadata_fields = {"sourceKind", "sourceUid", "sourceMode"}
            allowed = common | (
                metadata_fields | {"contentSha256"} if mount_type == "bind" else set()
            )
            if set(row) - allowed or not common <= set(row):
                raise StageTransitionError(f"{label} mount record has unsupported fields")
            source = row.get("source")
            target = row.get("target")
            read_only = row.get("readOnly")
            if (
                mount_type not in {"bind", "volume"}
                or not isinstance(source, str)
                or not source
                or not isinstance(target, str)
                or not target.startswith("/")
                or not isinstance(read_only, bool)
                or target in targets
            ):
                raise StageTransitionError(f"{label} mount identity is malformed")
            targets.add(target)
            if mount_type == "bind":
                if not metadata_fields <= set(row):
                    raise StageTransitionError(f"{label} bind ownership is missing")
                if (
                    row.get("sourceKind") not in {"file", "directory"}
                    or isinstance(row.get("sourceUid"), bool)
                    or not isinstance(row.get("sourceUid"), int)
                    or isinstance(row.get("sourceMode"), bool)
                    or not isinstance(row.get("sourceMode"), int)
                    or not 0 <= row["sourceMode"] <= 0o7777
                ):
                    raise StageTransitionError(f"{label} bind ownership is malformed")
                digest = row.get("contentSha256")
                needs_digest = read_only
                if needs_digest and (
                    not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None
                ):
                    raise StageTransitionError(f"{label} static bind content digest is missing")
                if digest is not None and (
                    not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None
                ):
                    raise StageTransitionError(f"{label} bind content digest is malformed")
                if not read_only and digest is not None:
                    raise StageTransitionError(f"{label} stateful bind must not hash its contents")
            validated.append(dict(row))
        if validated != sorted(
            validated, key=lambda row: (row["target"], row["type"], row["source"])
        ):
            raise StageTransitionError(f"{label} mount records are not canonical")
        result[service_name] = validated
    return result


def _verify_runtime_config_identity_sources(
    identity: dict[str, list[dict[str, Any]]], label: str
) -> None:
    """Recheck mount ownership and static read-only config content without hashing state."""

    for service_name, mounts in identity.items():
        for row in mounts:
            if row["type"] != "bind":
                continue
            path = Path(row["source"])
            try:
                metadata = path.stat()
                resolved = str(path.resolve(strict=True))
            except OSError as error:
                raise StageTransitionError(
                    f"{label} {service_name} bind source is unavailable"
                ) from error
            if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                raise StageTransitionError(f"{label} {service_name} bind source type changed")
            kind = "directory" if stat.S_ISDIR(metadata.st_mode) else "file"
            if (
                resolved != row["source"]
                or kind != row["sourceKind"]
                or metadata.st_uid != row["sourceUid"]
                or stat.S_IMODE(metadata.st_mode) != row["sourceMode"]
            ):
                raise StageTransitionError(
                    f"{label} {service_name} bind ownership changed after selection"
                )
            digest = row.get("contentSha256")
            if digest is None:
                continue
            if kind == "file":
                current_digest = _sha256_file(path)
            else:
                contents = _directory_manifest(path, "Teams authorization config")
                current_digest = _sha256_bytes(_canonical_json(contents))
            if current_digest != digest:
                raise StageTransitionError(
                    f"{label} {service_name} static config source changed after selection"
                )


def _service_mounts_match(actual: Any, expected: list[dict[str, Any]]) -> bool:
    """Compare Docker's bind/volume mount read-back with one exact rendered service."""

    if not isinstance(actual, list):
        return False
    observed: list[dict[str, Any]] = []
    for row in actual:
        if not isinstance(row, dict) or row.get("Type") not in {"bind", "volume"}:
            continue
        target = row.get("Destination")
        source = row.get("Source") if row.get("Type") == "bind" else row.get("Name")
        read_write = row.get("RW")
        if (
            not isinstance(target, str)
            or not isinstance(source, str)
            or not isinstance(read_write, bool)
        ):
            return False
        observed.append(
            {
                "type": row["Type"],
                "source": source,
                "target": target,
                "readOnly": not read_write,
            }
        )
    expected_mounts = [
        {key: row[key] for key in ("type", "source", "target", "readOnly")} for row in expected
    ]
    return (
        sorted(observed, key=lambda row: (row["target"], row["type"], row["source"]))
        == expected_mounts
    )


def _selection_file_state(
    path: Path,
    active_inputs: dict[str, Any],
    *,
    project: str,
    config_dir: Path,
    secrets_dir: Path,
    active_service_names: set[str],
) -> dict[str, Any]:
    """Verify an existing stable runtime selection or record its first-run absence."""

    if path.is_symlink() or not path.is_absolute():
        raise StageTransitionError("runtime selection path cannot be a symlink")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir() or parent.resolve(strict=True) != parent:
        raise StageTransitionError(
            "runtime selection parent must be an existing non-symlink directory"
        )
    parent_metadata = parent.stat()
    if parent_metadata.st_uid != os.geteuid() or stat.S_IMODE(parent_metadata.st_mode) != 0o700:
        raise StageTransitionError("runtime selection parent must be current-user-owned mode 0700")
    if not path.exists():
        return {"path": str(path.resolve()), "sha256": None, "document": None}
    document = _read_json(path, "active runtime selection")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise StageTransitionError("active runtime selection must be owner-only mode 0600")
    expected_fields = {
        "schemaVersion",
        "project",
        "operationId",
        "planSha256",
        "phase",
        "composeFiles",
        "envFiles",
        "imageLock",
        "secretsDir",
        "configDir",
        "huggingfaceSeed",
        "runpodKey",
        "runtimeConfigIdentity",
        "updatedAtUtc",
    }
    if set(document) != expected_fields or document.get("schemaVersion") != SELECTION_SCHEMA:
        raise StageTransitionError("active runtime selection schema is unsupported or open")
    if (
        document.get("project") != project
        or document.get("configDir") != str(config_dir.resolve(strict=True))
        or document.get("secretsDir") != str(secrets_dir.resolve(strict=True))
    ):
        raise StageTransitionError("active runtime selection paths differ from explicit inputs")
    if (
        document.get("phase") not in PHASES
        or not isinstance(document.get("operationId"), str)
        or not isinstance(document.get("updatedAtUtc"), str)
        or not isinstance(document.get("planSha256"), str)
        or SHA256_RE.fullmatch(document["planSha256"]) is None
    ):
        raise StageTransitionError("active runtime selection identity is malformed")
    for field, key in (("composeFiles", "composeFiles"), ("envFiles", "envFiles")):
        if document.get(field) != active_inputs[key]:
            raise StageTransitionError(
                f"active runtime selection {field} differs from explicit inputs"
            )
    if document.get("imageLock") != active_inputs["imageLock"]:
        raise StageTransitionError(
            "active runtime selection image lock differs from explicit inputs"
        )
    _verify_record(document.get("huggingfaceSeed"), "active runtime selection HF seed")
    _verify_record(document.get("runpodKey"), "active runtime selection RunPod key")
    mount_identity = _validate_runtime_config_identity(
        document.get("runtimeConfigIdentity"),
        expected_services=active_service_names,
        label="active runtime selection",
    )
    _verify_runtime_config_identity_sources(mount_identity, "active runtime selection")
    return {"path": str(path.resolve()), "sha256": _sha256_file(path), "document": document}


def _validate_output_directory(path: Path) -> None:
    """Require an owner-only artifact directory and an unused private plan path."""

    try:
        runtime_update._validate_plan_output_path(path)
    except runtime_update.RuntimeUpdateError as error:
        raise StageTransitionError(
            "stage transition plan output must be a new private artifact"
        ) from error


def _validate_private_directory(
    path: Path,
    label: str,
    *,
    require_artifact: bool = True,
    expected_uid: int | None = None,
) -> Path:
    """Validate one stable private directory without creating or changing it."""

    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError as error:
        raise StageTransitionError(f"private {label} directory is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != (os.geteuid() if expected_uid is None else expected_uid)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or resolved != path
        or (require_artifact and not preflight._private_artifact(path))
    ):
        raise StageTransitionError(
            f"private {label} directory must be a non-symlink owner-only directory"
        )
    return resolved


def _validate_active_candidate_configs(
    active_config_dir: Path,
    candidate_config_dir: Path,
    active: dict[str, Any],
    candidate: dict[str, Any],
) -> None:
    """Keep live config sources immutable and allow only reviewed new provider files."""

    if active_config_dir.resolve(strict=True) == candidate_config_dir.resolve(strict=True):
        raise StageTransitionError(
            "candidate config directory must be versioned separately from active config"
        )
    active_configs = active.get("configs", {})
    candidate_configs = candidate.get("configs", {})
    if not isinstance(active_configs, dict) or not isinstance(candidate_configs, dict):
        raise StageTransitionError("Compose config inventory is malformed")
    if not set(active_configs) <= set(candidate_configs):
        raise StageTransitionError("candidate removes existing Compose configs")
    for name, active_row in active_configs.items():
        candidate_row = candidate_configs.get(name)
        active_file = active_row.get("file") if isinstance(active_row, dict) else None
        candidate_file = candidate_row.get("file") if isinstance(candidate_row, dict) else None
        if not isinstance(active_file, str) or not isinstance(candidate_file, str):
            raise StageTransitionError(f"Compose config {name} is malformed")
        if Path(active_file).name != Path(candidate_file).name:
            raise StageTransitionError(f"candidate config {name} changes its canonical filename")
        active_digest = _sha256_file(Path(active_file))
        candidate_digest = _sha256_file(Path(candidate_file))
        if active_digest != candidate_digest and name not in {
            "navigator-principal",
            "connector-bindings",
            "connector-tool-catalog",
            "connector-bootstrap",
            "responsibility-targets",
        }:
            raise StageTransitionError(f"candidate changes unrelated Compose config {name}")
    added_configs = set(candidate_configs) - set(active_configs)
    expected_added = (
        {"teams-credential-notifications"}
        if "teams-credential-notifications" in candidate_configs
        else set()
    )
    if added_configs != expected_added:
        raise StageTransitionError(
            "candidate Compose config additions are outside the notification overlay"
        )


def _validate_active_candidate_secrets(active: dict[str, Any], candidate: dict[str, Any]) -> None:
    """Preserve active host-secret sources except the explicit HF seed and RunPod key inputs."""

    active_secrets = active.get("secrets", {})
    candidate_secrets = candidate.get("secrets", {})
    if not isinstance(active_secrets, dict) or not isinstance(candidate_secrets, dict):
        raise StageTransitionError("Compose secret inventory is malformed")
    if not set(active_secrets) <= set(candidate_secrets):
        raise StageTransitionError("candidate removes an existing Compose secret")
    allowed_additions = preflight.HUGGINGFACE_OPTIONAL_SECRET_NAMES | {
        "cloud-management-exchange-hmac-key",
        "gcp-wif-credential-config",
        "runpod-api-key",
    }
    added = set(candidate_secrets) - set(active_secrets)
    if not added <= allowed_additions:
        raise StageTransitionError("candidate adds a secret outside the HF/cloud provider set")
    for name, active_row in active_secrets.items():
        candidate_row = candidate_secrets.get(name)
        old_file = active_row.get("file") if isinstance(active_row, dict) else None
        new_file = candidate_row.get("file") if isinstance(candidate_row, dict) else None
        if not isinstance(old_file, str) or not isinstance(new_file, str):
            raise StageTransitionError(f"Compose secret {name} is malformed")
        if Path(old_file).name != Path(new_file).name:
            raise StageTransitionError(f"candidate secret {name} changes its canonical filename")
        if name not in {"account-center-huggingface-oauth-source", "runpod-api-key"} and str(
            Path(old_file).resolve(strict=True)
        ) != str(Path(new_file).resolve(strict=True)):
            raise StageTransitionError(f"candidate changes unrelated secret source {name}")


def _validate_compose_definition_deltas(active: dict[str, Any], candidate: dict[str, Any]) -> None:
    """Allow only the named provider overlay network, volume, secret, and config additions."""

    for field, allowed_additions in (
        (
            "networks",
            {
                "huggingface-exchange-private",
                "huggingface-account-center-private",
                "huggingface-egress",
                "cloud-management-egress",
            },
        ),
        ("volumes", {"cloud-management-state"}),
    ):
        old_rows = active.get(field, {})
        new_rows = candidate.get(field, {})
        if not isinstance(old_rows, dict) or not isinstance(new_rows, dict):
            raise StageTransitionError(f"Compose {field} definition inventory is malformed")
        if not set(old_rows) <= set(new_rows):
            raise StageTransitionError(f"candidate removes an existing Compose {field} definition")
        if any(new_rows[name] != old_rows[name] for name in old_rows):
            raise StageTransitionError(f"candidate changes an existing Compose {field} definition")
        if not (set(new_rows) - set(old_rows)) <= allowed_additions:
            raise StageTransitionError(f"candidate adds an unsupported Compose {field} definition")

    for field in ("configs", "secrets"):
        old_rows = active.get(field, {})
        new_rows = candidate.get(field, {})
        if not isinstance(old_rows, dict) or not isinstance(new_rows, dict):
            raise StageTransitionError(f"Compose {field} inventory is malformed")
        if not set(old_rows) <= set(new_rows):
            raise StageTransitionError(f"candidate removes an existing Compose {field}")


def _validate_candidate_secret_paths(
    *,
    candidate: dict[str, Any],
    active_secrets_dir: Path,
    huggingface_seed_file: Path,
    runpod_key_file: Path,
    phase: str,
) -> None:
    """Bind Compose's two staged credential paths to the explicit immutable inputs."""

    if phase not in PHASES:
        raise StageTransitionError("transition phase is invalid")
    if candidate.get("secrets", {}).get("account-center-huggingface-oauth-source", {}).get(
        "file"
    ) != str(huggingface_seed_file.resolve(strict=True)):
        raise StageTransitionError(
            "candidate Account Center OAuth mount differs from the explicit HF seed"
        )
    if candidate.get("secrets", {}).get("runpod-api-key", {}).get("file") != str(
        runpod_key_file.resolve(strict=True)
    ):
        raise StageTransitionError("candidate RunPod mount differs from the explicit API key file")
    for name, path in (("HF seed", huggingface_seed_file), ("RunPod key", runpod_key_file)):
        if path.is_symlink() or not path.is_file() or not preflight._private_artifact(path):
            raise StageTransitionError(f"candidate {name} must be a regular private artifact")
        metadata = path.stat()
        if (
            metadata.st_uid != 10001
            or metadata.st_mode & 0o077
            or stat.S_IMODE(metadata.st_mode) != 0o400
        ):
            raise StageTransitionError(f"candidate {name} must be owned by UID 10001 and mode 0400")
    if not active_secrets_dir.is_dir() or active_secrets_dir.is_symlink():
        raise StageTransitionError("active shared secrets directory is unavailable")
    if (
        candidate.get("services", {})
        .get("cloud-management-phase2", {})
        .get("environment", {})
        .get("CYRENE_CLOUD_MGMT_RUNPOD_API_KEY_FILE")
        != "/run/secrets/runpod-api-key"
    ):
        raise StageTransitionError(
            "RunPod provider must read its candidate key only through the secret mount"
        )


def _live_service_inventory(
    *,
    compose: dict[str, Any],
    image_lock: dict[str, Any],
    docker_prefix: list[str],
    mount_identity_by_service: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Inspect exact running project containers and reject duplicates or unmanaged workers."""

    project = compose.get("name")
    if not isinstance(project, str) or not project:
        raise StageTransitionError("Compose project name is missing")
    expected = _runtime_service_inventory(compose)
    expected_mount_identity = (
        _runtime_config_identity_for_compose(compose)
        if mount_identity_by_service is None
        else _validate_runtime_config_identity(
            mount_identity_by_service,
            expected_services=set(expected),
            label="runtime configuration identity",
        )
    )
    _verify_runtime_config_identity_sources(expected_mount_identity, "runtime configuration")
    locked_ids = _lock_image_ids(image_lock)
    output = runtime_update._checked_stdout(
        [
            *docker_prefix,
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.ID}}",
        ],
        label="stage transition active service inventory",
    )
    ids = [row.strip() for row in output.splitlines() if row.strip()]
    if not ids or any(runtime_update.CONTAINER_ID_RE.fullmatch(item) is None for item in ids):
        raise StageTransitionError("Docker returned an empty or malformed project inventory")
    rows = runtime_update._decode_json_output(
        runtime_update._checked_stdout(
            [*docker_prefix, "inspect", *ids], label="stage transition service inspection"
        ),
        "stage transition service inspection",
    )
    if not isinstance(rows, list) or len(rows) != len(ids):
        raise StageTransitionError("Docker returned an incomplete project inspection")
    by_service: dict[str, dict[str, Any]] = {}
    published_container_ids: list[str] = []
    for row in rows:
        config_row = row.get("Config")
        labels = config_row.get("Labels") if isinstance(config_row, dict) else None
        state = row.get("State")
        service_name = (
            labels.get("com.docker.compose.service") if isinstance(labels, dict) else None
        )
        if not isinstance(labels, dict) or labels.get("com.docker.compose.project") != project:
            raise StageTransitionError("running/stopped project container has an unknown owner")
        if service_name not in expected:
            raise StageTransitionError("project contains an unexpected runtime service container")
        if service_name in by_service:
            raise StageTransitionError(f"project service {service_name} has multiple containers")
        if (
            not isinstance(state, dict)
            or state.get("Running") is not True
            or state.get("Paused") is True
        ):
            raise StageTransitionError(f"project service {service_name} is not running")
        image_ref = config_row.get("Image") if isinstance(config_row, dict) else None
        image_id = row.get("Image")
        expected_image_ref = expected[service_name].get("image")
        if (
            image_ref != expected_image_ref
            or not isinstance(image_id, str)
            or image_id != locked_ids.get(expected_image_ref)
        ):
            raise StageTransitionError(
                f"project service {service_name} differs from its exact active image lock"
            )
        health_record = state.get("Health")
        health = health_record.get("Status") if isinstance(health_record, dict) else "running"
        if expected[service_name].get("healthcheck") and health != "healthy":
            raise StageTransitionError(f"project service {service_name} is not healthy")
        networks_record = row.get("NetworkSettings")
        networks = networks_record.get("Networks") if isinstance(networks_record, dict) else None
        if not isinstance(networks, dict) or not networks:
            raise StageTransitionError(f"project service {service_name} has no network inventory")
        expected_mounts = expected_mount_identity[service_name]
        actual_mounts = row.get("Mounts")
        if not _service_mounts_match(actual_mounts, expected_mounts):
            raise StageTransitionError(
                f"project service {service_name} mounted config/volume sources differ from the selected runtime identity"
            )
        container_id = row.get("Id")
        if (
            not isinstance(container_id, str)
            or FULL_CONTAINER_ID_RE.fullmatch(container_id) is None
        ):
            raise StageTransitionError(
                f"project service {service_name} has a malformed container ID"
            )
        by_service[service_name] = {
            "containerId": container_id,
            "imageRef": image_ref,
            "imageId": image_id,
            "health": health,
            "networkNames": sorted(networks),
            "mounts": expected_mounts,
        }
        if expected[service_name].get("ports"):
            host_config = row.get("HostConfig")
            bindings = host_config.get("PortBindings") if isinstance(host_config, dict) else None
            expected_port_rows = expected[service_name].get("ports", [])
            expected_bindings = {
                f"{item.get('target')}/{item.get('protocol', 'tcp')}": [
                    {
                        "HostIp": item.get("host_ip") or "0.0.0.0",
                        "HostPort": str(item.get("published", "")),
                    }
                ]
                for item in expected_port_rows
                if isinstance(item, dict)
            }
            if not isinstance(bindings, dict) or bindings != expected_bindings:
                raise StageTransitionError(
                    "active Teams ingress host port binding differs from Compose"
                )
            published_container_ids.append(row["Id"])
    if set(by_service) != set(expected):
        missing = sorted(set(expected) - set(by_service))
        raise StageTransitionError(
            "project live service inventory is incomplete: " + ", ".join(missing)
        )
    if published_container_ids:
        published = runtime_update._checked_stdout(
            [*docker_prefix, "ps", "--filter", "publish=443", "--format", "{{.ID}}"],
            label="public ingress port ownership",
        )
        port_ids = [value.strip() for value in published.splitlines() if value.strip()]
        if len(published_container_ids) != 1 or len(port_ids) != 1:
            raise StageTransitionError(
                "TCP 443 is not uniquely owned by the active project ingress"
            )
        if not port_ids[0].startswith(published_container_ids[0][:12]):
            raise StageTransitionError("TCP 443 has a non-project owner")
    return by_service


def _inspect_exact_service(
    *,
    project: str,
    service_name: str,
    expected_image: dict[str, str],
    has_healthcheck: bool,
    docker_prefix: list[str],
    allow_starting: bool = False,
) -> dict[str, Any]:
    """Read one exact target container after a recorded Compose mutation."""

    output = runtime_update._checked_stdout(
        [
            *docker_prefix,
            "ps",
            "--all",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service_name}",
            "--format",
            "{{.ID}}",
        ],
        label=f"{service_name} stage transition readback",
    )
    ids = [value.strip() for value in output.splitlines() if value.strip()]
    if len(ids) != 1 or runtime_update.CONTAINER_ID_RE.fullmatch(ids[0]) is None:
        raise StageTransitionError(
            f"{service_name} has no unique project-owned container after apply"
        )
    rows = runtime_update._decode_json_output(
        runtime_update._checked_stdout(
            [*docker_prefix, "inspect", ids[0]], label=f"{service_name} target inspection"
        ),
        f"{service_name} target inspection",
    )
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise StageTransitionError(f"{service_name} inspection is ambiguous")
    row = rows[0]
    container_config = row.get("Config")
    labels = container_config.get("Labels") if isinstance(container_config, dict) else None
    state = row.get("State")
    if (
        not isinstance(labels, dict)
        or labels.get("com.docker.compose.project") != project
        or labels.get("com.docker.compose.service") != service_name
        or not isinstance(state, dict)
        or state.get("Running") is not True
        or state.get("Paused") is True
        or row.get("Image") != expected_image.get("imageId")
        or container_config.get("Image") != expected_image.get("imageRef")
    ):
        raise StageTransitionError(f"{service_name} did not start its exact candidate image")
    health_record = state.get("Health")
    health = health_record.get("Status") if isinstance(health_record, dict) else "running"
    if has_healthcheck and health != "healthy" and not allow_starting:
        raise StageTransitionError(f"{service_name} has not passed its health check")
    networks_record = row.get("NetworkSettings")
    networks = networks_record.get("Networks") if isinstance(networks_record, dict) else None
    if not isinstance(networks, dict) or not networks:
        raise StageTransitionError(f"{service_name} has no active network inventory")
    container_id = row.get("Id")
    if not isinstance(container_id, str) or FULL_CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise StageTransitionError(f"{service_name} returned a malformed container ID")
    return {
        "containerId": container_id,
        "imageRef": expected_image["imageRef"],
        "imageId": expected_image["imageId"],
        "health": health,
        "networkNames": sorted(networks),
    }


def _wait_for_target_service(
    *,
    project: str,
    service_name: str,
    expected_image: dict[str, str],
    has_healthcheck: bool,
    timeout_seconds: int,
    docker_prefix: list[str],
) -> dict[str, Any]:
    """Poll read-only container state until the exact target is healthy or times out."""

    deadline = time.monotonic() + timeout_seconds
    while True:
        current = _inspect_exact_service(
            project=project,
            service_name=service_name,
            expected_image=expected_image,
            has_healthcheck=has_healthcheck,
            docker_prefix=docker_prefix,
            allow_starting=True,
        )
        if current["health"] == "unhealthy":
            raise StageTransitionError(f"{service_name} became unhealthy during candidate startup")
        if current["health"] == "healthy" or (
            not has_healthcheck and current["health"] == "running"
        ):
            return current
        if time.monotonic() >= deadline:
            raise StageTransitionError(
                f"{service_name} did not become healthy before the bounded timeout"
            )
        time.sleep(2)


def _live_sqlite_report(
    *, docker_prefix: list[str], container_id: str, database_path: str
) -> dict[str, Any]:
    """Read integrity, schema version, and schema-object identity from the live database."""

    script = (
        "import hashlib,json,sqlite3,sys;"
        "db=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True);"
        "integrity=db.execute('PRAGMA integrity_check').fetchone()[0];"
        "version=db.execute('PRAGMA user_version').fetchone()[0];"
        "objects=db.execute(\"SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name\").fetchall();"
        "schema=json.dumps(objects,sort_keys=True,separators=(',',':')).encode();"
        "print(json.dumps({'integrity':integrity,'userVersion':version,'schemaSha256':hashlib.sha256(schema).hexdigest(),'objects':[r[0]+':'+r[1] for r in objects]},separators=(',',':')));db.close()"
    )
    try:
        output = runtime_update._checked_stdout(
            [
                *docker_prefix,
                "exec",
                container_id,
                "/opt/venv/bin/python",
                "-c",
                script,
                database_path,
            ],
            label="Navigator Persistence live SQLite inspection",
            timeout_seconds=30,
        )
        report = runtime_update._decode_json_output(output, "live SQLite report")
    except (runtime_update.RuntimeUpdateError, subprocess.SubprocessError) as error:
        raise StageTransitionError(
            "live Navigator Persistence database could not be inspected"
        ) from error
    if (
        not isinstance(report, dict)
        or report.get("integrity") != "ok"
        or isinstance(report.get("userVersion"), bool)
        or not isinstance(report.get("userVersion"), int)
        or not isinstance(report.get("schemaSha256"), str)
        or not SHA256_RE.fullmatch(report["schemaSha256"])
        or not isinstance(report.get("objects"), list)
        or any(not isinstance(name, str) for name in report["objects"])
    ):
        raise StageTransitionError(
            "live Navigator Persistence database failed integrity/schema validation"
        )
    report["objects"] = sorted(report["objects"])
    return report


def _validate_database_migration(
    *,
    plan: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
) -> dict[str, Any]:
    """Accept only an intact, non-regressing additive SQLite schema migration."""

    persistence = journal.get("targetProgress", {}).get("navigator-persistence", {})
    container_id = (
        persistence.get("candidateContainerId") if isinstance(persistence, dict) else None
    )
    if not isinstance(container_id, str) or FULL_CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise StageTransitionError("candidate Persistence container identity is unavailable")
    current = _live_sqlite_report(
        docker_prefix=docker_prefix,
        container_id=container_id,
        database_path=plan["database"]["containerPath"],
    )
    before = journal["database"].get("snapshot")
    if not isinstance(before, dict):
        raise StageTransitionError("SQLite pre-transition snapshot is unavailable")
    before_objects = set(before.get("objects", []))
    current_objects = set(current.get("objects", []))
    if not before_objects <= current_objects or current["userVersion"] < before["userVersion"]:
        raise StageTransitionError(
            "candidate Persistence migration removed schema objects or regressed user_version"
        )
    current["addedObjects"] = sorted(current_objects - before_objects)
    current["removedObjects"] = []
    journal["database"]["postMigration"] = current
    journal["database"]["restorePerformed"] = False
    _write_journal(journal_path, journal, "DATABASE_MIGRATION_VERIFIED")
    return current


def _validate_post_update_capacity(
    *,
    compose: dict[str, Any],
    services: dict[str, dict[str, Any]],
    docker_prefix: list[str],
    minimum_available_mib: int,
) -> dict[str, Any]:
    """Require current RSS plus the fixed host reserve to fit before releasing ingress."""

    available_mib = preflight._parse_memavailable()
    total_memory_mib = preflight._parse_meminfo()
    if available_mib < minimum_available_mib:
        raise StageTransitionError(
            "post-transition MemAvailable fell below the configured host reserve"
        )
    stats = runtime_update._checked_stdout(
        [*docker_prefix, "stats", "--no-stream", "--format", "{{.Name}}|{{.MemUsage}}"],
        label="post-transition RSS inventory",
        timeout_seconds=30,
    )
    project_prefix = f"{compose.get('name')}-"
    observed: dict[str, float] = {}
    for line in stats.splitlines():
        name, separator, usage = line.partition("|")
        if not separator or not name.startswith(project_prefix):
            continue
        for service_name in services:
            if name.startswith(f"{project_prefix}{service_name}-"):
                observed[service_name] = preflight._docker_memory_usage_mib(usage)
                break
    if set(observed) != set(services):
        raise StageTransitionError(
            "Docker did not return complete per-service RSS after transition"
        )
    total_rss_mib = sum(observed.values())
    if total_rss_mib + minimum_available_mib > total_memory_mib:
        raise StageTransitionError("post-transition RSS plus host reserve exceeds guest memory")
    return {
        "memAvailableMiB": available_mib,
        "hostMemoryMiB": total_memory_mib,
        "minimumMemAvailableMiB": minimum_available_mib,
        "totalRssMiB": round(total_rss_mib, 2),
        "rssByServiceMiB": {name: round(value, 2) for name, value in sorted(observed.items())},
    }


def _selection_summary(document: dict[str, Any]) -> dict[str, Any]:
    """Return the safe runtime selection facts with no environment values."""

    return {
        "schemaVersion": document.get("schemaVersion"),
        "project": document.get("project"),
        "operationId": document.get("operationId"),
        "planSha256": document.get("planSha256"),
        "updatedAtUtc": document.get("updatedAtUtc"),
    }


def create_plan(
    *,
    phase: str,
    active_compose_files: list[Path],
    active_env_files: list[Path],
    active_image_lock: Path,
    candidate_compose_files: list[Path],
    candidate_env_files: list[Path],
    candidate_image_lock: Path,
    secrets_dir: Path,
    active_config_dir: Path,
    candidate_config_dir: Path,
    candidate_huggingface_seed: Path,
    candidate_runpod_key_file: Path,
    connector_auth_evidence: Path,
    connector_auth_task_id: str,
    notification_anchor_file: Path | None,
    notification_provenance_file: Path | None,
    active_selection_file: Path | None,
    promote_selection_file: Path,
    backup_dir: Path,
    journal_dir: Path,
    plan_path: Path,
    docker_prefix: list[str],
    drain_timeout_seconds: int,
    health_timeout_seconds: int,
    max_memory_mib: int,
    max_cpus: float,
    required_free_gib: float,
    minimum_mem_available_mib: int,
) -> dict[str, Any]:
    """Build a private, read-only plan bound to current containers and exact candidate files."""

    runtime_update._require_update_operator()
    if phase not in PHASES:
        raise StageTransitionError(
            "phase must be provider-bootstrap or issued-connector-activation"
        )
    runtime_update._validate_docker_prefix(docker_prefix)
    if not 60 <= drain_timeout_seconds <= 900 or not 30 <= health_timeout_seconds <= 600:
        raise StageTransitionError("drain/health timeouts are outside their bounded ranges")
    if (
        isinstance(max_memory_mib, bool)
        or not isinstance(max_memory_mib, int)
        or max_memory_mib <= 0
        or not isinstance(max_cpus, (int, float))
        or isinstance(max_cpus, bool)
        or max_cpus <= 0
        or not isinstance(required_free_gib, (int, float))
        or isinstance(required_free_gib, bool)
        or required_free_gib < 0
        or isinstance(minimum_mem_available_mib, bool)
        or not isinstance(minimum_mem_available_mib, int)
        or minimum_mem_available_mib <= 0
    ):
        raise StageTransitionError("host resource limits are malformed")
    _validate_output_directory(plan_path)
    active_config_dir = _validate_private_directory(
        active_config_dir, "active config", expected_uid=10001
    )
    candidate_config_dir = _validate_private_directory(
        candidate_config_dir, "candidate config", expected_uid=10001
    )
    secrets_dir = _validate_private_directory(secrets_dir, "active secrets", expected_uid=10001)
    backup_dir = _validate_private_directory(backup_dir, "SQLite backup", require_artifact=True)
    if active_config_dir == candidate_config_dir:
        raise StageTransitionError("candidate config directory must be separate from active config")
    if not connector_auth_task_id or len(connector_auth_task_id) > 128:
        raise StageTransitionError("connector auth task ID is malformed")
    if phase == "issued-connector-activation" and (
        notification_anchor_file is None or notification_provenance_file is None
    ):
        raise StageTransitionError("notification anchor/provenance are required for activation")
    if phase == "provider-bootstrap" and (
        notification_anchor_file is not None or notification_provenance_file is not None
    ):
        raise StageTransitionError("provider bootstrap must keep credential notifications disabled")

    active_inputs = {
        "composeFiles": _file_records(active_compose_files, "active Compose file"),
        "envFiles": _file_records(active_env_files, "active environment file"),
        "imageLock": _file_record(active_image_lock, "active image lock"),
        "configDirectoryFiles": _directory_manifest(active_config_dir, "active config"),
    }
    candidate_inputs = {
        "composeFiles": _file_records(candidate_compose_files, "candidate Compose file"),
        "envFiles": _file_records(candidate_env_files, "candidate environment file"),
        "imageLock": _file_record(candidate_image_lock, "candidate image lock"),
        "configDirectoryFiles": _directory_manifest(candidate_config_dir, "candidate config"),
        "huggingfaceSeed": _file_record(candidate_huggingface_seed, "candidate HF OAuth seed"),
        "runpodKey": _file_record(candidate_runpod_key_file, "candidate RunPod key"),
        "connectorAuthEvidence": _file_record(connector_auth_evidence, "connector auth evidence"),
        "notificationAnchor": (
            _file_record(notification_anchor_file, "notification source anchor")
            if notification_anchor_file is not None
            else None
        ),
        "notificationProvenance": (
            _file_record(notification_provenance_file, "notification provenance")
            if notification_provenance_file is not None
            else None
        ),
    }
    active_lock = runtime_update._load_lock(active_image_lock)
    candidate_lock = runtime_update._load_lock(candidate_image_lock)
    try:
        active_compose = preflight._compose_config(
            docker_prefix, active_compose_files, active_env_files
        )
        candidate_compose = preflight._compose_config(
            docker_prefix, candidate_compose_files, candidate_env_files
        )
    except (OSError, preflight.PreflightError, subprocess.SubprocessError) as error:
        raise StageTransitionError("active or candidate Compose could not be rendered") from error
    active_inputs["secretFiles"] = _secret_manifest(active_compose, "active")
    candidate_inputs["secretFiles"] = _secret_manifest(candidate_compose, "candidate")
    _validate_active_candidate_configs(
        active_config_dir, candidate_config_dir, active_compose, candidate_compose
    )
    _validate_active_candidate_secrets(active_compose, candidate_compose)
    _validate_candidate_secret_paths(
        candidate=candidate_compose,
        active_secrets_dir=secrets_dir,
        huggingface_seed_file=candidate_huggingface_seed,
        runpod_key_file=candidate_runpod_key_file,
        phase=phase,
    )
    targets, target_images = _validate_phase_topology(
        phase=phase,
        active=active_compose,
        candidate=candidate_compose,
        active_lock=active_lock,
        candidate_lock=candidate_lock,
    )
    if active_compose.get("name") != candidate_compose.get("name"):
        raise StageTransitionError("active and candidate Compose project names differ")
    project = active_compose["name"]
    try:
        candidate_preflight = preflight.run_preflight(
            mode="connector-auth-readonly",
            compose_files=[path.resolve() for path in candidate_compose_files],
            env_files=[path.resolve() for path in candidate_env_files],
            image_lock=candidate_image_lock.resolve(),
            secrets_dir=secrets_dir,
            config_dir=candidate_config_dir,
            github_hf_evidence=None,
            azure_gcp_ledger=None,
            runpod_ledger=None,
            azure_gcp_api_evidence=None,
            runpod_api_evidence=None,
            docker_prefix=docker_prefix,
            max_memory_mib=max_memory_mib,
            max_cpus=max_cpus,
            required_free_gib=required_free_gib,
            minimum_mem_available_mib=minimum_mem_available_mib,
            connector_auth_phase=phase,
            connector_auth_evidence=connector_auth_evidence.resolve(strict=True),
            connector_auth_task_id=connector_auth_task_id,
            huggingface_seed_file=candidate_huggingface_seed.resolve(strict=True),
            candidate_runpod_key_file=candidate_runpod_key_file.resolve(strict=True),
            notification_anchor_file=(
                notification_anchor_file.resolve(strict=True)
                if notification_anchor_file is not None
                else None
            ),
            notification_provenance_file=(
                notification_provenance_file.resolve(strict=True)
                if notification_provenance_file is not None
                else None
            ),
        )
    except (OSError, preflight.PreflightError, BuildError, subprocess.SubprocessError) as error:
        raise StageTransitionError(
            "credential read-only candidate preflight blocked the transition"
        ) from error
    if (
        candidate_preflight.get("mode") != "connector-auth-readonly"
        or candidate_preflight.get("credentialReadOnlyRuntimeReady") is not True
        or candidate_preflight.get("fullCloudWriteDeploymentReady") is not False
        or candidate_preflight.get("lifecycleAcceptance") != "NOT_RUN"
    ):
        raise StageTransitionError(
            "candidate preflight did not return the scoped read-only readiness contract"
        )
    if (
        active_selection_file is not None
        and active_selection_file.resolve() != promote_selection_file.resolve()
    ):
        raise StageTransitionError(
            "active and promoted runtime selection must use the same stable pointer path"
        )
    selection_state = _selection_file_state(
        promote_selection_file,
        active_inputs,
        project=project,
        config_dir=active_config_dir,
        secrets_dir=secrets_dir,
        active_service_names=set(_runtime_service_inventory(active_compose)),
    )
    active_mount_identity = (
        selection_state["document"]["runtimeConfigIdentity"]
        if selection_state["document"] is not None
        else _runtime_config_identity_for_compose(active_compose)
    )
    active_mount_identity = _validate_runtime_config_identity(
        active_mount_identity,
        expected_services=set(_runtime_service_inventory(active_compose)),
        label="active runtime configuration identity",
    )
    candidate_mount_identity = _candidate_runtime_config_identity(
        active_identity=active_mount_identity,
        candidate_compose=candidate_compose,
        target_services=set(targets),
    )
    _verify_runtime_config_identity_sources(active_mount_identity, "active runtime configuration")
    _verify_runtime_config_identity_sources(
        candidate_mount_identity, "candidate runtime configuration"
    )
    edge_service = _runtime_service_inventory(active_compose).get("teams-ingress")
    if not isinstance(edge_service, dict):
        raise StageTransitionError("active Teams ingress service is missing")
    active_services = _live_service_inventory(
        compose=active_compose,
        image_lock=active_lock,
        docker_prefix=docker_prefix,
        mount_identity_by_service=active_mount_identity,
    )
    edge_container = active_services.get("teams-ingress")
    navigator_container = active_services.get("navigator")
    if not isinstance(edge_container, dict) or not isinstance(navigator_container, dict):
        raise StageTransitionError("active Teams ingress or Navigator container is missing")
    edge_status = runtime_update._current_edge_status(
        docker_prefix, edge_container["containerId"], timeout_seconds=15
    )
    runtime_update._validate_edge_status(edge_status)
    if edge_status["paused"] is not False or edge_status["dead_count"] != 0:
        raise StageTransitionError(
            "Teams ingress must be accepting with no dead-letter events before planning"
        )
    navigator_status = runtime_update._current_navigator_status(
        docker_prefix, navigator_container["containerId"], timeout_seconds=15
    )
    runtime_update._validate_navigator_status(navigator_status)
    if navigator_status["mode"] != "accepting":
        raise StageTransitionError("Navigator must be accepting before transition planning")

    operation_id = str(uuid.uuid4())
    backup_path = backup_dir / f"{operation_id}.navigator-sessions.sqlite3"
    if backup_path.exists() or backup_path.is_symlink():
        raise StageTransitionError("online SQLite backup output already exists")
    selection_backup_path = backup_dir / f"{operation_id}.active-selection.previous.json"
    if selection_backup_path.exists() or selection_backup_path.is_symlink():
        raise StageTransitionError("active selection backup output already exists")
    plan: dict[str, Any] = {
        "schemaVersion": PLAN_SCHEMA,
        "operationId": operation_id,
        "phase": phase,
        "createdAtUtc": _utc_now(),
        "project": project,
        "activeInputs": active_inputs,
        "candidateInputs": candidate_inputs,
        "candidateInputPaths": {
            "connectorAuthTaskId": connector_auth_task_id,
            "notificationAnchorFile": (
                str(notification_anchor_file.resolve(strict=True))
                if notification_anchor_file is not None
                else None
            ),
            "notificationProvenanceFile": (
                str(notification_provenance_file.resolve(strict=True))
                if notification_provenance_file is not None
                else None
            ),
        },
        "activeConfigDir": str(active_config_dir),
        "candidateConfigDir": str(candidate_config_dir),
        "activeSecretsDir": str(secrets_dir),
        "journalDir": str(journal_dir.resolve()),
        "candidateSelectionFile": str(promote_selection_file.resolve()),
        "selectionBackupPath": str(selection_backup_path),
        "activeSelection": selection_state,
        "runtimeConfigIdentity": {
            "active": active_mount_identity,
            "candidate": candidate_mount_identity,
        },
        "candidateServices": target_images,
        "targets": targets,
        "activeServices": active_services,
        "edgeDelivery": {
            "containerId": edge_container["containerId"],
            "imageRef": edge_container["imageRef"],
            "imageId": edge_container["imageId"],
            "revision": edge_status["revision"],
            "deadCount": edge_status["dead_count"],
        },
        "navigatorDrain": {
            "containerId": navigator_container["containerId"],
            "imageRef": navigator_container["imageRef"],
            "imageId": navigator_container["imageId"],
            "instanceId": navigator_status["instanceId"],
            "generation": navigator_status["generation"],
        },
        "database": {
            "service": "navigator-persistence",
            "volume": "navigator-sessions",
            "containerPath": DATABASE_PATH,
            "backupPath": str(backup_path),
        },
        "candidatePreflight": {
            "mode": candidate_preflight["mode"],
            "phase": candidate_preflight["connectorAuthPhase"],
            "credentialReadOnlyRuntimeReady": candidate_preflight["credentialReadOnlyRuntimeReady"],
            "fullCloudWriteDeploymentReady": candidate_preflight["fullCloudWriteDeploymentReady"],
            "lifecycleAcceptance": candidate_preflight["lifecycleAcceptance"],
            "expectedProviderScopeDigests": candidate_preflight["expectedProviderScopeDigests"],
            "huggingfaceRepositoryScopeDigestSha256": candidate_preflight[
                "huggingfaceRepositoryScopeDigestSha256"
            ],
            "notificationInputs": {
                key: value
                for key, value in candidate_preflight.items()
                if key.startswith("credentialNotification")
            },
            "maxMemoryMiB": max_memory_mib,
            "maxCpus": max_cpus,
            "requiredFreeGiB": required_free_gib,
            "minimumMemAvailableMiB": minimum_mem_available_mib,
        },
        "policy": {
            "dockerPrefix": docker_prefix,
            "updateOrder": targets,
            "noDependencies": True,
            "pull": False,
            "build": False,
            "forceRecreate": True,
            "preserveNamedVolumes": True,
            "databaseRollback": "IMAGE_CONFIG_ONLY_NEVER_RESTORE_SNAPSHOT",
            "unknownDockerMutation": "RECOVERY_REQUIRED_NO_REPLAY",
            "pauseHolder": operation_id,
            "drainTimeoutSeconds": drain_timeout_seconds,
            "healthTimeoutSeconds": health_timeout_seconds,
            "selectionPromotion": "AFTER_ALL_HEALTH_AND_RESOURCE_READBACK",
        },
    }
    plan["planSha256"] = _sha256_bytes(_canonical_json(plan))
    _validate_plan(plan)
    try:
        runtime_update._write_private_json(plan_path, plan, require_artifact=True)
    except runtime_update.RuntimeUpdateError as error:
        raise StageTransitionError(
            "stage transition plan could not be persisted privately"
        ) from error
    return {"operationId": operation_id, "planSha256": plan["planSha256"]}


def _validate_plan(plan: dict[str, Any]) -> None:
    """Validate the closed private plan and its digest before any mutation."""

    required = {
        "schemaVersion",
        "operationId",
        "phase",
        "createdAtUtc",
        "project",
        "activeInputs",
        "candidateInputs",
        "candidateInputPaths",
        "activeConfigDir",
        "candidateConfigDir",
        "activeSecretsDir",
        "journalDir",
        "candidateSelectionFile",
        "selectionBackupPath",
        "activeSelection",
        "runtimeConfigIdentity",
        "candidateServices",
        "targets",
        "activeServices",
        "edgeDelivery",
        "navigatorDrain",
        "database",
        "candidatePreflight",
        "policy",
        "planSha256",
    }
    if set(plan) != required or plan.get("schemaVersion") != PLAN_SCHEMA:
        raise StageTransitionError("stage transition plan has an unsupported closed schema")
    for field in ("activeServices", "candidateServices"):
        if not isinstance(plan.get(field), dict):
            raise StageTransitionError(f"stage transition {field} record is malformed")
    if plan.get("phase") not in PHASES or not isinstance(plan.get("project"), str):
        raise StageTransitionError("stage transition plan identity is malformed")
    if (
        not isinstance(plan.get("targets"), list)
        or not plan["targets"]
        or any(not isinstance(name, str) for name in plan["targets"])
        or len(set(plan["targets"])) != len(plan["targets"])
    ):
        raise StageTransitionError("stage transition plan lacks its ordered service targets")
    if plan["targets"] != [name for name in SERVICE_ORDER if name in set(plan["targets"])]:
        raise StageTransitionError("stage transition targets are not in canonical dependency order")
    active_runtime_services = set(plan.get("activeServices", {}))
    added_services = {
        name
        for name, row in plan.get("candidateServices", {}).items()
        if isinstance(row, dict) and row.get("added") is True
    }
    runtime_identity = plan.get("runtimeConfigIdentity")
    if not isinstance(runtime_identity, dict) or set(runtime_identity) != {"active", "candidate"}:
        raise StageTransitionError("runtime configuration identity is malformed")
    _validate_runtime_config_identity(
        runtime_identity.get("active"),
        expected_services=active_runtime_services,
        label="active runtime configuration identity",
    )
    _validate_runtime_config_identity(
        runtime_identity.get("candidate"),
        expected_services=active_runtime_services | added_services,
        label="candidate runtime configuration identity",
    )
    if not isinstance(plan.get("planSha256"), str) or not SHA256_RE.fullmatch(plan["planSha256"]):
        raise StageTransitionError("stage transition plan digest is malformed")
    unsigned = dict(plan)
    expected_digest = unsigned.pop("planSha256")
    if _sha256_bytes(_canonical_json(unsigned)) != expected_digest:
        raise StageTransitionError("stage transition plan digest does not match its contents")
    for field in ("activeInputs", "candidateInputs"):
        if not isinstance(plan.get(field), dict):
            raise StageTransitionError(f"stage transition {field} record is malformed")
    readiness = plan.get("candidatePreflight")
    if (
        not isinstance(readiness, dict)
        or readiness.get("mode") != "connector-auth-readonly"
        or readiness.get("credentialReadOnlyRuntimeReady") is not True
        or readiness.get("fullCloudWriteDeploymentReady") is not False
        or readiness.get("lifecycleAcceptance") != "NOT_RUN"
    ):
        raise StageTransitionError("stage transition candidate readiness record is malformed")


def _verify_record(record: Any, label: str) -> None:
    """Require a frozen file input to retain its exact canonical path and digest."""

    if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
        raise StageTransitionError(f"{label} plan record is malformed")
    path_text = record.get("path")
    expected_digest = record.get("sha256")
    if (
        not isinstance(path_text, str)
        or not isinstance(expected_digest, str)
        or SHA256_RE.fullmatch(expected_digest) is None
    ):
        raise StageTransitionError(f"{label} plan record is malformed")
    path = Path(path_text)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise StageTransitionError(f"{label} disappeared after planning") from error
    if str(resolved) != path_text or _sha256_file(resolved) != expected_digest:
        raise StageTransitionError(f"{label} changed after planning; create a new plan")


def _verify_file_group(records: Any, label: str) -> None:
    """Recheck an ordered path/digest list from one immutable plan group."""

    if not isinstance(records, list) or not records:
        raise StageTransitionError(f"{label} file group is missing")
    for index, record in enumerate(records):
        _verify_record(record, f"{label} {index + 1}")


def _verify_directory_group(path_text: Any, records: Any, label: str) -> None:
    """Recheck a complete private directory manifest without changing any file."""

    if not isinstance(path_text, str) or not isinstance(records, list):
        raise StageTransitionError(f"{label} directory fingerprint is malformed")
    path = _validate_private_directory(Path(path_text), label, expected_uid=10001)
    if _directory_manifest(path, label) != records:
        raise StageTransitionError(f"{label} directory changed after planning; create a new plan")


def _verify_all_inputs(plan: dict[str, Any]) -> None:
    """Recheck every active and candidate input before any pause or Docker mutation."""

    active = plan["activeInputs"]
    candidate = plan["candidateInputs"]
    for side, rows in (("active", active), ("candidate", candidate)):
        _verify_file_group(rows.get("composeFiles"), f"{side} Compose")
        _verify_file_group(rows.get("envFiles"), f"{side} env")
        _verify_record(rows.get("imageLock"), f"{side} image lock")
        config_path = plan["activeConfigDir"] if side == "active" else plan["candidateConfigDir"]
        _verify_directory_group(config_path, rows.get("configDirectoryFiles"), f"{side} config")
    _verify_secret_manifest(active.get("secretFiles"), "active")
    _verify_runtime_config_identity_sources(
        plan["runtimeConfigIdentity"]["active"], "active runtime configuration"
    )
    _verify_runtime_config_identity_sources(
        plan["runtimeConfigIdentity"]["candidate"], "candidate runtime configuration"
    )
    for key, value in candidate.items():
        if key in {
            "composeFiles",
            "envFiles",
            "imageLock",
            "configDirectoryFiles",
            "secretFiles",
        }:
            continue
        if value is None:
            continue
        if isinstance(value, dict) and "path" in value:
            _verify_record(value, f"candidate {key}")
        elif isinstance(value, list):
            _verify_file_group(value, f"candidate {key}")
        else:
            raise StageTransitionError(f"candidate {key} fingerprint is malformed")
    _verify_secret_manifest(candidate.get("secretFiles"), "candidate")


def _check_selection_unchanged(plan: dict[str, Any]) -> None:
    """Require the stable runtime selector to remain at its planned prior revision."""

    record = plan.get("activeSelection")
    if not isinstance(record, dict) or set(record) != {"path", "sha256", "document"}:
        raise StageTransitionError("active runtime selection plan record is malformed")
    path = Path(record["path"])
    if path.is_symlink():
        raise StageTransitionError("runtime selection path became a symlink")
    current_digest = _sha256_file(path) if path.exists() else None
    if current_digest != record.get("sha256"):
        raise StageTransitionError("active runtime selection changed after planning")


def _candidate_preflight(plan: dict[str, Any]) -> dict[str, Any]:
    """Repeat static and read-only identity gates against the frozen candidate before mutation."""

    paths = plan.get("candidateInputPaths")
    readiness = plan.get("candidatePreflight")
    inputs = plan.get("candidateInputs")
    if (
        not isinstance(paths, dict)
        or not isinstance(readiness, dict)
        or not isinstance(inputs, dict)
    ):
        raise StageTransitionError("candidate preflight input record is malformed")
    phase = plan["phase"]
    evidence = inputs["connectorAuthEvidence"]
    seed = inputs["huggingfaceSeed"]
    key_file = inputs["runpodKey"]
    try:
        result = preflight.run_preflight(
            mode="connector-auth-readonly",
            compose_files=[Path(row["path"]) for row in inputs["composeFiles"]],
            env_files=[Path(row["path"]) for row in inputs["envFiles"]],
            image_lock=Path(inputs["imageLock"]["path"]),
            secrets_dir=Path(plan["activeSecretsDir"]),
            config_dir=Path(plan["candidateConfigDir"]),
            github_hf_evidence=None,
            azure_gcp_ledger=None,
            runpod_ledger=None,
            azure_gcp_api_evidence=None,
            runpod_api_evidence=None,
            docker_prefix=plan["policy"]["dockerPrefix"],
            max_memory_mib=readiness["maxMemoryMiB"],
            max_cpus=readiness["maxCpus"],
            required_free_gib=readiness["requiredFreeGiB"],
            minimum_mem_available_mib=readiness["minimumMemAvailableMiB"],
            connector_auth_phase=phase,
            connector_auth_evidence=Path(evidence["path"]),
            connector_auth_task_id=paths["connectorAuthTaskId"],
            huggingface_seed_file=Path(seed["path"]),
            candidate_runpod_key_file=Path(key_file["path"]),
            notification_anchor_file=(
                Path(paths["notificationAnchorFile"])
                if paths.get("notificationAnchorFile") is not None
                else None
            ),
            notification_provenance_file=(
                Path(paths["notificationProvenanceFile"])
                if paths.get("notificationProvenanceFile") is not None
                else None
            ),
        )
    except (OSError, preflight.PreflightError, BuildError, subprocess.SubprocessError) as error:
        raise StageTransitionError("fresh connector auth read-only preflight failed") from error
    if (
        result.get("credentialReadOnlyRuntimeReady") is not True
        or result.get("fullCloudWriteDeploymentReady") is not False
        or result.get("lifecycleAcceptance") != "NOT_RUN"
        or result.get("expectedProviderScopeDigests")
        != readiness.get("expectedProviderScopeDigests")
        or result.get("huggingfaceRepositoryScopeDigestSha256")
        != readiness.get("huggingfaceRepositoryScopeDigestSha256")
    ):
        raise StageTransitionError(
            "candidate read-only profile or provider scope changed after planning"
        )
    expected_notification = readiness.get("notificationInputs", {})
    observed_notification = {
        key: value for key, value in result.items() if key.startswith("credentialNotification")
    }
    if observed_notification != expected_notification:
        raise StageTransitionError("notification anchor/profile changed after planning")
    return result


def _make_journal(plan: dict[str, Any], plan_path: Path) -> dict[str, Any]:
    """Create the initial secret-free durable transition journal."""

    return {
        "schemaVersion": JOURNAL_SCHEMA,
        "operationId": plan["operationId"],
        "planSha256": plan["planSha256"],
        "planPath": str(plan_path.resolve(strict=True)),
        "project": plan["project"],
        "phase": "PRECHECKED",
        "state": "RUNNING",
        "createdAtUtc": _utc_now(),
        "updatedAtUtc": _utc_now(),
        "targets": plan["targets"],
        "completedTargets": [],
        "edge": {
            "containerId": plan["edgeDelivery"]["containerId"],
            "initialRevision": plan["edgeDelivery"]["revision"],
            "initialDeadCount": plan["edgeDelivery"]["deadCount"],
            "latestDeadCount": plan["edgeDelivery"]["deadCount"],
            "pauseHolder": plan["operationId"],
            "pauseIntent": False,
            "pauseRevision": None,
            "resumeRevision": None,
            "pauseHistory": [],
            "repauseExpectedRevision": None,
            "repauseHolder": None,
        },
        "navigator": {
            "initialContainerId": plan["navigatorDrain"]["containerId"],
            "initialInstanceId": plan["navigatorDrain"]["instanceId"],
            "initialGeneration": plan["navigatorDrain"]["generation"],
            "drainAttempt": None,
            "drainGeneration": None,
            "drained": False,
        },
        "database": {
            "backupPath": plan["database"]["backupPath"],
            "backupSha256": None,
            "snapshot": None,
            "postMigration": None,
            "restorePerformed": False,
        },
        "targetProgress": {
            service: {
                "phase": "PREPARED",
                "oldContainerId": plan["activeServices"].get(service, {}).get("containerId"),
                "oldImageId": plan["candidateServices"][service]["active"]["imageId"]
                if plan["candidateServices"][service]["active"] is not None
                else None,
                "candidateImageId": plan["candidateServices"][service]["candidate"]["imageId"],
                "added": plan["candidateServices"][service]["added"],
                "candidateContainerId": None,
                "applyIntent": False,
                "rollbackIntent": False,
                "rollbackContainerId": None,
            }
            for service in plan["targets"]
        },
        "selection": {
            "path": plan["candidateSelectionFile"],
            "backupPath": plan["selectionBackupPath"],
            "oldSha256": plan["activeSelection"]["sha256"],
            "newSha256": None,
            "promotionIntent": False,
            "promoted": False,
        },
        "resourceReadback": None,
        "lastErrorCode": None,
        "history": [{"phase": "PRECHECKED", "at": _utc_now()}],
    }


def _atomic_write_record(path: Path, document: dict[str, Any]) -> None:
    """Atomically replace an owner-only journal/selection without exposing partial JSON."""

    parent = path.parent
    if parent.is_symlink() or not parent.is_dir() or parent.resolve(strict=True) != parent:
        raise StageTransitionError("transaction record parent directory is unsafe")
    parent_metadata = parent.stat()
    if parent_metadata.st_uid != os.geteuid() or stat.S_IMODE(parent_metadata.st_mode) != 0o700:
        raise StageTransitionError(
            "transaction record parent must be owner-only and current-user-owned"
        )
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise StageTransitionError("transaction record path is unsafe")
    payload = _canonical_json(document) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise StageTransitionError(
            "transaction record could not be atomically persisted"
        ) from error


def _write_journal(
    path: Path, journal: dict[str, Any], phase: str, *, state: str | None = None
) -> None:
    """Persist phase intent before the next external side effect."""

    journal["phase"] = phase
    journal["updatedAtUtc"] = _utc_now()
    if state is not None:
        journal["state"] = state
    history = journal.get("history")
    if not isinstance(history, list):
        history = []
    history.append({"phase": phase, "at": journal["updatedAtUtc"]})
    journal["history"] = history[-200:]
    _atomic_write_record(path, journal)


def _read_journal(path: Path) -> dict[str, Any]:
    """Read a private stage transition journal and validate its identity fields."""

    try:
        metadata = path.lstat()
    except OSError as error:
        raise StageTransitionError("stage transition journal is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise StageTransitionError("stage transition journal must be an owner-only regular file")
    journal = _read_json(path, "stage transition journal")
    if (
        journal.get("schemaVersion") != JOURNAL_SCHEMA
        or not isinstance(journal.get("operationId"), str)
        or not isinstance(journal.get("planSha256"), str)
        or not SHA256_RE.fullmatch(journal["planSha256"])
        or not isinstance(journal.get("state"), str)
        or not isinstance(journal.get("phase"), str)
    ):
        raise StageTransitionError("stage transition journal is malformed")
    try:
        uuid.UUID(journal["operationId"])
    except ValueError as error:
        raise StageTransitionError("stage transition journal operation ID is invalid") from error
    return journal


def _journal_path(journal_dir: Path, operation_id: str) -> Path:
    """Return a UUID-constrained durable stage transition journal path."""

    try:
        canonical_id = str(uuid.UUID(operation_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise StageTransitionError("operation reference must be a UUID") from error
    return journal_dir / f"{canonical_id}.json"


def _journal_summary(journal: dict[str, Any]) -> dict[str, Any]:
    """Return safe transition status without paths, secrets, route values, or database names."""

    db = journal.get("database", {})
    database_snapshot = db.get("snapshot") if isinstance(db, dict) else None
    return {
        "schemaVersion": journal.get("schemaVersion"),
        "operationId": journal.get("operationId"),
        "planSha256": journal.get("planSha256"),
        "project": journal.get("project"),
        "state": journal.get("state"),
        "phase": journal.get("phase"),
        "targets": journal.get("targets"),
        "completedTargets": journal.get("completedTargets"),
        "databaseBackupSha256": db.get("backupSha256") if isinstance(db, dict) else None,
        "databaseUserVersion": database_snapshot.get("userVersion")
        if isinstance(database_snapshot, dict)
        else None,
        "selectionPromoted": journal.get("selection", {}).get("promoted"),
        "updatedAtUtc": journal.get("updatedAtUtc"),
        "lastErrorCode": journal.get("lastErrorCode"),
    }


def _selection_document_for_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Build the exact stable runtime selector that survives container/host restarts."""

    return {
        "schemaVersion": SELECTION_SCHEMA,
        "project": plan["project"],
        "operationId": plan["operationId"],
        "planSha256": plan["planSha256"],
        "phase": plan["phase"],
        "composeFiles": plan["candidateInputs"]["composeFiles"],
        "envFiles": plan["candidateInputs"]["envFiles"],
        "imageLock": plan["candidateInputs"]["imageLock"],
        "secretsDir": plan["activeSecretsDir"],
        "configDir": plan["candidateConfigDir"],
        "huggingfaceSeed": plan["candidateInputs"]["huggingfaceSeed"],
        "runpodKey": plan["candidateInputs"]["runpodKey"],
        "runtimeConfigIdentity": plan["runtimeConfigIdentity"]["candidate"],
        "updatedAtUtc": _utc_now(),
    }


def _atomic_sqlite_snapshot(
    *, plan: dict[str, Any], docker_prefix: list[str], journal: dict[str, Any], journal_path: Path
) -> dict[str, Any]:
    """Create a consistent SQLite online backup through Persistence's Python sqlite3 API."""

    service = journal["database"]
    container_id = plan["activeServices"]["navigator-persistence"]["containerId"]
    backup_path = Path(plan["database"]["backupPath"])
    if backup_path.exists() or backup_path.is_symlink():
        raise StageTransitionError("SQLite backup destination already exists")
    backup_temp = f"/tmp/cyrene-stage-transition-{plan['operationId']}.sqlite3"
    source_path = plan["database"]["containerPath"]
    script = (
        "import hashlib,json,os,sqlite3,sys;"
        "source_path,backup_path=sys.argv[1:3];"
        "source=sqlite3.connect('file:'+source_path+'?mode=ro',uri=True);"
        "target=sqlite3.connect(backup_path);"
        "source.backup(target);"
        "integrity=target.execute('PRAGMA integrity_check').fetchone()[0];"
        "version=target.execute('PRAGMA user_version').fetchone()[0];"
        "objects=target.execute(\"SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name\").fetchall();"
        "schema=json.dumps(objects,sort_keys=True,separators=(',',':')).encode();"
        "target.close();source.close();os.chmod(backup_path,0o600);"
        "print(json.dumps({'integrity':integrity,'userVersion':version,'schemaSha256':hashlib.sha256(schema).hexdigest(),'objects':[r[0]+':'+r[1] for r in objects]},separators=(',',':')))"
    )
    service["backupIntent"] = True
    _write_journal(journal_path, journal, "DATABASE_BACKUP_REQUESTED")
    report_text = runtime_update._checked_stdout(
        [
            *docker_prefix,
            "exec",
            container_id,
            "/opt/venv/bin/python",
            "-c",
            script,
            source_path,
            backup_temp,
        ],
        label="Navigator Persistence SQLite online backup",
        timeout_seconds=120,
    )
    report = runtime_update._decode_json_output(report_text, "Navigator Persistence backup report")
    if (
        not isinstance(report, dict)
        or report.get("integrity") != "ok"
        or isinstance(report.get("userVersion"), bool)
        or not isinstance(report.get("userVersion"), int)
        or not isinstance(report.get("schemaSha256"), str)
        or not SHA256_RE.fullmatch(report["schemaSha256"])
        or not isinstance(report.get("objects"), list)
        or any(not isinstance(name, str) for name in report["objects"])
    ):
        raise StageTransitionError(
            "Persistence online backup failed its integrity/schema read-back"
        )
    copy_result = runtime_update._run_command(
        [*docker_prefix, "cp", f"{container_id}:{backup_temp}", str(backup_path)],
        timeout_seconds=120,
        external_mutation=True,
    )
    if copy_result.returncode != 0:
        raise StageTransitionError(
            "SQLite online backup could not be copied to its private host artifact"
        )
    os.chmod(backup_path, 0o600)
    if os.geteuid() == 0:
        os.chown(backup_path, 0, 0)
    backup_sha256 = _sha256_file(backup_path)
    try:
        host_db = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
        integrity = host_db.execute("PRAGMA integrity_check").fetchone()[0]
        user_version = host_db.execute("PRAGMA user_version").fetchone()[0]
        objects = host_db.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
        host_db.close()
    except sqlite3.Error as error:
        raise StageTransitionError(
            "host SQLite online backup failed integrity verification"
        ) from error
    host_schema_digest = _sha256_bytes(_canonical_json(objects))
    if (
        integrity != "ok"
        or user_version != report["userVersion"]
        or host_schema_digest != report["schemaSha256"]
    ):
        raise StageTransitionError("host SQLite backup differs from the container online snapshot")
    report["objects"] = sorted(report["objects"])
    report["integrity"] = integrity
    report["backupSha256"] = backup_sha256
    service["backupIntent"] = False
    service["backupSha256"] = backup_sha256
    service["snapshot"] = report
    service["restorePerformed"] = False
    _write_journal(journal_path, journal, "DATABASE_BACKED_UP")
    # The temporary file is confined to the Persistence container's /tmp and is not source data.
    cleanup_script = (
        "from pathlib import Path;Path(__import__('sys').argv[1]).unlink(missing_ok=True)"
    )
    try:
        runtime_update._run_command(
            [
                *docker_prefix,
                "exec",
                container_id,
                "/opt/venv/bin/python",
                "-c",
                cleanup_script,
                backup_temp,
            ],
            timeout_seconds=15,
        )
    except runtime_update.RuntimeUpdateError:
        # The completed host snapshot is durable; failure to remove only this /tmp copy
        # does not change SQLite state and container recreation will discard it.
        pass
    return report


def _verify_active_baseline(
    plan: dict[str, Any], docker_prefix: list[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Re-render and re-inspect the active topology before pausing ingress."""

    inputs = plan["activeInputs"]
    compose_files = [Path(row["path"]) for row in inputs["composeFiles"]]
    env_files = [Path(row["path"]) for row in inputs["envFiles"]]
    lock_path = Path(inputs["imageLock"]["path"])
    active_lock = runtime_update._load_lock(lock_path)
    try:
        compose = preflight._compose_config(docker_prefix, compose_files, env_files)
    except (OSError, preflight.PreflightError, subprocess.SubprocessError) as error:
        raise StageTransitionError("active Compose could not be re-rendered") from error
    if compose.get("name") != plan["project"]:
        raise StageTransitionError("active Compose project changed after planning")
    services = _live_service_inventory(
        compose=compose,
        image_lock=active_lock,
        docker_prefix=docker_prefix,
        mount_identity_by_service=plan["runtimeConfigIdentity"]["active"],
    )
    if services != plan["activeServices"]:
        raise StageTransitionError(
            "active service IDs/images/network inventory changed after planning"
        )
    edge = runtime_update._current_edge_status(
        docker_prefix, plan["edgeDelivery"]["containerId"], timeout_seconds=15
    )
    runtime_update._validate_edge_status(edge)
    if (
        edge.get("paused") is not False
        or edge.get("revision") != plan["edgeDelivery"]["revision"]
        or edge.get("dead_count") != plan["edgeDelivery"]["deadCount"]
        or edge.get("dead_count") != 0
    ):
        raise StageTransitionError(
            "Teams ingress revision/dead-letter baseline changed after planning"
        )
    navigator = runtime_update._current_navigator_status(
        docker_prefix, plan["navigatorDrain"]["containerId"], timeout_seconds=15
    )
    runtime_update._validate_navigator_status(navigator)
    if (
        navigator.get("mode") != "accepting"
        or navigator.get("instanceId") != plan["navigatorDrain"]["instanceId"]
        or navigator.get("generation") != plan["navigatorDrain"]["generation"]
    ):
        raise StageTransitionError("Navigator process/drain generation changed after planning")
    return compose, active_lock


def _candidate_configs(
    plan: dict[str, Any], docker_prefix: list[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Re-render the frozen candidate Compose and image lock for one apply."""

    inputs = plan["candidateInputs"]
    compose_files = [Path(row["path"]) for row in inputs["composeFiles"]]
    env_files = [Path(row["path"]) for row in inputs["envFiles"]]
    lock = runtime_update._load_lock(Path(inputs["imageLock"]["path"]))
    try:
        compose = preflight._compose_config(docker_prefix, compose_files, env_files)
    except (OSError, preflight.PreflightError, subprocess.SubprocessError) as error:
        raise StageTransitionError("candidate Compose could not be re-rendered") from error
    if compose.get("name") != plan["project"]:
        raise StageTransitionError("candidate Compose project changed after planning")
    return compose, lock


def _compose_target_update(
    *,
    plan: dict[str, Any],
    service: str,
    use_candidate: bool,
    docker_prefix: list[str],
) -> subprocess.CompletedProcess[str]:
    """Run one ordered no-dependency, no-pull, no-build service replacement."""

    inputs = plan["candidateInputs"] if use_candidate else plan["activeInputs"]
    files = [Path(row["path"]) for row in inputs["composeFiles"]]
    env_files = [Path(row["path"]) for row in inputs["envFiles"]]
    return runtime_update._compose_up_stage_transition(
        docker_prefix=docker_prefix,
        project=plan["project"],
        compose_files=files,
        env_files=env_files,
        service=service,
        timeout_seconds=plan["policy"]["healthTimeoutSeconds"],
    )


def _update_target(
    *,
    plan: dict[str, Any],
    candidate_compose: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
    service_name: str,
) -> None:
    """Replace one service, persist its exact ID, then wait for its health read-back."""

    progress = journal["targetProgress"][service_name]
    if progress.get("phase") == "APPLIED":
        return
    candidate_service = _runtime_service_inventory(candidate_compose)[service_name]
    expected_image = plan["candidateServices"][service_name]["candidate"]
    progress["applyIntent"] = True
    progress["phase"] = "APPLY_REQUESTED"
    _write_journal(
        journal_path, journal, f"{service_name.upper().replace('-', '_')}_APPLY_REQUESTED"
    )
    try:
        result = _compose_target_update(
            plan=plan,
            service=service_name,
            use_candidate=True,
            docker_prefix=docker_prefix,
        )
    except runtime_update.RuntimeUpdateOutcomeUnknown as error:
        progress["phase"] = "APPLY_OUTCOME_UNKNOWN"
        journal["lastErrorCode"] = "docker_mutation_outcome_unknown"
        _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
        raise StageTransitionError(
            "Docker apply outcome is unknown; no service operation was replayed"
        ) from error
    except (OSError, runtime_update.RuntimeUpdateError, subprocess.SubprocessError) as error:
        progress["phase"] = "APPLY_OUTCOME_UNKNOWN"
        journal["lastErrorCode"] = "docker_mutation_outcome_unknown"
        _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
        raise StageTransitionError(
            "Docker apply did not return a safely reconcilable outcome"
        ) from error
    if result.returncode != 0:
        progress["phase"] = "APPLY_OUTCOME_UNKNOWN"
        journal["lastErrorCode"] = "docker_mutation_nonzero_exit"
        _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
        raise StageTransitionError(
            "Docker apply returned nonzero; no retry or automatic rollback was attempted"
        )
    try:
        current = _inspect_exact_service(
            project=plan["project"],
            service_name=service_name,
            expected_image=expected_image,
            has_healthcheck=bool(candidate_service.get("healthcheck")),
            docker_prefix=docker_prefix,
            allow_starting=True,
        )
    except StageTransitionError as error:
        progress["phase"] = "APPLY_READBACK_UNKNOWN"
        journal["lastErrorCode"] = "docker_mutation_readback_unknown"
        _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
        raise StageTransitionError(
            "Docker succeeded but exact service read-back is ambiguous"
        ) from error
    old_id = progress.get("oldContainerId")
    if isinstance(old_id, str) and current["containerId"] == old_id:
        progress["phase"] = "APPLY_READBACK_UNKNOWN"
        journal["lastErrorCode"] = "candidate_container_not_replaced"
        _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
        raise StageTransitionError("Compose did not replace the exact target container")
    progress["candidateContainerId"] = current["containerId"]
    progress["phase"] = "CONTAINER_READBACK"
    _write_journal(
        journal_path, journal, f"{service_name.upper().replace('-', '_')}_CONTAINER_READBACK"
    )
    current = _wait_for_target_service(
        project=plan["project"],
        service_name=service_name,
        expected_image=expected_image,
        has_healthcheck=bool(candidate_service.get("healthcheck")),
        timeout_seconds=plan["policy"]["healthTimeoutSeconds"],
        docker_prefix=docker_prefix,
    )
    progress["phase"] = "APPLIED"
    progress["applyIntent"] = False
    progress["healthyContainerId"] = current["containerId"]
    journal["completedTargets"].append(service_name)
    _write_journal(journal_path, journal, f"{service_name.upper().replace('-', '_')}_APPLIED")


def _current_selection_digest(path: Path) -> str | None:
    """Read only the current stable selector digest for local promotion reconciliation."""

    if path.is_symlink():
        raise StageTransitionError("runtime selection path became a symlink")
    return _sha256_file(path) if path.exists() else None


def _container_exists(container_id: str, docker_prefix: list[str]) -> bool:
    """Read Docker's exact-ID inventory; fail closed on an unavailable daemon."""

    if FULL_CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise StageTransitionError("container identity is malformed")
    output = runtime_update._checked_stdout(
        [
            *docker_prefix,
            "ps",
            "--all",
            "--no-trunc",
            "--filter",
            f"id={container_id}",
            "--format",
            "{{.ID}}",
        ],
        label="operation-owned container presence readback",
    )
    ids = [value.strip() for value in output.splitlines() if value.strip()]
    if any(FULL_CONTAINER_ID_RE.fullmatch(value) is None for value in ids):
        raise StageTransitionError("Docker returned a malformed exact-ID inventory")
    if not ids:
        return False
    if ids != [container_id]:
        raise StageTransitionError("Docker exact-ID inventory is ambiguous")
    return True


def _selection_promotion_uncertain(journal: dict[str, Any]) -> bool:
    """Detect a selector replacement that began but was not durably reconciled."""

    selection = journal.get("selection", {})
    if not isinstance(selection, dict):
        return True
    if selection.get("promoted") is True:
        return True
    if selection.get("promotionIntent") is not True:
        return False
    path_text = selection.get("path")
    if not isinstance(path_text, str):
        return True
    try:
        current = _current_selection_digest(Path(path_text))
    except (OSError, StageTransitionError):
        return True
    return current != selection.get("oldSha256")


def _promote_selection(
    *,
    plan: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
) -> dict[str, Any]:
    """Archive the old stable selector and atomically promote candidate desired state."""

    selection = journal["selection"]
    selection_path = Path(selection["path"])
    before_digest = _current_selection_digest(selection_path)
    if before_digest != selection.get("oldSha256"):
        raise StageTransitionError("active runtime selector changed before candidate promotion")
    active_doc = plan["activeSelection"].get("document")
    backup_path = Path(selection["backupPath"])
    if active_doc is not None:
        if backup_path.exists() or backup_path.is_symlink():
            raise StageTransitionError("runtime selector backup already exists")
        try:
            runtime_update._write_private_json(backup_path, active_doc, require_artifact=True)
        except runtime_update.RuntimeUpdateError as error:
            raise StageTransitionError("prior runtime selection could not be archived") from error
    selection_doc = _selection_document_for_plan(plan)
    selection_bytes = _canonical_json(selection_doc) + b"\n"
    selection["newSha256"] = _sha256_bytes(selection_bytes)
    selection["promotionIntent"] = True
    _write_journal(journal_path, journal, "RUNTIME_SELECTION_PROMOTION_REQUESTED")
    _atomic_write_record(selection_path, selection_doc)
    if _current_selection_digest(selection_path) != selection["newSha256"]:
        journal["lastErrorCode"] = "runtime_selection_readback_mismatch"
        _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
        raise StageTransitionError("candidate runtime selector failed its atomic read-back")
    selection["promotionIntent"] = False
    selection["promoted"] = True
    _write_journal(journal_path, journal, "RUNTIME_SELECTION_PROMOTED")
    return selection_doc


def _resume_live_navigator(
    *,
    plan: dict[str, Any],
    journal: dict[str, Any],
    candidate_compose: dict[str, Any],
    docker_prefix: list[str],
) -> None:
    """Require the replacement executor process to be accepting before ingress is resumed."""

    current = _live_service_inventory(
        compose=candidate_compose,
        image_lock=runtime_update._load_lock(Path(plan["candidateInputs"]["imageLock"]["path"])),
        docker_prefix=docker_prefix,
        mount_identity_by_service=plan["runtimeConfigIdentity"]["candidate"],
    )
    nav = current.get("navigator")
    if not isinstance(nav, dict):
        raise StageTransitionError("candidate Navigator container is missing")
    status = runtime_update._current_navigator_status(
        docker_prefix, nav["containerId"], timeout_seconds=15
    )
    runtime_update._validate_navigator_status(status)
    if (
        status.get("mode") != "accepting"
        or status.get("instanceId") == journal["navigator"]["initialInstanceId"]
    ):
        raise StageTransitionError(
            "replacement Navigator is not accepting in a fresh process instance"
        )


def _resume_old_executor_if_needed(
    *, plan: dict[str, Any], journal: dict[str, Any], docker_prefix: list[str]
) -> None:
    """Resume only the exact old drained process when no Navigator container was replaced."""

    progress = journal.get("targetProgress", {}).get("navigator", {})
    if isinstance(progress, dict) and progress.get("phase") == "APPLIED":
        return
    containers = runtime_update._list_project_navigator_containers(
        project=plan["project"], docker_prefix=docker_prefix
    )
    running = [row for row in containers if row.get("running") is True]
    if len(running) != 1 or not isinstance(running[0].get("containerId"), str):
        raise StageTransitionError("Navigator recovery inventory is ambiguous")
    current_id = running[0]["containerId"]
    status = runtime_update._current_navigator_status(
        docker_prefix,
        current_id,
        timeout_seconds=15,
    )
    runtime_update._validate_navigator_status(status)
    if (
        current_id == journal["navigator"]["initialContainerId"]
        and status.get("mode") == "draining"
    ):
        expected_generation = journal["navigator"]["initialGeneration"] + 1
        if (
            status.get("generation") != expected_generation
            or status.get("instanceId") != journal["navigator"]["initialInstanceId"]
        ):
            raise StageTransitionError("old Navigator drain generation cannot be safely resumed")
        runtime_update._resume_navigator_instance(
            docker_prefix=docker_prefix,
            container_id=current_id,
            instance_id=journal["navigator"]["initialInstanceId"],
            generation=expected_generation,
            timeout_seconds=30,
        )
    elif status.get("mode") != "accepting":
        raise StageTransitionError("Navigator did not return to an accepting generation")


def _resume_ingress_after_safe_state(
    *, journal: dict[str, Any], journal_path: Path, docker_prefix: list[str]
) -> None:
    """Release only this operation's durable ingress pause after service state is safe."""

    if (
        journal.get("edge", {}).get("pauseIntent") is True
        and journal["edge"].get("pauseRevision") is None
    ):
        runtime_update._recover_edge_pause(
            journal=journal,
            journal_path=journal_path,
            docker_prefix=docker_prefix,
            timeout_seconds=30,
        )
    runtime_update._resume_edge(
        journal=journal,
        journal_path=journal_path,
        docker_prefix=docker_prefix,
    )


def _rollback_known_mutations(
    *,
    plan: dict[str, Any],
    active_compose: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
) -> None:
    """Rollback only exact, recorded candidate containers; never restore SQLite bytes."""

    active_lock = runtime_update._load_lock(Path(plan["activeInputs"]["imageLock"]["path"]))
    active_images = _lock_image_ids(active_lock)
    for service_name in reversed(plan["targets"]):
        progress = journal["targetProgress"][service_name]
        candidate_id = progress.get("candidateContainerId")
        if not isinstance(candidate_id, str):
            if progress.get("applyIntent") is True:
                raise StageTransitionError(
                    "target apply outcome is unresolved; refusing rollback mutation"
                )
            continue
        expected_candidate = plan["candidateServices"][service_name]["candidate"]
        candidate_service = _runtime_service_inventory(
            preflight._compose_config(
                docker_prefix,
                [Path(row["path"]) for row in plan["candidateInputs"]["composeFiles"]],
                [Path(row["path"]) for row in plan["candidateInputs"]["envFiles"]],
            )
        ).get(service_name)
        if not isinstance(candidate_service, dict):
            raise StageTransitionError(
                f"candidate service {service_name} disappeared from its plan"
            )
        if progress["added"]:
            if not _container_exists(candidate_id, docker_prefix):
                progress["phase"] = "ROLLED_BACK"
                _write_journal(
                    journal_path,
                    journal,
                    f"{service_name.upper().replace('-', '_')}_ABSENT_READBACK",
                )
                continue
            current = runtime_update._checked_stdout(
                [*docker_prefix, "inspect", candidate_id],
                label="operation-owned provider container inspection",
            )
            inspected = runtime_update._decode_json_output(current, "created service inspection")
            if (
                not isinstance(inspected, list)
                or len(inspected) != 1
                or not isinstance(inspected[0], dict)
            ):
                raise StageTransitionError("created service ID inspection is ambiguous")
            row = inspected[0]
            config_row = row.get("Config")
            labels = config_row.get("Labels") if isinstance(config_row, dict) else None
            if (
                row.get("Id") != candidate_id
                or not isinstance(labels, dict)
                or labels.get("com.docker.compose.project") != plan["project"]
                or labels.get("com.docker.compose.service") != service_name
                or row.get("Image") != expected_candidate["imageId"]
                or config_row.get("Image") != expected_candidate["imageRef"]
            ):
                raise StageTransitionError(
                    "refusing to remove a container not owned by this exact operation"
                )
            progress["rollbackIntent"] = True
            progress["rollbackContainerId"] = candidate_id
            _write_journal(
                journal_path, journal, f"{service_name.upper().replace('-', '_')}_REMOVE_REQUESTED"
            )
            try:
                result = runtime_update._run_command(
                    [*docker_prefix, "rm", "-f", candidate_id],
                    timeout_seconds=60,
                    external_mutation=True,
                )
            except runtime_update.RuntimeUpdateOutcomeUnknown as error:
                raise StageTransitionError("owned-service removal outcome is unknown") from error
            if result.returncode != 0:
                raise StageTransitionError("owned-service removal returned nonzero")
            if _container_exists(candidate_id, docker_prefix):
                raise StageTransitionError(
                    "operation-owned provider container remained after rollback"
                )
            progress["phase"] = "ROLLED_BACK"
            progress["rollbackIntent"] = False
            _write_journal(
                journal_path, journal, f"{service_name.upper().replace('-', '_')}_REMOVED"
            )
            continue

        active_service = active_compose.get("services", {}).get(service_name)
        active_image = _service_image(active_compose, active_images, service_name)
        if not isinstance(active_service, dict):
            raise StageTransitionError(f"old service {service_name} is missing from active Compose")
        progress["rollbackIntent"] = True
        _write_journal(
            journal_path, journal, f"{service_name.upper().replace('-', '_')}_ROLLBACK_REQUESTED"
        )
        try:
            result = _compose_target_update(
                plan=plan,
                service=service_name,
                use_candidate=False,
                docker_prefix=docker_prefix,
            )
        except runtime_update.RuntimeUpdateOutcomeUnknown as error:
            raise StageTransitionError(f"{service_name} rollback outcome is unknown") from error
        if result.returncode != 0:
            raise StageTransitionError(f"{service_name} rollback returned nonzero")
        rolled_back = _wait_for_target_service(
            project=plan["project"],
            service_name=service_name,
            expected_image=active_image,
            has_healthcheck=bool(active_service.get("healthcheck")),
            timeout_seconds=plan["policy"]["healthTimeoutSeconds"],
            docker_prefix=docker_prefix,
        )
        if rolled_back["containerId"] == candidate_id:
            raise StageTransitionError(f"{service_name} rollback retained the candidate container")
        progress["rollbackIntent"] = False
        progress["rollbackContainerId"] = rolled_back["containerId"]
        progress["phase"] = "ROLLED_BACK"
        _write_journal(
            journal_path, journal, f"{service_name.upper().replace('-', '_')}_ROLLED_BACK"
        )

    active_services = _live_service_inventory(
        compose=active_compose,
        image_lock=active_lock,
        docker_prefix=docker_prefix,
        mount_identity_by_service=plan["runtimeConfigIdentity"]["active"],
    )
    runtime_update._verify_live_network_membership(active_compose, active_services, docker_prefix)
    _resume_old_executor_if_needed(plan=plan, journal=journal, docker_prefix=docker_prefix)
    _resume_ingress_after_safe_state(
        journal=journal,
        journal_path=journal_path,
        docker_prefix=docker_prefix,
    )
    _write_journal(journal_path, journal, "FAILED_ROLLED_BACK", state="FAILED_ROLLED_BACK")


def apply_plan_file(
    plan_path: Path,
    *,
    expected_digest: str,
) -> dict[str, Any]:
    """Apply the exact confirmed plan with drain, backup, ordered health checks, and safe recovery."""

    runtime_update._require_update_operator()
    if (
        plan_path.is_symlink()
        or not plan_path.is_file()
        or not preflight._private_artifact(plan_path)
    ):
        raise StageTransitionError("stage transition plan must be a private artifact file")
    metadata = plan_path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise StageTransitionError("stage transition plan must be an owner-only mode 0600 file")
    plan = _read_json(plan_path, "stage transition plan")
    _validate_plan(plan)
    if expected_digest != plan["planSha256"]:
        raise StageTransitionError("apply confirmation does not match the immutable plan digest")
    journal_dir = Path(plan["journalDir"])
    runtime_update._ensure_journal_directory(journal_dir, create=True)
    journal_path = _journal_path(journal_dir, plan["operationId"])
    if journal_path.exists() or journal_path.is_symlink():
        raise StageTransitionError("operation journal already exists; apply is never replayed")
    docker_prefix = plan["policy"]["dockerPrefix"]
    runtime_update._validate_docker_prefix(docker_prefix)
    with runtime_update._operation_lock(plan["project"]):
        _verify_all_inputs(plan)
        _check_selection_unchanged(plan)
        _candidate_preflight(plan)
        active_compose, _active_lock = _verify_active_baseline(plan, docker_prefix)
        candidate_compose, _candidate_lock = _candidate_configs(plan, docker_prefix)
        journal = _make_journal(plan, plan_path)
        _atomic_write_record(journal_path, journal)
        try:
            runtime_update._pause_edge(
                plan=plan,
                journal=journal,
                journal_path=journal_path,
                docker_prefix=docker_prefix,
            )
            runtime_update._drain_navigator(
                plan=plan,
                journal=journal,
                journal_path=journal_path,
                docker_prefix=docker_prefix,
            )
            _atomic_sqlite_snapshot(
                plan=plan,
                docker_prefix=docker_prefix,
                journal=journal,
                journal_path=journal_path,
            )
            for service_name in plan["targets"]:
                _update_target(
                    plan=plan,
                    candidate_compose=candidate_compose,
                    journal=journal,
                    journal_path=journal_path,
                    docker_prefix=docker_prefix,
                    service_name=service_name,
                )
            candidate_lock = runtime_update._load_lock(
                Path(plan["candidateInputs"]["imageLock"]["path"])
            )
            candidate_services = _live_service_inventory(
                compose=candidate_compose,
                image_lock=candidate_lock,
                docker_prefix=docker_prefix,
                mount_identity_by_service=plan["runtimeConfigIdentity"]["candidate"],
            )
            for service_name, old in plan["activeServices"].items():
                if (
                    service_name not in set(plan["targets"])
                    and candidate_services.get(service_name) != old
                ):
                    raise StageTransitionError(
                        f"untargeted service {service_name} changed during transition"
                    )
            runtime_update._verify_live_network_membership(
                candidate_compose, candidate_services, docker_prefix
            )
            _resume_live_navigator(
                plan=plan,
                journal=journal,
                candidate_compose=candidate_compose,
                docker_prefix=docker_prefix,
            )
            _validate_database_migration(
                plan=plan,
                journal=journal,
                journal_path=journal_path,
                docker_prefix=docker_prefix,
            )
            resource_readback = _validate_post_update_capacity(
                compose=candidate_compose,
                services=candidate_services,
                docker_prefix=docker_prefix,
                minimum_available_mib=plan["candidatePreflight"]["minimumMemAvailableMiB"],
            )
            journal["resourceReadback"] = resource_readback
            _write_journal(journal_path, journal, "RESOURCE_READBACK_VERIFIED")
            selection = _promote_selection(
                plan=plan,
                journal=journal,
                journal_path=journal_path,
            )
            _resume_ingress_after_safe_state(
                journal=journal,
                journal_path=journal_path,
                docker_prefix=docker_prefix,
            )
            _write_journal(journal_path, journal, "APPLIED", state="APPLIED")
            return {
                **_journal_summary(journal),
                "activeRuntimeSelection": _selection_summary(selection),
            }
        except runtime_update.RuntimeUpdateOutcomeUnknown as error:
            journal["lastErrorCode"] = "docker_mutation_outcome_unknown"
            _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
            raise StageTransitionError(
                "Docker mutation outcome is unknown; the ingress hold remains and no mutation was replayed"
            ) from error
        except StageTransitionError as error:
            if journal.get("state") == "RECOVERY_REQUIRED" or any(
                row.get("phase") in {"APPLY_OUTCOME_UNKNOWN", "APPLY_READBACK_UNKNOWN"}
                for row in journal.get("targetProgress", {}).values()
                if isinstance(row, dict)
            ):
                raise
            if _selection_promotion_uncertain(journal):
                journal["lastErrorCode"] = "runtime_selection_promotion_outcome_unknown"
                _write_journal(
                    journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED"
                )
                raise StageTransitionError(
                    "runtime selector promotion may have committed; ingress remains held for read-only recovery"
                ) from error
            try:
                _rollback_known_mutations(
                    plan=plan,
                    active_compose=active_compose,
                    journal=journal,
                    journal_path=journal_path,
                    docker_prefix=docker_prefix,
                )
            except (
                OSError,
                StageTransitionError,
                runtime_update.RuntimeUpdateError,
                subprocess.SubprocessError,
            ) as rollback_error:
                journal["lastErrorCode"] = "safe_rollback_not_verified"
                _write_journal(
                    journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED"
                )
                raise StageTransitionError(
                    "stage transition failed and rollback was not fully verified; ingress remains held"
                ) from rollback_error
            journal["lastErrorCode"] = "candidate_health_or_validation_failed"
            _write_journal(journal_path, journal, "FAILED_ROLLED_BACK", state="FAILED_ROLLED_BACK")
            raise StageTransitionError(
                "stage transition failed; old service images/config were restored without restoring SQLite"
            ) from error
        except (
            OSError,
            runtime_update.RuntimeUpdateError,
            preflight.PreflightError,
            subprocess.SubprocessError,
        ) as error:
            if journal.get("state") == "RECOVERY_REQUIRED":
                raise StageTransitionError(
                    "stage transition requires read-only recovery; no uncertain mutation was replayed"
                ) from error
            if _selection_promotion_uncertain(journal):
                journal["lastErrorCode"] = "runtime_selection_promotion_outcome_unknown"
                _write_journal(
                    journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED"
                )
                raise StageTransitionError(
                    "runtime selector promotion may have committed; ingress remains held for read-only recovery"
                ) from error
            try:
                _rollback_known_mutations(
                    plan=plan,
                    active_compose=active_compose,
                    journal=journal,
                    journal_path=journal_path,
                    docker_prefix=docker_prefix,
                )
            except (
                OSError,
                StageTransitionError,
                runtime_update.RuntimeUpdateError,
                subprocess.SubprocessError,
            ) as rollback_error:
                journal["lastErrorCode"] = "safe_rollback_not_verified"
                _write_journal(
                    journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED"
                )
                raise StageTransitionError(
                    "stage transition failed and rollback was not fully verified; ingress remains held"
                ) from rollback_error
            journal["lastErrorCode"] = "candidate_health_or_validation_failed"
            _write_journal(journal_path, journal, "FAILED_ROLLED_BACK", state="FAILED_ROLLED_BACK")
            raise StageTransitionError(
                "stage transition failed; old service images/config were restored without restoring SQLite"
            ) from error


def operation_status(journal_dir: Path, operation_id: str) -> dict[str, Any]:
    """Read one durable transition journal without contacting Docker or application APIs."""

    runtime_update._require_update_operator()
    runtime_update._ensure_journal_directory(journal_dir, create=False)
    return _journal_summary(_read_journal(_journal_path(journal_dir, operation_id)))


def resume_operation(operation_id: str, *, journal_dir: Path) -> dict[str, Any]:
    """Perform read-only recovery reconciliation; never repeat a Compose mutation."""

    runtime_update._require_update_operator()
    runtime_update._ensure_journal_directory(journal_dir, create=False)
    journal_path = _journal_path(journal_dir, operation_id)
    journal = _read_journal(journal_path)
    if journal.get("state") in {"APPLIED", "FAILED_ROLLED_BACK", "RECOVERY_REQUIRED"}:
        return _journal_summary(journal)
    plan_path = Path(journal.get("planPath", ""))
    plan = _read_json(plan_path, "stage transition plan")
    _validate_plan(plan)
    if plan.get("operationId") != journal.get("operationId") or plan.get(
        "planSha256"
    ) != journal.get("planSha256"):
        raise StageTransitionError("journal no longer matches its exact private plan")
    with runtime_update._operation_lock(plan["project"]):
        progress = journal.get("targetProgress")
        if not isinstance(progress, dict):
            raise StageTransitionError("stage transition target journal is malformed")
        unresolved = [
            name
            for name, row in progress.items()
            if isinstance(row, dict)
            and row.get("applyIntent") is True
            and row.get("phase") not in {"APPLIED", "ROLLED_BACK"}
        ]
        if unresolved or journal.get("selection", {}).get("promotionIntent") is True:
            journal["lastErrorCode"] = "docker_or_selection_outcome_unresolved"
            _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
            return _journal_summary(journal)
        # Reconciliation can report observed state, but it never issues another Docker command.
        try:
            _verify_all_inputs(plan)
            candidate_compose, candidate_lock = _candidate_configs(
                plan, plan["policy"]["dockerPrefix"]
            )
            candidate_state = _live_service_inventory(
                compose=candidate_compose,
                image_lock=candidate_lock,
                docker_prefix=plan["policy"]["dockerPrefix"],
                mount_identity_by_service=plan["runtimeConfigIdentity"]["candidate"],
            )
            expected_ids = {
                name: row.get("healthyContainerId")
                for name, row in progress.items()
                if isinstance(row, dict) and row.get("phase") == "APPLIED"
            }
            if any(
                not isinstance(expected_id, str)
                or candidate_state.get(name, {}).get("containerId") != expected_id
                for name, expected_id in expected_ids.items()
            ):
                raise StageTransitionError("recorded target container identity changed")
            selection = _current_selection_digest(Path(journal["selection"]["path"]))
            if journal["selection"].get("promoted") is True and selection != journal[
                "selection"
            ].get("newSha256"):
                raise StageTransitionError(
                    "promoted runtime selection no longer matches its journal"
                )
        except (
            OSError,
            StageTransitionError,
            runtime_update.RuntimeUpdateError,
            subprocess.SubprocessError,
        ):
            journal["lastErrorCode"] = "read_only_reconciliation_mismatch"
            _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
            return _journal_summary(journal)
        journal["lastErrorCode"] = "operation_interrupted_no_mutation_replayed"
        _write_journal(journal_path, journal, "RECOVERY_REQUIRED", state="RECOVERY_REQUIRED")
        return _journal_summary(journal)


def _add_common_plan_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the immutable active/candidate inputs and bounded policy settings."""

    parser.add_argument("--phase", choices=sorted(PHASES), required=True)
    parser.add_argument("--active-compose-file", type=Path, action="append", required=True)
    parser.add_argument("--active-env-file", type=Path, action="append", required=True)
    parser.add_argument("--active-image-lock", type=Path, required=True)
    parser.add_argument("--candidate-compose-file", type=Path, action="append", required=True)
    parser.add_argument("--candidate-env-file", type=Path, action="append", required=True)
    parser.add_argument("--candidate-image-lock", type=Path, required=True)
    parser.add_argument("--secrets-dir", type=Path, required=True)
    parser.add_argument("--active-config-dir", type=Path, required=True)
    parser.add_argument("--candidate-config-dir", type=Path, required=True)
    parser.add_argument("--candidate-huggingface-seed", type=Path, required=True)
    parser.add_argument("--candidate-runpod-key-file", type=Path, required=True)
    parser.add_argument("--connector-auth-evidence", type=Path, required=True)
    parser.add_argument("--connector-auth-task-id", required=True)
    parser.add_argument("--notification-anchor-file", type=Path)
    parser.add_argument("--notification-provenance-file", type=Path)
    parser.add_argument("--active-selection", type=Path)
    parser.add_argument("--promote-selection", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--docker-prefix",
        choices=("docker", "sudo-n-docker"),
        default="sudo-n-docker",
    )
    parser.add_argument("--drain-timeout-seconds", type=int, default=300)
    parser.add_argument("--health-timeout-seconds", type=int, default=300)
    parser.add_argument("--max-memory-mib", type=int, default=3584)
    parser.add_argument("--max-cpus", type=float, default=1.95)
    parser.add_argument("--required-free-gib", type=float, default=2.0)
    parser.add_argument("--minimum-mem-available-mib", type=int, default=MEMORY_RESERVE_DEFAULT_MIB)


def _build_parser() -> argparse.ArgumentParser:
    """Build the closed stage transition plan/apply/status/recovery interface."""

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan", help="create a private, read-only transition plan")
    _add_common_plan_arguments(plan_parser)
    apply_parser = commands.add_parser("apply", help="apply one confirmed transition plan")
    apply_parser.add_argument("--plan", type=Path, required=True)
    apply_parser.add_argument("--confirm-plan-sha256", required=True)
    status_parser = commands.add_parser("status", help="read one durable transition journal")
    status_parser.add_argument("--operation-id", required=True)
    status_parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)
    resume_parser = commands.add_parser(
        "resume", help="reconcile one interrupted operation read-only"
    )
    resume_parser.add_argument("--operation-id", required=True)
    resume_parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one formal plan/apply/status/recovery operation without unbounded Docker actions."""

    arguments = _build_parser().parse_args(argv)
    try:
        if arguments.command == "plan":
            docker_prefix = (
                ["docker"] if arguments.docker_prefix == "docker" else ["sudo", "-n", "docker"]
            )
            runtime_update._validate_docker_prefix(docker_prefix)
            result = create_plan(
                phase=arguments.phase,
                active_compose_files=[path.resolve() for path in arguments.active_compose_file],
                active_env_files=[path.resolve() for path in arguments.active_env_file],
                active_image_lock=arguments.active_image_lock.resolve(),
                candidate_compose_files=[
                    path.resolve() for path in arguments.candidate_compose_file
                ],
                candidate_env_files=[path.resolve() for path in arguments.candidate_env_file],
                candidate_image_lock=arguments.candidate_image_lock.resolve(),
                secrets_dir=arguments.secrets_dir.resolve(),
                active_config_dir=arguments.active_config_dir.resolve(),
                candidate_config_dir=arguments.candidate_config_dir.resolve(),
                candidate_huggingface_seed=arguments.candidate_huggingface_seed.resolve(),
                candidate_runpod_key_file=arguments.candidate_runpod_key_file.resolve(),
                connector_auth_evidence=arguments.connector_auth_evidence.resolve(),
                connector_auth_task_id=arguments.connector_auth_task_id,
                notification_anchor_file=(
                    arguments.notification_anchor_file.resolve()
                    if arguments.notification_anchor_file
                    else None
                ),
                notification_provenance_file=(
                    arguments.notification_provenance_file.resolve()
                    if arguments.notification_provenance_file
                    else None
                ),
                active_selection_file=(
                    arguments.active_selection.resolve()
                    if arguments.active_selection is not None
                    else None
                ),
                promote_selection_file=arguments.promote_selection.resolve(),
                backup_dir=arguments.backup_dir.resolve(),
                journal_dir=arguments.journal_dir.resolve(),
                plan_path=arguments.output.resolve(),
                docker_prefix=docker_prefix,
                drain_timeout_seconds=arguments.drain_timeout_seconds,
                health_timeout_seconds=arguments.health_timeout_seconds,
                max_memory_mib=arguments.max_memory_mib,
                max_cpus=arguments.max_cpus,
                required_free_gib=arguments.required_free_gib,
                minimum_mem_available_mib=arguments.minimum_mem_available_mib,
            )
        elif arguments.command == "apply":
            result = apply_plan_file(
                arguments.plan.resolve(),
                expected_digest=arguments.confirm_plan_sha256,
            )
        elif arguments.command == "status":
            result = operation_status(arguments.journal_dir.resolve(), arguments.operation_id)
        else:
            result = resume_operation(
                arguments.operation_id,
                journal_dir=arguments.journal_dir.resolve(),
            )
    except (
        OSError,
        StageTransitionError,
        runtime_update.RuntimeUpdateError,
        preflight.PreflightError,
        BuildError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Teams cloud stage transition blocked: {error}", file=sys.stderr)
        return 3 if arguments.command in {"apply", "resume"} else 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
