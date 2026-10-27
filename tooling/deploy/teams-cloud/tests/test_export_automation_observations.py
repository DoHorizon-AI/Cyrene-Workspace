"""
┌─────────────────────────────────────────────────────────────────────┐
│ 📄 test_export_automation_observations.py                            │
│ Module: tooling.deploy.teams_cloud.tests.observation_export          │
│ Role: Verify pinned, bounded and sanitized observer exports.         │
│                                                                      │
│ 模块职责：验证固定、有界且经过脱敏的观察结果导出。                        │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
exporter = importlib.import_module("export_automation_observations")
ExportError = exporter.ExportError
PROJECT = exporter.PROJECT
EVIDENCE_CLASS = exporter.EVIDENCE_CLASS
REFERENCE_OBSERVER_SHA256 = exporter.REFERENCE_OBSERVER_SHA256
export_snapshot = exporter.export_snapshot


_SERVICE_NAMES = [
    "api",
    "cache",
    "catalog",
    "directory",
    "echo",
    "exchange",
    "navigator",
    "postgres",
    "reactor",
    "scheduler",
    "teams-public-ingress",
    "worker-a",
    "worker-b",
    "yield",
]
_RUN_ID = "20261027T121000Z-abcdef123456"
_OBSERVED_AT = "2026-10-27T12:09:30.000Z"
_MANIFEST_SHA_PARTS = {
    "imageLockSha256": "a" * 64,
    "staticManifestSha256": "b" * 64,
    "renderedComposeSha256": "c" * 64,
    "referenceObserverSha256": REFERENCE_OBSERVER_SHA256,
}


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _manifest() -> dict[str, Any]:
    rows = []
    for index, service in enumerate(_SERVICE_NAMES):
        memory_bytes = 250 * 1024 * 1024 + (20 * 1024 * 1024 if index == 13 else 0)
        nano_cpus = 100_000_000 + (500_000_000 if index == 13 else 0)
        rows.append(
            {
                "service": service,
                "imageId": f"sha256:{format(index + 1, '064x')}",
                "platform": "linux/arm64",
                "memoryBytes": memory_bytes,
                "nanoCpus": nano_cpus,
                "pidsLimit": 256,
                "healthExpected": "none" if service == "teams-public-ingress" else "healthy",
                "privileged": False,
                "readOnlyRootFilesystem": True,
                "expectedNetworks": ["cyrene-private"],
                "hostPorts": (
                    [{"containerPort": "443/tcp", "hostIp": "127.0.0.1", "hostPort": "18443"}]
                    if service == "teams-public-ingress"
                    else []
                ),
            }
        )
    return {
        "schemaVersion": "cyrene-teams-health-manifest-v1",
        "project": PROJECT,
        "targetPlatform": "linux/arm64",
        "identityBindings": dict(_MANIFEST_SHA_PARTS),
        "policy": {
            "expectedServiceCount": 14,
            "expectedServiceNames": list(_SERVICE_NAMES),
            "expectedTotals": {"memoryBytes": 3520 * 1024 * 1024, "nanoCpus": 1_900_000_000},
            "healthPolicy": {
                "healthyServiceCount": 13,
                "healthlessService": "teams-public-ingress",
            },
            "hostPortPolicy": {
                "service": "teams-public-ingress",
                "hostIp": "127.0.0.1",
                "publishedPort": "18443",
                "containerPort": "443/tcp",
            },
            "hostGates": {
                "minMemAvailableBytes": 1,
                "minRootDiskAvailableBytes": 1,
                "minDataDiskAvailableBytes": None,
            },
            "schedule": {
                "intervalSeconds": 60,
                "durationSeconds": 86400,
                "expectedSamples": 1441,
                "maxScheduleLagSeconds": 5,
            },
            "userPolicy": {
                "requiredUid": 10001,
                "uid10001RequiredServices": [
                    name
                    for name in _SERVICE_NAMES
                    if name not in {"postgres", "teams-public-ingress"}
                ],
                "nonRootExceptionServices": ["postgres"],
                "allowRootUserServices": ["teams-public-ingress"],
            },
        },
        "expectedProjectNetworks": ["cyrene-private"],
        "expectedManagedNetworks": ["cyrene-private"],
        "services": rows,
        "tooling": {"observerSourceSha256": "d" * 64},
    }


def _sample(
    manifest: dict[str, Any],
    *,
    failures: list[dict[str, str]] | None = None,
    restart_count: int = 0,
) -> dict[str, Any]:
    service_rows = []
    for service in _SERVICE_NAMES:
        row_failures = [item["check"] for item in failures or [] if item["service"] == service]
        spec = next(row for row in manifest["services"] if row["service"] == service)
        service_rows.append(
            {
                "service": service,
                "state": "running",
                "health": spec["healthExpected"],
                "restartCount": restart_count if service == "api" else 0,
                "failures": row_failures,
                "containerId": "private-container-id-must-not-leak",
                "networks": spec["expectedNetworks"],
                "hostPorts": spec["hostPorts"],
            }
        )
    return {
        "sampleIndex": 0,
        "sampledAtUtc": _OBSERVED_AT,
        "scheduledAtUtc": _OBSERVED_AT,
        "scheduleLagSeconds": 0,
        "serviceCount": len(service_rows),
        "services": service_rows,
        "projectRunningServiceCount": 14,
        "projectManagedNetworks": manifest["expectedManagedNetworks"],
        "host": {"memAvailableBytes": 42, "privateHostData": "must not leak"},
        "resourceTotals": {"memoryBytes": 3520 * 1024 * 1024, "nanoCpus": 1_900_000_000},
        "failures": failures or [],
    }


def _write_private(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    path.write_bytes(raw)
    path.chmod(mode)


def _fixture(tmp_path: Path, *, sample: dict[str, Any] | None = None) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    owner_uid = os.getuid()
    output_gid = os.getgid()
    source_root = tmp_path / "private-source"
    run_directory = source_root / "observer-run"
    export_directory = tmp_path / "export"
    source_root.mkdir(mode=0o700)
    run_directory.mkdir(mode=0o700)
    export_directory.mkdir(mode=0o750)
    source_root.chmod(0o700)
    run_directory.chmod(0o700)
    export_directory.chmod(0o750)

    manifest = _manifest()
    manifest_path = source_root / "health-manifest.json"
    manifest_raw = _canonical(manifest)
    _write_private(manifest_path, manifest_raw)
    manifest_digest = hashlib.sha256(manifest_raw).hexdigest()

    sample_value = sample or _sample(manifest)
    sample_path = run_directory / f"samples-{_RUN_ID}.jsonl"
    sample_raw = _canonical(sample_value)
    _write_private(sample_path, sample_raw)
    sample_digest = hashlib.sha256(sample_raw).hexdigest()

    report = {
        "schemaVersion": 1,
        "status": "RUNNING",
        "evidenceClass": EVIDENCE_CLASS,
        "complete24h": False,
        "samplingComplete": False,
        "runId": _RUN_ID,
        "project": PROJECT,
        "manifestSha256": manifest_digest,
        "identityBindings": manifest["identityBindings"],
        "startedAtUtc": _OBSERVED_AT,
        "firstScheduledAtUtc": _OBSERVED_AT,
        "lastScheduledAtUtc": _OBSERVED_AT,
        "endedAtUtc": _OBSERVED_AT,
        "expectedSamples": 1,
        "samplesWritten": 1,
        "failedSamples": 0,
        "missedSlots": 0,
        "restartEventsByService": {},
        "failureCountsByService": {},
        "minMemAvailableBytes": None,
        "minRootDiskAvailableBytes": None,
        "minDataDiskAvailableBytes": None,
        "sampleFile": sample_path.name,
        "sampleFileSha256": sample_digest,
        "sampleFileBytes": len(sample_raw),
        "deliveryStatusProbeEnabled": False,
        "dockerExecUsed": False,
        "providersOrModelsCalled": False,
    }
    report_path = run_directory / f"report-{_RUN_ID}.json"
    report_raw = _canonical(report)
    _write_private(report_path, report_raw)

    profile = {
        "schemaVersion": 1,
        "project": PROJECT,
        "sourceRoot": str(source_root),
        "manifestPath": str(manifest_path),
        "reportPath": str(report_path),
        "samplePath": str(sample_path),
        "outputDirectory": str(export_directory),
        "outputName": "latest-status.json",
        "outputGid": output_gid,
        "expectedManifestSha256": manifest_digest,
        "expectedReportSha256": hashlib.sha256(report_raw).hexdigest(),
        "expectedSampleSha256": sample_digest,
        "hostRef": "synthetic-vm-ref",
        "maxAgeSeconds": 120,
        "maxClockSkewSeconds": 30,
        "serviceAliases": [
            {"serviceName": "teams-public-ingress", "serviceAlias": "ingress"},
            {"serviceName": "api", "serviceAlias": "backend"},
        ],
    }
    profile_path = tmp_path / "trusted-profile.json"
    _write_private(profile_path, _canonical(profile))
    return {
        "profile": profile,
        "profilePath": profile_path,
        "manifest": manifest,
        "manifestPath": manifest_path,
        "manifestRaw": manifest_raw,
        "report": report,
        "reportPath": report_path,
        "reportRaw": report_raw,
        "sample": sample_value,
        "samplePath": sample_path,
        "sampleRaw": sample_raw,
        "outputDirectory": export_directory,
        "ownerUid": owner_uid,
    }


def _now() -> datetime:
    return datetime(2026, 10, 27, 12, 10, tzinfo=UTC)


def _export(fixture: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    return export_snapshot(
        fixture["profilePath"], now=_now(), expected_owner_uid=fixture["ownerUid"]
    )


def test_exports_only_opaque_aliases_and_current_health_facts(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    status, path = _export(fixture)

    assert path == fixture["outputDirectory"] / "latest-status.json"
    assert status == {
        "schemaVersion": 1,
        "observedAt": _OBSERVED_AT,
        "hostRef": "synthetic-vm-ref",
        "state": "healthy",
        "services": [
            {"serviceAlias": "backend", "state": "healthy", "restartCount": 0},
            {"serviceAlias": "ingress", "state": "healthy", "restartCount": 0},
        ],
    }
    assert "private-container-id" not in path.read_text(encoding="utf-8")
    assert "private-network" not in path.read_text(encoding="utf-8")
    assert "privateHostData" not in path.read_text(encoding="utf-8")
    assert "runId" not in path.read_text(encoding="utf-8")
    output_mode = stat.S_IMODE(path.stat().st_mode)
    assert output_mode == 0o640
    assert path.stat().st_uid == fixture["ownerUid"]


def test_observer_run_state_does_not_become_health_state(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    assert fixture["report"]["status"] == "RUNNING"
    status, _ = _export(fixture)
    assert status["state"] == "healthy"

    fixture["sample"] = _sample(
        fixture["manifest"], failures=[{"service": "api", "check": "container_restart_observed"}]
    )
    sample_raw = _canonical(fixture["sample"])
    _write_private(fixture["samplePath"], sample_raw)
    fixture["report"]["sampleFileSha256"] = hashlib.sha256(sample_raw).hexdigest()
    fixture["report"]["sampleFileBytes"] = len(sample_raw)
    report_raw = _canonical(fixture["report"])
    _write_private(fixture["reportPath"], report_raw)
    fixture["profile"]["expectedSampleSha256"] = hashlib.sha256(sample_raw).hexdigest()
    fixture["profile"]["expectedReportSha256"] = hashlib.sha256(report_raw).hexdigest()
    _write_private(fixture["profilePath"], _canonical(fixture["profile"]))
    degraded, _ = _export(fixture)
    assert degraded["state"] == "degraded"
    assert degraded["services"][0]["state"] == "degraded"


def test_unknown_service_set_is_exported_as_unknown_not_healthy(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    partial = _sample(fixture["manifest"])
    partial["services"] = [
        item for item in partial["services"] if item["service"] != "teams-public-ingress"
    ]
    partial["serviceCount"] = 13
    sample_raw = _canonical(partial)
    _write_private(fixture["samplePath"], sample_raw)
    fixture["report"]["sampleFileSha256"] = hashlib.sha256(sample_raw).hexdigest()
    fixture["report"]["sampleFileBytes"] = len(sample_raw)
    report_raw = _canonical(fixture["report"])
    _write_private(fixture["reportPath"], report_raw)
    fixture["profile"]["expectedSampleSha256"] = hashlib.sha256(sample_raw).hexdigest()
    fixture["profile"]["expectedReportSha256"] = hashlib.sha256(report_raw).hexdigest()
    _write_private(fixture["profilePath"], _canonical(fixture["profile"]))

    status, _ = _export(fixture)
    assert status["state"] == "unknown"
    assert status["services"][1]["state"] == "unknown"
    assert status["services"][1]["restartCount"] is None


def test_restart_counter_is_preserved_as_a_number(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, sample=_sample(_manifest(), restart_count=4))
    status, _ = _export(fixture)
    assert status["services"][0] == {
        "serviceAlias": "backend",
        "state": "healthy",
        "restartCount": 4,
    }


def test_stale_sample_is_rejected_without_replacing_status(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    original, output = _export(fixture)
    stale_sample = _sample(fixture["manifest"])
    stale_sample["sampledAtUtc"] = "2026-10-27T11:00:00.000Z"
    stale_raw = _canonical(stale_sample)
    _write_private(fixture["samplePath"], stale_raw)
    fixture["report"]["sampleFileSha256"] = hashlib.sha256(stale_raw).hexdigest()
    fixture["report"]["sampleFileBytes"] = len(stale_raw)
    report_raw = _canonical(fixture["report"])
    _write_private(fixture["reportPath"], report_raw)
    fixture["profile"]["expectedSampleSha256"] = hashlib.sha256(stale_raw).hexdigest()
    fixture["profile"]["expectedReportSha256"] = hashlib.sha256(report_raw).hexdigest()
    _write_private(fixture["profilePath"], _canonical(fixture["profile"]))

    with pytest.raises(ExportError, match="sample_stale"):
        _export(fixture)
    assert json.loads(output.read_text(encoding="utf-8")) == original


def test_pinned_hash_and_scope_mismatches_fail_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["profile"]["expectedManifestSha256"] = "0" * 64
    _write_private(fixture["profilePath"], _canonical(fixture["profile"]))
    with pytest.raises(ExportError, match="input_sha256_mismatch"):
        _export(fixture)


def test_sample_symlink_and_permissive_mode_are_rejected(tmp_path: Path) -> None:
    symlink_fixture = _fixture(tmp_path / "symlink")
    sample_path = symlink_fixture["samplePath"]
    saved = sample_path.with_suffix(".saved")
    sample_path.rename(saved)
    sample_path.symlink_to(saved.name)
    with pytest.raises(ExportError, match="input_path_symlink"):
        _export(symlink_fixture)

    mode_fixture = _fixture(tmp_path / "mode")
    mode_fixture["samplePath"].chmod(0o644)
    with pytest.raises(ExportError, match="input_file_permissions_invalid"):
        _export(mode_fixture)


def test_profile_cannot_escape_the_trusted_source_root(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["profile"]["samplePath"] = str(tmp_path / "outside.jsonl")
    _write_private(fixture["profilePath"], _canonical(fixture["profile"]))
    with pytest.raises(ExportError, match="profile_input_directory_mismatch"):
        _export(fixture)


def test_sample_path_and_report_run_identity_must_match(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["report"]["runId"] = "20261027T121000Z-000000000000"
    report_raw = _canonical(fixture["report"])
    _write_private(fixture["reportPath"], report_raw)
    fixture["profile"]["expectedReportSha256"] = hashlib.sha256(report_raw).hexdigest()
    _write_private(fixture["profilePath"], _canonical(fixture["profile"]))
    with pytest.raises(ExportError, match="report_run_path_mismatch"):
        _export(fixture)
