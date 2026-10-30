"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_release_observation.py                                      │
│  Module: tooling.deploy.teams_cloud.tests.test_release_observation   │
│  Role: Verify the pinned observer boundary with local simulations.   │
│                                                                     │
│  模块职责：模拟验证观察器边界；不构成真实 VM 或 24 小时验收证据。    │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
observation = importlib.import_module("release_observation")


def _raw(value: Any) -> bytes:
    """Use the adapter's stable private JSON encoding for synthetic records."""

    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _write(path: Path, raw: bytes, mode: int = 0o600) -> None:
    """Create one local-only test fixture with production-like file mode."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Build fake selector/profile/unit files; no daemon, Docker, or provider is called."""

    artifacts = tmp_path / ".artifacts"
    selection_dir = artifacts / "selection"
    lock_path = artifacts / "locks" / "image-lock.json"
    _write(lock_path, b'{"images":[]}')
    selected = {
        "schemaVersion": "cyrene-teams-runtime-selection-v2",
        "project": "cyrene-teams-cloud",
        "phase": "issued-connector-activation",
        "operationId": "simulated-operation",
        "planSha256": "1" * 64,
        "imageLock": {
            "path": str(lock_path),
            "sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        },
    }
    selection_path = selection_dir / "current-runtime-selection.json"
    _write(selection_path, _raw(selected))

    source_files = {
        observation.OBSERVER: b"#!/usr/bin/env python3\n# simulated observer\n",
        observation.GENERATOR: b"# simulated manifest generator\n",
        observation.POLICY: _raw(
            {
                "expectedServiceCount": 14,
                "expectedTotals": observation.EXPECTED_TOTALS,
                "schedule": observation.EXPECTED_SCHEDULE,
                "hostGates": {
                    "minMemAvailableBytes": 1024**3,
                    "minRootDiskAvailableBytes": 1024**3,
                },
            }
        ),
    }
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in source_files.items()}
    source_files[observation.WRAPPER] = "\n".join(
        (
            'EXPECTED_PHASE="issued-connector-activation"',
            'EXPECTED_PROJECT="cyrene-teams-cloud"',
            f'EXPECTED_OBSERVER_SHA256="{hashes[observation.OBSERVER]}"',
            f'EXPECTED_GENERATOR_SHA256="{hashes[observation.GENERATOR]}"',
            f'EXPECTED_POLICY_SHA256="{hashes[observation.POLICY]}"',
            "EXPECTED_SERVICE_COUNT=14",
            "EXPECTED_MEMORY_BYTES=3690987520",
            "EXPECTED_NANO_CPUS=1900000000",
            "MIN_MEM_AVAILABLE_BYTES=1073741824",
            "MIN_ROOT_DISK_AVAILABLE_BYTES=2147483648",
        )
    ).encode()
    hashes[observation.WRAPPER] = hashlib.sha256(source_files[observation.WRAPPER]).hexdigest()
    profile_sha = hashlib.sha256(_raw(hashes)).hexdigest()
    profile_dir = artifacts / "observation-profile" / profile_sha
    profile_dir.mkdir(mode=0o700, parents=True)
    for name, raw in source_files.items():
        _write(profile_dir / name, raw)

    release_dir = artifacts / "releases" / "teams-arm64-aaaaaaaaaaaa-bbbbbbbbbbbb"
    release_dir.mkdir(mode=0o700, parents=True)
    observer_root = tmp_path / "observer-state"
    manifests = observer_root / "manifests"
    runs = observer_root / "runs"
    manifests.mkdir(mode=0o700, parents=True)
    runs.mkdir(mode=0o700)
    run_dir = runs / "20261027T120000Z-aaaaaaaaaaaa"
    run_dir.mkdir(mode=0o700)
    installed = (
        tmp_path
        / "usr-local-sbin"
        / f"cyrene-final-health-observer-{hashes[observation.OBSERVER][:12]}"
    )
    _write(installed, source_files[observation.OBSERVER], mode=0o700)

    selector_sha = hashlib.sha256(selection_path.read_bytes()).hexdigest()
    lock_sha = selected["imageLock"]["sha256"]
    service_names = [f"service-{index:02d}" for index in range(14)]
    static = {
        "selectorSha256": selector_sha,
        "imageLockSha256": lock_sha,
        "project": "cyrene-teams-cloud",
        "healthObserverSourceSha256": hashes[observation.OBSERVER],
        "manifestGeneratorSourceSha256": hashes[observation.GENERATOR],
        "healthPolicyTemplateSha256": hashes[observation.POLICY],
        "expectedServiceCount": 14,
        "serviceNames": service_names,
    }
    static_path = profile_dir / (
        f"final-health-static-{lock_sha[:12]}-{selector_sha[:12]}-"
        f"{hashes[observation.OBSERVER][:12]}.json"
    )
    _write(static_path, _raw(static))
    static_sha = hashlib.sha256(static_path.read_bytes()).hexdigest()
    manifest = {
        "project": "cyrene-teams-cloud",
        "identityBindings": {"imageLockSha256": lock_sha, "staticManifestSha256": static_sha},
        "tooling": {
            "observerSourceSha256": hashes[observation.OBSERVER],
            "manifestGeneratorSha256": hashes[observation.GENERATOR],
        },
        "policy": {
            "expectedServiceCount": 14,
            "expectedServiceNames": service_names,
            "expectedTotals": observation.EXPECTED_TOTALS,
            "schedule": observation.EXPECTED_SCHEDULE,
            "hostGates": {
                "minMemAvailableBytes": 1024**3,
                "minRootDiskAvailableBytes": 2 * 1024**3,
            },
        },
    }
    manifest_raw = _raw(manifest)
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    manifest_path = manifests / f"final-health-manifest-{manifest_sha}.json"
    _write(manifest_path, manifest_raw)
    unit = f"{observation.UNIT_PREFIX}{manifest_sha[:16]}.service"
    argv = [
        str(installed),
        "--manifest",
        str(manifest_path),
        "--expected-sha256",
        manifest_sha,
        "--output-dir",
        str(run_dir),
        "--docker",
        "/usr/bin/docker",
    ]
    state = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "MainPID": "4321",
        "User": "root",
        "Group": "root",
        "NoNewPrivileges": "yes",
        "UMask": "0077",
    }
    value_argv = argv

    monkeypatch.setattr(observation, "SELECTION_PATH", selection_path)
    monkeypatch.setattr(observation, "OBSERVER_ROOT", observer_root)
    monkeypatch.setattr(observation, "OBSERVER_BIN", installed.parent)
    monkeypatch.setattr(observation.os, "geteuid", lambda: 0)

    # The CI process is not root. These shims simulate the host's verified root-owned
    # artifact boundary while retaining symlink, mode, size, and byte-hash checks.
    def private_dir(path: Path, *, create: bool = False) -> Path:
        if create and not path.exists():
            path.mkdir(mode=0o700)
        info = path.lstat()
        assert path.is_dir() and not path.is_symlink() and info.st_mode & 0o777 == 0o700
        return path

    def private_file(
        path: Path, *, mode: int = 0o600, maximum: int = observation.MAX_JSON
    ) -> bytes:
        try:
            info = path.lstat()
        except FileNotFoundError as error:
            raise observation.ObservationBoundaryError("PRIVATE_FILE_UNAVAILABLE") from error
        assert path.is_file() and not path.is_symlink() and info.st_mode & 0o777 == mode
        assert info.st_size <= maximum
        return path.read_bytes()

    lock_owner = {"uid": 10001}

    def private_metadata(
        path: Path,
        *,
        mode: int = 0o600,
        maximum: int = observation.MAX_JSON,
        allowed_owners: tuple[int, ...] = (0,),
    ) -> Any:
        info = path.lstat()
        assert path.is_file() and not path.is_symlink() and info.st_mode & 0o777 == mode
        assert info.st_size <= maximum
        assert path.resolve(strict=True) == path
        owner = lock_owner["uid"] if path == lock_path else 0
        if owner not in allowed_owners:
            raise observation.ObservationBoundaryError("PRIVATE_FILE_OWNER_MODE_OR_PATH_INVALID")
        return SimpleNamespace(st_mode=info.st_mode, st_uid=owner, st_size=info.st_size)

    monkeypatch.setattr(observation, "_private_dir", private_dir)
    monkeypatch.setattr(observation, "_private_file", private_file)
    monkeypatch.setattr(observation, "_private_metadata", private_metadata)
    monkeypatch.setattr(observation, "_process_argv", lambda _pid: value_argv.copy())
    monkeypatch.setattr(
        observation, "_unit_names", lambda: [unit] if state["LoadState"] == "loaded" else []
    )
    monkeypatch.setattr(observation, "_unit_properties", lambda _name: state.copy())

    return {
        "selectionPath": selection_path,
        "releaseDir": release_dir,
        "policy": {"sourceDirectory": str(profile_dir), "sourceHashes": hashes},
        "lockOwner": lock_owner,
        "selected": selected,
        "state": state,
        "unit": unit,
        "manifestSha": manifest_sha,
        "staticSha": static_sha,
        "manifestPath": manifest_path,
        "runDir": run_dir,
        "runs": runs,
        "argv": argv,
        "staticPath": static_path,
        "wrapperCalls": [],
    }


def _stop_report(value: dict[str, Any]) -> tuple[bytes, bytes]:
    """Write the simulated observer's immutable final report and sample bytes."""

    run_id = "20261027T120001Z-bbbbbbbbbbbb"
    sample = b'{"sampleIndex":0}\n'
    sample_path = value["runDir"] / f"samples-{run_id}.jsonl"
    _write(sample_path, sample)
    report = {
        "schemaVersion": 1,
        "status": "RUNNING",
        "evidenceClass": "HEALTH_ONLY_NOT_BUSINESS_OR_PROVIDER_ACCEPTANCE",
        "complete24h": False,
        "runId": run_id,
        "manifestSha256": value["manifestSha"],
        "identityBindings": {
            "imageLockSha256": value["selected"]["imageLock"]["sha256"],
            "staticManifestSha256": value["staticSha"],
        },
        "expectedSamples": 1441,
        "samplesWritten": 1,
        "failedSamples": 1,
        "missedSlots": 0,
        "failureCountsByService": {"exchange": {"unhealthy": 1}},
        "restartEventsByService": {},
        "providersOrModelsCalled": False,
        "dockerExecUsed": False,
        "sampleFile": sample_path.name,
        "sampleFileSha256": hashlib.sha256(sample).hexdigest(),
        "sampleFileBytes": len(sample),
    }
    report_path = value["runDir"] / f"report-{run_id}.json"
    report_raw = _raw(report)
    _write(report_path, report_raw)
    return report_raw, sample


def _versioned_profile_policy(
    value: dict[str, Any], *, service_count: int, totals: dict[str, int]
) -> dict[str, Any]:
    """Re-seal a synthetic observer source profile with one exact version-2 budget."""

    source_dir = Path(value["policy"]["sourceDirectory"])
    contents = {name: (source_dir / name).read_bytes() for name in observation.FILES}
    template = json.loads(contents[observation.POLICY])
    template.update(
        schemaVersion=2,
        expectedServiceCount=service_count,
        expectedTotals=totals,
    )
    contents[observation.POLICY] = _raw(template)
    wrapper = contents[observation.WRAPPER].decode()
    policy_sha = hashlib.sha256(contents[observation.POLICY]).hexdigest()
    wrapper = re.sub(r"EXPECTED_SERVICE_COUNT=[0-9]+", f"EXPECTED_SERVICE_COUNT={service_count}", wrapper)
    wrapper = re.sub(
        r"EXPECTED_MEMORY_BYTES=[0-9]+",
        f"EXPECTED_MEMORY_BYTES={totals['memoryBytes']}",
        wrapper,
    )
    wrapper = re.sub(
        r"EXPECTED_NANO_CPUS=[0-9]+",
        f"EXPECTED_NANO_CPUS={totals['nanoCpus']}",
        wrapper,
    )
    wrapper = re.sub(
        r'EXPECTED_POLICY_SHA256="[0-9a-f]{64}"',
        f'EXPECTED_POLICY_SHA256="{policy_sha}"',
        wrapper,
    )
    contents[observation.WRAPPER] = wrapper.encode()
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()}
    profile_sha = hashlib.sha256(_raw(hashes)).hexdigest()
    profile_dir = source_dir.parent / profile_sha
    profile_dir.mkdir(mode=0o700)
    for name, raw in contents.items():
        _write(profile_dir / name, raw)
    return {"sourceDirectory": str(profile_dir), "sourceHashes": hashes}


def _completed_run(value: dict[str, Any], *, fail_one_sample: bool = False) -> dict[str, Any]:
    """Write a sealed 1,441-row fixture matching the exact selector and manifest."""

    run_id = "20261027T120100Z-dddddddddddd"
    names = [f"service-{index:02d}" for index in range(14)]
    samples = []
    for sample_index in range(1441):
        service_rows = [{"service": name, "failures": []} for name in names]
        failures = []
        if fail_one_sample and sample_index == 0:
            service_rows[0]["failures"] = ["container_restart_observed"]
            failures = [{"service": names[0], "check": "container_restart_observed"}]
        samples.append(
            {
                "sampleIndex": sample_index,
                "serviceCount": 14,
                "services": service_rows,
                "projectRunningServiceCount": 14,
                "failures": failures,
                "host": {
                    "memAvailableBytes": 2 * 1024**3,
                    "rootDisk": {"availableBytes": 3 * 1024**3},
                },
                "resourceTotals": observation.EXPECTED_TOTALS,
                "scheduleLagSeconds": 0,
            }
        )
    sample_raw = b"".join(_raw(sample) + b"\n" for sample in samples)
    sample_path = value["runDir"] / f"samples-{run_id}.jsonl"
    _write(sample_path, sample_raw)
    report = {
        "schemaVersion": 1,
        "status": "FAIL" if fail_one_sample else "PASS",
        "evidenceClass": "HEALTH_ONLY_NOT_BUSINESS_OR_PROVIDER_ACCEPTANCE",
        "complete24h": True,
        "samplingComplete": True,
        "runId": run_id,
        "manifestSha256": value["manifestSha"],
        "identityBindings": {
            "imageLockSha256": value["selected"]["imageLock"]["sha256"],
            "staticManifestSha256": value["staticSha"],
        },
        "expectedSamples": 1441,
        "samplesWritten": 1441,
        "failedSamples": 1 if fail_one_sample else 0,
        "missedSlots": 0,
        "failureCountsByService": (
            {names[0]: {"container_restart_observed": 1}} if fail_one_sample else {}
        ),
        "restartEventsByService": {names[0]: 1} if fail_one_sample else {},
        "providersOrModelsCalled": False,
        "dockerExecUsed": False,
        "sampleFile": sample_path.name,
        "sampleFileSha256": hashlib.sha256(sample_raw).hexdigest(),
        "sampleFileBytes": len(sample_raw),
    }
    report_path = value["runDir"] / f"report-{run_id}.json"
    report_raw = _raw(report)
    _write(report_path, report_raw)
    return {
        "reportPath": report_path,
        "reportSha256": hashlib.sha256(report_raw).hexdigest(),
        "samplePath": sample_path,
        "sampleSha256": hashlib.sha256(sample_raw).hexdigest(),
    }


def _successful_wrapper_start(
    value: dict[str, Any], argv: list[str]
) -> subprocess.CompletedProcess[str]:
    """Simulate the actual wrapper's bare unit name and a fresh observer run ID."""

    run_dir = value["runs"] / "20261027T120002Z-cccccccccccc"
    run_dir.mkdir(mode=0o700)
    value["runDir"] = run_dir
    value["argv"][6] = str(run_dir)
    value["state"].update(
        LoadState="loaded", ActiveState="active", SubState="running", MainPID="4321"
    )
    summary = {
        "status": "OBSERVER_STARTED",
        "evidenceClass": "HEALTH_ONLY_NOT_BUSINESS_OR_PROVIDER_ACCEPTANCE",
        "unit": value["unit"].removesuffix(".service"),
        "serviceCount": 14,
        "samplesPlanned": 1441,
        "durationSeconds": 86400,
        "runtimeMaxSeconds": 93600,
        "manifestSha256": value["manifestSha"],
        "selectorSha256": hashlib.sha256(value["selectionPath"].read_bytes()).hexdigest(),
        "imageLockSha256": value["selected"]["imageLock"]["sha256"],
        "preflight": "RUNNING_ZERO_FAILURES",
    }
    return subprocess.CompletedProcess(argv, 0, json.dumps(summary) + "\n", "")


def test_close_stops_exact_active_observer_and_preserves_real_report_hashes(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    value = harness
    stopped: list[str] = []
    report_raw: bytes | None = None
    sample_raw: bytes | None = None

    def systemctl(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
        nonlocal report_raw, sample_raw
        assert args[0] == "stop" and args[1] == value["unit"] and timeout == 45
        stopped.append(args[1])
        report_raw, sample_raw = _stop_report(value)
        value["state"].update(ActiveState="inactive", SubState="dead", MainPID="0")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(observation, "_systemctl", systemctl)
    result = observation.close_active_observation(
        value["selectionPath"], value["releaseDir"], value["policy"]
    )
    assert stopped == [value["unit"]]
    assert result["status"] == "OBSERVER_CLOSED"
    assert report_raw is not None and sample_raw is not None
    assert result["observer"]["reportSha256"] == hashlib.sha256(report_raw).hexdigest()
    assert result["observer"]["sampleSha256"] == hashlib.sha256(sample_raw).hexdigest()
    assert result["observer"]["failedSamples"] == 1
    assert result["observer"]["complete24h"] is False
    assert result["appOrProviderCalls"] is False
    assert (
        observation.close_active_observation(
            value["selectionPath"], value["releaseDir"], value["policy"]
        )["status"]
        == "ALREADY_CLOSED"
    )
    assert stopped == [value["unit"]]


def test_close_receipts_preserve_multiple_safe_recovery_observer_instances(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    value = harness
    stopped = []

    def systemctl(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
        assert args[0] == "stop" and args[1] == value["unit"]
        stopped.append(args[1])
        _stop_report(value)
        value["state"].update(ActiveState="inactive", SubState="dead", MainPID="0")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(observation, "_systemctl", systemctl)
    observation.close_active_observation(
        value["selectionPath"], value["releaseDir"], value["policy"]
    )
    monkeypatch.setattr(
        observation.subprocess, "run", lambda argv, **kwargs: _successful_wrapper_start(value, argv)
    )
    restarted = observation.start_post_deployment_observation(
        value["selectionPath"], value["releaseDir"], value["policy"]
    )
    assert restarted["status"] == "OBSERVER_STARTED"
    second = observation.close_active_observation(
        value["selectionPath"], value["releaseDir"], value["policy"]
    )
    receipts = list((value["releaseDir"] / "observation-boundary").glob("close-receipt-*.json"))
    assert second["status"] == "OBSERVER_CLOSED"
    assert len(stopped) == 2
    assert len(receipts) == 2
    assert len({observation._json(path)[0]["observerKey"] for path in receipts}) == 2


def test_nonmatching_active_selector_is_held_without_stopping_any_unit(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    value = harness
    selected = value["selected"] | {"operationId": "other-writer"}
    value["selectionPath"].write_bytes(_raw(selected))
    stopped = []
    monkeypatch.setattr(
        observation,
        "_systemctl",
        lambda args, **kwargs: stopped.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    with pytest.raises(observation.ObservationBoundaryError):
        observation.close_active_observation(
            value["selectionPath"], value["releaseDir"], value["policy"]
        )
    assert stopped == []


def test_selector_accepts_only_root_or_runtime_owned_exact_private_lock(
    harness: dict[str, Any],
) -> None:
    value = harness
    value["lockOwner"]["uid"] = 10001
    selected = observation._selection(value["selectionPath"])
    assert selected["imageLockSha256"] == value["selected"]["imageLock"]["sha256"]
    value["lockOwner"]["uid"] = 0
    observation._selection(value["selectionPath"])
    value["lockOwner"]["uid"] = 1000
    with pytest.raises(
        observation.ObservationBoundaryError,
        match="PRIVATE_FILE_OWNER_MODE_OR_PATH_INVALID",
    ):
        observation._selection(value["selectionPath"])


def test_start_requires_exact_wrapper_summary_and_running_unit_readback(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    value = harness
    value["state"].update(
        LoadState="not-found", ActiveState="inactive", SubState="dead", MainPID="0"
    )

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        value["wrapperCalls"].append((argv, kwargs))
        assert argv == [
            "/bin/bash",
            str(Path(value["policy"]["sourceDirectory"]) / observation.WRAPPER),
            "--expected-image-lock-sha256",
            value["selected"]["imageLock"]["sha256"],
        ]
        value["state"].update(
            LoadState="loaded", ActiveState="active", SubState="running", MainPID="4321"
        )
        summary = {
            "status": "OBSERVER_STARTED",
            "evidenceClass": "HEALTH_ONLY_NOT_BUSINESS_OR_PROVIDER_ACCEPTANCE",
            "unit": value["unit"].removesuffix(".service"),
            "serviceCount": 14,
            "samplesPlanned": 1441,
            "durationSeconds": 86400,
            "runtimeMaxSeconds": 93600,
            "manifestSha256": value["manifestSha"],
            "selectorSha256": hashlib.sha256(value["selectionPath"].read_bytes()).hexdigest(),
            "imageLockSha256": value["selected"]["imageLock"]["sha256"],
            "preflight": "RUNNING_ZERO_FAILURES",
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(summary) + "\n", "")

    monkeypatch.setattr(observation.subprocess, "run", run)
    result = observation.start_post_deployment_observation(
        value["selectionPath"], value["releaseDir"], value["policy"]
    )
    assert result["status"] == "OBSERVER_STARTED"
    assert result["complete24h"] is False
    assert result["evidenceClass"] == "HEALTH_ONLY_NOT_BUSINESS_OR_PROVIDER_ACCEPTANCE"
    assert len(value["wrapperCalls"]) == 1


def test_repeated_start_returns_already_running_without_relaunch(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    value = harness
    calls = []
    monkeypatch.setattr(observation.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    first = observation.start_post_deployment_observation(
        value["selectionPath"], value["releaseDir"], value["policy"]
    )
    second = observation.start_post_deployment_observation(
        value["selectionPath"], value["releaseDir"], value["policy"]
    )
    assert first["status"] == "ALREADY_RUNNING"
    assert second["status"] == "ALREADY_RUNNING"
    assert calls == []


def test_read_status_is_read_only_and_reports_exact_running_selector(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    value = harness
    before = sorted(
        path.relative_to(value["releaseDir"]) for path in value["releaseDir"].rglob("*")
    )
    result = observation.read_observation_status(value["selectionPath"], value["policy"])
    after = sorted(path.relative_to(value["releaseDir"]) for path in value["releaseDir"].rglob("*"))
    assert result["status"] == "RUNNING"
    assert result["unit"] == value["unit"]
    assert (
        result["selectorSha256"] == hashlib.sha256(value["selectionPath"].read_bytes()).hexdigest()
    )
    assert result["complete24h"] is False
    assert before == after == []
    value["state"].update(
        LoadState="not-found", ActiveState="inactive", SubState="dead", MainPID="0"
    )
    absent = observation.read_observation_status(value["selectionPath"], value["policy"])
    assert absent["status"] == "NOT_RUNNING"
    assert absent["unit"] is None
    assert absent["selectorSha256"] == result["selectorSha256"]
    assert before == after == []


def test_completed_status_requires_exact_full_clean_report_and_samples(
    harness: dict[str, Any],
) -> None:
    value = harness
    value["state"].update(
        LoadState="not-found", ActiveState="inactive", SubState="dead", MainPID="0"
    )
    evidence = _completed_run(value)
    result = observation.read_observation_status(value["selectionPath"], value["policy"])
    assert result["status"] == "COMPLETED"
    assert result["complete24h"] is True
    assert result["unit"] == value["unit"]
    assert result["report"]["reportSha256"] == evidence["reportSha256"]
    assert result["report"]["sampleSha256"] == evidence["sampleSha256"]


def test_completed_but_unhealthy_report_is_not_accepted(harness: dict[str, Any]) -> None:
    value = harness
    value["state"].update(
        LoadState="not-found", ActiveState="inactive", SubState="dead", MainPID="0"
    )
    _completed_run(value, fail_one_sample=True)
    result = observation.read_observation_status(value["selectionPath"], value["policy"])
    assert result["status"] == "FAILED"
    assert result["complete24h"] is True
    assert result["report"]["failedSamples"] == 1


def test_completed_report_for_old_selector_is_not_current(harness: dict[str, Any]) -> None:
    value = harness
    value["state"].update(
        LoadState="not-found", ActiveState="inactive", SubState="dead", MainPID="0"
    )
    _completed_run(value)
    selected = value["selected"] | {"operationId": "new-selector"}
    value["selectionPath"].write_bytes(_raw(selected))
    result = observation.read_observation_status(value["selectionPath"], value["policy"])
    assert result["status"] == "NOT_RUNNING"
    assert result["complete24h"] is False


def test_completed_report_with_tampered_sample_is_held(harness: dict[str, Any]) -> None:
    value = harness
    value["state"].update(
        LoadState="not-found", ActiveState="inactive", SubState="dead", MainPID="0"
    )
    evidence = _completed_run(value)
    evidence["samplePath"].write_bytes(evidence["samplePath"].read_bytes() + b" ")
    with pytest.raises(observation.ObservationBoundaryError):
        observation.read_observation_status(value["selectionPath"], value["policy"])


def test_unknown_start_outcome_is_never_replayed(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    value = harness
    value["state"].update(
        LoadState="not-found", ActiveState="inactive", SubState="dead", MainPID="0"
    )
    calls = []

    def failed(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 2, '{"status":"SETUP_ERROR","code":"failed"}\n', ""
        )

    monkeypatch.setattr(observation.subprocess, "run", failed)
    with pytest.raises(observation.ObservationBoundaryError) as error:
        observation.start_post_deployment_observation(
            value["selectionPath"], value["releaseDir"], value["policy"]
        )
    assert error.value.state == "RECOVERY_REQUIRED"
    with pytest.raises(observation.ObservationBoundaryError, match="START_INTENT_WITHOUT_RECEIPT"):
        observation.start_post_deployment_observation(
            value["selectionPath"], value["releaseDir"], value["policy"]
        )
    assert len(calls) == 1


def test_source_hash_change_fails_before_observer_mutation(harness: dict[str, Any]) -> None:
    value = harness
    path = Path(value["policy"]["sourceDirectory"]) / observation.WRAPPER
    path.write_bytes(path.read_bytes() + b"# changed\n")
    with pytest.raises(observation.ObservationBoundaryError, match="SOURCE_HASH_MISMATCH"):
        observation.start_post_deployment_observation(
            value["selectionPath"], value["releaseDir"], value["policy"]
        )


def test_wrapper_cannot_pin_a_different_observer_generation(harness: dict[str, Any]) -> None:
    value = harness
    source = Path(value["policy"]["sourceDirectory"])
    wrapper = source / observation.WRAPPER
    wrapper.write_bytes(
        wrapper.read_bytes().replace(
            value["policy"]["sourceHashes"][observation.OBSERVER].encode(), b"0" * 64
        )
    )
    hashes = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in observation.FILES
    }
    new_profile_sha = hashlib.sha256(_raw(hashes)).hexdigest()
    new_source = source.parent / new_profile_sha
    source.rename(new_source)
    value["policy"] = {"sourceDirectory": str(new_source), "sourceHashes": hashes}
    with pytest.raises(observation.ObservationBoundaryError, match="WRAPPER_GATES_WEAKENED"):
        observation.start_post_deployment_observation(
            value["selectionPath"], value["releaseDir"], value["policy"]
        )


def test_graph_observer_profile_v2_binds_15_services_to_the_approved_budget(
    harness: dict[str, Any],
) -> None:
    value = harness
    approved = {
        "memoryBytes": 3584 * 1024**2,
        "nanoCpus": 1_950_000_000,
    }
    policy = _versioned_profile_policy(value, service_count=15, totals=approved)
    profile = observation._source_profile(policy)
    assert profile["version"] == 2
    assert profile["expectedServiceCount"] == 15
    assert profile["expectedTotals"] == approved

    over_budget = {
        "memoryBytes": approved["memoryBytes"] + 1,
        "nanoCpus": approved["nanoCpus"],
    }
    invalid_policy = _versioned_profile_policy(value, service_count=15, totals=over_budget)
    with pytest.raises(
        observation.ObservationBoundaryError,
        match="OBSERVER_PROFILE_GATES_WEAKENED",
    ):
        observation._source_profile(invalid_policy)
