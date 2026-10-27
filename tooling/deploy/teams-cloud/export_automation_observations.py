"""
┌─────────────────────────────────────────────────────────────────────┐
│ 📄 export_automation_observations.py                                │
│ Module: tooling.deploy.teams_cloud.export_automation_observations    │
│ Role: Verify one root observer snapshot and publish a safe projection.│
│                                                                      │
│ 模块职责：校验一份 root 观察快照并发布最小化只读状态。                    │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "cyrene-teams-health-manifest-v1"
PROJECT = "cyrene-teams-cloud"
PLATFORM = "linux/arm64"
EVIDENCE_CLASS = "HEALTH_ONLY_NOT_BUSINESS_OR_PROVIDER_ACCEPTANCE"
REFERENCE_OBSERVER_SHA256 = "ef1497dfa9e8737dd6598ef3e16f4f54e723d8d4c320c240cd8f9969329276e0"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SAFE_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SAFE_HOST_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SAFE_OUTPUT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json$")
MAX_PROFILE_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_SAMPLE_BYTES = 48 * 1024 * 1024
MAX_SAMPLE_LINE_BYTES = 32 * 1024
OUTPUT_SCHEMA_VERSION = 1
OUTPUT_MODE = 0o640


class ExportError(Exception):
    """Stable, non-sensitive exporter error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ServiceAlias:
    """One profile-approved opaque alias for a service row in the pinned manifest."""

    source_name: str
    alias: str


@dataclass(frozen=True)
class TrustedProfile:
    """Paths, hashes, scope and freshness supplied by a root-owned profile."""

    source_root: Path
    manifest_path: Path
    report_path: Path
    sample_path: Path
    output_directory: Path
    output_name: str
    output_gid: int
    expected_manifest_sha256: str
    expected_report_sha256: str
    expected_sample_sha256: str
    host_ref: str
    max_age_seconds: int
    max_clock_skew_seconds: int
    aliases: tuple[ServiceAlias, ...]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys in the trusted profile and observer records."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExportError("json_duplicate_key")
        result[key] = value
    return result


def _canonical_json(value: Any) -> bytes:
    """Match the canonical JSON newline format emitted by the root observer."""

    try:
        return (
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ExportError("json_value_invalid") from None


def _safe_absolute_path(value: Any) -> Path:
    """Accept a normalized absolute path without parent traversal."""

    if not isinstance(value, str) or not value or len(value) > 4096 or not value.startswith("/"):
        raise ExportError("profile_path_invalid")
    path = Path(value)
    if ".." in path.parts or str(path) != value:
        raise ExportError("profile_path_invalid")
    return path


def _read_regular_file(
    path: Path,
    *,
    root: Path | None,
    owner_uid: int,
    maximum_bytes: int,
    expected_sha256: str | None = None,
) -> tuple[bytes, str]:
    """Open one bounded root-owned regular file without following symlinks."""

    try:
        if path.resolve(strict=True) != path:
            raise ExportError("input_path_symlink")
        if root is not None:
            path.relative_to(root)
            parent = path.parent
            while parent != root:
                parent_info = parent.lstat()
                if (
                    not stat.S_ISDIR(parent_info.st_mode)
                    or parent_info.st_uid != owner_uid
                    or stat.S_IMODE(parent_info.st_mode) & 0o022
                    or parent.resolve(strict=True) != parent
                ):
                    raise ExportError("input_directory_permissions_invalid")
                parent = parent.parent
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != owner_uid
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size < 1
            or before.st_size > maximum_bytes
        ):
            raise ExportError("input_file_permissions_invalid")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_uid != owner_uid
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_size != before.st_size
            ):
                raise ExportError("input_file_changed")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(maximum_bytes + 1)
        finally:
            os.close(descriptor)
    except ExportError:
        raise
    except (OSError, ValueError):
        raise ExportError("input_file_unavailable") from None
    if len(raw) != before.st_size or len(raw) > maximum_bytes:
        raise ExportError("input_file_size_mismatch")
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ExportError("input_sha256_mismatch")
    return raw, digest


def _strict_json(raw: bytes, code: str) -> Any:
    """Decode duplicate-free UTF-8 JSON without exposing document details."""

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ExportError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ExportError(code) from None


def _reject_constant(value: str) -> None:
    """Reject JavaScript NaN and Infinity extensions."""

    raise ValueError(f"non-standard JSON constant: {value}")


def _bounded_int(value: Any, *, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def load_trusted_profile(path: Path, *, expected_owner_uid: int = 0) -> TrustedProfile:
    """Load a root-owned 0600 profile with pinned input hashes and source scope."""

    raw, _ = _read_regular_file(
        path,
        root=None,
        owner_uid=expected_owner_uid,
        maximum_bytes=MAX_PROFILE_BYTES,
    )
    row = _strict_json(raw, "profile_json_invalid")
    if not isinstance(row, dict) or row.get("schemaVersion") != 1:
        raise ExportError("profile_schema_invalid")
    required = {
        "schemaVersion",
        "project",
        "sourceRoot",
        "manifestPath",
        "reportPath",
        "samplePath",
        "outputDirectory",
        "outputName",
        "outputGid",
        "expectedManifestSha256",
        "expectedReportSha256",
        "expectedSampleSha256",
        "hostRef",
        "maxAgeSeconds",
        "maxClockSkewSeconds",
        "serviceAliases",
    }
    if set(row) != required or row.get("project") != PROJECT:
        raise ExportError("profile_scope_invalid")
    for field in ("expectedManifestSha256", "expectedReportSha256", "expectedSampleSha256"):
        if not _sha256(row.get(field)):
            raise ExportError("profile_hash_invalid")
    host_ref = row.get("hostRef")
    if not isinstance(host_ref, str) or SAFE_HOST_REF_RE.fullmatch(host_ref) is None:
        raise ExportError("profile_host_ref_invalid")
    if not _bounded_int(row.get("outputGid"), minimum=0, maximum=2**31 - 1):
        raise ExportError("profile_output_group_invalid")
    if not _bounded_int(row.get("maxAgeSeconds"), minimum=1, maximum=600):
        raise ExportError("profile_freshness_invalid")
    if not _bounded_int(row.get("maxClockSkewSeconds"), minimum=0, maximum=120):
        raise ExportError("profile_clock_skew_invalid")
    output_name = row.get("outputName")
    if not isinstance(output_name, str) or SAFE_OUTPUT_NAME_RE.fullmatch(output_name) is None:
        raise ExportError("profile_output_name_invalid")
    raw_aliases = row.get("serviceAliases")
    if not isinstance(raw_aliases, list) or not 1 <= len(raw_aliases) <= 14:
        raise ExportError("profile_service_aliases_invalid")
    aliases: list[ServiceAlias] = []
    names: set[str] = set()
    visible_aliases: set[str] = set()
    for item in raw_aliases:
        if not isinstance(item, dict) or set(item) != {"serviceName", "serviceAlias"}:
            raise ExportError("profile_service_alias_invalid")
        source_name, alias = item["serviceName"], item["serviceAlias"]
        if not isinstance(source_name, str) or SAFE_SERVICE_RE.fullmatch(source_name) is None:
            raise ExportError("profile_service_name_invalid")
        if not isinstance(alias, str) or SAFE_ALIAS_RE.fullmatch(alias) is None:
            raise ExportError("profile_service_alias_invalid")
        if source_name in names or alias in visible_aliases:
            raise ExportError("profile_service_alias_duplicate")
        names.add(source_name)
        visible_aliases.add(alias)
        aliases.append(ServiceAlias(source_name, alias))

    source_root = _safe_absolute_path(row.get("sourceRoot"))
    manifest_path = _safe_absolute_path(row.get("manifestPath"))
    report_path = _safe_absolute_path(row.get("reportPath"))
    sample_path = _safe_absolute_path(row.get("samplePath"))
    output_directory = _safe_absolute_path(row.get("outputDirectory"))
    if report_path.parent != sample_path.parent:
        raise ExportError("profile_input_directory_mismatch")
    return TrustedProfile(
        source_root=source_root,
        manifest_path=manifest_path,
        report_path=report_path,
        sample_path=sample_path,
        output_directory=output_directory,
        output_name=output_name,
        output_gid=row["outputGid"],
        expected_manifest_sha256=row["expectedManifestSha256"],
        expected_report_sha256=row["expectedReportSha256"],
        expected_sample_sha256=row["expectedSampleSha256"],
        host_ref=host_ref,
        max_age_seconds=row["maxAgeSeconds"],
        max_clock_skew_seconds=row["maxClockSkewSeconds"],
        aliases=tuple(aliases),
    )


def _validate_private_directory(
    path: Path, expected_owner_uid: int, expected_mode: int
) -> os.stat_result:
    """Validate a non-symlink directory before resolving any trusted child paths."""

    try:
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != expected_owner_uid
            or stat.S_IMODE(info.st_mode) != expected_mode
            or path.resolve(strict=True) != path
        ):
            raise ExportError("directory_permissions_invalid")
        return info
    except ExportError:
        raise
    except OSError:
        raise ExportError("directory_unavailable") from None


def _validate_manifest(
    manifest: Any, profile: TrustedProfile
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Validate the pinned manifest scope and closed 14-service health policy."""

    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != MANIFEST_SCHEMA:
        raise ExportError("manifest_schema_invalid")
    if manifest.get("project") != PROJECT or manifest.get("targetPlatform") != PLATFORM:
        raise ExportError("manifest_scope_invalid")
    identity = manifest.get("identityBindings")
    tooling = manifest.get("tooling")
    policy = manifest.get("policy")
    services = manifest.get("services")
    if (
        not isinstance(identity, dict)
        or not isinstance(tooling, dict)
        or not isinstance(policy, dict)
    ):
        raise ExportError("manifest_sections_invalid")
    for key in (
        "imageLockSha256",
        "staticManifestSha256",
        "renderedComposeSha256",
        "referenceObserverSha256",
    ):
        if not _sha256(identity.get(key)):
            raise ExportError("manifest_identity_invalid")
    if identity.get("referenceObserverSha256") != REFERENCE_OBSERVER_SHA256:
        raise ExportError("manifest_observer_scope_invalid")
    if not _sha256(tooling.get("observerSourceSha256")):
        raise ExportError("manifest_observer_source_invalid")
    expected_names = policy.get("expectedServiceNames")
    if (
        policy.get("expectedServiceCount") != 14
        or not isinstance(expected_names, list)
        or len(expected_names) != 14
        or any(
            not isinstance(name, str) or SAFE_SERVICE_RE.fullmatch(name) is None
            for name in expected_names
        )
        or expected_names != sorted(set(expected_names))
        or not isinstance(services, list)
        or len(services) != 14
    ):
        raise ExportError("manifest_service_set_invalid")
    rows: dict[str, dict[str, Any]] = {}
    memory_total = cpu_total = 0
    for item in services:
        if not isinstance(item, dict):
            raise ExportError("manifest_service_row_invalid")
        name = item.get("service")
        if not isinstance(name, str) or name not in expected_names or name in rows:
            raise ExportError("manifest_service_identity_invalid")
        if not _sha256(str(item.get("imageId", "")).removeprefix("sha256:")):
            raise ExportError("manifest_image_binding_invalid")
        if (
            item.get("platform") != PLATFORM
            or item.get("healthExpected") not in {"healthy", "none"}
            or item.get("privileged") is not False
            or type(item.get("readOnlyRootFilesystem")) is not bool
        ):
            raise ExportError("manifest_service_policy_invalid")
        for field in ("memoryBytes", "nanoCpus", "pidsLimit"):
            if not _bounded_int(item.get(field), minimum=1, maximum=2**53 - 1):
                raise ExportError("manifest_resource_limit_invalid")
        networks = item.get("expectedNetworks")
        ports = item.get("hostPorts")
        if (
            not isinstance(networks, list)
            or not networks
            or any(
                not isinstance(network, str) or SAFE_SERVICE_RE.fullmatch(network) is None
                for network in networks
            )
            or not isinstance(ports, list)
        ):
            raise ExportError("manifest_service_scope_invalid")
        if name == "teams-public-ingress":
            if ports != [{"containerPort": "443/tcp", "hostIp": "127.0.0.1", "hostPort": "18443"}]:
                raise ExportError("manifest_ingress_port_policy_invalid")
        elif ports:
            raise ExportError("manifest_unexpected_host_port")
        memory_total += item["memoryBytes"]
        cpu_total += item["nanoCpus"]
        rows[name] = item
    if set(rows) != set(expected_names):
        raise ExportError("manifest_service_set_mismatch")
    health_policy = policy.get("healthPolicy")
    if (
        not isinstance(health_policy, dict)
        or health_policy.get("healthyServiceCount") != 13
        or health_policy.get("healthlessService") != "teams-public-ingress"
        or rows.get("teams-public-ingress", {}).get("healthExpected") != "none"
    ):
        raise ExportError("manifest_health_policy_invalid")
    totals = policy.get("expectedTotals")
    if (
        not isinstance(totals, dict)
        or totals.get("memoryBytes") != memory_total
        or totals.get("nanoCpus") != cpu_total
        or memory_total != 3520 * 1024 * 1024
        or cpu_total != 1_900_000_000
    ):
        raise ExportError("manifest_resource_totals_invalid")
    port_policy = policy.get("hostPortPolicy")
    if (
        not isinstance(port_policy, dict)
        or port_policy.get("service") != "teams-public-ingress"
        or port_policy.get("hostIp") != "127.0.0.1"
        or port_policy.get("publishedPort") != "18443"
        or port_policy.get("containerPort") != "443/tcp"
    ):
        raise ExportError("manifest_host_port_policy_invalid")
    project_networks = manifest.get("expectedProjectNetworks")
    network_union = sorted(
        {network for item in rows.values() for network in item["expectedNetworks"]}
    )
    managed_networks = manifest.get("expectedManagedNetworks")
    if (
        project_networks != network_union
        or not isinstance(managed_networks, list)
        or any(
            not isinstance(network, str) or SAFE_SERVICE_RE.fullmatch(network) is None
            for network in managed_networks
        )
        or managed_networks != sorted(set(managed_networks))
        or not set(managed_networks).issubset(set(network_union))
    ):
        raise ExportError("manifest_network_scope_invalid")
    user_policy = policy.get("userPolicy")
    if not isinstance(user_policy, dict) or user_policy.get("requiredUid") != 10001:
        raise ExportError("manifest_user_policy_invalid")
    user_sets = [
        user_policy.get("uid10001RequiredServices"),
        user_policy.get("nonRootExceptionServices"),
        user_policy.get("allowRootUserServices"),
    ]
    if any(not isinstance(values, list) for values in user_sets) or any(
        any(not isinstance(name, str) for name in values)
        for values in user_sets
        if isinstance(values, list)
    ):
        raise ExportError("manifest_user_policy_invalid")
    normalized_user_sets = [set(values) for values in user_sets]
    if (
        any(len(values) != len(set(values)) for values in user_sets)
        or any(
            normalized_user_sets[left] & normalized_user_sets[right]
            for left in range(3)
            for right in range(left + 1, 3)
        )
        or set.union(*normalized_user_sets) != set(expected_names)
    ):
        raise ExportError("manifest_user_policy_invalid")
    host_gates = policy.get("hostGates")
    if (
        not isinstance(host_gates, dict)
        or not _bounded_int(host_gates.get("minMemAvailableBytes"), minimum=1, maximum=2**53 - 1)
        or not _bounded_int(
            host_gates.get("minRootDiskAvailableBytes"), minimum=1, maximum=2**53 - 1
        )
        or (
            host_gates.get("minDataDiskAvailableBytes") is not None
            and not _bounded_int(
                host_gates.get("minDataDiskAvailableBytes"), minimum=1, maximum=2**53 - 1
            )
        )
    ):
        raise ExportError("manifest_host_gates_invalid")
    schedule = policy.get("schedule")
    if (
        not isinstance(schedule, dict)
        or schedule.get("intervalSeconds") != 60
        or schedule.get("durationSeconds") != 86400
        or schedule.get("expectedSamples") != 1441
        or not _bounded_int(schedule.get("maxScheduleLagSeconds"), minimum=0, maximum=59)
    ):
        raise ExportError("manifest_schedule_invalid")
    alias_names = {item.source_name for item in profile.aliases}
    if not alias_names.issubset(set(expected_names)):
        raise ExportError("profile_service_scope_mismatch")
    return identity, rows


def _parse_time(value: Any) -> datetime:
    """Parse one canonical UTC timestamp emitted by the observer."""

    if not isinstance(value, str) or len(value) > 64 or not value.endswith("Z"):
        raise ExportError("sample_timestamp_invalid")
    try:
        result = datetime.fromisoformat(value[:-1]).replace(tzinfo=UTC)
    except ValueError:
        raise ExportError("sample_timestamp_invalid") from None
    if result.tzinfo != UTC:
        raise ExportError("sample_timestamp_invalid")
    return result


def _observer_report(
    report: Any,
    *,
    manifest_identity: dict[str, Any],
    manifest_sha256: str,
    profile: TrustedProfile,
    sample_bytes: bytes,
    sample_sha256: str,
    now: datetime,
) -> dict[str, Any]:
    """Verify the final observer receipt before selecting its last JSONL row."""

    if not isinstance(report, dict) or report.get("schemaVersion") != 1:
        raise ExportError("report_schema_invalid")
    if report.get("evidenceClass") != EVIDENCE_CLASS or report.get("project") != PROJECT:
        raise ExportError("report_scope_invalid")
    if report.get("status") not in {"PASS", "FAIL", "RUNNING"}:
        raise ExportError("report_status_invalid")
    run_id = report.get("runId")
    if (
        not isinstance(run_id, str)
        or re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{12}", run_id) is None
        or profile.report_path.name != f"report-{run_id}.json"
        or profile.sample_path.name != f"samples-{run_id}.jsonl"
    ):
        raise ExportError("report_run_path_mismatch")
    if (
        report.get("manifestSha256") != manifest_sha256
        or report.get("identityBindings") != manifest_identity
    ):
        raise ExportError("report_manifest_binding_mismatch")
    if report.get("sampleFile") != profile.sample_path.name:
        raise ExportError("report_sample_path_mismatch")
    if (
        report.get("sampleFileSha256") != sample_sha256
        or sample_sha256 != profile.expected_sample_sha256
    ):
        raise ExportError("sample_sha256_mismatch")
    if report.get("sampleFileBytes") != len(sample_bytes):
        raise ExportError("sample_size_mismatch")
    written, expected = report.get("samplesWritten"), report.get("expectedSamples")
    if (
        not _bounded_int(written, minimum=1, maximum=1441)
        or not _bounded_int(expected, minimum=1, maximum=1441)
        or written > expected
    ):
        raise ExportError("report_sample_count_invalid")
    if type(report.get("complete24h")) is not bool:
        raise ExportError("report_completion_invalid")
    return report


def _latest_sample(sample_bytes: bytes, report: dict[str, Any]) -> dict[str, Any]:
    """Decode only the newest canonical observer row, without projecting old samples."""

    if not sample_bytes.endswith(b"\n"):
        raise ExportError("sample_file_incomplete")
    line = sample_bytes[:-1].rsplit(b"\n", 1)[-1]
    if not line or len(line) > MAX_SAMPLE_LINE_BYTES:
        raise ExportError("sample_line_invalid")
    row = _strict_json(line, "sample_json_invalid")
    if _canonical_json(row).rstrip(b"\n") != line:
        raise ExportError("sample_line_noncanonical")
    if not isinstance(row, dict) or row.get("sampleIndex") != report["samplesWritten"] - 1:
        raise ExportError("sample_latest_index_invalid")
    return row


def _project_snapshot(
    sample: dict[str, Any],
    manifest: dict[str, Any],
    manifest_rows: dict[str, dict[str, Any]],
    profile: TrustedProfile,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Map one fresh full-scope sample to opaque aliases and coarse health states."""

    sampled_at = _parse_time(sample.get("sampledAtUtc"))
    if sampled_at > now + timedelta(seconds=profile.max_clock_skew_seconds):
        raise ExportError("sample_timestamp_future")
    if now - sampled_at > timedelta(seconds=profile.max_age_seconds):
        raise ExportError("sample_stale")
    service_rows = sample.get("services")
    if not isinstance(service_rows, list) or len(service_rows) > 14:
        raise ExportError("sample_services_invalid")
    observed: dict[str, dict[str, Any]] = {}
    for item in service_rows:
        if not isinstance(item, dict) or not isinstance(item.get("service"), str):
            raise ExportError("sample_service_row_invalid")
        name = item["service"]
        if name not in manifest_rows or name in observed:
            raise ExportError("sample_service_scope_invalid")
        observed[name] = item

    failures = sample.get("failures")
    if not isinstance(failures, list) or len(failures) > 4096:
        raise ExportError("sample_failures_invalid")
    service_count = sample.get("serviceCount")
    expected_networks = manifest.get("expectedManagedNetworks")
    complete_set = service_count == 14 and set(observed) == set(manifest_rows)
    expected_totals = manifest["policy"]["expectedTotals"]
    totals = sample.get("resourceTotals")
    totals_match = (
        isinstance(totals, dict)
        and totals.get("memoryBytes") == expected_totals["memoryBytes"]
        and totals.get("nanoCpus") == expected_totals["nanoCpus"]
    )
    running_count = sample.get("projectRunningServiceCount")
    scope_facts_match = (
        sample.get("projectManagedNetworks") == expected_networks
        and running_count == 14
        and totals_match
    )
    any_degraded = bool(failures) or not scope_facts_match
    any_unknown = not complete_set or not _bounded_int(running_count, minimum=0, maximum=100)
    service_states: dict[str, tuple[str, int | None]] = {}
    for name, spec in manifest_rows.items():
        item = observed.get(name)
        if item is None:
            service_states[name] = ("unknown", None)
            continue
        row_failures = item.get("failures")
        if not isinstance(row_failures, list) or len(row_failures) > 256:
            raise ExportError("sample_service_failures_invalid")
        restart_count = item.get("restartCount")
        if not _bounded_int(restart_count, minimum=0, maximum=2**53 - 1):
            service_states[name] = ("unknown", None)
            any_unknown = True
            continue
        expected_health = spec["healthExpected"]
        state = item.get("state")
        health = item.get("health")
        if state == "missing_or_uninspectable":
            service_state = "unknown"
            any_unknown = True
        elif (
            state == "running"
            and health == expected_health
            and item.get("networks") == spec["expectedNetworks"]
            and item.get("hostPorts") == spec["hostPorts"]
            and not row_failures
        ):
            service_state = "healthy"
        else:
            service_state = "degraded"
            any_degraded = True
        service_states[name] = (service_state, restart_count)

    output_services = [
        {
            "serviceAlias": alias.alias,
            "state": service_states[alias.source_name][0],
            "restartCount": service_states[alias.source_name][1],
        }
        for alias in profile.aliases
    ]
    output_services.sort(key=lambda item: item["serviceAlias"])

    if any_unknown:
        state = "unknown"
    elif any_degraded:
        state = "degraded"
    else:
        state = "healthy"
    return {
        "schemaVersion": OUTPUT_SCHEMA_VERSION,
        "observedAt": sampled_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "hostRef": profile.host_ref,
        "state": state,
        "services": output_services,
    }


def _write_status_once(
    directory: Path,
    name: str,
    output_gid: int,
    payload: bytes,
    *,
    expected_owner_uid: int,
) -> Path:
    """Atomically replace the fixed root-owned, group-readable status file."""

    directory_info = _validate_private_directory(directory, expected_owner_uid, 0o750)
    if directory_info.st_gid != output_gid:
        raise ExportError("output_group_mismatch")
    destination = directory / name
    try:
        if destination.exists() or destination.is_symlink():
            existing = destination.lstat()
            if (
                not stat.S_ISREG(existing.st_mode)
                or existing.st_uid != expected_owner_uid
                or existing.st_gid != output_gid
                or stat.S_IMODE(existing.st_mode) != OUTPUT_MODE
                or destination.resolve(strict=True) != destination
            ):
                raise ExportError("output_file_permissions_invalid")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".automation-health-", suffix=".tmp", dir=directory
        )
        temporary = Path(temporary_name)
        try:
            os.fchown(descriptor, expected_owner_uid, output_gid)
            os.fchmod(descriptor, OUTPUT_MODE)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise
        output_info = destination.lstat()
        if (
            not stat.S_ISREG(output_info.st_mode)
            or output_info.st_uid != expected_owner_uid
            or output_info.st_gid != output_gid
            or stat.S_IMODE(output_info.st_mode) != OUTPUT_MODE
        ):
            raise ExportError("output_file_permissions_invalid")
    except ExportError:
        raise
    except (OSError, ValueError):
        raise ExportError("output_write_failed") from None
    return destination


def export_snapshot(
    profile_path: Path,
    *,
    now: datetime | None = None,
    expected_owner_uid: int = 0,
) -> tuple[dict[str, Any], Path]:
    """Verify pinned inputs, project the newest sample once, and publish it atomically."""

    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo != UTC:
        raise ExportError("clock_timezone_invalid")
    profile = load_trusted_profile(profile_path, expected_owner_uid=expected_owner_uid)
    source_info = _validate_private_directory(profile.source_root, expected_owner_uid, 0o700)
    _ = source_info
    manifest_bytes, manifest_sha = _read_regular_file(
        profile.manifest_path,
        root=profile.source_root,
        owner_uid=expected_owner_uid,
        maximum_bytes=MAX_MANIFEST_BYTES,
        expected_sha256=profile.expected_manifest_sha256,
    )
    report_bytes, _report_sha = _read_regular_file(
        profile.report_path,
        root=profile.source_root,
        owner_uid=expected_owner_uid,
        maximum_bytes=MAX_REPORT_BYTES,
        expected_sha256=profile.expected_report_sha256,
    )
    sample_bytes, sample_sha = _read_regular_file(
        profile.sample_path,
        root=profile.source_root,
        owner_uid=expected_owner_uid,
        maximum_bytes=MAX_SAMPLE_BYTES,
        expected_sha256=profile.expected_sample_sha256,
    )

    manifest = _strict_json(manifest_bytes, "manifest_json_invalid")
    identity, manifest_rows = _validate_manifest(manifest, profile)
    report = _strict_json(report_bytes, "report_json_invalid")
    report = _observer_report(
        report,
        manifest_identity=identity,
        manifest_sha256=manifest_sha,
        profile=profile,
        sample_bytes=sample_bytes,
        sample_sha256=sample_sha,
        now=now,
    )
    sample = _latest_sample(sample_bytes, report)
    status = _project_snapshot(sample, manifest, manifest_rows, profile, now=now)
    output_path = _write_status_once(
        profile.output_directory,
        profile.output_name,
        profile.output_gid,
        _canonical_json(status),
        expected_owner_uid=expected_owner_uid,
    )
    return status, output_path


def _error_status(code: str) -> str:
    """Map stable input failures to explicit non-pass outcomes for host orchestration."""

    if code in {
        "input_file_unavailable",
        "directory_unavailable",
    } or code.startswith(
        ("profile_", "manifest_unavailable", "report_sample_path", "sample_file_")
    ):
        return "NOT_CONFIGURED"
    if code in {
        "input_file_permissions_invalid",
        "input_directory_permissions_invalid",
        "input_path_symlink",
        "directory_permissions_invalid",
        "output_group_mismatch",
        "output_file_permissions_invalid",
    }:
        return "FORBIDDEN"
    if code in {"sample_stale", "sample_timestamp_future"}:
        return "TIMEOUT"
    if code.endswith(("sha256_mismatch", "binding_mismatch")):
        return "INVALID"
    return "UNAVAILABLE"


def main(argv: list[str] | None = None) -> int:
    """Export one safe status projection and print only a stable result code."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trust-profile", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        status, output_path = export_snapshot(args.trust_profile.absolute())
    except ExportError as error:
        print(
            json.dumps(
                {
                    "status": _error_status(error.code),
                    "code": error.code,
                },
                sort_keys=True,
            )
        )
        return 1
    except Exception:  # noqa: BLE001 - hide paths and internal values at this trust boundary.
        print('{"status":"UNAVAILABLE","code":"export_internal_error"}')
        return 1
    print(
        json.dumps(
            {"status": "EXPORTED", "state": status["state"], "output": output_path.name},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
