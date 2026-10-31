"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 runtime_update.py                                                    │
│  Module: tooling.deploy.teams_cloud.runtime_update                      │
│  Role: Plan and apply bounded, drain-aware Navigator/Exchange updates.   │
│                                                                          │
│  模块职责：安全计划并执行有界的 Navigator/Exchange 热更新。              │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import preflight
from build_arm64 import BuildError

PLAN_SCHEMA = "cyrene-teams-runtime-update-plan-v1"
JOURNAL_SCHEMA = "cyrene-teams-runtime-update-journal-v1"
DEFAULT_JOURNAL_DIR = Path("/var/lib/cyrene/runtime-update")
GLOBAL_LOCK_DIRECTORY = Path("/run/cyrene-runtime-update")
GLOBAL_LOCK_OWNER_UID = 0
TARGET_ORDER = ("github-issues", "exchange", "navigator")
STAGE_TRANSITION_TARGET_ORDER = (
    "navigator-persistence",
    "account-center",
    "huggingface",
    "cloud-management-phase2",
    "exchange",
    "navigator",
)
# Keep the one-shot stage Compose primitive aligned with both formal phase orders.
# The legacy runtime-update CLI continues to use ALLOWED_TARGETS below.
FORMAL_STAGE_TRANSITION_TARGETS = frozenset(
    {
        *STAGE_TRANSITION_TARGET_ORDER,
        "microsoft-graph",
        "teams-provider",
        "teams-ingress",
        "workspace-authority",
    }
)
LEGACY_TARGETS_ONLY = ["exchange", "navigator"]
ALLOWED_TARGETS = set(TARGET_ORDER)
LOCK_SERVICE_FOR_TARGET = {
    "github-issues": "github-issues-provider",
    "huggingface": "huggingface-provider",
    "navigator-persistence": "navigator-persistence",
    "navigator": "dsh-executor",
    "account-center": "account-center",
    "exchange": "exchange",
    "cloud-management-phase2": "cloud-management",
    "workspace-authority": "workspace-authority",
}
CONTAINER_ID_RE = re.compile(r"[0-9a-f]{12,64}")
FULL_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}")
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
RUNTIME_ACTIVITY_MARKERS = {
    "CYRENE_RUNTIME_ACTIVITY_SOURCE_ID",
    "CYRENE_RUNTIME_ACTIVITY_SOURCE_TOKEN_FILE",
    "CYRENE_RUNTIME_ACTIVITY_CATALOG_GENERATION",
    "CYRENE_RUNTIME_MAINTENANCE_SOCKET",
}
DEFAULT_RUNTIME_ACTIVITY_TOKEN_FILE = "/run/secrets/cyrene-runtime-activity-token"
DEFAULT_RUNTIME_MAINTENANCE_SOCKET = "/run/cyrene/runtime-maintenance.sock"
NAVIGATOR_TOKEN_FILE = "/run/secrets/navigator-session-token"
NAVIGATOR_BASE_URL = "http://127.0.0.1:8080"
TEAMS_INGRESS_BASE_URL = "http://127.0.0.1:3978"
NAVIGATOR_DRAIN_FIELDS = {
    "mode",
    "generation",
    "token",
    "startedAt",
    "instanceId",
    "countsComplete",
    "activeTasks",
    "queuedTasks",
    "dispatches",
    "notifications",
    "activeWorkDrained",
}
EDGE_STATUS_FIELDS = {
    "paused",
    "revision",
    "active_delivery_count",
    "queued_count",
    "leased_count",
    "retry_wait_count",
    "delivered_count",
    "rejected_count",
    "dead_count",
    "pending_bytes",
    "retained_body_bytes",
    "oldest_pending_at",
    "last_delivered_at",
}


class RuntimeUpdateError(ValueError):
    """A deployment input, runtime identity, or update phase failed closed."""


class RuntimeUpdateOutcomeUnknown(RuntimeUpdateError):
    """A timed-out Docker mutation may still be running in the daemon."""


def _utc_now() -> str:
    """Return a canonical UTC timestamp for durable operation records."""

    return datetime.now(UTC).isoformat()


def _canonical_json(value: Any) -> bytes:
    """Serialize JSON deterministically for plan and source identity digests."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _reject_json_constant(value: str) -> None:
    """Reject non-standard NaN/Infinity values before they enter a signed plan."""

    raise ValueError(f"non-finite JSON number is unsupported: {value}")


def _sha256_bytes(value: bytes) -> str:
    """Return a lowercase SHA-256 digest for one in-memory canonical document."""

    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    """Hash one regular non-symlink input without returning its contents."""

    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeUpdateError(f"update input is unavailable: {path.name}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeUpdateError(f"update input is not a regular file: {path.name}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise RuntimeUpdateError(f"update input could not be read: {path.name}") from error
    return digest.hexdigest()


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    """Read bounded duplicate-key-free JSON without echoing file contents."""

    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeUpdateError(f"{label} is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeUpdateError(f"{label} is not a regular file")
    try:
        if metadata.st_size > 16 * 1024 * 1024:
            raise RuntimeUpdateError(f"{label} exceeds the input size limit")
        raw = path.read_bytes()
        if len(raw) > 16 * 1024 * 1024:
            raise RuntimeUpdateError(f"{label} exceeds the input size limit")
        document = json.loads(
            raw,
            object_pairs_hook=preflight._unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        if isinstance(error, RuntimeUpdateError):
            raise
        raise RuntimeUpdateError(f"{label} is invalid JSON") from error
    if not isinstance(document, dict):
        raise RuntimeUpdateError(f"{label} must be a JSON object")
    return document


def _record_files(paths: list[Path], label: str) -> list[dict[str, str]]:
    """Bind ordered CLI input files to normalized paths and content digests."""

    if not paths:
        raise RuntimeUpdateError(f"at least one {label} is required")
    return [
        {"path": str(path.resolve(strict=True)), "sha256": _sha256_file(path)} for path in paths
    ]


def _input_paths(inputs: dict[str, Any], key: str) -> list[Path]:
    """Return validated file paths from one closed plan input group."""

    rows = inputs.get(key)
    if not isinstance(rows, list) or not rows:
        raise RuntimeUpdateError(f"plan {key} input list is missing")
    result: list[Path] = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"path", "sha256"}
            or not isinstance(row.get("path"), str)
            or not isinstance(row.get("sha256"), str)
            or SHA256_RE.fullmatch(row["sha256"]) is None
        ):
            raise RuntimeUpdateError(f"plan {key} input record is malformed")
        path = Path(row["path"])
        if (
            path.resolve(strict=True) != path
            or path.is_symlink()
            or not path.is_file()
            or _sha256_file(path) != row["sha256"]
        ):
            raise RuntimeUpdateError(f"plan {key} input changed after review")
        result.append(path)
    return result


def _verify_input_hash(inputs: dict[str, Any], key: str) -> Path:
    """Recheck one plan-bound file and return its path."""

    rows = inputs.get(key)
    if (
        not isinstance(rows, dict)
        or set(rows) != {"path", "sha256"}
        or not isinstance(rows.get("path"), str)
        or not isinstance(rows.get("sha256"), str)
        or SHA256_RE.fullmatch(rows["sha256"]) is None
    ):
        raise RuntimeUpdateError(f"plan {key} input record is malformed")
    path = Path(rows["path"])
    if (
        path.resolve(strict=True) != path
        or path.is_symlink()
        or not path.is_file()
        or _sha256_file(path) != rows["sha256"]
    ):
        raise RuntimeUpdateError(f"plan {key} input changed after review")
    return path


def _load_lock(path: Path) -> dict[str, Any]:
    """Read the public image-lock metadata required for exact service mapping."""

    lock = _read_json_object(path, "image lock")
    if (
        lock.get("schemaVersion") != "cyrene-teams-cloud-image-lock-v1"
        or lock.get("targetPlatform") != "linux/arm64"
        or lock.get("state") not in {"LOCAL_ONLY", "PUBLISHED"}
        or not isinstance(lock.get("images"), list)
        or not isinstance(lock.get("externalImages"), dict)
        or not isinstance(lock.get("sources"), dict)
    ):
        raise RuntimeUpdateError("image lock does not match the canonical ARM64 schema")
    return lock


def _image_rows_by_service(lock: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index unique built-image lock rows by their canonical build-service names."""

    rows: dict[str, dict[str, Any]] = {}
    references: set[str] = set()
    for row in lock.get("images", []):
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("service"), str)
            or not isinstance(row.get("image"), str)
            or not isinstance(row.get("imageId"), str)
            or IMAGE_ID_RE.fullmatch(row["imageId"]) is None
        ):
            raise RuntimeUpdateError("image lock contains a malformed built-image row")
        if row["service"] in rows or row["image"] in references:
            raise RuntimeUpdateError("image lock contains duplicate service or image references")
        rows[row["service"]] = row
        references.add(row["image"])
    return rows


def _target_images(
    compose: dict[str, Any], lock: dict[str, Any], targets: list[str]
) -> dict[str, dict[str, Any]]:
    """Bind each Compose service target to its canonical locked image row."""

    services = compose.get("services")
    if not isinstance(services, dict):
        raise RuntimeUpdateError("rendered Compose configuration has no services")
    rows = _image_rows_by_service(lock)
    result: dict[str, dict[str, Any]] = {}
    for target in targets:
        service = services.get(target)
        lock_service = LOCK_SERVICE_FOR_TARGET[target]
        row = rows.get(lock_service)
        if (
            not isinstance(service, dict)
            or not isinstance(service.get("image"), str)
            or not isinstance(row, dict)
            or row.get("image") != service.get("image")
        ):
            raise RuntimeUpdateError(
                f"Compose {target} image does not match its canonical {lock_service} lock row"
            )
        result[target] = {
            "lockService": lock_service,
            "image": row["image"],
            "imageId": row["imageId"],
            "sourceCommits": row.get("sourceCommits", {}),
        }
    return result


def _source_identity_sha256(lock: dict[str, Any], targets: list[str]) -> str:
    """Digest source commits/trees for the targets without serializing raw build inputs."""

    rows = _image_rows_by_service(lock)
    target_sources = {
        target: rows[LOCK_SERVICE_FOR_TARGET[target]].get("sourceCommits", {}) for target in targets
    }
    return _sha256_bytes(
        _canonical_json({"sources": lock.get("sources"), "targets": target_sources})
    )


def _validate_lock_pair(
    active: dict[str, Any], candidate: dict[str, Any], targets: list[str]
) -> None:
    """Require exact non-target image rows and external image locks across the candidate."""

    active_rows = _image_rows_by_service(active)
    candidate_rows = _image_rows_by_service(candidate)
    if set(active_rows) != set(candidate_rows):
        raise RuntimeUpdateError("candidate lock changes the built-service inventory")
    target_lock_services = {LOCK_SERVICE_FOR_TARGET[target] for target in targets}
    for service, row in active_rows.items():
        if service not in target_lock_services and candidate_rows[service] != row:
            raise RuntimeUpdateError(f"candidate lock changes non-target image service {service}")
    if candidate.get("externalImages") != active.get("externalImages"):
        raise RuntimeUpdateError("candidate lock changes a non-target external image")


def _validate_runtime_activity_static(compose: dict[str, Any]) -> None:
    """Reject Compose markers or mounts that require the unsupported maintenance broker."""

    services = compose.get("services", {})
    for service_name in ("navigator", "exchange"):
        service = services.get(service_name) if isinstance(services, dict) else None
        if not isinstance(service, dict):
            raise RuntimeUpdateError(f"required service {service_name} is missing")
        environment = service.get("environment", {})
        if not isinstance(environment, dict):
            raise RuntimeUpdateError(f"{service_name} environment is malformed")
        mapped_names = {
            source.split("=", 1)[0]
            for source in str(environment.get("CYRENE_SECRET_FILE_MAP", "")).split(",")
            if "=" in source
        }
        if RUNTIME_ACTIVITY_MARKERS & (set(environment) | mapped_names):
            raise RuntimeUpdateError("managed RuntimeMaintenance configuration is unsupported")
        mounted_targets = {
            row.get("target")
            for field in ("secrets", "volumes")
            for row in service.get(field, [])
            if isinstance(row, dict)
        }
        configured_secret_targets = {
            "/run/secrets/" + str(row.get("target", row.get("source", "")))
            for row in service.get("secrets", [])
            if isinstance(row, dict)
        }
        if (
            DEFAULT_RUNTIME_ACTIVITY_TOKEN_FILE in mounted_targets
            or DEFAULT_RUNTIME_ACTIVITY_TOKEN_FILE in configured_secret_targets
            or DEFAULT_RUNTIME_MAINTENANCE_SOCKET in mounted_targets
        ):
            raise RuntimeUpdateError("managed RuntimeMaintenance mounts are unsupported")


def _validate_compose_pair(
    active: dict[str, Any], candidate: dict[str, Any], targets: list[str]
) -> None:
    """Allow candidate Compose to differ only in target service image references."""

    if active.get("name") != candidate.get("name"):
        raise RuntimeUpdateError("active and candidate Compose project names differ")
    active_without_images = json.loads(json.dumps(active))
    candidate_without_images = json.loads(json.dumps(candidate))
    for target in targets:
        for config in (active_without_images, candidate_without_images):
            services = config.get("services")
            service = services.get(target) if isinstance(services, dict) else None
            if not isinstance(service, dict) or not isinstance(service.get("image"), str):
                raise RuntimeUpdateError(f"Compose target {target} is missing its image reference")
            service.pop("image")
    if active_without_images != candidate_without_images:
        raise RuntimeUpdateError(
            "candidate Compose changes configuration outside the selected immutable images"
        )


def _network_services(compose: dict[str, Any], service_name: str) -> set[str]:
    """Return one service's declared Compose network names."""

    services = compose.get("services", {})
    service = services.get(service_name) if isinstance(services, dict) else None
    if not isinstance(service, dict):
        return set()
    networks = service.get("networks", {})
    if isinstance(networks, list):
        return set(networks)
    if isinstance(networks, dict):
        return set(networks)
    return set()


def _validate_standalone_callers(compose: dict[str, Any]) -> None:
    """Reject extra Compose callers, published service ports, and independent workers."""

    services = compose.get("services", {})
    if not isinstance(services, dict):
        raise RuntimeUpdateError("rendered Compose configuration has no services")
    for required in ("navigator", "exchange", "teams-ingress", "navigator-persistence"):
        if required not in services:
            raise RuntimeUpdateError(f"standalone Teams service {required} is missing")
    _validate_navigator_stop_contract(services["navigator"])
    if not isinstance(services["exchange"], dict):
        raise RuntimeUpdateError("Exchange Compose service is malformed")
    if services["navigator"].get("ports") or services["exchange"].get("ports"):
        raise RuntimeUpdateError("Navigator and Exchange must remain private Compose services")
    expected_networks = {
        "navigator-exchange-private": {"navigator", "exchange"},
        "navigator-teams-private": {"navigator", "teams-ingress"},
        "navigator-persistence-private": {"navigator", "navigator-persistence"},
        "navigator-egress": {"navigator"},
        "teams-egress": {"teams-ingress"},
        "teams-proxy-private": {"teams-ingress", "teams-public-ingress"},
        "account-private": {"exchange", "account-center"},
        "cloud-management-private": {"exchange", "cloud-management-phase2"},
        "exchange-authority-private": {"exchange", "workspace-authority"},
        "exchange-computer-private": {"exchange", "computer-runtime-proxy"},
        "computer-runtime-private": {"computer-runtime", "computer-runtime-proxy"},
        "github-exchange-private": {"exchange", "github-issues"},
        "teams-provider-private": {"exchange", "teams-provider"},
    }
    exact_network_members = {
        "navigator-exchange-private": {"navigator", "exchange"},
        "navigator-teams-private": {"navigator", "teams-ingress"},
        "navigator-persistence-private": {"navigator", "navigator-persistence"},
        "navigator-egress": {"navigator"},
        "teams-egress": {"teams-ingress"},
        "teams-proxy-private": {"teams-ingress", "teams-public-ingress"},
    }
    for service_name in ("navigator", "exchange"):
        actual_networks = _network_services(compose, service_name)
        if not actual_networks:
            raise RuntimeUpdateError(f"{service_name} has no private network attachment")
        for network in actual_networks:
            allowed = expected_networks.get(network)
            if allowed is None:
                raise RuntimeUpdateError(f"unsupported network attached to {service_name}")
            members = {name for name in services if network in _network_services(compose, name)}
            if not members <= allowed or (
                network in exact_network_members and members != exact_network_members[network]
            ):
                raise RuntimeUpdateError(f"network {network} has an unsupported service consumer")
    for network, allowed in expected_networks.items():
        if network in compose.get("networks", {}) and any(
            name in services and network in _network_services(compose, name) for name in allowed
        ):
            members = {name for name in services if network in _network_services(compose, name)}
            if not members <= allowed or (
                network in exact_network_members and members != exact_network_members[network]
            ):
                raise RuntimeUpdateError(f"network {network} has an unsupported service consumer")
    navigator_exchange_callers = {
        name
        for name in services
        if "navigator-exchange-private" in _network_services(compose, name)
    }
    if navigator_exchange_callers != {"navigator", "exchange"}:
        raise RuntimeUpdateError(
            "Navigator must be the only business caller on its private Exchange network"
        )


def _validate_navigator_stop_contract(service: Any) -> None:
    """Require the known 30s executor drain + 5s teardown budget and 40s grace."""

    if not isinstance(service, dict):
        raise RuntimeUpdateError("Navigator Compose service is malformed")
    environment = service.get("environment")
    if not isinstance(environment, dict):
        raise RuntimeUpdateError("Navigator Compose environment is malformed")
    if (
        environment.get("CYRENE_EXECUTOR_DRAIN_TIMEOUT_MS") != "30000"
        or environment.get("CYRENE_EXECUTOR_ABORT_CLEANUP_TIMEOUT_MS") != "5000"
        or service.get("stop_grace_period") != "40s"
    ):
        raise RuntimeUpdateError(
            "Navigator stop budget must remain 30s drain + 5s cleanup under a 40s Compose grace"
        )


def _run_preflight(
    *,
    compose_files: list[Path],
    env_files: list[Path],
    image_lock: Path,
    secrets_dir: Path,
    config_dir: Path,
    docker_prefix: list[str],
    max_memory_mib: int,
    max_cpus: float,
    required_free_gib: float,
    minimum_mem_available_mib: int,
    recovery_only: bool = False,
) -> dict[str, Any]:
    """Run canonical Stage 1 preflight, or its config-only recovery variant."""

    try:
        result = preflight.run_preflight(
            mode="stage1",
            compose_files=compose_files,
            env_files=env_files,
            image_lock=image_lock,
            secrets_dir=secrets_dir,
            config_dir=config_dir,
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
            allow_owned_staging_ingress=True,
            recovery_only=recovery_only,
        )
    except (OSError, preflight.PreflightError, BuildError, subprocess.SubprocessError) as error:
        raise RuntimeUpdateError("canonical Stage 1 preflight blocked the update plan") from error
    if result.get("mode") != "stage1":
        raise RuntimeUpdateError("canonical Stage 1 preflight did not return READY")
    if recovery_only:
        if (
            result.get("deploymentReady") is not False
            or result.get("status") != "RECOVERY_CONFIGURATION_ONLY"
        ):
            raise RuntimeUpdateError("canonical recovery preflight returned an invalid status")
    elif result.get("deploymentReady") is not True:
        raise RuntimeUpdateError("canonical Stage 1 preflight did not return READY")
    return result


def _plan_digest(plan: dict[str, Any]) -> str:
    """Hash every reviewed plan field except its self-referential digest."""

    document = {key: value for key, value in plan.items() if key != "planSha256"}
    return _sha256_bytes(_canonical_json(document))


def _validate_plan(plan: dict[str, Any]) -> None:
    """Validate the closed versioned plan shape and its content digest."""

    expected_fields = {
        "schemaVersion",
        "operationId",
        "createdAtUtc",
        "project",
        "targets",
        "activeInputs",
        "candidateInputs",
        "activeSourceIdentitySha256",
        "candidateSourceIdentitySha256",
        "candidateImages",
        "activeServices",
        "edgeDelivery",
        "navigatorDrain",
        "policy",
        "preflight",
        "planSha256",
    }
    if set(plan) != expected_fields or plan.get("schemaVersion") != PLAN_SCHEMA:
        raise RuntimeUpdateError("runtime update plan does not match the supported closed schema")
    try:
        operation_id = str(uuid.UUID(str(plan.get("operationId"))))
    except (ValueError, TypeError, AttributeError) as error:
        raise RuntimeUpdateError("runtime update plan operation ID is invalid") from error
    if operation_id != plan.get("operationId"):
        raise RuntimeUpdateError("runtime update plan operation ID is not canonical")
    if (
        not isinstance(plan.get("project"), str)
        or not plan["project"]
        or not isinstance(plan.get("targets"), list)
        or not plan["targets"]
        or any(not isinstance(target, str) for target in plan["targets"])
        or len(plan["targets"]) != len(set(plan["targets"]))
        or set(plan["targets"]) - ALLOWED_TARGETS
        or plan["targets"] != sorted(plan["targets"])
    ):
        raise RuntimeUpdateError("runtime update plan project or target scope is invalid")
    if (
        not isinstance(plan.get("planSha256"), str)
        or SHA256_RE.fullmatch(plan["planSha256"]) is None
        or _plan_digest(plan) != plan["planSha256"]
    ):
        raise RuntimeUpdateError("runtime update plan digest does not match its reviewed contents")
    if not isinstance(plan.get("candidateImages"), dict) or set(plan["candidateImages"]) != set(
        plan["targets"]
    ):
        raise RuntimeUpdateError("runtime update plan candidate image set is invalid")
    try:
        created_at = datetime.fromisoformat(plan.get("createdAtUtc", ""))
    except (TypeError, ValueError) as error:
        raise RuntimeUpdateError("runtime update plan creation timestamp is malformed") from error
    if created_at.tzinfo is None or created_at.utcoffset() != UTC.utcoffset(created_at):
        raise RuntimeUpdateError("runtime update plan creation timestamp must be UTC")
    if not isinstance(plan.get("activeServices"), dict) or not plan["activeServices"]:
        raise RuntimeUpdateError("runtime update plan lacks the active service inventory")
    for group_name in ("activeInputs", "candidateInputs"):
        group = plan.get(group_name)
        if not isinstance(group, dict) or set(group) != {"composeFiles", "envFiles", "imageLock"}:
            raise RuntimeUpdateError(f"runtime update {group_name} records are malformed")
        for list_name in ("composeFiles", "envFiles"):
            records = group.get(list_name)
            if not isinstance(records, list) or not records:
                raise RuntimeUpdateError(f"runtime update {group_name} {list_name} are missing")
            for record in records:
                if (
                    not isinstance(record, dict)
                    or set(record) != {"path", "sha256"}
                    or not isinstance(record.get("path"), str)
                    or not isinstance(record.get("sha256"), str)
                    or SHA256_RE.fullmatch(record["sha256"]) is None
                ):
                    raise RuntimeUpdateError(f"runtime update {group_name} file hash is malformed")
        lock = group.get("imageLock")
        if (
            not isinstance(lock, dict)
            or set(lock) != {"path", "sha256"}
            or not isinstance(lock.get("path"), str)
            or not isinstance(lock.get("sha256"), str)
            or SHA256_RE.fullmatch(lock["sha256"]) is None
        ):
            raise RuntimeUpdateError(f"runtime update {group_name} image lock hash is malformed")
    for field in ("activeSourceIdentitySha256", "candidateSourceIdentitySha256"):
        if not isinstance(plan.get(field), str) or SHA256_RE.fullmatch(plan[field]) is None:
            raise RuntimeUpdateError("runtime update source identity digest is malformed")
    policy = plan.get("policy")
    policy_fields = {
        "scope",
        "managedRuntimeMaintenance",
        "targetsOnly",
        "updateOrder",
        "edgeImageReplacement",
        "composeNoDeps",
        "dockerPrefix",
        "drainTimeoutSeconds",
        "healthTimeoutSeconds",
    }
    if not isinstance(policy, dict) or set(policy) != policy_fields:
        raise RuntimeUpdateError("runtime update policy is malformed")
    expected_order = [target for target in TARGET_ORDER if target in plan["targets"]]
    targets_only = policy.get("targetsOnly") if isinstance(policy, dict) else None
    legacy_targets_only_allowed = (
        targets_only == LEGACY_TARGETS_ONLY and "github-issues" not in plan["targets"]
    )
    if (
        policy.get("scope") != "standalone-teams"
        or policy.get("managedRuntimeMaintenance") != "UNMANAGED_REQUIRED"
        or (targets_only != list(TARGET_ORDER) and not legacy_targets_only_allowed)
        or policy.get("updateOrder") != expected_order
        or policy.get("edgeImageReplacement") is not False
        or policy.get("composeNoDeps") is not True
        or policy.get("dockerPrefix")
        not in (
            ["docker"],
            ["/usr/bin/docker"],
            ["sudo", "-n", "docker"],
            ["sudo", "-n", "/usr/bin/docker"],
        )
        or isinstance(policy.get("drainTimeoutSeconds"), bool)
        or not isinstance(policy.get("drainTimeoutSeconds"), int)
        or not 60 <= policy["drainTimeoutSeconds"] <= 900
        or isinstance(policy.get("healthTimeoutSeconds"), bool)
        or not isinstance(policy.get("healthTimeoutSeconds"), int)
        or not 30 <= policy["healthTimeoutSeconds"] <= 600
    ):
        raise RuntimeUpdateError("runtime update policy is outside the supported contract")
    preflight_record = plan.get("preflight")
    preflight_fields = {
        "mode",
        "activeDeploymentReady",
        "candidateDeploymentReady",
        "secretsDir",
        "configDir",
        "maxMemoryMiB",
        "maxCpus",
        "requiredFreeGiB",
        "minimumMemAvailableMiB",
    }
    if (
        not isinstance(preflight_record, dict)
        or set(preflight_record) != preflight_fields
        or preflight_record.get("mode") != "stage1"
        or preflight_record.get("activeDeploymentReady") is not True
        or preflight_record.get("candidateDeploymentReady") is not True
        or any(
            not isinstance(preflight_record.get(field), str)
            for field in ("secretsDir", "configDir")
        )
    ):
        raise RuntimeUpdateError("runtime update preflight evidence is malformed")
    if any(
        field not in preflight_record
        or isinstance(preflight_record.get(field), bool)
        or not isinstance(preflight_record.get(field), (int, float))
        or not math.isfinite(preflight_record[field])
        for field in ("maxMemoryMiB", "maxCpus", "requiredFreeGiB", "minimumMemAvailableMiB")
    ):
        raise RuntimeUpdateError("runtime update preflight limits are malformed")
    if (
        preflight_record["maxMemoryMiB"] <= 0
        or preflight_record["maxCpus"] <= 0
        or preflight_record["requiredFreeGiB"] < 0
        or preflight_record["minimumMemAvailableMiB"] <= 0
    ):
        raise RuntimeUpdateError("runtime update preflight limits must be positive")
    edge = plan.get("edgeDelivery")
    if (
        not isinstance(edge, dict)
        or set(edge) != {"service", "containerId", "imageRef", "imageId", "revision", "deadCount"}
        or edge.get("service") != "teams-ingress"
        or not isinstance(edge.get("containerId"), str)
        or FULL_CONTAINER_ID_RE.fullmatch(edge["containerId"]) is None
        or not isinstance(edge.get("imageRef"), str)
        or not edge["imageRef"]
        or not isinstance(edge.get("imageId"), str)
        or IMAGE_ID_RE.fullmatch(edge["imageId"]) is None
        or isinstance(edge.get("revision"), bool)
        or not isinstance(edge.get("revision"), int)
        or edge["revision"] < 0
        or isinstance(edge.get("deadCount"), bool)
        or not isinstance(edge.get("deadCount"), int)
        or edge["deadCount"] != 0
    ):
        raise RuntimeUpdateError("runtime update edge baseline is malformed or has dead messages")
    navigator = plan.get("navigatorDrain")
    if (
        not isinstance(navigator, dict)
        or set(navigator) != {"containerId", "imageRef", "imageId", "instanceId", "generation"}
        or not isinstance(navigator.get("containerId"), str)
        or FULL_CONTAINER_ID_RE.fullmatch(navigator["containerId"]) is None
        or not isinstance(navigator.get("imageRef"), str)
        or not navigator["imageRef"]
        or not isinstance(navigator.get("imageId"), str)
        or IMAGE_ID_RE.fullmatch(navigator["imageId"]) is None
        or not isinstance(navigator.get("instanceId"), str)
        or not navigator["instanceId"]
        or isinstance(navigator.get("generation"), bool)
        or not isinstance(navigator.get("generation"), int)
        or navigator["generation"] < 0
    ):
        raise RuntimeUpdateError("runtime update Navigator baseline is malformed")
    for target, row in plan["candidateImages"].items():
        if (
            not isinstance(row, dict)
            or set(row) != {"lockService", "image", "imageId", "sourceCommits"}
            or row.get("lockService") != LOCK_SERVICE_FOR_TARGET[target]
            or not isinstance(row.get("image"), str)
            or not row["image"]
            or not isinstance(row.get("imageId"), str)
            or IMAGE_ID_RE.fullmatch(row["imageId"]) is None
            or not isinstance(row.get("sourceCommits"), dict)
        ):
            raise RuntimeUpdateError(f"runtime update candidate image for {target} is malformed")
    if not isinstance(plan.get("activeServices"), dict) or not plan["activeServices"]:
        raise RuntimeUpdateError("runtime update plan lacks the active service inventory")
    for name, row in plan["activeServices"].items():
        if (
            not isinstance(name, str)
            or not isinstance(row, dict)
            or set(row) != {"containerId", "imageRef", "imageId", "health", "networkNames"}
            or not isinstance(row.get("containerId"), str)
            or FULL_CONTAINER_ID_RE.fullmatch(row["containerId"]) is None
            or not isinstance(row.get("imageRef"), str)
            or not row["imageRef"]
            or not isinstance(row.get("imageId"), str)
            or IMAGE_ID_RE.fullmatch(row["imageId"]) is None
            or not isinstance(row.get("health"), str)
            or row["health"] not in {"healthy", "running"}
            or not isinstance(row.get("networkNames"), list)
            or not row["networkNames"]
            or any(not isinstance(network, str) for network in row["networkNames"])
            or len(row["networkNames"]) != len(set(row["networkNames"]))
        ):
            raise RuntimeUpdateError(f"active service baseline for {name} is malformed")
    ingress = plan["activeServices"].get("teams-ingress")
    navigator_service = plan["activeServices"].get("navigator")
    if (
        not isinstance(ingress, dict)
        or edge["containerId"] != ingress["containerId"]
        or edge["imageRef"] != ingress["imageRef"]
        or edge["imageId"] != ingress["imageId"]
        or not isinstance(navigator_service, dict)
        or navigator["containerId"] != navigator_service["containerId"]
        or navigator["imageRef"] != navigator_service["imageRef"]
        or navigator["imageId"] != navigator_service["imageId"]
    ):
        raise RuntimeUpdateError(
            "runtime update control baselines do not match active container inventory"
        )


def _load_plan_file(path: Path, expected_digest: str | None = None) -> dict[str, Any]:
    """Read one owner-only private plan and verify its self-digest and optional confirmation."""

    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeUpdateError("runtime update plan is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or path.resolve(strict=True) != path
        or not preflight._private_artifact(path)
    ):
        raise RuntimeUpdateError("runtime update plan must be a regular private artifact")
    plan = _read_json_object(path, "runtime update plan")
    _validate_plan(plan)
    if expected_digest is not None and expected_digest != plan["planSha256"]:
        raise RuntimeUpdateError("explicit plan digest does not match the reviewed plan")
    return plan


def _resolve_plan_inputs(
    plan: dict[str, Any],
    *,
    docker_prefix: list[str],
    run_full_preflight: bool,
    inspect_active: bool = True,
    recovery_only: bool = False,
) -> dict[str, Any]:
    """Re-hash and render every input, then enforce the active/candidate image contract."""

    policy = plan["policy"]
    if docker_prefix != policy.get("dockerPrefix"):
        raise RuntimeUpdateError("Docker command prefix differs from the reviewed plan")
    active_inputs = plan["activeInputs"]
    candidate_inputs = plan["candidateInputs"]
    active_compose_files = _input_paths(active_inputs, "composeFiles")
    active_env_files = _input_paths(active_inputs, "envFiles")
    active_image_lock_path = _verify_input_hash(active_inputs, "imageLock")
    candidate_compose_files = _input_paths(candidate_inputs, "composeFiles")
    candidate_env_files = _input_paths(candidate_inputs, "envFiles")
    candidate_image_lock_path = _verify_input_hash(candidate_inputs, "imageLock")
    active_lock = _load_lock(active_image_lock_path)
    candidate_lock = _load_lock(candidate_image_lock_path)
    _validate_lock_pair(active_lock, candidate_lock, plan["targets"])
    try:
        active_compose = preflight._compose_config(
            docker_prefix, active_compose_files, active_env_files
        )
        candidate_compose = preflight._compose_config(
            docker_prefix, candidate_compose_files, candidate_env_files
        )
    except (OSError, preflight.PreflightError, subprocess.SubprocessError) as error:
        raise RuntimeUpdateError("active or candidate Compose could not be rendered") from error
    _validate_compose_pair(active_compose, candidate_compose, plan["targets"])
    _validate_runtime_activity_static(active_compose)
    _validate_runtime_activity_static(candidate_compose)
    _validate_standalone_callers(active_compose)
    _validate_standalone_callers(candidate_compose)
    active_targets = _target_images(active_compose, active_lock, plan["targets"])
    candidate_targets = _target_images(candidate_compose, candidate_lock, plan["targets"])
    if candidate_targets != plan["candidateImages"]:
        raise RuntimeUpdateError("candidate target image identity differs from the reviewed plan")
    if (
        _source_identity_sha256(active_lock, plan["targets"]) != plan["activeSourceIdentitySha256"]
        or _source_identity_sha256(candidate_lock, plan["targets"])
        != plan["candidateSourceIdentitySha256"]
    ):
        raise RuntimeUpdateError("active or candidate source identity changed after review")
    if plan.get("project") != active_compose.get("name"):
        raise RuntimeUpdateError("rendered Compose project differs from the reviewed plan")
    preflight_data = plan["preflight"]
    secrets_dir = Path(preflight_data["secretsDir"])
    config_dir = Path(preflight_data["configDir"])
    if run_full_preflight:
        for compose_files, env_files, lock_path in (
            (active_compose_files, active_env_files, active_image_lock_path),
            (candidate_compose_files, candidate_env_files, candidate_image_lock_path),
        ):
            _run_preflight(
                compose_files=compose_files,
                env_files=env_files,
                image_lock=lock_path,
                secrets_dir=secrets_dir,
                config_dir=config_dir,
                docker_prefix=docker_prefix,
                max_memory_mib=preflight_data["maxMemoryMiB"],
                max_cpus=preflight_data["maxCpus"],
                required_free_gib=preflight_data["requiredFreeGiB"],
                minimum_mem_available_mib=preflight_data["minimumMemAvailableMiB"],
                recovery_only=recovery_only,
            )
    active_services = (
        _inspect_project_services(
            compose=active_compose,
            image_lock=active_lock,
            docker_prefix=docker_prefix,
        )
        if inspect_active
        else None
    )
    return {
        "activeCompose": active_compose,
        "candidateCompose": candidate_compose,
        "activeLock": active_lock,
        "candidateLock": candidate_lock,
        "activeServices": active_services,
        "activeTargets": active_targets,
        "candidateTargets": candidate_targets,
    }


def _validate_initial_runtime_state(
    plan: dict[str, Any], context: dict[str, Any], docker_prefix: list[str]
) -> None:
    """Require the exact reviewed active containers and unpaused standalone control state."""

    services = context["activeServices"]
    if services != plan["activeServices"]:
        raise RuntimeUpdateError("active service container/image identities changed after planning")
    _verify_runtime_unmanaged(services, docker_prefix)
    _verify_live_network_membership(context["activeCompose"], services, docker_prefix)
    ingress = services["teams-ingress"]
    navigator = services["navigator"]
    edge_status = _call_internal_api(
        docker_prefix,
        ingress["containerId"],
        service="teams-ingress",
        method="GET",
        path="/_internal/teams/delivery/status",
        body=None,
        timeout_seconds=15,
    )
    _validate_edge_status(edge_status)
    if (
        edge_status["paused"] is not False
        or edge_status["revision"] != plan["edgeDelivery"]["revision"]
        or edge_status["dead_count"] != plan["edgeDelivery"]["deadCount"]
    ):
        raise RuntimeUpdateError("Teams delivery pause revision changed after planning")
    navigator_status = _call_internal_api(
        docker_prefix,
        navigator["containerId"],
        service="navigator",
        method="GET",
        path="/internal/v1/executor/drain",
        body=None,
        timeout_seconds=15,
    )
    _validate_navigator_status(navigator_status)
    if (
        navigator_status["mode"] != "accepting"
        or navigator_status["instanceId"] != plan["navigatorDrain"]["instanceId"]
        or navigator_status["generation"] != plan["navigatorDrain"]["generation"]
    ):
        raise RuntimeUpdateError("Navigator process/drain generation changed after planning")


def _ensure_journal_directory(path: Path, *, create: bool) -> None:
    """Require a persistent owner-only journal directory, creating it only during apply."""

    if path.is_symlink():
        raise RuntimeUpdateError("runtime update journal directory cannot be a symlink")
    if not path.exists():
        if not create:
            raise RuntimeUpdateError("runtime update journal directory is unavailable")
        try:
            path.mkdir(mode=0o700, parents=True)
        except OSError as error:
            raise RuntimeUpdateError(
                "runtime update journal directory could not be created"
            ) from error
    if not path.is_dir() or path.is_symlink() or path.resolve(strict=True) != path:
        raise RuntimeUpdateError("runtime update journal path is not a real directory")
    try:
        metadata = path.stat()
    except OSError as error:
        raise RuntimeUpdateError("runtime update journal directory is unavailable") from error
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise RuntimeUpdateError(
            "runtime update journal directory must be owner-only and owned by this user"
        )


def _require_update_operator() -> None:
    """Keep every persistent update command under the one root-owned operator identity."""

    if os.geteuid() != GLOBAL_LOCK_OWNER_UID:
        raise RuntimeUpdateError(
            "runtime update plan/apply/status/resume must run as root to preserve one project lock"
        )


def _ensure_global_lock_directory() -> None:
    """Create and verify the root-private run-time directory that owns project locks."""

    parent = GLOBAL_LOCK_DIRECTORY.parent
    try:
        parent_metadata = parent.lstat()
    except OSError as error:
        raise RuntimeUpdateError("global runtime update lock parent is unavailable") from error
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != GLOBAL_LOCK_OWNER_UID
        or stat.S_IMODE(parent_metadata.st_mode) & 0o022
    ):
        raise RuntimeUpdateError("global runtime update lock parent is unsafe")
    try:
        GLOBAL_LOCK_DIRECTORY.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise RuntimeUpdateError("global runtime update lock directory is unavailable") from error
    try:
        metadata = GLOBAL_LOCK_DIRECTORY.lstat()
    except OSError as error:
        raise RuntimeUpdateError("global runtime update lock directory is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != GLOBAL_LOCK_OWNER_UID
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or GLOBAL_LOCK_DIRECTORY.resolve(strict=True) != GLOBAL_LOCK_DIRECTORY
    ):
        raise RuntimeUpdateError(
            "global runtime update lock directory must be root-owned mode 0700"
        )


@contextmanager
def _operation_lock(project: str) -> Iterator[None]:
    """Serialize every updater for one Compose project independent of journal location."""

    _require_update_operator()
    if not isinstance(project, str) or not project:
        raise RuntimeUpdateError("Compose project is required for the global update lock")
    _ensure_global_lock_directory()
    lock_path = _operation_lock_path(project)
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        created = False
        before = None
        try:
            before = lock_path.lstat()
        except FileNotFoundError:
            pass
        try:
            descriptor = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            descriptor = os.open(lock_path, flags)
        metadata = os.fstat(descriptor)
        if before is not None and (before.st_dev, before.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise RuntimeUpdateError("runtime update lock changed inode while opening")
        after = lock_path.lstat()
        if (after.st_dev, after.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeUpdateError("runtime update lock path changed while opening")
        if created:
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != GLOBAL_LOCK_OWNER_UID
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise RuntimeUpdateError("runtime update lock must be root-owned mode 0600")
    except RuntimeUpdateError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise RuntimeUpdateError("runtime update lock could not be opened safely") from error
    if descriptor is None:
        raise RuntimeUpdateError("runtime update lock could not be opened safely")
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeUpdateError(
                "another runtime update operation is active for this project"
            ) from error
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _operation_lock_path(project: str) -> Path:
    """Return the single root-owned lock path for one Compose project."""

    project_digest = _sha256_bytes(project.encode("utf-8"))[:24]
    return GLOBAL_LOCK_DIRECTORY / f"{project_digest}.lock"


def _atomic_journal_write(path: Path, document: dict[str, Any]) -> None:
    """Durably replace one 0600 journal record without exposing partial JSON."""

    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeUpdateError("runtime update journal path is unsafe")
    payload = _canonical_json(document) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeUpdateError("runtime update journal could not be persisted") from error


def _read_journal(path: Path) -> dict[str, Any]:
    """Read and validate one operation journal without emitting credential fields."""

    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeUpdateError("runtime update journal is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RuntimeUpdateError("runtime update journal must be an owner-only regular file")
    journal = _read_json_object(path, "runtime update journal")
    if (
        journal.get("schemaVersion") != JOURNAL_SCHEMA
        or not isinstance(journal.get("operationId"), str)
        or not isinstance(journal.get("planSha256"), str)
        or SHA256_RE.fullmatch(journal["planSha256"]) is None
        or not isinstance(journal.get("phase"), str)
        or not isinstance(journal.get("state"), str)
        or not isinstance(journal.get("targets"), list)
        or not isinstance(journal.get("updatedTargets"), list)
    ):
        raise RuntimeUpdateError("runtime update journal is malformed")
    try:
        uuid.UUID(journal["operationId"])
    except ValueError as error:
        raise RuntimeUpdateError("runtime update journal operation ID is invalid") from error
    return journal


def _journal_path(journal_dir: Path, operation_id: str) -> Path:
    """Return one UUID-constrained journal filename."""

    try:
        canonical_id = str(uuid.UUID(operation_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise RuntimeUpdateError("operation reference must be a UUID") from error
    return journal_dir / f"{canonical_id}.json"


def _new_journal(plan: dict[str, Any], plan_path: Path) -> dict[str, Any]:
    """Create the secret-free initial phase record for one reviewed update."""

    return {
        "schemaVersion": JOURNAL_SCHEMA,
        "operationId": plan["operationId"],
        "planSha256": plan["planSha256"],
        "planPath": str(plan_path.resolve(strict=True)),
        "targets": plan["targets"],
        "updatedTargets": [],
        "state": "RUNNING",
        "phase": "PRECHECKED",
        "createdAtUtc": _utc_now(),
        "updatedAtUtc": _utc_now(),
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
            "resumed": False,
            "replacementContainerId": None,
            "stop": None,
            "processRestart": None,
            "restartIntent": None,
        },
        "targetProgress": {},
        "lastErrorCode": None,
        "history": [{"phase": "PRECHECKED", "at": _utc_now()}],
    }


def _set_phase(
    journal: dict[str, Any],
    journal_path: Path,
    phase: str,
    *,
    state: str | None = None,
    error_code: str | None = None,
) -> None:
    """Persist each operation phase before the next external side effect."""

    journal["phase"] = phase
    journal["updatedAtUtc"] = _utc_now()
    if state is not None:
        journal["state"] = state
    if error_code is not None:
        journal["lastErrorCode"] = error_code
    history = journal.get("history")
    if not isinstance(history, list):
        history = []
    history.append({"phase": phase, "at": journal["updatedAtUtc"]})
    journal["history"] = history[-100:]
    _atomic_journal_write(journal_path, journal)


def _journal_summary(journal: dict[str, Any]) -> dict[str, Any]:
    """Return status-only operation metadata; omit plans, paths, and control tokens."""

    navigator = journal.get("navigator", {})
    stop_record = navigator.get("stop") if isinstance(navigator, dict) else None
    safe_stop = None
    if isinstance(stop_record, dict):
        safe_stop = {
            key: stop_record.get(key)
            for key in (
                "containerId",
                "timeoutSeconds",
                "commandExitCode",
                "commandOutcomeUnknown",
                "exitCode",
                "finishedAt",
                "oomKilled",
                "graceful",
            )
        }
    return {
        "schemaVersion": journal["schemaVersion"],
        "operationId": journal["operationId"],
        "planSha256": journal["planSha256"],
        "state": journal["state"],
        "phase": journal["phase"],
        "targets": journal["targets"],
        "updatedTargets": journal["updatedTargets"],
        "createdAtUtc": journal["createdAtUtc"],
        "updatedAtUtc": journal["updatedAtUtc"],
        "lastErrorCode": journal.get("lastErrorCode"),
        "deliveryDeadCount": journal.get("edge", {}).get("latestDeadCount"),
        "navigatorStop": safe_stop,
    }


def operation_status(journal_dir: Path, operation_id: str) -> dict[str, Any]:
    """Read one durable update phase without contacting containers or services."""

    _require_update_operator()
    _ensure_journal_directory(journal_dir, create=False)
    journal = _read_journal(_journal_path(journal_dir, operation_id))
    return _journal_summary(journal)


def _validate_docker_prefix(docker_prefix: list[str]) -> None:
    """Allow only the Docker executable with the supported noninteractive sudo prefix."""

    if docker_prefix not in (
        ["docker"],
        ["/usr/bin/docker"],
        ["sudo", "-n", "docker"],
        ["sudo", "-n", "/usr/bin/docker"],
    ):
        raise RuntimeUpdateError("Docker prefix must be docker or sudo -n docker")


def _compose_base_argv(
    docker_prefix: list[str], project: str, compose_files: list[Path], env_files: list[Path]
) -> list[str]:
    """Build a shell-free Compose argv using the exact reviewed file order."""

    argv = [*docker_prefix, "compose", "--project-name", project]
    for path in compose_files:
        argv.extend(["--file", str(path)])
    for path in env_files:
        argv.extend(["--env-file", str(path)])
    for profile in preflight.ACTIVE_COMPOSE_PROFILES:
        argv.extend(["--profile", profile])
    return argv


def _compose_up(
    *,
    docker_prefix: list[str],
    project: str,
    compose_files: list[Path],
    env_files: list[Path],
    service: str,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    """Replace one existing service from an exact input set without touching dependencies."""

    if service not in ALLOWED_TARGETS:
        raise RuntimeUpdateError(
            "Compose update target is outside the approved provider/Exchange/Navigator set"
        )
    argv = [
        *_compose_base_argv(docker_prefix, project, compose_files, env_files),
        "up",
        "--detach",
        "--no-deps",
        "--pull",
        "never",
        "--no-build",
        service,
    ]
    return _run_command(argv, timeout_seconds=timeout_seconds, external_mutation=True)


def _compose_up_stage_transition(
    *,
    docker_prefix: list[str],
    project: str,
    compose_files: list[Path],
    env_files: list[Path],
    service: str,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    """Replace one explicit Teams cloud phase service without dependencies or image I/O."""

    if service not in FORMAL_STAGE_TRANSITION_TARGETS:
        raise RuntimeUpdateError(
            "service is outside the formal Teams cloud stage-transition target set"
        )
    argv = [
        *_compose_base_argv(docker_prefix, project, compose_files, env_files),
        "up",
        "--detach",
        "--no-deps",
        "--pull",
        "never",
        "--no-build",
        "--force-recreate",
        service,
    ]
    return _run_command(argv, timeout_seconds=timeout_seconds, external_mutation=True)


def _current_edge_status(
    docker_prefix: list[str], container_id: str, *, timeout_seconds: float = 15
) -> dict[str, Any]:
    """Read the durable Teams delivery spool state from the ingress container."""

    response = _call_internal_api(
        docker_prefix,
        container_id,
        service="teams-ingress",
        method="GET",
        path="/_internal/teams/delivery/status",
        body=None,
        timeout_seconds=timeout_seconds,
    )
    _validate_edge_status(response)
    return response


def _current_navigator_status(
    docker_prefix: list[str], container_id: str, *, timeout_seconds: float = 15
) -> dict[str, Any]:
    """Read the current Navigator executor instance and drain generation."""

    response = _call_internal_api(
        docker_prefix,
        container_id,
        service="navigator",
        method="GET",
        path="/internal/v1/executor/drain",
        body=None,
        timeout_seconds=timeout_seconds,
    )
    _validate_navigator_status(response)
    return response


def _verify_edge_dead_count(edge: dict[str, Any], observed: int, phase: str) -> None:
    """Fail closed if any pause/resume observation adds a dead-lettered delivery."""

    edge["latestDeadCount"] = observed
    if observed != edge["initialDeadCount"]:
        raise RuntimeUpdateError(
            f"Teams delivery dead-letter count changed during {phase}; preserve the recorded DLQ and require recovery"
        )


def _pause_edge(
    *,
    plan: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
) -> None:
    """Pause only this operation's durable delivery holder and verify the revision."""

    edge = journal["edge"]
    initial_revision = edge["initialRevision"]
    expected_paused_revision = initial_revision + 1
    holder = edge.get("pauseHolder")
    if not isinstance(holder, str) or not holder:
        raise RuntimeUpdateError(
            "Teams delivery pause holder is missing from the operation journal"
        )
    edge["pauseIntent"] = True
    _set_phase(journal, journal_path, "EDGE_PAUSE_REQUESTED")
    status = _current_edge_status(docker_prefix, edge["containerId"])
    if status["paused"]:
        if status["revision"] != expected_paused_revision:
            raise RuntimeUpdateError("Teams delivery is paused under an unknown revision")
    elif status["revision"] != initial_revision:
        raise RuntimeUpdateError("Teams delivery revision changed before this operation paused it")
    _verify_edge_dead_count(edge, status["dead_count"], "pre-pause readback")

    # Replaying the same holder/revision is contractually idempotent if the first reply was lost.
    response = _call_internal_api(
        docker_prefix,
        edge["containerId"],
        service="teams-ingress",
        method="POST",
        path="/_internal/teams/delivery/pause",
        body={"holder": holder, "expected_revision": initial_revision},
        timeout_seconds=plan["policy"]["drainTimeoutSeconds"] + 15,
    )
    _validate_edge_status(response)
    _verify_edge_dead_count(edge, response["dead_count"], "pause CAS")
    _atomic_journal_write(journal_path, journal)
    if response["paused"] is not True or response["revision"] != expected_paused_revision:
        raise RuntimeUpdateError("Teams delivery did not enter this operation's paused revision")
    deadline = time.monotonic() + plan["policy"]["drainTimeoutSeconds"]
    while response["active_delivery_count"] != 0:
        if time.monotonic() >= deadline:
            raise RuntimeUpdateError(
                "Teams delivery drain timed out before active forwards settled"
            )
        time.sleep(2)
        response = _current_edge_status(docker_prefix, edge["containerId"])
        _verify_edge_dead_count(edge, response["dead_count"], "pause drain polling")
        if not response["paused"] or response["revision"] != expected_paused_revision:
            raise RuntimeUpdateError("Teams delivery pause holder/revision changed while draining")
    _verify_edge_dead_count(edge, response["dead_count"], "pause drain completion")
    edge["pauseRevision"] = expected_paused_revision
    _set_phase(journal, journal_path, "EDGE_PAUSED")


def _recover_edge_pause(
    *,
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
    timeout_seconds: int = 915,
) -> None:
    """Establish this operation's durable edge hold before any rollback mutation."""

    edge = journal["edge"]
    status = _current_edge_status(docker_prefix, edge["containerId"])
    _verify_edge_dead_count(edge, status["dead_count"], "pause recovery readback")
    pause_revision = edge.get("pauseRevision")
    resume_revision = edge.get("resumeRevision")
    if (
        status["paused"] is False
        and status["revision"] == edge["initialRevision"]
        and pause_revision is None
        and edge.get("pauseIntent") is not True
    ):
        return
    if status["paused"]:
        repause_expected_revision = edge.get("repauseExpectedRevision")
        if (
            isinstance(repause_expected_revision, int)
            and status["revision"] == repause_expected_revision + 1
            and isinstance(edge.get("repauseHolder"), str)
        ):
            expected_revision = repause_expected_revision
            holder = edge["repauseHolder"]
        elif isinstance(pause_revision, int) and status["revision"] == pause_revision:
            expected_revision = pause_revision - 1
            holder = edge.get("pauseHolder")
        elif pause_revision is None and status["revision"] == edge["initialRevision"] + 1:
            expected_revision = edge["initialRevision"]
            holder = journal["operationId"]
        else:
            raise RuntimeUpdateError("Teams delivery pause revision is not owned by this operation")
        if not isinstance(holder, str) or not holder:
            raise RuntimeUpdateError("Teams delivery pause holder is not journaled")
    else:
        if (
            pause_revision is None
            and status["revision"] == edge["initialRevision"]
            and edge.get("pauseIntent") is True
        ):
            expected_revision = status["revision"]
            holder = edge.get("pauseHolder") or journal["operationId"]
            if not isinstance(holder, str) or not holder:
                raise RuntimeUpdateError("Teams delivery initial pause holder is not journaled")
            _set_phase(journal, journal_path, "ROLLBACK_EDGE_PAUSE_REPLAY_REQUESTED")
        else:
            known_resume_revisions = set()
            if isinstance(pause_revision, int):
                known_resume_revisions.add(pause_revision + 1)
            if isinstance(resume_revision, int):
                known_resume_revisions.add(resume_revision)
            if status["revision"] not in known_resume_revisions:
                raise RuntimeUpdateError("Teams delivery was resumed under an unknown revision")
            expected_revision = status["revision"]
            pending_expected_revision = edge.get("repauseExpectedRevision")
            pending_holder = edge.get("repauseHolder")
            if pending_expected_revision == expected_revision and isinstance(pending_holder, str):
                holder = pending_holder
            else:
                holder = str(
                    uuid.uuid5(
                        uuid.UUID(journal["operationId"]),
                        f"teams-delivery-repause:{expected_revision}",
                    )
                )
                edge["repauseExpectedRevision"] = expected_revision
                edge["repauseHolder"] = holder
            _set_phase(journal, journal_path, "ROLLBACK_EDGE_REPAUSE_REQUESTED")
    response = _call_internal_api(
        docker_prefix,
        edge["containerId"],
        service="teams-ingress",
        method="POST",
        path="/_internal/teams/delivery/pause",
        body={"holder": holder, "expected_revision": expected_revision},
        timeout_seconds=timeout_seconds,
    )
    _validate_edge_status(response)
    if (
        response["paused"] is not True
        or response["revision"] != expected_revision + 1
        or response["active_delivery_count"] != 0
    ):
        raise RuntimeUpdateError("Teams delivery pause ownership could not be recovered")
    _verify_edge_dead_count(edge, response["dead_count"], "pause recovery CAS")
    edge.setdefault("pauseHistory", []).append(response["revision"])
    edge["pauseRevision"] = response["revision"]
    edge["pauseHolder"] = holder
    edge["resumeRevision"] = None
    edge["repauseExpectedRevision"] = None
    edge["repauseHolder"] = None
    _atomic_journal_write(journal_path, journal)


def _drain_navigator(
    *,
    plan: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
) -> None:
    """Stop dispatch and wait for active Navigator work while preserving its durable queue."""

    navigator = journal["navigator"]
    initial_generation = navigator["initialGeneration"]
    expected_generation = initial_generation + 1
    container_id = navigator["initialContainerId"]
    drain_attempt = navigator.get("drainAttempt")
    expected_attempt = {
        "containerId": container_id,
        "instanceId": navigator["initialInstanceId"],
        "initialGeneration": initial_generation,
        "generation": expected_generation,
    }
    if drain_attempt is None:
        navigator["drainAttempt"] = expected_attempt
    elif drain_attempt != expected_attempt:
        raise RuntimeUpdateError("Navigator drain intent does not match the reviewed plan")
    _set_phase(journal, journal_path, "NAVIGATOR_DRAIN_REQUESTED")
    status = _current_navigator_status(docker_prefix, container_id)
    if status["instanceId"] != navigator["initialInstanceId"]:
        raise RuntimeUpdateError("Navigator process changed before executor drain")
    if status["mode"] == "accepting":
        if status["generation"] != initial_generation:
            raise RuntimeUpdateError("Navigator drain generation changed before this operation")
        response = _call_internal_api(
            docker_prefix,
            container_id,
            service="navigator",
            method="POST",
            path="/internal/v1/executor/drain",
            body={},
            timeout_seconds=30,
        )
        _validate_navigator_status(response)
        status = response
    if (
        status["mode"] != "draining"
        or status["generation"] != expected_generation
        or status["instanceId"] != navigator["initialInstanceId"]
        or not isinstance(status["token"], str)
    ):
        raise RuntimeUpdateError("Navigator did not enter the expected drain generation")
    navigator["drainGeneration"] = expected_generation
    _atomic_journal_write(journal_path, journal)
    deadline = time.monotonic() + plan["policy"]["drainTimeoutSeconds"]
    while True:
        if (
            status["activeWorkDrained"]
            and status["countsComplete"] is True
            and status["activeTasks"] == 0
            and status["dispatches"]["active"] == 0
            and status["dispatches"]["waiting"] == 0
            and status["notifications"]["pollingPaused"]
            and status["notifications"]["inFlight"] is False
        ):
            navigator["drained"] = True
            _set_phase(journal, journal_path, "NAVIGATOR_DRAINED")
            return
        if time.monotonic() >= deadline:
            raise RuntimeUpdateError(
                "Navigator active work did not drain before the bounded timeout"
            )
        time.sleep(2)
        status = _current_navigator_status(docker_prefix, container_id)
        if (
            status["mode"] != "draining"
            or status["generation"] != expected_generation
            or status["instanceId"] != navigator["initialInstanceId"]
            or not isinstance(status["token"], str)
        ):
            raise RuntimeUpdateError("Navigator drain fencing state changed while waiting")


def _resume_navigator_instance(
    *,
    docker_prefix: list[str],
    container_id: str,
    instance_id: str,
    generation: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Resume the matching process generation without persisting its token in the journal."""

    status = _current_navigator_status(docker_prefix, container_id)
    if status["instanceId"] != instance_id:
        raise RuntimeUpdateError("Navigator process identity changed before resume")
    if status["mode"] == "accepting" and status["generation"] == generation:
        return status
    if status["mode"] != "draining" or status["generation"] != generation:
        raise RuntimeUpdateError("Navigator drain generation cannot be resumed safely")
    token = status.get("token")
    if not isinstance(token, str) or not token:
        raise RuntimeUpdateError("Navigator drain resume token is unavailable")
    response = _call_internal_api(
        docker_prefix,
        container_id,
        service="navigator",
        method="POST",
        path="/internal/v1/executor/drain/resume",
        body={"generation": generation, "token": token},
        timeout_seconds=timeout_seconds,
    )
    _validate_navigator_status(response)
    if (
        response["mode"] != "accepting"
        or response["generation"] != generation
        or response["instanceId"] != instance_id
        or response["token"] is not None
    ):
        raise RuntimeUpdateError("Navigator did not resume the exact drained process generation")
    return response


def _resume_edge(
    *,
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
) -> None:
    """Release this operation's pause by holder/revision CAS and read back its revision."""

    edge = journal["edge"]
    pause_revision = edge.get("pauseRevision")
    if pause_revision is None:
        return
    status = _current_edge_status(docker_prefix, edge["containerId"])
    _verify_edge_dead_count(edge, status["dead_count"], "resume preflight readback")
    if status["paused"] is False and status["revision"] == pause_revision + 1:
        edge["resumeRevision"] = status["revision"]
        edge["latestDeadCount"] = status["dead_count"]
        _set_phase(journal, journal_path, "EDGE_RESUMED")
        return
    if status["paused"] is not True or status["revision"] != pause_revision:
        raise RuntimeUpdateError(
            "Teams delivery pause no longer matches this operation's CAS holder"
        )
    _set_phase(journal, journal_path, "EDGE_RESUME_REQUESTED")
    holder = edge.get("pauseHolder")
    if not isinstance(holder, str) or not holder:
        raise RuntimeUpdateError(
            "Teams delivery resume holder is missing from the operation journal"
        )
    response = _call_internal_api(
        docker_prefix,
        edge["containerId"],
        service="teams-ingress",
        method="POST",
        path="/_internal/teams/delivery/resume",
        body={"holder": holder, "expected_revision": pause_revision},
        timeout_seconds=30,
    )
    _validate_edge_status(response)
    if response["paused"] is not False or response["revision"] != pause_revision + 1:
        raise RuntimeUpdateError("Teams delivery spool did not resume its exact paused revision")
    _verify_edge_dead_count(edge, response["dead_count"], "resume CAS")
    edge["resumeRevision"] = response["revision"]
    _set_phase(journal, journal_path, "EDGE_RESUMED")


def _verify_final_edge_delivery_readback(
    *, journal: dict[str, Any], docker_prefix: list[str]
) -> dict[str, Any]:
    """Require the resumed spool revision and unchanged DLQ count before APPLIED."""

    edge = journal["edge"]
    status = _current_edge_status(docker_prefix, edge["containerId"])
    _verify_edge_dead_count(edge, status["dead_count"], "final APPLIED readback")
    resume_revision = edge.get("resumeRevision")
    if (
        status["paused"] is not False
        or resume_revision is None
        or status["revision"] != resume_revision
    ):
        raise RuntimeUpdateError(
            "Teams delivery final readback differs from the operation's resumed revision"
        )
    return status


def _active_image_option(plan: dict[str, Any], target: str) -> dict[str, str]:
    """Return one reviewed active image reference/ID pair for a target."""

    snapshot = plan["activeServices"].get(target)
    if not isinstance(snapshot, dict):
        raise RuntimeUpdateError(f"active service snapshot for {target} is missing")
    return {"imageRef": snapshot["imageRef"], "imageId": snapshot["imageId"]}


def _candidate_image_option(plan: dict[str, Any], target: str) -> dict[str, str]:
    """Return one immutable candidate image reference/ID pair for a target."""

    image = plan["candidateImages"].get(target)
    if not isinstance(image, dict):
        raise RuntimeUpdateError(f"candidate image for {target} is missing")
    return {"imageRef": image["image"], "imageId": image["imageId"]}


def _operation_service_state(
    *,
    plan: dict[str, Any],
    context: dict[str, Any],
    docker_prefix: list[str],
    updated_targets: set[str],
    in_progress_target: str | None = None,
    health_exceptions: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Read all services while allowing only the exact old/new target image pair."""

    overrides: dict[str, dict[str, str] | list[dict[str, str]]] = {}
    for target in plan["targets"]:
        if target in updated_targets:
            overrides[target] = _candidate_image_option(plan, target)
        elif target == in_progress_target:
            overrides[target] = [
                _active_image_option(plan, target),
                _candidate_image_option(plan, target),
            ]
        else:
            overrides[target] = _active_image_option(plan, target)
    services = _read_transition_state(
        plan=plan,
        context=context,
        docker_prefix=docker_prefix,
        image_overrides=overrides,
        health_exceptions=health_exceptions,
    )
    for target in plan["targets"]:
        current = services[target]
        baseline = plan["activeServices"][target]
        if target in updated_targets and current["containerId"] == baseline["containerId"]:
            raise RuntimeUpdateError(f"target service {target} did not receive a new container")
        if target not in updated_targets and target != in_progress_target and current != baseline:
            raise RuntimeUpdateError(f"target service {target} changed outside this operation")
        if (
            target == in_progress_target
            and current["imageId"] == baseline["imageId"]
            and current["containerId"] != baseline["containerId"]
        ):
            raise RuntimeUpdateError(f"target service {target} changed outside this operation")
    return services


def _read_transition_state(
    *,
    plan: dict[str, Any],
    context: dict[str, Any],
    docker_prefix: list[str],
    image_overrides: dict[str, dict[str, str] | list[dict[str, str]]],
    health_exceptions: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Read a reviewed old/new transition and require every non-target to stay exact."""

    services = _inspect_project_services(
        compose=context["activeCompose"],
        image_lock=context["activeLock"],
        docker_prefix=docker_prefix,
        image_overrides=image_overrides,
        health_exceptions=health_exceptions,
    )
    for name, baseline in plan["activeServices"].items():
        current = services.get(name)
        if not isinstance(current, dict):
            raise RuntimeUpdateError(f"active service {name} disappeared during update")
        if name not in plan["targets"] and current != baseline:
            raise RuntimeUpdateError(f"non-target service {name} changed during update")
    _verify_live_network_membership(context["activeCompose"], services, docker_prefix)
    return services


def _wait_for_target_health(
    *,
    plan: dict[str, Any],
    context: dict[str, Any],
    docker_prefix: list[str],
    updated_targets: set[str],
    target: str,
    expected_image: dict[str, str],
) -> dict[str, Any]:
    """Wait for one exact locked target container to become Docker-healthy."""

    deadline = time.monotonic() + plan["policy"]["healthTimeoutSeconds"]
    while True:
        services = _operation_service_state(
            plan=plan,
            context=context,
            docker_prefix=docker_prefix,
            updated_targets=updated_targets,
            in_progress_target=target,
            health_exceptions={target},
        )
        current = services[target]
        if (
            current["imageId"] != expected_image["imageId"]
            or current["imageRef"] != expected_image["imageRef"]
        ):
            raise RuntimeUpdateError(f"target service {target} did not run the exact locked image")
        if current["health"] == "healthy":
            return current
        if current["health"] == "unhealthy" or time.monotonic() >= deadline:
            raise RuntimeUpdateError(f"target service {target} failed its health read-back")
        time.sleep(2)


def _wait_for_exact_service_health(
    *,
    plan: dict[str, Any],
    context: dict[str, Any],
    docker_prefix: list[str],
    target: str,
    image_option: dict[str, str],
    image_overrides: dict[str, dict[str, str] | list[dict[str, str]]],
) -> dict[str, Any]:
    """Wait for a candidate or rollback image with all other services healthy."""

    deadline = time.monotonic() + plan["policy"]["healthTimeoutSeconds"]
    while True:
        services = _read_transition_state(
            plan=plan,
            context=context,
            docker_prefix=docker_prefix,
            image_overrides=image_overrides,
            health_exceptions={target},
        )
        current = services[target]
        if (
            current["imageRef"] != image_option["imageRef"]
            or current["imageId"] != image_option["imageId"]
        ):
            raise RuntimeUpdateError(f"target service {target} did not run the exact locked image")
        if current["health"] == "healthy":
            return current
        if current["health"] == "unhealthy" or time.monotonic() >= deadline:
            raise RuntimeUpdateError(f"target service {target} failed its health read-back")
        time.sleep(2)


def _update_target(
    *,
    plan: dict[str, Any],
    context: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
    target: str,
) -> None:
    """Replace one target only after drain and reconcile by immutable image read-back."""

    updated_targets = set(journal["updatedTargets"])
    progress = journal["targetProgress"].get(target)
    if target in updated_targets:
        services = _operation_service_state(
            plan=plan,
            context=context,
            docker_prefix=docker_prefix,
            updated_targets=updated_targets,
        )
        if services[target]["health"] != "healthy":
            raise RuntimeUpdateError(f"previously updated target {target} is not healthy")
        return
    if progress is None:
        progress = {
            "phase": "PREPARED",
            "oldContainerId": plan["activeServices"][target]["containerId"],
            "oldImageId": plan["activeServices"][target]["imageId"],
            "candidateImageId": plan["candidateImages"][target]["imageId"],
            "newContainerId": None,
        }
        journal["targetProgress"][target] = progress
        _atomic_journal_write(journal_path, journal)

    services = _operation_service_state(
        plan=plan,
        context=context,
        docker_prefix=docker_prefix,
        updated_targets=updated_targets,
        in_progress_target=target,
        health_exceptions={target},
    )
    _verify_runtime_unmanaged(services, docker_prefix)
    current = services[target]
    active_option = _active_image_option(plan, target)
    candidate_option = _candidate_image_option(plan, target)
    if current["imageId"] == candidate_option["imageId"]:
        if current["containerId"] == progress["oldContainerId"]:
            raise RuntimeUpdateError(f"target service {target} retained its old container ID")
        if current["health"] != "healthy":
            current = _wait_for_target_health(
                plan=plan,
                context=context,
                docker_prefix=docker_prefix,
                updated_targets=updated_targets,
                target=target,
                expected_image=candidate_option,
            )
    elif current["imageId"] == active_option["imageId"]:
        if current["containerId"] != progress["oldContainerId"]:
            raise RuntimeUpdateError(f"target service {target} changed outside this operation")
        if target == "navigator":
            _stop_drained_navigator(
                plan=plan,
                context=context,
                journal=journal,
                journal_path=journal_path,
                docker_prefix=docker_prefix,
                container_id=current["containerId"],
            )
        progress["phase"] = "REPLACE_REQUESTED"
        journal["currentTarget"] = target
        _set_phase(journal, journal_path, f"REPLACING_{target.upper()}")
        candidate_inputs = plan["candidateInputs"]
        candidate_compose = _input_paths(candidate_inputs, "composeFiles")
        candidate_env = _input_paths(candidate_inputs, "envFiles")
        try:
            command = _compose_up(
                docker_prefix=docker_prefix,
                project=plan["project"],
                compose_files=candidate_compose,
                env_files=candidate_env,
                service=target,
                timeout_seconds=plan["policy"]["healthTimeoutSeconds"] + 60,
            )
        except RuntimeUpdateOutcomeUnknown:
            progress["phase"] = "REPLACE_OUTCOME_UNKNOWN"
            journal["unknownDockerMutation"] = {
                "service": target,
                "kind": "compose-up",
                "requestedAtUtc": journal["updatedAtUtc"],
                "oldContainerId": progress["oldContainerId"],
                "oldImageId": progress["oldImageId"],
                "candidateImageId": progress["candidateImageId"],
            }
            _set_phase(
                journal,
                journal_path,
                "COMPOSE_UPDATE_OUTCOME_UNKNOWN",
                state="RECOVERY_REQUIRED",
                error_code="compose_update_outcome_unknown",
            )
            raise
        services = _operation_service_state(
            plan=plan,
            context=context,
            docker_prefix=docker_prefix,
            updated_targets=updated_targets,
            in_progress_target=target,
            health_exceptions={target},
        )
        current = services[target]
        if current["imageId"] != candidate_option["imageId"]:
            if command.returncode != 0 and current["containerId"] == progress["oldContainerId"]:
                raise RuntimeUpdateError(f"Compose failed to replace target service {target}")
            raise RuntimeUpdateError(f"target service {target} has an unknown post-update image")
        if current["containerId"] == progress["oldContainerId"]:
            raise RuntimeUpdateError(f"target service {target} was not replaced")
        if current["health"] != "healthy":
            current = _wait_for_target_health(
                plan=plan,
                context=context,
                docker_prefix=docker_prefix,
                updated_targets=updated_targets,
                target=target,
                expected_image=candidate_option,
            )
    else:
        raise RuntimeUpdateError(f"target service {target} is not on a reviewed old/new image")

    progress["phase"] = "UPDATED_HEALTHY"
    progress["newContainerId"] = current["containerId"]
    journal["updatedTargets"].append(target)
    journal["updatedTargets"].sort(key=TARGET_ORDER.index)
    journal.pop("currentTarget", None)
    _set_phase(journal, journal_path, f"{target.upper()}_HEALTHY")


def _verify_new_navigator_accepting(
    *,
    plan: dict[str, Any],
    services: dict[str, dict[str, Any]],
    docker_prefix: list[str],
) -> dict[str, Any]:
    """Require a replacement Navigator process to start in its fresh accepting generation."""

    navigator = services.get("navigator")
    if not isinstance(navigator, dict):
        raise RuntimeUpdateError("replacement Navigator container is missing")
    status = _current_navigator_status(docker_prefix, navigator["containerId"])
    if (
        status["mode"] != "accepting"
        or status["generation"] != 0
        or status["token"] is not None
        or status["startedAt"] is not None
        or status["instanceId"] == plan["navigatorDrain"]["instanceId"]
    ):
        raise RuntimeUpdateError(
            "new Navigator process did not start in a fresh accepting generation"
        )
    return status


def _inspect_container_state(docker_prefix: list[str], container_id: str) -> dict[str, Any]:
    """Read only Docker lifecycle metadata for the exact stopped Navigator container."""

    if CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise RuntimeUpdateError("Navigator container ID is malformed during stop read-back")
    output = _checked_stdout(
        [
            *docker_prefix,
            "inspect",
            "--format",
            "{{json .State}}",
            container_id,
        ],
        label="Navigator stop read-back",
        timeout_seconds=15,
    )
    state = _decode_json_output(output, "Navigator stop read-back")
    required_fields = {
        "Status",
        "Running",
        "Paused",
        "Restarting",
        "OOMKilled",
        "ExitCode",
        "Error",
        "StartedAt",
        "FinishedAt",
    }
    if not isinstance(state, dict) or not required_fields <= set(state):
        raise RuntimeUpdateError("Navigator stop lifecycle metadata is malformed")
    if (
        not isinstance(state.get("Status"), str)
        or not isinstance(state.get("Running"), bool)
        or not isinstance(state.get("Paused"), bool)
        or not isinstance(state.get("Restarting"), bool)
        or not isinstance(state.get("OOMKilled"), bool)
        or isinstance(state.get("ExitCode"), bool)
        or not isinstance(state.get("ExitCode"), int)
        or not isinstance(state.get("StartedAt"), str)
        or not isinstance(state.get("FinishedAt"), str)
        or not isinstance(state.get("Error"), str)
    ):
        raise RuntimeUpdateError("Navigator stop lifecycle metadata has invalid field types")
    return state


def _list_project_navigator_containers(
    *, project: str, docker_prefix: list[str]
) -> list[dict[str, Any]]:
    """Inspect running and stopped Navigator containers without exposing environment values."""

    output = _checked_stdout(
        [
            *docker_prefix,
            "ps",
            "--all",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "label=com.docker.compose.service=navigator",
            "--format",
            "{{.ID}}",
        ],
        label="Navigator recovery inventory",
    )
    container_ids = [line.strip() for line in output.splitlines() if line.strip()]
    if any(CONTAINER_ID_RE.fullmatch(container_id) is None for container_id in container_ids):
        raise RuntimeUpdateError("Docker returned a malformed Navigator recovery ID")
    if not container_ids:
        return []
    rows = _decode_json_output(
        _checked_stdout(
            [*docker_prefix, "inspect", *container_ids],
            label="Navigator recovery inspection",
        ),
        "Navigator recovery inspection",
    )
    if not isinstance(rows, list) or len(rows) != len(container_ids):
        raise RuntimeUpdateError("Docker returned an incomplete Navigator recovery inventory")
    result: list[dict[str, Any]] = []
    for row in rows:
        config = row.get("Config") if isinstance(row, dict) else None
        labels = config.get("Labels") if isinstance(config, dict) else None
        state = row.get("State") if isinstance(row, dict) else None
        if (
            not isinstance(labels, dict)
            or labels.get("com.docker.compose.project") != project
            or labels.get("com.docker.compose.service") != "navigator"
            or not isinstance(state, dict)
            or not isinstance(state.get("Running"), bool)
            or not isinstance(config.get("Image"), str)
            or not isinstance(row.get("Image"), str)
            or IMAGE_ID_RE.fullmatch(row["Image"]) is None
            or not isinstance(row.get("Id"), str)
            or re.fullmatch(r"[0-9a-f]{64}", row["Id"]) is None
        ):
            raise RuntimeUpdateError("Navigator recovery inventory contains an unknown container")
        result.append(
            {
                "containerId": row["Id"],
                "imageRef": config["Image"],
                "imageId": row["Image"],
                "running": state["Running"],
            }
        )
    return result


def _recover_old_navigator_before_candidate_start(
    *,
    plan: dict[str, Any],
    context: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
) -> bool:
    """Restart only the old locked image if the journal proves candidate Compose never began."""

    if "navigator" not in plan["targets"]:
        return False
    progress = journal.get("targetProgress", {}).get("navigator")
    stop_record = journal.get("navigator", {}).get("stop")
    progress_phase = progress.get("phase") if isinstance(progress, dict) else None
    if (
        progress_phase
        not in {
            "PREPARED",
            "ACTIVE_IMAGE_RESTART_REQUESTED",
            "STOP_FAILED_RECOVERED",
            "RECOVERY_OLD_IMAGE_RESTARTED",
        }
        or not isinstance(stop_record, dict)
        or stop_record.get("containerId") != plan["activeServices"]["navigator"]["containerId"]
    ):
        return False

    containers = _list_project_navigator_containers(
        project=plan["project"], docker_prefix=docker_prefix
    )
    if len(containers) > 1:
        raise RuntimeUpdateError(
            "multiple Navigator containers prevent safe interrupted-stop recovery"
        )
    active_image = _active_image_option(plan, "navigator")
    candidate_image = _candidate_image_option(plan, "navigator")
    if not containers:
        if progress_phase != "PREPARED" or journal["phase"] not in {
            "NAVIGATOR_STOP_REQUESTED",
            "ROLLBACK_STARTING",
            "RECOVERY_REQUIRED",
        }:
            raise RuntimeUpdateError(
                "Navigator container is missing after candidate replacement may have begun"
            )
        stopped_container_id = stop_record["containerId"]
        command_exit_code = None
    else:
        current = containers[0]
        if current["imageId"] == candidate_image["imageId"]:
            raise RuntimeUpdateError("candidate Navigator exists but cannot be proven drained")
        if (
            current["imageId"] != active_image["imageId"]
            or current["imageRef"] != active_image["imageRef"]
        ):
            raise RuntimeUpdateError(
                "Navigator recovery found an image outside the reviewed old/candidate pair"
            )
        if current["running"]:
            if progress_phase != "ACTIVE_IMAGE_RESTART_REQUESTED":
                return False
            stopped_container_id = stop_record["containerId"]
            command_exit_code = (
                journal["navigator"].get("restartIntent", {}).get("composeCommandExitCode")
            )
        elif progress_phase != "PREPARED":
            raise RuntimeUpdateError(
                "Navigator stopped after an old-image restart intent; active work state cannot be proven"
            )
        else:
            expected_container_id = plan["activeServices"]["navigator"]["containerId"]
            if current["containerId"] != expected_container_id:
                raise RuntimeUpdateError(
                    "stopped active Navigator container identity is not owned by this operation"
                )
            stopped_container_id = current["containerId"]
            command_exit_code = None
    if progress_phase == "PREPARED":
        _record_active_navigator_restart_intent(
            journal=journal,
            journal_path=journal_path,
            stopped_container_id=stopped_container_id,
            reason="interrupted_before_candidate",
        )
        active_inputs = plan["activeInputs"]
        try:
            command = _compose_up(
                docker_prefix=docker_prefix,
                project=plan["project"],
                compose_files=_input_paths(active_inputs, "composeFiles"),
                env_files=_input_paths(active_inputs, "envFiles"),
                service="navigator",
                timeout_seconds=plan["policy"]["healthTimeoutSeconds"] + 60,
            )
        except RuntimeUpdateOutcomeUnknown:
            journal["unknownDockerMutation"] = {
                "service": "navigator",
                "kind": "compose-active-image-restart",
                "requestedAtUtc": journal["navigator"]["restartIntent"]["requestedAtUtc"],
                "stoppedContainerId": stopped_container_id,
                "activeImageId": active_image["imageId"],
            }
            _set_phase(
                journal,
                journal_path,
                "NAVIGATOR_RESTART_OUTCOME_UNKNOWN",
                state="RECOVERY_REQUIRED",
                error_code="navigator_restart_outcome_unknown",
            )
            raise
        command_exit_code = command.returncode
        journal["navigator"]["restartIntent"]["composeCommandExitCode"] = command_exit_code
        journal["navigator"]["restartIntent"]["composeCommandReturnedAtUtc"] = _utc_now()
        _atomic_journal_write(journal_path, journal)
    image_overrides = {
        target: (
            _candidate_image_option(plan, target)
            if target in journal["updatedTargets"]
            else _active_image_option(plan, target)
        )
        for target in plan["targets"]
    }
    services = _read_transition_state(
        plan=plan,
        context=context,
        docker_prefix=docker_prefix,
        image_overrides=image_overrides,
        health_exceptions={"navigator"},
    )
    navigator = services["navigator"]
    if (
        navigator["imageId"] != active_image["imageId"]
        or navigator["imageRef"] != active_image["imageRef"]
    ):
        raise RuntimeUpdateError(
            "interrupted-stop recovery did not restore the active Navigator image"
        )
    navigator = _wait_for_exact_service_health(
        plan=plan,
        context=context,
        docker_prefix=docker_prefix,
        target="navigator",
        image_option=active_image,
        image_overrides=image_overrides,
    )
    _verify_runtime_unmanaged(services, docker_prefix)
    _verify_live_network_membership(context["activeCompose"], services, docker_prefix)
    status = _current_navigator_status(docker_prefix, navigator["containerId"])
    if (
        status["mode"] != "accepting"
        or status["generation"] != 0
        or status["token"] is not None
        or status["startedAt"] is not None
        or status["instanceId"] == journal["navigator"]["initialInstanceId"]
    ):
        raise RuntimeUpdateError(
            "recovered active Navigator is not accepting in a fresh generation"
        )
    _complete_active_navigator_restart(
        journal=journal,
        journal_path=journal_path,
        navigator=navigator,
        instance_id=status["instanceId"],
        compose_command_exit_code=command_exit_code,
        terminal_target_phase="RECOVERY_OLD_IMAGE_RESTARTED",
    )
    return True


def _navigator_stop_is_graceful(command_exit_code: int | None, lifecycle: dict[str, Any]) -> bool:
    """Classify only a confirmed zero-exit, non-OOM container stop as graceful."""

    return (
        command_exit_code == 0
        and lifecycle.get("Status") == "exited"
        and lifecycle.get("Running") is False
        and lifecycle.get("Paused") is False
        and lifecycle.get("Restarting") is False
        and lifecycle.get("OOMKilled") is False
        and lifecycle.get("ExitCode") == 0
        and lifecycle.get("Error") == ""
        and lifecycle.get("StartedAt") not in {None, "", "0001-01-01T00:00:00Z"}
        and lifecycle.get("FinishedAt") not in {None, "", "0001-01-01T00:00:00Z"}
    )


def _record_active_navigator_restart_intent(
    *,
    journal: dict[str, Any],
    journal_path: Path,
    stopped_container_id: str,
    reason: str,
) -> None:
    """Persist old-image restart ownership before Compose can create or start a container."""

    if reason not in {"non_graceful_stop", "interrupted_before_candidate"}:
        raise RuntimeUpdateError("active Navigator restart reason is unsupported")
    progress = journal.get("targetProgress", {}).get("navigator")
    if not isinstance(progress, dict):
        raise RuntimeUpdateError("Navigator restart lacks a target progress record")
    journal["navigator"]["restartIntent"] = {
        "kind": "active-image-restart",
        "reason": reason,
        "stoppedContainerId": stopped_container_id,
        "activeImageId": progress["oldImageId"],
        "requestedAtUtc": _utc_now(),
    }
    progress["phase"] = "ACTIVE_IMAGE_RESTART_REQUESTED"
    _set_phase(
        journal,
        journal_path,
        "NAVIGATOR_ACTIVE_IMAGE_RESTART_REQUESTED",
        state="ROLLING_BACK",
    )


def _complete_active_navigator_restart(
    *,
    journal: dict[str, Any],
    journal_path: Path,
    navigator: dict[str, Any],
    instance_id: str,
    compose_command_exit_code: int | None,
    terminal_target_phase: str,
) -> None:
    """Record a freshly verified old-image process after its restart intent."""

    if terminal_target_phase not in {"STOP_FAILED_RECOVERED", "RECOVERY_OLD_IMAGE_RESTARTED"}:
        raise RuntimeUpdateError("active Navigator restart completion phase is unsupported")
    intent = journal["navigator"].get("restartIntent")
    if not isinstance(intent, dict) or intent.get("kind") != "active-image-restart":
        raise RuntimeUpdateError("active Navigator restart intent is missing")
    journal["navigator"]["processRestart"] = {
        "stoppedContainerId": intent["stoppedContainerId"],
        "containerId": navigator["containerId"],
        "instanceId": instance_id,
        "instanceIdChanged": True,
        "activeImageVerified": True,
        "composeCommandExitCode": compose_command_exit_code,
    }
    journal["navigator"]["replacementContainerId"] = navigator["containerId"]
    progress = journal["targetProgress"]["navigator"]
    progress["phase"] = terminal_target_phase
    progress["newContainerId"] = navigator["containerId"]
    _set_phase(
        journal,
        journal_path,
        "NAVIGATOR_ACTIVE_IMAGE_RESTARTED",
        state="ROLLING_BACK",
    )


def _stop_drained_navigator(
    *,
    plan: dict[str, Any],
    context: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
    container_id: str,
) -> None:
    """Stop only the drained old Navigator and persist its real Docker exit status."""

    stop_record = journal["navigator"].get("stop")
    if isinstance(stop_record, dict):
        if (
            stop_record.get("containerId") != container_id
            or stop_record.get("graceful") is not True
        ):
            raise RuntimeUpdateError("Navigator has a prior incomplete or non-graceful stop record")
        lifecycle = _inspect_container_state(docker_prefix, container_id)
        if not (
            lifecycle["Status"] == "exited"
            and lifecycle["Running"] is False
            and lifecycle["Paused"] is False
            and lifecycle["Restarting"] is False
            and lifecycle["OOMKilled"] is False
            and lifecycle["ExitCode"] == 0
            and lifecycle["Error"] == ""
            and lifecycle["StartedAt"] == stop_record.get("startedAt")
            and lifecycle["FinishedAt"] == stop_record.get("finishedAt")
        ):
            raise RuntimeUpdateError(
                "Navigator stop read-back changed after the recorded graceful stop"
            )
        return
    if not journal["navigator"].get("drained"):
        raise RuntimeUpdateError("Navigator must be drained before stopping its container")
    before = _current_navigator_status(docker_prefix, container_id)
    if not (
        before["mode"] == "draining"
        and before["instanceId"] == journal["navigator"]["initialInstanceId"]
        and before["generation"] == journal["navigator"].get("drainGeneration")
        and before["activeWorkDrained"]
        and before["countsComplete"] is True
        and before["activeTasks"] == 0
        and before["dispatches"]["active"] == 0
        and before["dispatches"]["waiting"] == 0
        and before["notifications"]["pollingPaused"]
        and before["notifications"]["inFlight"] is False
    ):
        raise RuntimeUpdateError("Navigator drain proof changed before container stop")

    journal["navigator"]["stop"] = {
        "containerId": container_id,
        "requestedAtUtc": _utc_now(),
        "timeoutSeconds": 40,
        "commandExitCode": None,
        "exitCode": None,
        "startedAt": None,
        "finishedAt": None,
        "oomKilled": None,
        "graceful": None,
    }
    _set_phase(journal, journal_path, "NAVIGATOR_STOP_REQUESTED")
    command_error: RuntimeUpdateError | None = None
    try:
        result = _run_command(
            [*docker_prefix, "stop", "--time", "40", container_id],
            timeout_seconds=55,
            external_mutation=True,
        )
    except RuntimeUpdateOutcomeUnknown:
        stop_record = journal["navigator"]["stop"]
        stop_record["commandOutcomeUnknown"] = True
        try:
            observed_state = _inspect_container_state(docker_prefix, container_id)
        except RuntimeUpdateError:
            stop_record["observedStateReadable"] = False
        else:
            stop_record["observedStateReadable"] = True
            stop_record["observedStatus"] = observed_state["Status"]
            stop_record["observedExitCode"] = observed_state["ExitCode"]
            stop_record["observedRunning"] = observed_state["Running"]
        _set_phase(
            journal,
            journal_path,
            "NAVIGATOR_STOP_OUTCOME_UNKNOWN",
            state="RECOVERY_REQUIRED",
            error_code="navigator_stop_outcome_unknown",
        )
        raise
    except RuntimeUpdateError as error:
        result = None
        command_error = error
    try:
        lifecycle = _inspect_container_state(docker_prefix, container_id)
    except RuntimeUpdateError:
        journal["navigator"]["stop"]["commandOutcomeUnknown"] = True
        _atomic_journal_write(journal_path, journal)
        raise
    stop_record = journal["navigator"]["stop"]
    stop_record["commandExitCode"] = result.returncode if result is not None else None
    stop_record["commandOutcomeUnknown"] = command_error is not None
    stop_record["exitCode"] = lifecycle["ExitCode"]
    stop_record["startedAt"] = lifecycle["StartedAt"]
    stop_record["finishedAt"] = lifecycle["FinishedAt"]
    stop_record["oomKilled"] = lifecycle["OOMKilled"]
    stop_record["graceful"] = _navigator_stop_is_graceful(
        result.returncode if result is not None else None,
        lifecycle,
    )
    _atomic_journal_write(journal_path, journal)
    if stop_record["graceful"] is not True:
        if lifecycle["Running"]:
            status = _current_navigator_status(docker_prefix, container_id)
            if (
                status["mode"] == "draining"
                and status["instanceId"] == journal["navigator"]["initialInstanceId"]
                and status["generation"] == journal["navigator"].get("drainGeneration")
            ):
                _resume_navigator_instance(
                    docker_prefix=docker_prefix,
                    container_id=container_id,
                    instance_id=status["instanceId"],
                    generation=status["generation"],
                    timeout_seconds=30,
                )
            elif status["mode"] != "accepting":
                raise RuntimeUpdateError("Navigator stop outcome is unknown; leaving edge paused")
        else:
            _restore_active_navigator_after_stop_failure(
                plan=plan,
                context=context,
                journal=journal,
                journal_path=journal_path,
                docker_prefix=docker_prefix,
                stopped_container_id=container_id,
            )
        raise RuntimeUpdateError("Navigator stopped without a verified clean exit code 0")


def _restore_active_navigator_after_stop_failure(
    *,
    plan: dict[str, Any],
    context: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
    stopped_container_id: str,
) -> None:
    """Restart the reviewed active image after a non-graceful pre-replacement stop."""

    _record_active_navigator_restart_intent(
        journal=journal,
        journal_path=journal_path,
        stopped_container_id=stopped_container_id,
        reason="non_graceful_stop",
    )
    active_inputs = plan["activeInputs"]
    try:
        command = _compose_up(
            docker_prefix=docker_prefix,
            project=plan["project"],
            compose_files=_input_paths(active_inputs, "composeFiles"),
            env_files=_input_paths(active_inputs, "envFiles"),
            service="navigator",
            timeout_seconds=plan["policy"]["healthTimeoutSeconds"] + 60,
        )
    except RuntimeUpdateOutcomeUnknown:
        journal["unknownDockerMutation"] = {
            "service": "navigator",
            "kind": "compose-active-image-restart",
            "requestedAtUtc": journal["navigator"]["restartIntent"]["requestedAtUtc"],
            "stoppedContainerId": stopped_container_id,
            "activeImageId": _active_image_option(plan, "navigator")["imageId"],
        }
        _set_phase(
            journal,
            journal_path,
            "NAVIGATOR_RESTART_OUTCOME_UNKNOWN",
            state="RECOVERY_REQUIRED",
            error_code="navigator_restart_outcome_unknown",
        )
        raise
    journal["navigator"]["restartIntent"]["composeCommandExitCode"] = command.returncode
    journal["navigator"]["restartIntent"]["composeCommandReturnedAtUtc"] = _utc_now()
    _atomic_journal_write(journal_path, journal)
    image_overrides = {target: _active_image_option(plan, target) for target in plan["targets"]}
    services = _read_transition_state(
        plan=plan,
        context=context,
        docker_prefix=docker_prefix,
        image_overrides=image_overrides,
        health_exceptions={"navigator"},
    )
    navigator = services["navigator"]
    if navigator["imageId"] != plan["activeServices"]["navigator"]["imageId"]:
        raise RuntimeUpdateError("active Navigator image could not be restored after stop failure")
    navigator = _wait_for_exact_service_health(
        plan=plan,
        context=context,
        docker_prefix=docker_prefix,
        target="navigator",
        image_option=_active_image_option(plan, "navigator"),
        image_overrides=image_overrides,
    )
    status = _current_navigator_status(docker_prefix, navigator["containerId"])
    if (
        status["mode"] != "accepting"
        or status["generation"] != 0
        or status["token"] is not None
        or status["startedAt"] is not None
        or status["instanceId"] == journal["navigator"]["initialInstanceId"]
    ):
        raise RuntimeUpdateError("restarted active Navigator is not in its fresh accepting state")
    _complete_active_navigator_restart(
        journal=journal,
        journal_path=journal_path,
        navigator=navigator,
        instance_id=status["instanceId"],
        compose_command_exit_code=command.returncode,
        terminal_target_phase="STOP_FAILED_RECOVERED",
    )


def _ensure_navigator_resumed(
    *,
    plan: dict[str, Any],
    context: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
) -> None:
    """Resume the old drained process or verify the new process's default accepting mode."""

    if not journal["navigator"]["drained"]:
        raise RuntimeUpdateError("Navigator work drain was not verified before resume")
    updated_targets = set(journal["updatedTargets"])
    services = _operation_service_state(
        plan=plan,
        context=context,
        docker_prefix=docker_prefix,
        updated_targets=updated_targets,
    )
    _verify_runtime_unmanaged(services, docker_prefix)
    navigator = services["navigator"]
    if navigator["containerId"] == journal["navigator"]["initialContainerId"]:
        generation = journal["navigator"].get("drainGeneration")
        if not isinstance(generation, int):
            raise RuntimeUpdateError("Navigator drain generation was not journaled")
        _set_phase(journal, journal_path, "NAVIGATOR_RESUME_REQUESTED")
        status = _resume_navigator_instance(
            docker_prefix=docker_prefix,
            container_id=navigator["containerId"],
            instance_id=journal["navigator"]["initialInstanceId"],
            generation=generation,
            timeout_seconds=30,
        )
        if status["mode"] != "accepting":
            raise RuntimeUpdateError("Navigator did not resume the drained active process")
    elif "navigator" in updated_targets:
        status = _verify_new_navigator_accepting(
            plan=plan, services=services, docker_prefix=docker_prefix
        )
        journal["navigator"]["replacementContainerId"] = navigator["containerId"]
    else:
        raise RuntimeUpdateError("Navigator container changed outside the selected update targets")
    journal["navigator"]["resumed"] = True
    _set_phase(journal, journal_path, "NAVIGATOR_RESUMED")


def _drain_current_navigator_for_rollback(
    *,
    plan: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
    container_id: str,
    timeout_seconds: int,
) -> None:
    """Drain the currently running Navigator before rolling back an Exchange image."""

    status = _current_navigator_status(docker_prefix, container_id)
    rollback_drain = journal["navigator"].get("rollbackDrain")
    if status["mode"] == "accepting":
        expected_instance = status["instanceId"]
        starting_generation = status["generation"]
        rollback_drain = {
            "containerId": container_id,
            "instanceId": expected_instance,
            "initialGeneration": starting_generation,
            "generation": None,
        }
        journal["navigator"]["rollbackDrain"] = rollback_drain
        _set_phase(journal, journal_path, "ROLLBACK_NAVIGATOR_DRAIN_REQUESTED")
        response = _call_internal_api(
            docker_prefix,
            container_id,
            service="navigator",
            method="POST",
            path="/internal/v1/executor/drain",
            body={},
            timeout_seconds=30,
        )
        _validate_navigator_status(response)
        status = response
        if (
            status["mode"] != "draining"
            or status["instanceId"] != expected_instance
            or status["generation"] != starting_generation + 1
            or not isinstance(status["token"], str)
        ):
            raise RuntimeUpdateError("Navigator could not enter a rollback drain generation")
        rollback_drain["generation"] = status["generation"]
        _atomic_journal_write(journal_path, journal)
    elif status["mode"] == "draining":
        expected_generation = None
        if (
            status["instanceId"] == journal["navigator"]["initialInstanceId"]
            and journal["navigator"].get("drainGeneration") == status["generation"]
        ) or (
            isinstance(rollback_drain, dict)
            and rollback_drain.get("containerId") == container_id
            and rollback_drain.get("instanceId") == status["instanceId"]
            and rollback_drain.get("generation") == status["generation"]
        ):
            expected_generation = status["generation"]
        elif (
            isinstance(rollback_drain, dict)
            and rollback_drain.get("containerId") == container_id
            and rollback_drain.get("instanceId") == status["instanceId"]
            and rollback_drain.get("generation") is None
            and isinstance(rollback_drain.get("initialGeneration"), int)
            and status["generation"] == rollback_drain.get("initialGeneration", -1) + 1
        ):
            expected_generation = status["generation"]
            rollback_drain["generation"] = expected_generation
            _atomic_journal_write(journal_path, journal)
        elif (
            isinstance(journal["navigator"].get("drainAttempt"), dict)
            and journal["navigator"]["drainAttempt"].get("containerId") == container_id
            and journal["navigator"]["drainAttempt"].get("instanceId") == status["instanceId"]
            and journal["navigator"]["drainAttempt"].get("generation") == status["generation"]
        ):
            expected_generation = status["generation"]
            journal["navigator"]["drainGeneration"] = expected_generation
        if expected_generation is None or not isinstance(status["token"], str):
            raise RuntimeUpdateError("Navigator is draining under an unknown rollback generation")
        rollback_drain = {
            "containerId": container_id,
            "instanceId": status["instanceId"],
            "generation": expected_generation,
        }
        journal["navigator"]["rollbackDrain"] = rollback_drain
    else:
        raise RuntimeUpdateError("Navigator rollback drain mode is unsupported")

    deadline = time.monotonic() + timeout_seconds
    while not (
        status["activeWorkDrained"]
        and status["countsComplete"] is True
        and status["activeTasks"] == 0
        and status["dispatches"]["active"] == 0
        and status["dispatches"]["waiting"] == 0
        and status["notifications"]["pollingPaused"]
        and status["notifications"]["inFlight"] is False
    ):
        if time.monotonic() >= deadline:
            raise RuntimeUpdateError("Navigator rollback drain exceeded its bounded timeout")
        time.sleep(2)
        status = _current_navigator_status(docker_prefix, container_id)
        if (
            status["mode"] != "draining"
            or status["instanceId"] != rollback_drain["instanceId"]
            or status["generation"] != rollback_drain["generation"]
            or not isinstance(status["token"], str)
        ):
            raise RuntimeUpdateError("Navigator rollback drain fencing state changed")


def _resume_navigator_after_rollback(
    *,
    plan: dict[str, Any],
    journal: dict[str, Any],
    docker_prefix: list[str],
    current: dict[str, dict[str, Any]],
) -> None:
    """Resume the exact still-running process or verify a replacement starts cleanly."""

    navigator = current.get("navigator")
    if not isinstance(navigator, dict):
        raise RuntimeUpdateError("Navigator container is missing after rollback")
    status = _current_navigator_status(docker_prefix, navigator["containerId"])
    original_container = journal["navigator"]["initialContainerId"]
    if navigator["containerId"] == original_container:
        process_restart = journal["navigator"].get("processRestart")
        if status["instanceId"] != journal["navigator"]["initialInstanceId"]:
            if (
                not isinstance(process_restart, dict)
                or process_restart.get("containerId") != navigator["containerId"]
                or process_restart.get("instanceIdChanged") is not True
                or status["mode"] != "accepting"
                or status["generation"] != 0
                or status["token"] is not None
                or status["startedAt"] is not None
            ):
                raise RuntimeUpdateError(
                    "Navigator process identity changed outside this operation"
                )
            return
        known_generation = journal["navigator"].get("drainGeneration")
        rollback_drain = journal["navigator"].get("rollbackDrain")
        if (
            isinstance(rollback_drain, dict)
            and rollback_drain.get("containerId") == original_container
            and isinstance(rollback_drain.get("generation"), int)
        ):
            known_generation = rollback_drain["generation"]
        if (
            known_generation is None
            and isinstance(rollback_drain, dict)
            and rollback_drain.get("containerId") == original_container
            and rollback_drain.get("instanceId") == status["instanceId"]
            and rollback_drain.get("generation") is None
            and isinstance(rollback_drain.get("initialGeneration"), int)
            and status["generation"] == rollback_drain["initialGeneration"] + 1
        ):
            known_generation = status["generation"]
            rollback_drain["generation"] = known_generation
        if status["mode"] == "draining" and known_generation is None:
            drain_attempt = journal["navigator"].get("drainAttempt")
            if (
                isinstance(drain_attempt, dict)
                and drain_attempt.get("containerId") == original_container
                and status["instanceId"] == journal["navigator"]["initialInstanceId"]
                and status["generation"] == drain_attempt.get("generation")
            ):
                known_generation = status["generation"]
                journal["navigator"]["drainGeneration"] = known_generation
        rollback_drain = journal["navigator"].get("rollbackDrain")
        if (
            status["mode"] == "draining"
            and known_generation is None
            and isinstance(rollback_drain, dict)
            and rollback_drain.get("containerId") == original_container
            and rollback_drain.get("instanceId") == status["instanceId"]
            and isinstance(rollback_drain.get("initialGeneration"), int)
            and rollback_drain.get("generation") == status["generation"]
        ):
            known_generation = status["generation"]
        if (
            status["mode"] == "accepting"
            and known_generation is None
            and isinstance(journal["navigator"].get("drainAttempt"), dict)
            and status["instanceId"] == journal["navigator"]["initialInstanceId"]
            and status["generation"] == journal["navigator"]["drainAttempt"].get("generation")
        ):
            known_generation = status["generation"]
            journal["navigator"]["drainGeneration"] = known_generation
        if status["mode"] == "draining":
            if status["generation"] != known_generation:
                raise RuntimeUpdateError("Navigator drain generation is unknown during rollback")
            _resume_navigator_instance(
                docker_prefix=docker_prefix,
                container_id=original_container,
                instance_id=journal["navigator"]["initialInstanceId"],
                generation=known_generation,
                timeout_seconds=30,
            )
        elif status["mode"] == "accepting":
            allowed_generations = {journal["navigator"]["initialGeneration"]}
            if isinstance(known_generation, int):
                allowed_generations.add(known_generation)
            if status["generation"] not in allowed_generations:
                raise RuntimeUpdateError("Navigator generation changed outside rollback")
        else:
            raise RuntimeUpdateError("Navigator mode is unknown after rollback")
    else:
        if "navigator" not in plan["targets"]:
            raise RuntimeUpdateError("Navigator container changed outside the selected targets")
        if (
            status["mode"] != "accepting"
            or status["generation"] != 0
            or status["token"] is not None
        ):
            raise RuntimeUpdateError(
                "replacement Navigator is not accepting in its initial generation"
            )
        journal["navigator"]["replacementContainerId"] = navigator["containerId"]


def _rollback_operation(
    *,
    plan: dict[str, Any],
    context: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
) -> None:
    """Restore only known candidate services to their prior immutable images."""

    _set_phase(journal, journal_path, "ROLLBACK_STARTING", state="ROLLING_BACK")
    _recover_edge_pause(
        journal=journal,
        journal_path=journal_path,
        docker_prefix=docker_prefix,
    )
    _recover_old_navigator_before_candidate_start(
        plan=plan,
        context=context,
        journal=journal,
        journal_path=journal_path,
        docker_prefix=docker_prefix,
    )
    image_options = {
        target: [_active_image_option(plan, target), _candidate_image_option(plan, target)]
        for target in plan["targets"]
    }
    current = _read_transition_state(
        plan=plan,
        context=context,
        docker_prefix=docker_prefix,
        image_overrides=image_options,
        health_exceptions=set(plan["targets"]),
    )
    for target in plan["targets"]:
        snapshot = current[target]
        progress = journal["targetProgress"].get(target)
        known_progress = isinstance(progress, dict) and progress.get("phase") in {
            "REPLACE_REQUESTED",
            "UPDATED_HEALTHY",
            "STOP_FAILED_RECOVERED",
            "RECOVERY_OLD_IMAGE_RESTARTED",
            "ROLLBACK_REQUESTED",
            "ROLLED_BACK",
        }
        if snapshot["imageId"] == _candidate_image_option(plan, target)["imageId"]:
            if (
                not known_progress
                or snapshot["containerId"] == plan["activeServices"][target]["containerId"]
            ):
                raise RuntimeUpdateError(
                    f"candidate {target} container is not owned by this operation"
                )
        elif snapshot["imageId"] == _active_image_option(plan, target)["imageId"]:
            if snapshot["containerId"] != plan["activeServices"][target]["containerId"] and not (
                known_progress
                and (
                    progress.get("phase") in {"ROLLBACK_REQUESTED", "ROLLED_BACK"}
                    or (
                        target == "navigator"
                        and progress.get("phase")
                        in {"STOP_FAILED_RECOVERED", "RECOVERY_OLD_IMAGE_RESTARTED"}
                        and isinstance(journal["navigator"].get("processRestart"), dict)
                        and journal["navigator"]["processRestart"].get("containerId")
                        == snapshot["containerId"]
                    )
                )
            ):
                raise RuntimeUpdateError(
                    f"active {target} container changed outside this operation"
                )
        else:
            raise RuntimeUpdateError(
                f"target {target} is on an unknown image; manual recovery is required"
            )

    changed_targets = {
        target
        for target in plan["targets"]
        if current[target]["imageId"] == _candidate_image_option(plan, target)["imageId"]
    }
    if changed_targets:
        _drain_current_navigator_for_rollback(
            plan=plan,
            journal=journal,
            journal_path=journal_path,
            docker_prefix=docker_prefix,
            container_id=current["navigator"]["containerId"],
            timeout_seconds=plan["policy"]["drainTimeoutSeconds"],
        )
    _verify_runtime_unmanaged(current, docker_prefix)

    active_inputs = plan["activeInputs"]
    active_compose_files = _input_paths(active_inputs, "composeFiles")
    active_env_files = _input_paths(active_inputs, "envFiles")
    for target in TARGET_ORDER:
        if target not in plan["targets"]:
            continue
        current = _read_transition_state(
            plan=plan,
            context=context,
            docker_prefix=docker_prefix,
            image_overrides={
                service: [
                    _active_image_option(plan, service),
                    _candidate_image_option(plan, service),
                ]
                for service in plan["targets"]
            },
            health_exceptions=set(plan["targets"]),
        )
        snapshot = current[target]
        active_image = _active_image_option(plan, target)
        if snapshot["imageId"] == active_image["imageId"]:
            if snapshot["health"] != "healthy":
                snapshot = _wait_for_exact_service_health(
                    plan=plan,
                    context=context,
                    docker_prefix=docker_prefix,
                    target=target,
                    image_option=active_image,
                    image_overrides={
                        service: [
                            _active_image_option(plan, service),
                            _candidate_image_option(plan, service),
                        ]
                        for service in plan["targets"]
                    },
                )
            continue
        progress = journal["targetProgress"].get(target)
        if not isinstance(progress, dict):
            raise RuntimeUpdateError(f"rollback lacks a recorded {target} replacement phase")
        progress["phase"] = "ROLLBACK_REQUESTED"
        _set_phase(journal, journal_path, f"ROLLBACK_{target.upper()}_REQUESTED")
        command = _compose_up(
            docker_prefix=docker_prefix,
            project=plan["project"],
            compose_files=active_compose_files,
            env_files=active_env_files,
            service=target,
            timeout_seconds=plan["policy"]["healthTimeoutSeconds"] + 60,
        )
        current = _read_transition_state(
            plan=plan,
            context=context,
            docker_prefix=docker_prefix,
            image_overrides={
                service: [
                    _active_image_option(plan, service),
                    _candidate_image_option(plan, service),
                ]
                for service in plan["targets"]
            },
            health_exceptions=set(plan["targets"]),
        )
        snapshot = current[target]
        if snapshot["imageId"] != active_image["imageId"]:
            if (
                command.returncode != 0
                and snapshot["imageId"] == _candidate_image_option(plan, target)["imageId"]
            ):
                raise RuntimeUpdateError(f"rollback command failed for {target}")
            raise RuntimeUpdateError(
                f"rollback did not restore the active immutable {target} image"
            )
        if (
            snapshot["containerId"] == progress["oldContainerId"]
            and snapshot["imageId"] != progress["oldImageId"]
        ):
            raise RuntimeUpdateError(f"rollback {target} container identity is inconsistent")
        if snapshot["health"] != "healthy":
            snapshot = _wait_for_exact_service_health(
                plan=plan,
                context=context,
                docker_prefix=docker_prefix,
                target=target,
                image_option=active_image,
                image_overrides={
                    service: [
                        _active_image_option(plan, service),
                        _candidate_image_option(plan, service),
                    ]
                    for service in plan["targets"]
                },
            )
        progress["phase"] = "ROLLED_BACK"
        progress["newContainerId"] = snapshot["containerId"]
        if target == "navigator":
            journal["navigator"]["replacementContainerId"] = snapshot["containerId"]
        journal["updatedTargets"] = [item for item in journal["updatedTargets"] if item != target]
        _set_phase(journal, journal_path, f"ROLLBACK_{target.upper()}_HEALTHY")

    final_overrides = {target: _active_image_option(plan, target) for target in plan["targets"]}
    services = _read_transition_state(
        plan=plan,
        context=context,
        docker_prefix=docker_prefix,
        image_overrides=final_overrides,
    )
    _verify_runtime_unmanaged(services, docker_prefix)
    _verify_live_network_membership(context["activeCompose"], services, docker_prefix)
    _resume_navigator_after_rollback(
        plan=plan,
        journal=journal,
        docker_prefix=docker_prefix,
        current=services,
    )
    journal["navigator"]["resumed"] = True
    _resume_edge(journal=journal, journal_path=journal_path, docker_prefix=docker_prefix)
    _set_phase(
        journal,
        journal_path,
        "ROLLBACK_COMPLETE",
        state="FAILED_ROLLED_BACK",
        error_code="update_failed_rolled_back",
    )


def _execute_update(
    *,
    plan: dict[str, Any],
    context: dict[str, Any],
    journal: dict[str, Any],
    journal_path: Path,
    docker_prefix: list[str],
) -> dict[str, Any]:
    """Run a paused-edge, drained-Nav update and roll back only known immutable states."""

    try:
        _pause_edge(
            plan=plan,
            journal=journal,
            journal_path=journal_path,
            docker_prefix=docker_prefix,
        )
        _drain_navigator(
            plan=plan,
            journal=journal,
            journal_path=journal_path,
            docker_prefix=docker_prefix,
        )
        for target in plan["policy"]["updateOrder"]:
            _update_target(
                plan=plan,
                context=context,
                journal=journal,
                journal_path=journal_path,
                docker_prefix=docker_prefix,
                target=target,
            )
        _ensure_navigator_resumed(
            plan=plan,
            context=context,
            journal=journal,
            journal_path=journal_path,
            docker_prefix=docker_prefix,
        )
        _resume_edge(journal=journal, journal_path=journal_path, docker_prefix=docker_prefix)
        _verify_final_edge_delivery_readback(journal=journal, docker_prefix=docker_prefix)
        _set_phase(journal, journal_path, "UPDATE_COMPLETE", state="APPLIED")
        return _journal_summary(journal)
    except RuntimeUpdateOutcomeUnknown as failure:
        error_code = journal.get("lastErrorCode")
        if not isinstance(error_code, str) or not error_code:
            error_code = "docker_mutation_outcome_unknown"
        _set_phase(
            journal,
            journal_path,
            "RECOVERY_REQUIRED",
            state="RECOVERY_REQUIRED",
            error_code=error_code,
        )
        raise RuntimeUpdateError(
            "Docker mutation outcome is unknown; no rollback or edge resume was attempted. Use status and reconcile the exact container state before manual recovery."
        ) from failure
    except (RuntimeUpdateError, OSError, subprocess.SubprocessError) as failure:
        try:
            _rollback_operation(
                plan=plan,
                context=context,
                journal=journal,
                journal_path=journal_path,
                docker_prefix=docker_prefix,
            )
        except (RuntimeUpdateError, OSError, subprocess.SubprocessError) as rollback_failure:
            error_code = (
                "docker_rollback_outcome_unknown"
                if isinstance(rollback_failure, RuntimeUpdateOutcomeUnknown)
                else "safe_rollback_not_verified"
            )
            _set_phase(
                journal,
                journal_path,
                "RECOVERY_REQUIRED",
                state="RECOVERY_REQUIRED",
                error_code=error_code,
            )
            raise RuntimeUpdateError(
                "update failed and automatic rollback could not be verified; edge delivery remains paused when its hold is known"
            ) from rollback_failure
        raise RuntimeUpdateError(
            f"update failed; the previously locked services were restored ({type(failure).__name__})"
        ) from failure


def apply_plan_file(
    plan_path: Path,
    *,
    expected_digest: str,
    journal_dir: Path = DEFAULT_JOURNAL_DIR,
) -> dict[str, Any]:
    """Freshly verify and apply one owner-reviewed private plan exactly once."""

    _require_update_operator()
    plan = _load_plan_file(plan_path, expected_digest)
    docker_prefix = plan["policy"]["dockerPrefix"]
    _validate_docker_prefix(docker_prefix)
    _ensure_journal_directory(journal_dir, create=True)
    journal_path = _journal_path(journal_dir, plan["operationId"])
    with _operation_lock(plan["project"]):
        if journal_path.exists() or journal_path.is_symlink():
            raise RuntimeUpdateError(
                "this plan already has an operation journal; use resume or status"
            )
        context = _resolve_plan_inputs(
            plan,
            docker_prefix=docker_prefix,
            run_full_preflight=True,
            inspect_active=True,
        )
        _validate_initial_runtime_state(plan, context, docker_prefix)
        journal = _new_journal(plan, plan_path)
        _atomic_journal_write(journal_path, journal)
        return _execute_update(
            plan=plan,
            context=context,
            journal=journal,
            journal_path=journal_path,
            docker_prefix=docker_prefix,
        )


def _plan_for_journal(journal: dict[str, Any], expected_digest: str) -> tuple[dict[str, Any], Path]:
    """Load the exact plan bound to one durable operation journal."""

    if expected_digest != journal.get("planSha256"):
        raise RuntimeUpdateError("operation confirmation digest does not match its journal")
    plan_path_value = journal.get("planPath")
    if not isinstance(plan_path_value, str):
        raise RuntimeUpdateError("operation journal has no reviewed plan reference")
    plan_path = Path(plan_path_value)
    plan = _load_plan_file(plan_path, expected_digest)
    if plan.get("operationId") != journal.get("operationId"):
        raise RuntimeUpdateError("operation journal and reviewed plan references differ")
    return plan, plan_path


def _has_unresolved_docker_mutation(journal: dict[str, Any]) -> bool:
    """Detect durable pre-call intents whose Docker daemon outcome may be unknown."""

    if isinstance(journal.get("unknownDockerMutation"), dict):
        return True
    if journal.get("phase") == "NAVIGATOR_ACTIVE_IMAGE_RESTART_REQUESTED":
        intent = journal.get("navigator", {}).get("restartIntent")
        if not (
            isinstance(intent, dict)
            and intent.get("kind") == "active-image-restart"
            and intent.get("composeCommandExitCode") == 0
            and isinstance(intent.get("composeCommandReturnedAtUtc"), str)
        ):
            return True
    if journal.get("phase") in {
        "NAVIGATOR_STOP_REQUESTED",
        "REPLACING_EXCHANGE",
        "REPLACING_NAVIGATOR",
        "ROLLBACK_EXCHANGE_REQUESTED",
        "ROLLBACK_NAVIGATOR_REQUESTED",
    }:
        stop_record = journal.get("navigator", {}).get("stop")
        if journal.get("phase") != "NAVIGATOR_STOP_REQUESTED" or not (
            isinstance(stop_record, dict)
            and isinstance(stop_record.get("commandExitCode"), int)
            and isinstance(stop_record.get("graceful"), bool)
            and isinstance(stop_record.get("startedAt"), str)
            and isinstance(stop_record.get("finishedAt"), str)
        ):
            return True
    if isinstance(journal.get("navigator", {}).get("stop"), dict):
        stop_record = journal["navigator"]["stop"]
        if (
            stop_record.get("commandExitCode") is None
            or stop_record.get("exitCode") is None
            or stop_record.get("finishedAt") is None
            or stop_record.get("graceful") is None
        ):
            return True
    for progress in journal.get("targetProgress", {}).values():
        if isinstance(progress, dict) and progress.get("phase") in {
            "REPLACE_REQUESTED",
            "REPLACE_OUTCOME_UNKNOWN",
            "ROLLBACK_REQUESTED",
        }:
            return True
    return False


def resume_operation(
    operation_id: str,
    *,
    expected_digest: str,
    journal_dir: Path = DEFAULT_JOURNAL_DIR,
) -> dict[str, Any]:
    """Conservatively reconcile a nonterminal operation back to its prior locked images."""

    _require_update_operator()
    _ensure_journal_directory(journal_dir, create=False)
    journal_path = _journal_path(journal_dir, operation_id)
    snapshot = _read_journal(journal_path)
    if snapshot["state"] in {"APPLIED", "FAILED_ROLLED_BACK"}:
        if expected_digest != snapshot["planSha256"]:
            raise RuntimeUpdateError("operation confirmation digest does not match its journal")
        return _journal_summary(snapshot)
    snapshot_plan, _plan_path = _plan_for_journal(snapshot, expected_digest)
    with _operation_lock(snapshot_plan["project"]):
        journal = _read_journal(journal_path)
        plan, _plan_path = _plan_for_journal(journal, expected_digest)
        if plan["project"] != snapshot_plan["project"]:
            raise RuntimeUpdateError("operation project changed while acquiring the update lock")
        if journal["state"] in {"APPLIED", "FAILED_ROLLED_BACK"}:
            return _journal_summary(journal)
        docker_prefix = plan["policy"]["dockerPrefix"]
        _validate_docker_prefix(docker_prefix)
        unknown_error_codes = {
            "compose_update_outcome_unknown",
            "docker_rollback_outcome_unknown",
            "navigator_stop_outcome_unknown",
            "navigator_restart_outcome_unknown",
        }
        if journal.get("lastErrorCode") in unknown_error_codes or _has_unresolved_docker_mutation(
            journal
        ):
            error_code = journal.get("lastErrorCode")
            if error_code not in unknown_error_codes:
                error_code = "docker_mutation_intent_unresolved"
            try:
                _recover_edge_pause(
                    journal=journal,
                    journal_path=journal_path,
                    docker_prefix=docker_prefix,
                    timeout_seconds=plan["policy"]["drainTimeoutSeconds"] + 15,
                )
            except (RuntimeUpdateError, OSError, subprocess.SubprocessError) as error:
                _set_phase(
                    journal,
                    journal_path,
                    "RECOVERY_REQUIRED",
                    state="RECOVERY_REQUIRED",
                    error_code=error_code,
                )
                raise RuntimeUpdateError(
                    "Docker mutation outcome remains unknown and the edge hold could not be reconfirmed"
                ) from error
            _set_phase(
                journal,
                journal_path,
                "RECOVERY_REQUIRED",
                state="RECOVERY_REQUIRED",
                error_code=error_code,
            )
            raise RuntimeUpdateError(
                "Docker mutation outcome remains unknown; the edge hold is confirmed and no replacement was retried"
            )
        context = _resolve_plan_inputs(
            plan,
            docker_prefix=docker_prefix,
            run_full_preflight=True,
            inspect_active=False,
            recovery_only=True,
        )
        try:
            _rollback_operation(
                plan=plan,
                context=context,
                journal=journal,
                journal_path=journal_path,
                docker_prefix=docker_prefix,
            )
        except (RuntimeUpdateError, OSError, subprocess.SubprocessError) as error:
            error_code = (
                "docker_rollback_outcome_unknown"
                if isinstance(error, RuntimeUpdateOutcomeUnknown)
                else "safe_rollback_not_verified"
            )
            _set_phase(
                journal,
                journal_path,
                "RECOVERY_REQUIRED",
                state="RECOVERY_REQUIRED",
                error_code=error_code,
            )
            raise RuntimeUpdateError(
                "operation remains in recovery-required state; no unverified container was stopped or replaced"
            ) from error
        return _journal_summary(journal)


def _write_private_json(path: Path, document: dict[str, Any], *, require_artifact: bool) -> None:
    """Atomically write one owner-only JSON plan or journal document."""

    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise RuntimeUpdateError("private output directory is unavailable")
    parent_metadata = parent.stat()
    if (
        parent.resolve(strict=True) != parent
        or parent_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
    ):
        raise RuntimeUpdateError("private output directory must be owner-only and non-symlinked")
    if require_artifact and not preflight._private_artifact(path):
        raise RuntimeUpdateError("reviewed update plans must stay under private umbrella artifacts")
    if path.exists() or path.is_symlink():
        raise RuntimeUpdateError("refusing to overwrite an existing runtime update record")
    payload = _canonical_json(document) + b"\n"
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, path, follow_symlinks=False)
        except FileExistsError as error:
            raise RuntimeUpdateError(
                "refusing to overwrite an existing runtime update record"
            ) from error
        temporary_path.unlink()
        directory_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except RuntimeUpdateError:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except OSError as error:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeUpdateError("private runtime update record could not be saved") from error


def _validate_plan_output_path(path: Path) -> None:
    """Require a plan destination that cannot overwrite source or public files."""

    if path.is_symlink() or not preflight._private_artifact(path):
        raise RuntimeUpdateError("plan output must be a non-symlink file under private artifacts")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise RuntimeUpdateError("plan output directory is unavailable")
    metadata = parent.stat()
    if (
        parent.resolve(strict=True) != parent
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RuntimeUpdateError("plan output directory must be owner-only and non-symlinked")


def create_plan(
    *,
    active_compose_files: list[Path],
    active_env_files: list[Path],
    active_image_lock: Path,
    candidate_compose_files: list[Path],
    candidate_env_files: list[Path],
    candidate_image_lock: Path,
    secrets_dir: Path,
    config_dir: Path,
    targets: list[str],
    plan_path: Path,
    docker_prefix: list[str],
    drain_timeout_seconds: int,
    health_timeout_seconds: int,
    max_memory_mib: int,
    max_cpus: float,
    required_free_gib: float,
    minimum_mem_available_mib: int,
) -> dict[str, Any]:
    """Build a read-only plan bound to exact active containers and candidate files."""

    _require_update_operator()
    if (
        not targets
        or any(not isinstance(target, str) for target in targets)
        or set(targets) - ALLOWED_TARGETS
        or len(set(targets)) != len(targets)
    ):
        raise RuntimeUpdateError(
            "targets must be a unique subset of github-issues, exchange, and navigator"
        )
    _validate_docker_prefix(docker_prefix)
    ordered_targets = sorted(targets)
    if not 60 <= drain_timeout_seconds <= 900 or not 30 <= health_timeout_seconds <= 600:
        raise RuntimeUpdateError("drain and health timeouts are outside their bounded ranges")
    _validate_plan_output_path(plan_path)
    if (
        isinstance(max_memory_mib, bool)
        or not isinstance(max_memory_mib, int)
        or max_memory_mib <= 0
        or not math.isfinite(max_cpus)
        or max_cpus <= 0
        or not math.isfinite(required_free_gib)
        or required_free_gib < 0
        or isinstance(minimum_mem_available_mib, bool)
        or not isinstance(minimum_mem_available_mib, int)
        or minimum_mem_available_mib <= 0
    ):
        raise RuntimeUpdateError("preflight resource limits are outside their valid range")
    active_inputs = {
        "composeFiles": _record_files(active_compose_files, "active Compose file"),
        "envFiles": _record_files(active_env_files, "active environment file"),
        "imageLock": {
            "path": str(active_image_lock.resolve(strict=True)),
            "sha256": _sha256_file(active_image_lock),
        },
    }
    candidate_inputs = {
        "composeFiles": _record_files(candidate_compose_files, "candidate Compose file"),
        "envFiles": _record_files(candidate_env_files, "candidate environment file"),
        "imageLock": {
            "path": str(candidate_image_lock.resolve(strict=True)),
            "sha256": _sha256_file(candidate_image_lock),
        },
    }
    active_lock = _load_lock(active_image_lock)
    candidate_lock = _load_lock(candidate_image_lock)
    _validate_lock_pair(active_lock, candidate_lock, ordered_targets)
    try:
        active_compose = preflight._compose_config(
            docker_prefix, active_compose_files, active_env_files
        )
        candidate_compose = preflight._compose_config(
            docker_prefix, candidate_compose_files, candidate_env_files
        )
    except (OSError, preflight.PreflightError, subprocess.SubprocessError) as error:
        raise RuntimeUpdateError("active or candidate Compose could not be rendered") from error
    _validate_compose_pair(active_compose, candidate_compose, ordered_targets)
    _validate_runtime_activity_static(active_compose)
    _validate_runtime_activity_static(candidate_compose)
    _validate_standalone_callers(active_compose)
    _validate_standalone_callers(candidate_compose)
    active_targets = _target_images(active_compose, active_lock, ordered_targets)
    candidate_targets = _target_images(candidate_compose, candidate_lock, ordered_targets)
    unchanged_targets = [
        target
        for target in ordered_targets
        if active_targets[target]["imageId"] == candidate_targets[target]["imageId"]
        or active_targets[target]["image"] == candidate_targets[target]["image"]
    ]
    if unchanged_targets:
        raise RuntimeUpdateError(
            "each selected target must use a distinct candidate image reference and image ID"
        )
    active_preflight = _run_preflight(
        compose_files=active_compose_files,
        env_files=active_env_files,
        image_lock=active_image_lock,
        secrets_dir=secrets_dir,
        config_dir=config_dir,
        docker_prefix=docker_prefix,
        max_memory_mib=max_memory_mib,
        max_cpus=max_cpus,
        required_free_gib=required_free_gib,
        minimum_mem_available_mib=minimum_mem_available_mib,
    )
    candidate_preflight = _run_preflight(
        compose_files=candidate_compose_files,
        env_files=candidate_env_files,
        image_lock=candidate_image_lock,
        secrets_dir=secrets_dir,
        config_dir=config_dir,
        docker_prefix=docker_prefix,
        max_memory_mib=max_memory_mib,
        max_cpus=max_cpus,
        required_free_gib=required_free_gib,
        minimum_mem_available_mib=minimum_mem_available_mib,
    )
    active_services = _inspect_project_services(
        compose=active_compose,
        image_lock=active_lock,
        docker_prefix=docker_prefix,
    )
    _verify_runtime_unmanaged(active_services, docker_prefix)
    _verify_live_network_membership(active_compose, active_services, docker_prefix)
    edge_service = active_services.get("teams-ingress")
    navigator_service = active_services.get("navigator")
    if not isinstance(edge_service, dict) or not isinstance(navigator_service, dict):
        raise RuntimeUpdateError("active Teams ingress or Navigator container is missing")
    edge_status = _call_internal_api(
        docker_prefix,
        edge_service["containerId"],
        service="teams-ingress",
        method="GET",
        path="/_internal/teams/delivery/status",
        body=None,
        timeout_seconds=15,
    )
    _validate_edge_status(edge_status)
    if edge_status["paused"] is not False:
        raise RuntimeUpdateError("Teams delivery spool is already paused by another operation")
    if edge_status["dead_count"] != 0:
        raise RuntimeUpdateError(
            "dead-letter Teams messages require explicit owner recovery before update"
        )
    navigator_status = _call_internal_api(
        docker_prefix,
        navigator_service["containerId"],
        service="navigator",
        method="GET",
        path="/internal/v1/executor/drain",
        body=None,
        timeout_seconds=15,
    )
    _validate_navigator_status(navigator_status)
    if navigator_status["mode"] != "accepting":
        raise RuntimeUpdateError("Navigator is not accepting work before update planning")
    plan: dict[str, Any] = {
        "schemaVersion": PLAN_SCHEMA,
        "operationId": str(uuid.uuid4()),
        "createdAtUtc": _utc_now(),
        "project": active_compose.get("name"),
        "targets": ordered_targets,
        "activeInputs": active_inputs,
        "candidateInputs": candidate_inputs,
        "activeSourceIdentitySha256": _source_identity_sha256(active_lock, ordered_targets),
        "candidateSourceIdentitySha256": _source_identity_sha256(candidate_lock, ordered_targets),
        "candidateImages": candidate_targets,
        "activeServices": active_services,
        "edgeDelivery": {
            "service": "teams-ingress",
            "containerId": edge_service["containerId"],
            "imageRef": edge_service["imageRef"],
            "imageId": edge_service["imageId"],
            "revision": edge_status["revision"],
            "deadCount": edge_status["dead_count"],
        },
        "navigatorDrain": {
            "containerId": navigator_service["containerId"],
            "imageRef": navigator_service["imageRef"],
            "imageId": navigator_service["imageId"],
            "instanceId": navigator_status["instanceId"],
            "generation": navigator_status["generation"],
        },
        "policy": {
            "scope": "standalone-teams",
            "managedRuntimeMaintenance": "UNMANAGED_REQUIRED",
            "targetsOnly": list(TARGET_ORDER),
            "updateOrder": [target for target in TARGET_ORDER if target in ordered_targets],
            "edgeImageReplacement": False,
            "composeNoDeps": True,
            "dockerPrefix": docker_prefix,
            "drainTimeoutSeconds": drain_timeout_seconds,
            "healthTimeoutSeconds": health_timeout_seconds,
        },
        "preflight": {
            "mode": "stage1",
            "activeDeploymentReady": active_preflight["deploymentReady"],
            "candidateDeploymentReady": candidate_preflight["deploymentReady"],
            "secretsDir": str(secrets_dir.resolve(strict=True)),
            "configDir": str(config_dir.resolve(strict=True)),
            "maxMemoryMiB": max_memory_mib,
            "maxCpus": max_cpus,
            "requiredFreeGiB": required_free_gib,
            "minimumMemAvailableMiB": minimum_mem_available_mib,
        },
    }
    plan["planSha256"] = _plan_digest(plan)
    _validate_plan(plan)
    _write_private_json(plan_path, plan, require_artifact=True)
    return {"operationId": plan["operationId"], "planSha256": plan["planSha256"]}


def _run_command(
    argv: list[str],
    *,
    input_data: str | None = None,
    timeout_seconds: float = 30.0,
    external_mutation: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one argv-only Docker command while keeping raw output internal."""

    try:
        return subprocess.run(
            argv,
            input=input_data,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        if external_mutation:
            raise RuntimeUpdateOutcomeUnknown(
                "Docker mutation timed out; the daemon outcome is unknown"
            ) from error
        raise RuntimeUpdateError(f"Docker command timed out: {Path(argv[0]).name}") from error
    except OSError as error:
        raise RuntimeUpdateError(f"Docker command is unavailable: {Path(argv[0]).name}") from error


def _checked_stdout(argv: list[str], *, label: str, timeout_seconds: float = 30.0) -> str:
    """Require one read-only Docker command to succeed without echoing stderr."""

    result = _run_command(argv, timeout_seconds=timeout_seconds)
    if result.returncode != 0:
        raise RuntimeUpdateError(f"read-only {label} command failed")
    return result.stdout


def _decode_json_output(text: str, label: str) -> Any:
    """Parse bounded, duplicate-key-free Docker/API JSON output."""

    if len(text) > 16 * 1024 * 1024:
        raise RuntimeUpdateError(f"{label} output exceeds its size limit")
    try:
        return json.loads(
            text,
            object_pairs_hook=preflight._unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise RuntimeUpdateError(f"{label} returned invalid JSON") from error


_INTERNAL_HTTP_SCRIPT = r"""
import json
import signal
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

class RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None

token_path, url, method = sys.argv[1:4]
request_timeout = float(sys.argv[4])
# Keep the one-shot container bounded even if a peer sends a slow response.
signal.setitimer(signal.ITIMER_REAL, request_timeout)
try:
    token = Path(token_path).read_text(encoding="ascii").strip()
    payload = sys.stdin.read(65537)
    if not token or len(payload) > 65536:
        raise ValueError("invalid internal request input")
    body = payload.encode("utf-8") if method == "POST" else None
    request = Request(
        url,
        data=body,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method=method,
    )
    try:
        response = build_opener(RejectRedirectHandler()).open(
            request, timeout=request_timeout
        )
        status_code = response.status
        response_bytes = response.read(65537)
    except HTTPError as error:
        status_code = error.code
        response_bytes = error.read(65537)
    if len(response_bytes) > 65536:
        raise ValueError("internal response exceeds limit")
    try:
        response_document = json.loads(response_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        response_document = None
    if not isinstance(response_document, dict):
        response_document = {}
    retry_dead_route = method == "POST" and url.endswith(
        "/_internal/teams/delivery/retry-dead"
    )
    if retry_dead_route:
        retried_count = response_document.get("retried_count")
        if (
            isinstance(retried_count, bool)
            or not isinstance(retried_count, int)
            or retried_count < 0
        ):
            raise ValueError("internal retry count is malformed")
    if "/internal/v1/executor/drain" in url:
        fields = {
            "mode", "generation", "token", "startedAt", "instanceId", "countsComplete",
            "activeTasks", "queuedTasks", "dispatches", "notifications", "activeWorkDrained",
        }
    else:
        fields = {
            "paused", "revision", "active_delivery_count", "queued_count", "leased_count",
            "retry_wait_count", "delivered_count", "rejected_count", "dead_count",
            "pending_bytes", "retained_body_bytes", "oldest_pending_at", "last_delivered_at",
        }
        if retry_dead_route:
            fields.add("retried_count")
    safe_document = {name: response_document[name] for name in fields if name in response_document}
    print(json.dumps({"httpStatus": status_code, "document": safe_document}, separators=(",", ":")))
except (OSError, UnicodeError, ValueError, URLError):
    print(json.dumps({"bridgeError": "internal_request_failed"}, separators=(",", ":")))
    sys.exit(2)
"""


def _internal_control_probe_target(
    docker_prefix: list[str],
    container_id: str,
    *,
    service: str,
    token_path: str,
) -> tuple[str, str, str]:
    """Resolve one running target image and its sole read-only control token mount."""

    inspect_format = (
        "{{json .Id}}\n"
        "{{json .Image}}\n"
        "{{json .Config.Image}}\n"
        "{{json .Config.Labels}}\n"
        "{{json .State.Running}}\n"
        "{{json .State.Paused}}\n"
        "{{json .HostConfig.NetworkMode}}\n"
        "{{json .Mounts}}"
    )
    output = _checked_stdout(
        [*docker_prefix, "inspect", "--format", inspect_format, container_id],
        label="internal control target inspection",
        timeout_seconds=15,
    )
    try:
        inspected = [
            _decode_json_output(line, "internal control target metadata")
            for line in output.splitlines()
        ]
    except RuntimeUpdateError as error:
        raise RuntimeUpdateError("internal control target inspection is malformed") from error
    if len(inspected) != 8:
        raise RuntimeUpdateError("internal control target inspection is incomplete")
    actual_id, image_id, image_ref, labels, running, paused, network_mode, mounts = inspected
    project = labels.get("com.docker.compose.project") if isinstance(labels, dict) else None
    service_label = labels.get("com.docker.compose.service") if isinstance(labels, dict) else None
    if (
        not isinstance(actual_id, str)
        or FULL_CONTAINER_ID_RE.fullmatch(actual_id) is None
        or not actual_id.startswith(container_id)
        or not isinstance(image_id, str)
        or IMAGE_ID_RE.fullmatch(image_id) is None
        or not isinstance(image_ref, str)
        or not image_ref
        or not isinstance(project, str)
        or not project
        or service_label != service
        or running is not True
        or paused is not False
        or not isinstance(network_mode, str)
        or not network_mode
        or network_mode == "host"
        or not isinstance(mounts, list)
    ):
        raise RuntimeUpdateError("internal control target identity is unavailable")

    token_mounts = [
        mount
        for mount in mounts
        if isinstance(mount, dict) and mount.get("Destination") == token_path
    ]
    if len(token_mounts) != 1:
        raise RuntimeUpdateError("internal control token mount is missing or ambiguous")
    mount = token_mounts[0]
    token_source = mount.get("Source")
    if (
        mount.get("Type") != "bind"
        or mount.get("RW") is not False
        or not isinstance(token_source, str)
        or not Path(token_source).is_absolute()
        or any(character in token_source for character in ',\r\n"')
    ):
        raise RuntimeUpdateError("internal control token mount is not a read-only file bind")
    token_source_path = Path(token_source)
    try:
        source_metadata = token_source_path.lstat()
        resolved_source = token_source_path.resolve(strict=True)
    except OSError as error:
        raise RuntimeUpdateError("internal control token source is unavailable") from error
    if (
        stat.S_ISLNK(source_metadata.st_mode)
        or not stat.S_ISREG(source_metadata.st_mode)
        or resolved_source != token_source_path
    ):
        raise RuntimeUpdateError("internal control token source is not a regular file")
    return actual_id, image_id, str(resolved_source)


def _call_internal_api(
    docker_prefix: list[str],
    container_id: str,
    *,
    service: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Call one fixed loopback control endpoint with its mounted token kept in-container."""

    if CONTAINER_ID_RE.fullmatch(container_id) is None:
        raise RuntimeUpdateError("internal control container ID is malformed")
    if service == "navigator":
        base_url = NAVIGATOR_BASE_URL
        token_path = NAVIGATOR_TOKEN_FILE
        allowed = {
            ("GET", "/internal/v1/executor/drain"),
            ("POST", "/internal/v1/executor/drain"),
            ("POST", "/internal/v1/executor/drain/resume"),
        }
    elif service == "teams-ingress":
        base_url = TEAMS_INGRESS_BASE_URL
        token_path = "/run/secrets/teams-delivery-admin-token"
        allowed = {
            ("GET", "/_internal/teams/delivery/status"),
            ("POST", "/_internal/teams/delivery/pause"),
            ("POST", "/_internal/teams/delivery/resume"),
            ("POST", "/_internal/teams/delivery/retry-dead"),
        }
    else:
        raise RuntimeUpdateError("internal control service is unsupported")
    if (method, path) not in allowed or method not in {"GET", "POST"}:
        raise RuntimeUpdateError("internal control route is outside the closed allowlist")
    target_id, image_id, token_source = _internal_control_probe_target(
        docker_prefix,
        container_id,
        service=service,
        token_path=token_path,
    )
    request_body = "" if body is None else json.dumps(body, sort_keys=True, separators=(",", ":"))
    argv = [
        *docker_prefix,
        "run",
        "--rm",
        "--pull=never",
        "--interactive",
        "--network",
        f"container:{target_id}",
        "--entrypoint",
        "python3",
        "--user",
        "10001:10001",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--memory",
        "64m",
        "--memory-swap",
        "64m",
        "--cpus",
        "0.1",
        "--pids-limit",
        "32",
        "--mount",
        f"type=bind,src={token_source},dst={token_path},readonly",
        image_id,
        "-c",
        _INTERNAL_HTTP_SCRIPT,
        token_path,
        base_url + path,
        method,
        str(max(1.0, timeout_seconds - 2.0)),
    ]
    result = _run_command(
        argv,
        input_data=request_body,
        timeout_seconds=timeout_seconds,
        external_mutation=method == "POST",
    )
    if result.returncode != 0:
        if method == "POST":
            raise RuntimeUpdateOutcomeUnknown(
                f"internal {service} POST probe failed; operation outcome is unknown"
            )
        raise RuntimeUpdateError(f"internal {service} control request did not complete")
    try:
        envelope = _decode_json_output(result.stdout, f"internal {service} control request")
    except RuntimeUpdateError as error:
        if method == "POST":
            raise RuntimeUpdateOutcomeUnknown(
                f"internal {service} POST probe returned an unreadable response"
            ) from error
        raise
    if not isinstance(envelope, dict) or set(envelope) not in (
        {"httpStatus", "document"},
        {"bridgeError"},
    ):
        if method == "POST":
            raise RuntimeUpdateOutcomeUnknown(
                f"internal {service} POST probe response is ambiguous"
            )
        raise RuntimeUpdateError(f"internal {service} control response is malformed")
    if "bridgeError" in envelope:
        if method == "POST":
            raise RuntimeUpdateOutcomeUnknown(f"internal {service} POST probe outcome is unknown")
        raise RuntimeUpdateError(f"internal {service} control request failed")
    if not isinstance(envelope.get("httpStatus"), int) or not isinstance(
        envelope.get("document"), dict
    ):
        if method == "POST":
            raise RuntimeUpdateOutcomeUnknown(
                f"internal {service} POST probe response is ambiguous"
            )
        raise RuntimeUpdateError(f"internal {service} control response is malformed")
    if method == "POST" and envelope["httpStatus"] != 200:
        raise RuntimeUpdateOutcomeUnknown(
            f"internal {service} POST returned a non-success status; operation outcome is unknown"
        )
    response = dict(envelope["document"])
    response["_httpStatus"] = envelope["httpStatus"]
    return response


def _require_http_success(response: dict[str, Any], label: str) -> dict[str, Any]:
    """Convert one internal HTTP status to a bounded error without exposing response text."""

    document = dict(response)
    status_code = document.pop("_httpStatus", None)
    if status_code != 200:
        raise RuntimeUpdateError(f"{label} returned HTTP {status_code}")
    return document


def _validate_edge_status(response: dict[str, Any]) -> None:
    """Require the frozen Teams durable-spool status shape and bounded counters."""

    document = _require_http_success(response, "Teams delivery status")
    if set(document) != EDGE_STATUS_FIELDS:
        raise RuntimeUpdateError("Teams delivery status fields do not match the frozen contract")
    if not isinstance(document.get("paused"), bool):
        raise RuntimeUpdateError("Teams delivery paused state is malformed")
    if any(
        isinstance(document.get(name), bool)
        or not isinstance(document.get(name), int)
        or document[name] < 0
        for name in (
            "revision",
            "active_delivery_count",
            "queued_count",
            "leased_count",
            "retry_wait_count",
            "delivered_count",
            "rejected_count",
            "dead_count",
            "pending_bytes",
            "retained_body_bytes",
        )
    ):
        raise RuntimeUpdateError("Teams delivery counters or revision are malformed")
    if any(
        (value := document.get(name)) is not None
        and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < 0
            or (isinstance(value, float) and not math.isfinite(value))
        )
        for name in ("oldest_pending_at", "last_delivered_at")
    ):
        raise RuntimeUpdateError("Teams delivery timestamps are malformed")
    response.clear()
    response.update(document)


def _validate_navigator_status(response: dict[str, Any]) -> None:
    """Require the frozen Navigator drain shape without persisting its resume token."""

    document = _require_http_success(response, "Navigator executor drain status")
    if set(document) != NAVIGATOR_DRAIN_FIELDS:
        raise RuntimeUpdateError("Navigator drain fields do not match the frozen contract")
    if (
        document.get("mode") not in {"accepting", "draining"}
        or not isinstance(document.get("instanceId"), str)
        or not document["instanceId"]
        or isinstance(document.get("generation"), bool)
        or not isinstance(document.get("generation"), int)
        or document["generation"] < 0
        or isinstance(document.get("activeTasks"), bool)
        or not isinstance(document.get("activeTasks"), int)
        or document["activeTasks"] < 0
        or isinstance(document.get("queuedTasks"), bool)
        or not isinstance(document.get("queuedTasks"), int)
        or document["queuedTasks"] < 0
        or not isinstance(document.get("activeWorkDrained"), bool)
    ):
        raise RuntimeUpdateError("Navigator drain status is malformed")
    token = document.get("token")
    if token is not None and (not isinstance(token, str) or not token or len(token) > 4096):
        raise RuntimeUpdateError("Navigator drain operation token is malformed")
    started_at = document.get("startedAt")
    if started_at is not None and (
        isinstance(started_at, bool) or not isinstance(started_at, int) or started_at < 0
    ):
        raise RuntimeUpdateError("Navigator drain start timestamp is malformed")
    dispatches = document.get("dispatches")
    if (
        not isinstance(dispatches, dict)
        or set(dispatches) != {"active", "waiting"}
        or any(
            isinstance(dispatches.get(field), bool)
            or not isinstance(dispatches.get(field), int)
            or dispatches[field] < 0
            for field in ("active", "waiting")
        )
    ):
        raise RuntimeUpdateError("Navigator dispatch drain status is malformed")
    notifications = document.get("notifications")
    if (
        not isinstance(notifications, dict)
        or set(notifications) != {"configured", "pollingPaused", "inFlight"}
        or not isinstance(notifications.get("configured"), bool)
        or not isinstance(notifications.get("pollingPaused"), bool)
        or not isinstance(notifications.get("inFlight"), bool)
    ):
        raise RuntimeUpdateError("Navigator notification drain status is malformed")
    if document.get("countsComplete") is not True:
        raise RuntimeUpdateError("Navigator durable task counts are incomplete")
    if (
        document["mode"] == "accepting"
        and (token is not None or started_at is not None or notifications["pollingPaused"])
    ) or (
        document["mode"] == "draining"
        and (token is None or started_at is None or not notifications["pollingPaused"])
    ):
        raise RuntimeUpdateError("Navigator drain mode does not match its fencing metadata")
    if document["activeWorkDrained"] and (
        document["activeTasks"] != 0
        or dispatches["active"] != 0
        or dispatches["waiting"] != 0
        or notifications["inFlight"]
    ):
        raise RuntimeUpdateError("Navigator reported drained while active work remains")
    response.clear()
    response.update(document)


def _runtime_activity_probe_script() -> str:
    """Return a container-local predicate that reports no credential/path values."""

    return "\n".join(
        [
            "import json, os",
            "from pathlib import Path",
            f"markers = {sorted(RUNTIME_ACTIVITY_MARKERS)!r}",
            f"token_default = {DEFAULT_RUNTIME_ACTIVITY_TOKEN_FILE!r}",
            f"socket_default = {DEFAULT_RUNTIME_MAINTENANCE_SOCKET!r}",
            "present = sorted(name for name in markers if name in os.environ)",
            "token_path = Path(os.environ.get('CYRENE_RUNTIME_ACTIVITY_SOURCE_TOKEN_FILE', token_default))",
            "socket_path = Path(os.environ.get('CYRENE_RUNTIME_MAINTENANCE_SOCKET', socket_default))",
            "print(json.dumps({'managed': bool(present) or token_path.is_file() or socket_path.exists(), 'markerCount': len(present), 'tokenFilePresent': token_path.is_file(), 'socketPresent': socket_path.exists()}, separators=(',', ':')))",
        ]
    )


def _verify_runtime_unmanaged(
    services: dict[str, dict[str, Any]], docker_prefix: list[str]
) -> None:
    """Reject current containers configured for the unsupported maintenance broker."""

    for service_name in ("navigator", "exchange"):
        service = services.get(service_name)
        if not isinstance(service, dict):
            raise RuntimeUpdateError(f"required service {service_name} is missing")
        result = _run_command(
            [
                *docker_prefix,
                "exec",
                service["containerId"],
                "python3",
                "-c",
                _runtime_activity_probe_script(),
            ],
            timeout_seconds=10,
        )
        if result.returncode != 0:
            raise RuntimeUpdateError(f"RuntimeMaintenance mode could not be read in {service_name}")
        document = _decode_json_output(result.stdout, f"{service_name} RuntimeMaintenance probe")
        if (
            not isinstance(document, dict)
            or set(document) != {"managed", "markerCount", "tokenFilePresent", "socketPresent"}
            or not isinstance(document.get("managed"), bool)
            or any(
                not isinstance(document.get(field), bool)
                for field in ("tokenFilePresent", "socketPresent")
            )
            or isinstance(document.get("markerCount"), bool)
            or not isinstance(document.get("markerCount"), int)
        ):
            raise RuntimeUpdateError(f"{service_name} RuntimeMaintenance probe is malformed")
        if document["managed"]:
            raise RuntimeUpdateError(
                "managed RuntimeMaintenance mode is unsupported by this updater"
            )


def _inspect_project_services(
    *,
    compose: dict[str, Any],
    image_lock: dict[str, Any],
    docker_prefix: list[str],
    image_overrides: dict[str, dict[str, str] | list[dict[str, str]]] | None = None,
    health_exceptions: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Read exact running Stage 1 container IDs, immutable images, health, and network names."""

    project = compose.get("name")
    if not isinstance(project, str) or not project:
        raise RuntimeUpdateError("rendered Compose project name is missing")
    expected = preflight._expected_stage1_services(compose)
    required_stage1_services = set(expected)
    navigator = expected.get("navigator")
    navigator_labels = navigator.get("labels") if isinstance(navigator, dict) else None
    navigator_environment = navigator.get("environment") if isinstance(navigator, dict) else None
    if (
        not isinstance(navigator_labels, dict)
        or navigator_labels.get("com.cyrene.runtime.profile") != "codebuddy-acp"
        or not isinstance(navigator_environment, dict)
        or navigator_environment.get("CYRENE_HARNESS_EXECUTION_BACKEND") != "codebuddy-acp"
    ):
        raise RuntimeUpdateError(
            "Stage 1 runtime update requires the CodeBuddy ACP Navigator profile"
        )
    lock_ids = _locked_image_ids(image_lock)
    image_overrides = image_overrides or {}
    health_exceptions = health_exceptions or set()
    ids_text = _checked_stdout(
        [
            *docker_prefix,
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{.ID}}",
        ],
        label="running project inventory",
    )
    container_ids = [line.strip() for line in ids_text.splitlines() if line.strip()]
    if not container_ids:
        raise RuntimeUpdateError("no running Compose project containers were found")
    if any(CONTAINER_ID_RE.fullmatch(value) is None for value in container_ids):
        raise RuntimeUpdateError("Docker returned a malformed running container ID")
    output = _checked_stdout(
        [*docker_prefix, "inspect", *container_ids], label="service inspection"
    )
    rows = _decode_json_output(output, "Docker service inspection")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeUpdateError("Docker service inspection did not return container records")
    by_service: dict[str, dict[str, Any]] = {}
    for row in rows:
        config = row.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        state = row.get("State")
        network_settings = row.get("NetworkSettings")
        networks = network_settings.get("Networks") if isinstance(network_settings, dict) else None
        project_label = (
            labels.get("com.docker.compose.project") if isinstance(labels, dict) else None
        )
        service_name = (
            labels.get("com.docker.compose.service") if isinstance(labels, dict) else None
        )
        if project_label != project or not isinstance(service_name, str):
            raise RuntimeUpdateError(
                "running container is not owned by the selected Compose project"
            )
        if service_name not in expected:
            raise RuntimeUpdateError(
                "unexpected running worker exists in the selected Compose project"
            )
        if (
            service_name == "navigator"
            and labels.get("com.cyrene.runtime.profile") != "codebuddy-acp"
        ):
            raise RuntimeUpdateError(
                "running Navigator does not match the rendered CodeBuddy ACP profile"
            )
        if service_name in by_service:
            raise RuntimeUpdateError(
                f"Compose service {service_name} has multiple running containers"
            )
        if (
            not isinstance(config, dict)
            or not isinstance(state, dict)
            or state.get("Running") is not True
        ):
            raise RuntimeUpdateError(f"Compose service {service_name} is not running")
        image_ref = config.get("Image")
        image_id = row.get("Image")
        override = image_overrides.get(service_name)
        if isinstance(override, dict):
            image_options = [override]
        elif isinstance(override, list):
            image_options = override
        else:
            expected_image = expected[service_name].get("image")
            image_options = [{"imageRef": expected_image, "imageId": lock_ids.get(expected_image)}]
        if (
            not isinstance(image_ref, str)
            or not isinstance(image_id, str)
            or not any(
                isinstance(option, dict)
                and option.get("imageRef") == image_ref
                and option.get("imageId") == image_id
                for option in image_options
            )
        ):
            raise RuntimeUpdateError(
                f"Compose service {service_name} does not match its locked image"
            )
        health_record = state.get("Health")
        health = health_record.get("Status") if isinstance(health_record, dict) else None
        has_healthcheck = bool(expected[service_name].get("healthcheck"))
        if has_healthcheck and health != "healthy" and service_name not in health_exceptions:
            raise RuntimeUpdateError(f"Compose service {service_name} is not healthy")
        if not isinstance(networks, dict):
            raise RuntimeUpdateError(f"Compose service {service_name} has no network inventory")
        network_names = sorted(name for name in networks if isinstance(name, str))
        if not network_names:
            raise RuntimeUpdateError(f"Compose service {service_name} has no active network")
        container_id = row.get("Id")
        if not isinstance(container_id, str) or re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
            raise RuntimeUpdateError(
                f"Compose service {service_name} has a malformed full container ID"
            )
        by_service[service_name] = {
            "containerId": container_id,
            "imageRef": image_ref,
            "imageId": image_id,
            "health": health if health is not None else "running",
            "networkNames": network_names,
        }
    if set(by_service) != required_stage1_services:
        missing = sorted(required_stage1_services - set(by_service))
        unexpected = sorted(set(by_service) - required_stage1_services)
        raise RuntimeUpdateError(
            "Stage 1 Compose service inventory differs from the rendered topology; "
            f"missing={','.join(missing)} unexpected={','.join(unexpected)}"
        )
    computer_services = required_stage1_services & {
        "computer-runtime",
        "computer-runtime-proxy",
    }
    if computer_services and computer_services != {"computer-runtime", "computer-runtime-proxy"}:
        raise RuntimeUpdateError("the known computer runtime and proxy must be running together")
    return by_service


def _locked_image_ids(image_lock: dict[str, Any]) -> dict[str, str]:
    """Map local image refs to immutable Docker IDs across built and external lock rows."""

    image_ids: dict[str, str] = {}
    for row in image_lock.get("images", []):
        if not isinstance(row, dict):
            raise RuntimeUpdateError("image lock contains a malformed built-image row")
        image = row.get("image")
        image_id = row.get("imageId")
        if (
            not isinstance(image, str)
            or not isinstance(image_id, str)
            or IMAGE_ID_RE.fullmatch(image_id) is None
            or image in image_ids
        ):
            raise RuntimeUpdateError("image lock contains a duplicate or malformed local image")
        image_ids[image] = image_id
    for row in image_lock.get("externalImages", {}).values():
        if not isinstance(row, dict):
            raise RuntimeUpdateError("image lock contains a malformed external-image row")
        image = row.get("localImage")
        image_id = row.get("imageId")
        if (
            not isinstance(image, str)
            or not isinstance(image_id, str)
            or IMAGE_ID_RE.fullmatch(image_id) is None
            or image in image_ids
        ):
            raise RuntimeUpdateError("image lock contains a duplicate or malformed external image")
        image_ids[image] = image_id
    return image_ids


def _verify_live_network_membership(
    compose: dict[str, Any],
    services: dict[str, dict[str, Any]],
    docker_prefix: list[str],
) -> None:
    """Require every live Navigator/Exchange network peer to be a known Compose service."""

    project = compose.get("name")
    compose_services = compose.get("services", {})
    network_definitions = compose.get("networks", {})
    if (
        not isinstance(project, str)
        or not isinstance(compose_services, dict)
        or not isinstance(network_definitions, dict)
    ):
        raise RuntimeUpdateError("rendered Compose network inventory is malformed")
    relevant_networks = _network_services(compose, "navigator") | _network_services(
        compose, "exchange"
    )
    if not relevant_networks:
        raise RuntimeUpdateError("Navigator and Exchange have no shared network inventory")
    for logical_name in sorted(relevant_networks):
        allowed = {
            "navigator-exchange-private": {"navigator", "exchange"},
            "navigator-teams-private": {"navigator", "teams-ingress"},
            "navigator-persistence-private": {"navigator", "navigator-persistence"},
            "navigator-egress": {"navigator"},
            "account-private": {"exchange", "account-center"},
            "cloud-management-private": {"exchange", "cloud-management-phase2"},
            "graph-exchange-private": {"exchange", "microsoft-graph"},
            "huggingface-exchange-private": {"exchange", "huggingface"},
            "exchange-authority-private": {"exchange", "workspace-authority"},
            "exchange-computer-private": {"exchange", "computer-runtime-proxy"},
            "github-exchange-private": {"exchange", "github-issues"},
            "teams-provider-private": {"exchange", "teams-provider"},
        }.get(logical_name)
        if allowed is None:
            raise RuntimeUpdateError(
                f"unsupported network attached to Navigator or Exchange: {logical_name}"
            )
        configured_peers = {
            name for name in compose_services if logical_name in _network_services(compose, name)
        }
        if not configured_peers <= allowed:
            raise RuntimeUpdateError(f"network {logical_name} declares an unsupported consumer")
        if (
            logical_name
            in {
                "navigator-exchange-private",
                "navigator-teams-private",
                "navigator-persistence-private",
                "navigator-egress",
                "graph-exchange-private",
                "huggingface-exchange-private",
            }
            and configured_peers != allowed
        ):
            raise RuntimeUpdateError(
                f"network {logical_name} does not match the standalone caller set"
            )
        live_peers = configured_peers & set(services)
        if not live_peers:
            raise RuntimeUpdateError(f"network {logical_name} has no running Compose peer")
        network_definition = network_definitions.get(logical_name, {})
        actual_name = (
            network_definition.get("name") if isinstance(network_definition, dict) else None
        )
        if not isinstance(actual_name, str) or not actual_name:
            actual_name = f"{project}_{logical_name}"
        expected_ids: set[str] = set()
        for service_name in live_peers:
            snapshot = services[service_name]
            if actual_name not in snapshot.get("networkNames", []):
                raise RuntimeUpdateError(f"{service_name} is missing its expected private network")
            expected_ids.add(snapshot["containerId"])
        output = _checked_stdout(
            [*docker_prefix, "network", "inspect", actual_name],
            label="private service network inspection",
        )
        network_rows = _decode_json_output(output, "Docker network inspection")
        if (
            not isinstance(network_rows, list)
            or len(network_rows) != 1
            or not isinstance(network_rows[0], dict)
            or network_rows[0].get("Name") != actual_name
        ):
            raise RuntimeUpdateError(f"Docker network {logical_name} could not be verified")
        containers = network_rows[0].get("Containers") or {}
        if not isinstance(containers, dict) or set(containers) != expected_ids:
            raise RuntimeUpdateError(
                f"network {logical_name} has an unknown or missing container peer"
            )


def _add_common_preflight_arguments(parser: argparse.ArgumentParser) -> None:
    """Add immutable deployment inputs shared by the plan command."""

    parser.add_argument("--active-compose-file", type=Path, action="append", required=True)
    parser.add_argument("--active-env-file", type=Path, action="append", required=True)
    parser.add_argument("--active-image-lock", type=Path, required=True)
    parser.add_argument("--candidate-compose-file", type=Path, action="append", required=True)
    parser.add_argument("--candidate-env-file", type=Path, action="append", required=True)
    parser.add_argument("--candidate-image-lock", type=Path, required=True)
    parser.add_argument("--secrets-dir", type=Path, required=True)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument(
        "--docker-prefix",
        choices=("docker", "sudo-n-docker"),
        default="sudo-n-docker",
    )
    parser.add_argument("--drain-timeout-seconds", type=int, default=300)
    parser.add_argument("--health-timeout-seconds", type=int, default=180)
    parser.add_argument("--max-memory-mib", type=int, default=3584)
    parser.add_argument("--max-cpus", type=float, default=1.95)
    parser.add_argument("--required-free-gib", type=float, default=2.0)
    parser.add_argument("--minimum-mem-available-mib", type=int, default=1024)


def _build_parser() -> argparse.ArgumentParser:
    """Construct the closed plan/apply/resume/status command-line interface."""

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    plan_parser = commands.add_parser("plan", help="write a read-only private update plan")
    _add_common_preflight_arguments(plan_parser)
    plan_parser.add_argument(
        "--target", choices=sorted(ALLOWED_TARGETS), action="append", required=True
    )
    plan_parser.add_argument("--output", type=Path, required=True)

    apply_parser = commands.add_parser("apply", help="apply one confirmed private plan")
    apply_parser.add_argument("--plan", type=Path, required=True)
    apply_parser.add_argument("--confirm-plan-sha256", required=True)
    apply_parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)

    resume_parser = commands.add_parser("resume", help="reconcile a nonterminal operation safely")
    resume_parser.add_argument("--operation-id", required=True)
    resume_parser.add_argument("--confirm-plan-sha256", required=True)
    resume_parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)

    status_parser = commands.add_parser("status", help="read one durable operation journal")
    status_parser.add_argument("--operation-id", required=True)
    status_parser.add_argument("--journal-dir", type=Path, default=DEFAULT_JOURNAL_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run a read-only plan, explicit bounded update, safe rollback recovery, or status query."""

    arguments_list = list(sys.argv[1:] if argv is None else argv)
    if arguments_list and arguments_list[0] == "stage-transition":
        # Keep the v1 target contract closed; the separate v2 transaction owns provider topology.
        import stage_transition

        return stage_transition.main(arguments_list[1:])
    parser = _build_parser()
    arguments = parser.parse_args(arguments_list)
    try:
        if arguments.command == "plan":
            docker_prefix = (
                ["docker"] if arguments.docker_prefix == "docker" else ["sudo", "-n", "docker"]
            )
            _validate_docker_prefix(docker_prefix)
            result = create_plan(
                active_compose_files=[path.resolve() for path in arguments.active_compose_file],
                active_env_files=[path.resolve() for path in arguments.active_env_file],
                active_image_lock=arguments.active_image_lock.resolve(),
                candidate_compose_files=[
                    path.resolve() for path in arguments.candidate_compose_file
                ],
                candidate_env_files=[path.resolve() for path in arguments.candidate_env_file],
                candidate_image_lock=arguments.candidate_image_lock.resolve(),
                secrets_dir=arguments.secrets_dir.resolve(),
                config_dir=arguments.config_dir.resolve(),
                targets=arguments.target,
                plan_path=arguments.output,
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
                arguments.plan,
                expected_digest=arguments.confirm_plan_sha256,
                journal_dir=arguments.journal_dir,
            )
        elif arguments.command == "resume":
            result = resume_operation(
                arguments.operation_id,
                expected_digest=arguments.confirm_plan_sha256,
                journal_dir=arguments.journal_dir,
            )
        else:
            result = operation_status(arguments.journal_dir, arguments.operation_id)
    except (
        OSError,
        RuntimeUpdateError,
        subprocess.SubprocessError,
        preflight.PreflightError,
    ) as error:
        print(f"Runtime update blocked: {error}", file=sys.stderr)
        return 3 if arguments.command in {"apply", "resume"} else 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
