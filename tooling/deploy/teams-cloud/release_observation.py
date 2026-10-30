"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 release_observation.py                                           │
│  Module: tooling.deploy.teams_cloud.release_observation             │
│  Role: Preserve and restart the pinned health-only observer.         │
│                                                                     │
│  模块职责：只管理已验证观察器，保留原始样本并启动新观察基线。       │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "cyrene-teams-release-observation-boundary-v1"
FILES = (
    "observe_final_vm.py",
    "generate_health_manifest.py",
    "prepare_and_start_final_observer_v1.sh",
    "health-policy.template.json",
)
OBSERVER, GENERATOR, WRAPPER, POLICY = FILES
SELECTION_PATH = Path(
    "/var/lib/cyrene-connectors-runtime/.artifacts/selection/current-runtime-selection.json"
)
OBSERVER_ROOT = Path("/var/lib/cyrene-final-health-observation")
OBSERVER_BIN = Path("/usr/local/sbin")
UNIT_PREFIX = "cyrene-final-health-observer-"
UNIT_RE = re.compile(r"^cyrene-final-health-observer-([0-9a-f]{16})\.service$")
UNIT_SUMMARY_RE = re.compile(r"^cyrene-final-health-observer-([0-9a-f]{16})$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
RELEASE_RE = re.compile(r"^teams-arm64-[0-9a-f]{12}-[0-9a-f]{12}$")
RUN_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")
EXPECTED_TOTALS = {"memoryBytes": 3520 * 1024**2, "nanoCpus": 1_900_000_000}
MAX_RUNTIME_TOTALS = {"memoryBytes": 3584 * 1024**2, "nanoCpus": 1_950_000_000}
EXPECTED_SCHEDULE = {
    "intervalSeconds": 60,
    "durationSeconds": 86400,
    "expectedSamples": 1441,
    "maxScheduleLagSeconds": 5,
}
MAX_JSON = 8 * 1024**2
MAX_SAMPLE = 128 * 1024**2
ENV = {
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}


class ObservationBoundaryError(ValueError):
    """A stable stop code for an observer transition that cannot be proved."""

    def __init__(self, code: str, *, state: str = "HELD") -> None:
        super().__init__(code)
        self.code = code
        self.state = state


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys in control-plane receipts."""

    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ObservationBoundaryError("DUPLICATE_JSON_FIELD")
        result[key] = value
    return result


def _private_metadata(
    path: Path,
    *,
    mode: int = 0o600,
    maximum: int = MAX_JSON,
    allowed_owners: tuple[int, ...] = (0,),
) -> os.stat_result:
    """Validate a canonical private file with an explicit owner allowlist."""
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in allowed_owners
            or stat.S_IMODE(info.st_mode) != mode
            or info.st_size > maximum
            or path.resolve(strict=True) != path
        ):
            raise ObservationBoundaryError("PRIVATE_FILE_OWNER_MODE_OR_PATH_INVALID")
        return info
    except OSError as error:
        raise ObservationBoundaryError("PRIVATE_FILE_UNAVAILABLE") from error


def _private_file(path: Path, *, mode: int = 0o600, maximum: int = MAX_JSON) -> bytes:
    """Read one canonical root-owned regular file without following symlinks."""

    try:
        _private_metadata(path, mode=mode, maximum=maximum)
        return path.read_bytes()
    except OSError as error:
        raise ObservationBoundaryError("PRIVATE_FILE_UNAVAILABLE") from error


def _image_lock_file(path: Path) -> bytes:
    """Read the selector-bound lock owned by root or the runtime UID 10001."""

    try:
        _private_metadata(path, mode=0o600, maximum=16 * 1024**2, allowed_owners=(0, 10001))
        return path.read_bytes()
    except OSError as error:
        raise ObservationBoundaryError("IMAGE_LOCK_FILE_UNAVAILABLE") from error


def _private_dir(path: Path, *, create: bool = False) -> Path:
    """Require a canonical root-owned 0700 directory."""

    try:
        if create and not path.exists():
            path.mkdir(mode=0o700)
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o700
            or path.resolve(strict=True) != path
        ):
            raise ObservationBoundaryError("PRIVATE_DIRECTORY_OWNER_MODE_OR_PATH_INVALID")
    except OSError as error:
        raise ObservationBoundaryError("PRIVATE_DIRECTORY_UNAVAILABLE") from error
    return path


def _json(path: Path, *, maximum: int = MAX_JSON) -> tuple[dict[str, Any], bytes]:
    """Read strict JSON from one root-only artifact."""

    raw = _private_file(path, maximum=maximum)
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ObservationBoundaryError("OBSERVER_JSON_INVALID") from error
    if not isinstance(value, dict):
        raise ObservationBoundaryError("OBSERVER_JSON_OBJECT_REQUIRED")
    return value, raw


def _sha(raw: bytes) -> str:
    """Hash exact bytes without re-serializing signed or sealed documents."""

    return hashlib.sha256(raw).hexdigest()


def _source_profile(policy: dict[str, Any]) -> dict[str, Any]:
    """Validate the four-file operator profile and its fixed resource schedule."""

    if not isinstance(policy, dict) or set(policy) != {"sourceDirectory", "sourceHashes"}:
        raise ObservationBoundaryError("OBSERVER_PROFILE_SCHEMA_INVALID")
    source_text, hashes = policy["sourceDirectory"], policy["sourceHashes"]
    if not isinstance(source_text, str) or not Path(source_text).is_absolute():
        raise ObservationBoundaryError("OBSERVER_PROFILE_PATH_INVALID")
    source = _private_dir(Path(source_text))
    if ".artifacts" not in source.parts or source.parent.name != "observation-profile":
        raise ObservationBoundaryError("OBSERVER_PROFILE_DIRECTORY_INVALID")
    if not isinstance(hashes, dict) or set(hashes) != set(FILES):
        raise ObservationBoundaryError("OBSERVER_PROFILE_HASH_SET_INVALID")
    actual: dict[str, str] = {}
    content: dict[str, bytes] = {}
    for name in FILES:
        expected = hashes[name]
        if not isinstance(expected, str) or SHA_RE.fullmatch(expected) is None:
            raise ObservationBoundaryError("OBSERVER_PROFILE_HASH_INVALID")
        raw = _private_file(source / name, maximum=2 * 1024**2)
        actual[name] = _sha(raw)
        content[name] = raw
        if actual[name] != expected:
            raise ObservationBoundaryError("OBSERVER_PROFILE_SOURCE_HASH_MISMATCH")
    profile_sha = _sha(json.dumps(actual, sort_keys=True, separators=(",", ":")).encode())
    if source.name != profile_sha:
        raise ObservationBoundaryError("OBSERVER_PROFILE_DIRECTORY_SHA_MISMATCH")
    try:
        template = json.loads(content[POLICY], object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ObservationBoundaryError("OBSERVER_POLICY_TEMPLATE_INVALID") from error
    template_gates = template.get("hostGates") if isinstance(template, dict) else None
    profile_version = template.get("schemaVersion", 1) if isinstance(template, dict) else None
    expected_count = template.get("expectedServiceCount") if isinstance(template, dict) else None
    expected_totals = template.get("expectedTotals") if isinstance(template, dict) else None
    if profile_version == 1:
        topology_valid = expected_count == 14 and expected_totals == EXPECTED_TOTALS
    elif profile_version == 2:
        topology_valid = (
            type(expected_count) is int
            and expected_count in {14, 15, 16}
            and isinstance(expected_totals, dict)
            and set(expected_totals) == {"memoryBytes", "nanoCpus"}
            and all(type(value) is int and value > 0 for value in expected_totals.values())
            and expected_totals["memoryBytes"] <= MAX_RUNTIME_TOTALS["memoryBytes"]
            and expected_totals["nanoCpus"] <= MAX_RUNTIME_TOTALS["nanoCpus"]
        )
    else:
        topology_valid = False
    if (
        not isinstance(template, dict)
        or not topology_valid
        or template.get("schedule") != EXPECTED_SCHEDULE
        or not isinstance(template_gates, dict)
        or type(template_gates.get("minMemAvailableBytes")) is not int
        or template_gates["minMemAvailableBytes"] < 1024**3
    ):
        raise ObservationBoundaryError("OBSERVER_PROFILE_GATES_WEAKENED")
    # The wrapper strengthens the template's 1 GiB root-disk floor to 2 GiB.
    required_wrapper_values = (
        b'EXPECTED_PHASE="issued-connector-activation"',
        b'EXPECTED_PROJECT="cyrene-teams-cloud"',
        f'EXPECTED_OBSERVER_SHA256="{actual[OBSERVER]}"'.encode(),
        f'EXPECTED_GENERATOR_SHA256="{actual[GENERATOR]}"'.encode(),
        f'EXPECTED_POLICY_SHA256="{actual[POLICY]}"'.encode(),
        f"EXPECTED_SERVICE_COUNT={expected_count}".encode(),
        f"EXPECTED_MEMORY_BYTES={expected_totals['memoryBytes']}".encode(),
        f"EXPECTED_NANO_CPUS={expected_totals['nanoCpus']}".encode(),
        b"MIN_MEM_AVAILABLE_BYTES=1073741824",
        b"MIN_ROOT_DISK_AVAILABLE_BYTES=2147483648",
    )
    wrapper = content[WRAPPER]
    if any(value not in wrapper for value in required_wrapper_values):
        raise ObservationBoundaryError("OBSERVER_WRAPPER_GATES_WEAKENED")
    return {
        "directory": source,
        "hashes": actual,
        "sha256": profile_sha,
        "version": profile_version,
        "expectedServiceCount": expected_count,
        "expectedTotals": dict(expected_totals),
    }


def _selection(path: Path) -> dict[str, Any]:
    """Read the exact current issued selector and its separately pinned lock."""

    if path != SELECTION_PATH:
        raise ObservationBoundaryError("OBSERVER_SELECTION_PATH_NOT_CANONICAL")
    _private_dir(path.parent)
    selected, raw = _json(path, maximum=1024**2)
    if (
        selected.get("schemaVersion") != "cyrene-teams-runtime-selection-v2"
        or selected.get("project") != "cyrene-teams-cloud"
        or selected.get("phase") != "issued-connector-activation"
    ):
        raise ObservationBoundaryError("OBSERVER_SELECTION_NOT_ISSUED_RUNTIME")
    lock = selected.get("imageLock")
    if (
        not isinstance(lock, dict)
        or set(lock) != {"path", "sha256"}
        or not isinstance(lock["path"], str)
        or not Path(lock["path"]).is_absolute()
        or not isinstance(lock["sha256"], str)
    ):
        raise ObservationBoundaryError("OBSERVER_IMAGE_LOCK_REFERENCE_INVALID")
    lock_raw = _image_lock_file(Path(lock["path"]))
    lock_sha = _sha(lock_raw)
    if lock_sha != lock["sha256"]:
        raise ObservationBoundaryError("OBSERVER_IMAGE_LOCK_DIGEST_MISMATCH")
    return {
        "document": selected,
        "selectorSha256": _sha(raw),
        "imageLockSha256": lock_sha,
    }


def _release_dir(path: Path, profile: dict[str, Any]) -> Path:
    """Require one private release directory under the caller's artifact root."""

    result = _private_dir(path)
    if (
        ".artifacts" not in path.parts
        or RELEASE_RE.fullmatch(path.name) is None
        or path.parent.parent != profile["directory"].parent.parent
    ):
        raise ObservationBoundaryError("RELEASE_DIRECTORY_ID_INVALID")
    return result


def _boundary_dir(release: Path) -> Path:
    """Create the release-scoped immutable receipt directory if absent."""

    path = release / "observation-boundary"
    if not path.exists():
        path.mkdir(mode=0o700)
    return _private_dir(path)


def _write_once(path: Path, value: dict[str, Any]) -> None:
    """Publish an immutable 0600 receipt or intent with exclusive creation."""

    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _systemctl(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
    """Run systemd with a fixed binary, sanitized environment, and no shell."""

    try:
        return subprocess.run(
            ["/usr/bin/systemctl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd="/",
            env=ENV,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ObservationBoundaryError(
            "SYSTEMD_OUTCOME_UNKNOWN", state="RECOVERY_REQUIRED"
        ) from error


def _unit_names() -> list[str]:
    """List only transient health-observer services."""

    result = _systemctl(
        [
            "list-units",
            "--all",
            "--type=service",
            "--no-legend",
            "--plain",
            f"{UNIT_PREFIX}*.service",
        ]
    )
    if result.returncode != 0:
        raise ObservationBoundaryError("OBSERVER_UNIT_LIST_UNKNOWN", state="RECOVERY_REQUIRED")
    names = []
    for row in result.stdout.splitlines():
        fields = row.lstrip("● ").split()
        if fields and fields[0].startswith(UNIT_PREFIX):
            if UNIT_RE.fullmatch(fields[0]) is None:
                raise ObservationBoundaryError("OBSERVER_UNIT_NAME_INVALID")
            names.append(fields[0])
    return sorted(set(names))


def _unit_properties(unit: str) -> dict[str, str]:
    """Read identity and liveness fields for one exact transient service."""

    if UNIT_RE.fullmatch(unit) is None:
        raise ObservationBoundaryError("OBSERVER_UNIT_NAME_INVALID")
    keys = "LoadState,ActiveState,SubState,MainPID,User,Group,NoNewPrivileges,UMask"
    result = _systemctl(["show", "--no-pager", f"--property={keys}", unit])
    if result.returncode != 0:
        raise ObservationBoundaryError("OBSERVER_UNIT_STATE_UNKNOWN", state="RECOVERY_REQUIRED")
    values = dict(line.partition("=")[::2] for line in result.stdout.splitlines() if "=" in line)
    if set(values) != set(keys.split(",")):
        raise ObservationBoundaryError("OBSERVER_UNIT_STATE_MALFORMED")
    return values


def _process_argv(pid: int) -> list[str]:
    """Read procfs argv and prove that the observer process runs as root."""

    if pid <= 0:
        raise ObservationBoundaryError("OBSERVER_MAIN_PID_INVALID")
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
        uid = next(line for line in status.splitlines() if line.startswith("Uid:")).split()[1]
        argv = [part.decode("utf-8", "strict") for part in raw.split(b"\0") if part]
    except (OSError, UnicodeDecodeError, StopIteration, IndexError) as error:
        raise ObservationBoundaryError("OBSERVER_PROCESS_IDENTITY_UNAVAILABLE") from error
    if uid != "0" or not argv:
        raise ObservationBoundaryError("OBSERVER_PROCESS_IDENTITY_INVALID")
    return argv


def _unit_identity(
    unit: str, state: dict[str, str], selected: dict[str, Any], profile: dict[str, Any]
) -> dict[str, Any]:
    """Bind process argv, installed source, manifest, static receipt and selector."""

    if any(
        state[key] != value
        for key, value in {
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "running",
            "User": "root",
            "Group": "root",
            "NoNewPrivileges": "yes",
            "UMask": "0077",
        }.items()
    ):
        raise ObservationBoundaryError("OBSERVER_UNIT_SECURITY_OR_LIVENESS_MISMATCH")
    try:
        pid = int(state["MainPID"])
    except ValueError as error:
        raise ObservationBoundaryError("OBSERVER_MAIN_PID_INVALID") from error
    observer_sha = profile["hashes"][OBSERVER]
    installed = OBSERVER_BIN / f"cyrene-final-health-observer-{observer_sha[:12]}"
    installed_raw = _private_file(installed, mode=0o700, maximum=16 * 1024**2)
    if _sha(installed_raw) != observer_sha:
        raise ObservationBoundaryError("INSTALLED_OBSERVER_SOURCE_MISMATCH")
    argv = _process_argv(pid)
    if argv[0] == str(installed):
        command = argv
    elif len(argv) == 10 and Path(argv[0]).name.startswith("python3") and argv[1] == str(installed):
        command = argv[1:]
    else:
        raise ObservationBoundaryError("OBSERVER_PROCESS_COMMAND_MISMATCH")
    if (
        len(command) != 9
        or command[1] != "--manifest"
        or command[3] != "--expected-sha256"
        or command[5] != "--output-dir"
        or command[7] != "--docker"
    ):
        raise ObservationBoundaryError("OBSERVER_PROCESS_ARGUMENTS_MISMATCH")
    manifest_path, manifest_sha, run_dir = Path(command[2]), command[4], Path(command[6])
    if (
        not isinstance(manifest_sha, str)
        or SHA_RE.fullmatch(manifest_sha) is None
        or unit != f"{UNIT_PREFIX}{manifest_sha[:16]}.service"
        or manifest_path
        != OBSERVER_ROOT / "manifests" / f"final-health-manifest-{manifest_sha}.json"
        or run_dir.parent != OBSERVER_ROOT / "runs"
        or RUN_RE.fullmatch(run_dir.name) is None
        or not Path(command[8]).is_absolute()
        or Path(command[8]).name != "docker"
    ):
        raise ObservationBoundaryError("OBSERVER_PROCESS_PATH_BINDING_MISMATCH")
    manifest, manifest_raw = _json(manifest_path)
    identity = manifest.get("identityBindings")
    tooling = manifest.get("tooling")
    if (
        _sha(manifest_raw) != manifest_sha
        or manifest.get("project") != "cyrene-teams-cloud"
        or not isinstance(identity, dict)
        or identity.get("imageLockSha256") != selected["imageLockSha256"]
        or not isinstance(identity.get("staticManifestSha256"), str)
        or not isinstance(tooling, dict)
        or tooling.get("observerSourceSha256") != observer_sha
        or tooling.get("manifestGeneratorSha256") != profile["hashes"][GENERATOR]
    ):
        raise ObservationBoundaryError("OBSERVER_MANIFEST_BINDING_MISMATCH")
    static_path = profile["directory"] / (
        f"final-health-static-{selected['imageLockSha256'][:12]}-"
        f"{selected['selectorSha256'][:12]}-{observer_sha[:12]}.json"
    )
    static, static_raw = _json(static_path)
    if (
        _sha(static_raw) != identity["staticManifestSha256"]
        or static.get("selectorSha256") != selected["selectorSha256"]
        or static.get("imageLockSha256") != selected["imageLockSha256"]
        or static.get("healthObserverSourceSha256") != observer_sha
        or static.get("manifestGeneratorSourceSha256") != profile["hashes"][GENERATOR]
        or static.get("healthPolicyTemplateSha256") != profile["hashes"][POLICY]
        or static.get("expectedServiceCount") != profile["expectedServiceCount"]
        or not isinstance(static.get("serviceNames"), list)
        or len(static["serviceNames"]) != profile["expectedServiceCount"]
        or len(set(static["serviceNames"])) != profile["expectedServiceCount"]
    ):
        raise ObservationBoundaryError("OBSERVER_STATIC_SELECTOR_BINDING_MISMATCH")
    policy = manifest.get("policy")
    gates = policy.get("hostGates") if isinstance(policy, dict) else None
    if (
        not isinstance(policy, dict)
        or policy.get("expectedServiceCount") != profile["expectedServiceCount"]
        or policy.get("expectedTotals") != profile["expectedTotals"]
        or policy.get("schedule") != EXPECTED_SCHEDULE
        or policy.get("expectedServiceNames") != static["serviceNames"]
        or not isinstance(gates, dict)
        or type(gates.get("minMemAvailableBytes")) is not int
        or gates["minMemAvailableBytes"] < 1024**3
        or type(gates.get("minRootDiskAvailableBytes")) is not int
        or gates["minRootDiskAvailableBytes"] < 2 * 1024**3
    ):
        raise ObservationBoundaryError("OBSERVER_MANIFEST_GATES_WEAKENED")
    output_dir = _private_dir(run_dir)
    return {
        "unit": unit,
        "mainPid": pid,
        "manifestPath": str(manifest_path),
        "manifestSha256": manifest_sha,
        "staticManifestSha256": identity["staticManifestSha256"],
        "selectorSha256": selected["selectorSha256"],
        "imageLockSha256": selected["imageLockSha256"],
        "outputDirectory": str(output_dir),
        "observerSourceSha256": observer_sha,
        "expectedServiceNames": static["serviceNames"],
        "expectedServiceCount": profile["expectedServiceCount"],
        "expectedTotals": profile["expectedTotals"],
    }


def _active_observers(selected: dict[str, Any], profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Reject nonmatching active monitors; return at most one exact monitor."""

    found = []
    for unit in _unit_names():
        state = _unit_properties(unit)
        if state["ActiveState"] in {"activating", "active", "reloading"}:
            if state["ActiveState"] != "active" or state["SubState"] != "running":
                raise ObservationBoundaryError(
                    "OBSERVER_UNIT_STATE_UNSTABLE", state="RECOVERY_REQUIRED"
                )
            identity = _unit_identity(unit, state, selected, profile)
            if identity["selectorSha256"] != selected["selectorSha256"]:
                raise ObservationBoundaryError(
                    "NONMATCHING_ACTIVE_OBSERVER", state="RECOVERY_REQUIRED"
                )
            found.append(identity)
    if len(found) > 1:
        raise ObservationBoundaryError("MULTIPLE_ACTIVE_OBSERVERS", state="RECOVERY_REQUIRED")
    return found


def _sample_evidence(
    path: Path,
    expected_count: Any,
    expected_names: list[str],
    *,
    expected_service_count: int,
    expected_totals: dict[str, int],
    full_run: bool,
    require_clean: bool,
) -> tuple[str, int, int]:
    """Stream-check sample hashes, row count, sequence, and full-run health."""

    try:
        _private_metadata(path, maximum=MAX_SAMPLE)
        if type(expected_count) is not int or not 0 <= expected_count <= 1441:
            raise ObservationBoundaryError("OBSERVER_SAMPLE_FILE_INVALID")
        digest = hashlib.sha256()
        size = count = 0
        with path.open("rb") as stream:
            for line in stream:
                size += len(line)
                if len(line) > 4 * 1024**2 or size > MAX_SAMPLE or count >= 1441:
                    raise ObservationBoundaryError("OBSERVER_SAMPLE_BOUNDS_INVALID")
                digest.update(line)
                try:
                    sample = json.loads(line, object_pairs_hook=_pairs)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ObservationBoundaryError("OBSERVER_SAMPLE_JSON_INVALID") from error
                if (
                    not isinstance(sample, dict)
                    or type(sample.get("sampleIndex")) is not int
                    or sample["sampleIndex"] != count
                ):
                    raise ObservationBoundaryError("OBSERVER_SAMPLE_SEQUENCE_INVALID")
                if full_run:
                    services = sample.get("services")
                    failures = sample.get("failures")
                    names = (
                        [row.get("service") for row in services if isinstance(row, dict)]
                        if isinstance(services, list)
                        else []
                    )
                    if (
                        not isinstance(services, list)
                        or len(names) != len(services)
                        or any(not isinstance(name, str) for name in names)
                        or any(name not in expected_names for name in names)
                        or len(names) != len(set(names))
                        or not isinstance(failures, list)
                        or any(
                            not isinstance(row, dict) or not isinstance(row.get("failures"), list)
                            for row in services
                        )
                    ):
                        raise ObservationBoundaryError("OBSERVER_COMPLETED_SAMPLE_NOT_CLEAN")
                    if require_clean:
                        host, totals = sample.get("host"), sample.get("resourceTotals")
                        if (
                            sample.get("serviceCount") != expected_service_count
                            or sample.get("projectRunningServiceCount")
                            != expected_service_count
                            or sorted(names) != expected_names
                            or failures
                            or any(row["failures"] for row in services)
                            or not isinstance(host, dict)
                            or type(host.get("memAvailableBytes")) is not int
                            or host["memAvailableBytes"] < 1024**3
                            or not isinstance(host.get("rootDisk"), dict)
                            or type(host["rootDisk"].get("availableBytes")) is not int
                            or host["rootDisk"]["availableBytes"] < 2 * 1024**3
                            or totals != expected_totals
                            or type(sample.get("scheduleLagSeconds")) not in {int, float}
                            or not math.isfinite(sample["scheduleLagSeconds"])
                            or sample["scheduleLagSeconds"] < 0
                            or sample["scheduleLagSeconds"] > 5
                        ):
                            raise ObservationBoundaryError("OBSERVER_COMPLETED_SAMPLE_NOT_CLEAN")
                count += 1
        if count != expected_count:
            raise ObservationBoundaryError("OBSERVER_SAMPLE_COUNT_MISMATCH")
        return digest.hexdigest(), size, count
    except OSError as error:
        raise ObservationBoundaryError("OBSERVER_SAMPLE_FILE_UNAVAILABLE") from error


def _final_report(
    identity: dict[str, Any], *, allow_missing: bool = False
) -> dict[str, Any] | None:
    """Verify and preserve the actual full-run report and sample-file digest."""

    directory = Path(identity["outputDirectory"])
    found = []
    for path in directory.glob("report-*.json"):
        if path.is_symlink() or not path.is_file():
            raise ObservationBoundaryError("OBSERVER_REPORT_PATH_INVALID")
        report, raw = _json(path, maximum=1024**2)
        if report.get("manifestSha256") != identity["manifestSha256"]:
            continue
        if report.get("expectedSamples") != 1441:
            continue
        run_id = report.get("runId")
        sample_name = report.get("sampleFile")
        if (
            not isinstance(run_id, str)
            or RUN_RE.fullmatch(run_id) is None
            or path.name != f"report-{run_id}.json"
            or sample_name != f"samples-{run_id}.jsonl"
        ):
            raise ObservationBoundaryError("OBSERVER_REPORT_SAMPLE_NAME_INVALID")
        sample_path = directory / sample_name
        complete = report.get("complete24h")
        failed, missed, written = (
            report.get("failedSamples"),
            report.get("missedSlots"),
            report.get("samplesWritten"),
        )
        sample_sha, sample_bytes, _sample_count = _sample_evidence(
            sample_path,
            written,
            identity.get("expectedServiceNames", []),
            expected_service_count=identity["expectedServiceCount"],
            expected_totals=identity["expectedTotals"],
            full_run=complete is True,
            require_clean=report.get("status") == "PASS",
        )
        bindings = report.get("identityBindings")
        if (
            sample_sha != report.get("sampleFileSha256")
            or type(report.get("sampleFileBytes")) is not int
            or sample_bytes != report.get("sampleFileBytes")
            or report.get("status") not in {"PASS", "FAIL", "RUNNING"}
            or type(complete) is not bool
            or type(failed) is not int
            or failed < 0
            or type(missed) is not int
            or missed < 0
            or type(written) is not int
            or not 0 <= written <= 1441
            or (complete and (written != 1441 or report["status"] not in {"PASS", "FAIL"}))
            or (complete and report.get("samplingComplete") is not True)
            or (not complete and report["status"] != "RUNNING")
            or (report["status"] == "PASS" and (not complete or failed != 0 or missed != 0))
            or (report["status"] == "FAIL" and (not complete or failed == 0 and missed == 0))
            or report.get("evidenceClass") != "HEALTH_ONLY_NOT_BUSINESS_OR_PROVIDER_ACCEPTANCE"
            or not isinstance(bindings, dict)
            or bindings.get("imageLockSha256") != identity["imageLockSha256"]
            or bindings.get("staticManifestSha256") != identity.get("staticManifestSha256")
            or report.get("providersOrModelsCalled") is not False
            or report.get("dockerExecUsed") is not False
        ):
            raise ObservationBoundaryError("OBSERVER_REPORT_SAMPLE_BINDING_INVALID")
        failures = report.get("failureCountsByService")
        if (
            not isinstance(failures, dict)
            or any(
                not isinstance(service, str)
                or not isinstance(counts, dict)
                or any(
                    not isinstance(code, str) or type(count) is not int or count < 0
                    for code, count in counts.items()
                )
                for service, counts in failures.items()
            )
            or (
                report["status"] == "PASS"
                and any(count for counts in failures.values() for count in counts.values())
            )
        ):
            raise ObservationBoundaryError("OBSERVER_FAILURE_COUNTS_INVALID")
        found.append(
            {
                "reportFile": path.name,
                "reportSha256": _sha(raw),
                "runId": run_id,
                "manifestSha256": identity["manifestSha256"],
                "selectorSha256": identity.get("selectorSha256"),
                "imageLockSha256": identity["imageLockSha256"],
                "staticManifestSha256": identity.get("staticManifestSha256"),
                "sampleFile": sample_name,
                "sampleSha256": sample_sha,
                "status": report["status"],
                "complete24h": complete,
                "failedSamples": failed,
                "missedSlots": missed,
                "samplesWritten": written,
                "failureCountsByService": failures,
                "restartEventsByService": report.get("restartEventsByService", {}),
                "startedAtUtc": report.get("startedAtUtc"),
                "endedAtUtc": report.get("endedAtUtc"),
            }
        )
    if not found and allow_missing:
        return None
    if len(found) != 1:
        raise ObservationBoundaryError(
            "OBSERVER_FINAL_REPORT_NOT_UNIQUE", state="RECOVERY_REQUIRED"
        )
    return found[0]


def _historical_manifest(
    path: Path, selected: dict[str, Any], profile: dict[str, Any]
) -> dict[str, Any] | None:
    """Find a sealed manifest for the current selector without relying on unit liveness."""

    prefix = "final-health-manifest-"
    if path.parent != OBSERVER_ROOT / "manifests" or not path.name.startswith(prefix):
        raise ObservationBoundaryError("OBSERVER_MANIFEST_PATH_INVALID")
    digest = path.name.removeprefix(prefix).removesuffix(".json")
    if SHA_RE.fullmatch(digest) is None or path.name != f"{prefix}{digest}.json":
        raise ObservationBoundaryError("OBSERVER_MANIFEST_PATH_INVALID")
    manifest, raw = _json(path)
    identity, tooling = manifest.get("identityBindings"), manifest.get("tooling")
    if (
        _sha(raw) != digest
        or manifest.get("project") != "cyrene-teams-cloud"
        or not isinstance(identity, dict)
        or not isinstance(tooling, dict)
    ):
        raise ObservationBoundaryError("OBSERVER_MANIFEST_BINDING_MISMATCH")
    if (
        identity.get("imageLockSha256") != selected["imageLockSha256"]
        or tooling.get("observerSourceSha256") != profile["hashes"][OBSERVER]
        or tooling.get("manifestGeneratorSha256") != profile["hashes"][GENERATOR]
    ):
        return None
    static_path = profile["directory"] / (
        f"final-health-static-{selected['imageLockSha256'][:12]}-"
        f"{selected['selectorSha256'][:12]}-{profile['hashes'][OBSERVER][:12]}.json"
    )
    try:
        static, static_raw = _json(static_path)
    except ObservationBoundaryError as error:
        if error.code == "PRIVATE_FILE_UNAVAILABLE":
            return None
        raise
    if identity.get("staticManifestSha256") != _sha(static_raw):
        return None
    if (
        static.get("selectorSha256") != selected["selectorSha256"]
        or static.get("imageLockSha256") != selected["imageLockSha256"]
        or static.get("healthObserverSourceSha256") != profile["hashes"][OBSERVER]
        or static.get("manifestGeneratorSourceSha256") != profile["hashes"][GENERATOR]
        or static.get("healthPolicyTemplateSha256") != profile["hashes"][POLICY]
        or static.get("expectedServiceCount") != profile["expectedServiceCount"]
        or not isinstance(static.get("serviceNames"), list)
        or len(static["serviceNames"]) != profile["expectedServiceCount"]
        or len(set(static["serviceNames"])) != profile["expectedServiceCount"]
    ):
        raise ObservationBoundaryError("OBSERVER_STATIC_SELECTOR_BINDING_MISMATCH")
    policy = manifest.get("policy")
    gates = policy.get("hostGates") if isinstance(policy, dict) else None
    if (
        not isinstance(policy, dict)
        or policy.get("expectedServiceCount") != profile["expectedServiceCount"]
        or policy.get("expectedTotals") != profile["expectedTotals"]
        or policy.get("schedule") != EXPECTED_SCHEDULE
        or policy.get("expectedServiceNames") != static["serviceNames"]
        or not isinstance(gates, dict)
        or type(gates.get("minMemAvailableBytes")) is not int
        or gates["minMemAvailableBytes"] < 1024**3
        or type(gates.get("minRootDiskAvailableBytes")) is not int
        or gates["minRootDiskAvailableBytes"] < 2 * 1024**3
    ):
        raise ObservationBoundaryError("OBSERVER_MANIFEST_GATES_WEAKENED")
    return {
        "manifestSha256": digest,
        "selectorSha256": selected["selectorSha256"],
        "imageLockSha256": selected["imageLockSha256"],
        "staticManifestSha256": identity["staticManifestSha256"],
        "expectedServiceNames": static["serviceNames"],
        "expectedServiceCount": profile["expectedServiceCount"],
        "expectedTotals": profile["expectedTotals"],
    }


def _latest_observation_report(
    selected: dict[str, Any], profile: dict[str, Any]
) -> dict[str, Any] | None:
    """Read completed or interrupted full-run reports for the exact current selector."""

    manifests_dir = OBSERVER_ROOT / "manifests"
    runs_dir = OBSERVER_ROOT / "runs"
    if not manifests_dir.exists() or not runs_dir.exists():
        return None
    _private_dir(manifests_dir)
    _private_dir(runs_dir)
    manifests = sorted(manifests_dir.glob("final-health-manifest-*.json"))
    if len(manifests) > 10000:
        raise ObservationBoundaryError("OBSERVER_HISTORY_LIMIT_EXCEEDED")
    reports = []
    for manifest_path in manifests:
        binding = _historical_manifest(manifest_path, selected, profile)
        if binding is None:
            continue
        for run_dir in sorted(runs_dir.iterdir()):
            if RUN_RE.fullmatch(run_dir.name) is None:
                continue
            _private_dir(run_dir)
            identity = {**binding, "outputDirectory": str(run_dir)}
            report = _final_report(identity, allow_missing=True)
            if report is not None:
                reports.append(report)
    return max(reports, key=lambda item: item["runId"]) if reports else None


def _receipt(
    action: str, release: Path, selected: dict[str, Any], profile: dict[str, Any]
) -> dict[str, Any]:
    """Return the shared immutable identity envelope for a boundary receipt."""

    return {
        "schemaVersion": SCHEMA,
        "action": action,
        "releaseId": release.name,
        "selectorSha256": selected["selectorSha256"],
        "imageLockSha256": selected["imageLockSha256"],
        "sourceProfileSha256": profile["sha256"],
        "sourceHashes": profile["hashes"],
        "updatedAtUtc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def _instance_key(identity: dict[str, Any]) -> str:
    """Key receipts by the exact unit, selector, and unique observer run directory."""

    value = {
        "unit": identity["unit"],
        "selectorSha256": identity["selectorSha256"],
        "runId": Path(identity["outputDirectory"]).name,
    }
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _receipt_documents(boundary: Path, prefix: str) -> list[dict[str, Any]]:
    """Read existing immutable boundary receipts for idempotency and recovery."""

    result = []
    for path in sorted(boundary.glob(f"{prefix}-*.json")):
        if re.fullmatch(rf"{re.escape(prefix)}-[0-9a-f]{{64}}\.json", path.name) is None:
            raise ObservationBoundaryError("BOUNDARY_RECEIPT_NAME_INVALID")
        receipt, _ = _json(path, maximum=256 * 1024)
        result.append(receipt)
    return result


def _pending_intent(boundary: Path, prefix: str) -> bool:
    """Find any immutable action intent without a matching completed receipt."""

    intents = sorted(boundary.glob(f"{prefix}-intent-*.json"))
    for path in intents:
        if path.is_symlink() or not path.is_file():
            raise ObservationBoundaryError("BOUNDARY_INTENT_PATH_INVALID")
        intent, _ = _json(path, maximum=256 * 1024)
        attempt_id = intent.get("attemptId")
        if not isinstance(attempt_id, str) or re.fullmatch(r"[0-9a-f]{32}", attempt_id) is None:
            raise ObservationBoundaryError("BOUNDARY_INTENT_INVALID", state="RECOVERY_REQUIRED")
        matches = [
            item
            for item in _receipt_documents(boundary, f"{prefix}-receipt")
            if item.get("attemptId") == attempt_id
        ]
        if not matches:
            return True
        if len(matches) != 1 or any(
            matches[0].get(key) != intent.get(key)
            for key in ("action", "releaseId", "selectorSha256", "sourceProfileSha256")
        ):
            raise ObservationBoundaryError(
                "BOUNDARY_INTENT_RECEIPT_MISMATCH", state="RECOVERY_REQUIRED"
            )
    return False


def read_observation_status(
    selection_path: Path, observer_policy: dict[str, Any]
) -> dict[str, Any]:
    """Read current observer liveness against the same pinned selector/profile."""

    if os.geteuid() != 0:
        raise ObservationBoundaryError("ROOT_OPERATOR_REQUIRED")
    profile = _source_profile(observer_policy)
    selected = _selection(selection_path)
    active = _active_observers(selected, profile)
    if active:
        identity = active[0]
        result = {
            "schemaVersion": SCHEMA,
            "status": "RUNNING",
            "unit": identity["unit"],
            "selectorSha256": selected["selectorSha256"],
            "complete24h": False,
            "evidenceClass": "HEALTH_ONLY_NOT_BUSINESS_OR_PROVIDER_ACCEPTANCE",
            "imageLockSha256": identity["imageLockSha256"],
        }
    else:
        report = _latest_observation_report(selected, profile)
        if report is None:
            result = {
                "schemaVersion": SCHEMA,
                "status": "NOT_RUNNING",
                "unit": None,
                "selectorSha256": selected["selectorSha256"],
                "complete24h": False,
                "evidenceClass": "HEALTH_ONLY_NOT_BUSINESS_OR_PROVIDER_ACCEPTANCE",
            }
        else:
            successful = (
                report["status"] == "PASS"
                and report["complete24h"] is True
                and report["samplesWritten"] == 1441
                and report["failedSamples"] == 0
                and report["missedSlots"] == 0
            )
            result = {
                "schemaVersion": SCHEMA,
                "status": "COMPLETED"
                if successful
                else "FAILED"
                if report["complete24h"]
                else "NOT_RUNNING",
                "unit": f"{UNIT_PREFIX}{report['manifestSha256'][:16]}.service",
                "selectorSha256": selected["selectorSha256"],
                "imageLockSha256": selected["imageLockSha256"],
                "complete24h": report["complete24h"],
                "evidenceClass": "HEALTH_ONLY_NOT_BUSINESS_OR_PROVIDER_ACCEPTANCE",
                "report": report,
            }
    if _selection(selection_path)["selectorSha256"] != selected["selectorSha256"]:
        raise ObservationBoundaryError(
            "OBSERVER_SELECTOR_CHANGED_DURING_STATUS", state="RECOVERY_REQUIRED"
        )
    return result


def close_active_observation(
    selection_path: Path, release_dir: Path, observer_policy: dict[str, Any]
) -> dict[str, Any]:
    """Stop only the exact current observer and retain its original report and samples."""

    if os.geteuid() != 0:
        raise ObservationBoundaryError("ROOT_OPERATOR_REQUIRED")
    profile = _source_profile(observer_policy)
    selected = _selection(selection_path)
    release = _release_dir(release_dir, profile)
    boundary = _boundary_dir(release)
    if _pending_intent(boundary, "close"):
        raise ObservationBoundaryError("CLOSE_INTENT_WITHOUT_RECEIPT", state="RECOVERY_REQUIRED")
    active = _active_observers(selected, profile)
    if not active:
        prior = [
            receipt
            for receipt in _receipt_documents(boundary, "close-receipt")
            if receipt.get("selectorSha256") == selected["selectorSha256"]
            and receipt.get("sourceProfileSha256") == profile["sha256"]
        ]
        if prior:
            latest = max(prior, key=lambda item: item.get("updatedAtUtc", ""))
            return {**latest, "status": "ALREADY_CLOSED"}
        raise ObservationBoundaryError("ACTIVE_OBSERVER_NOT_FOUND", state="RECOVERY_REQUIRED")
    identity = active[0]
    key = _instance_key(identity)
    receipt_path = boundary / f"close-receipt-{key}.json"
    if receipt_path.exists():
        raise ObservationBoundaryError("CLOSE_RECEIPT_CONFLICT", state="RECOVERY_REQUIRED")
    attempt_id = uuid.uuid4().hex
    _write_once(
        boundary / f"close-intent-{attempt_id}.json",
        {
            **_receipt("CLOSE", release, selected, profile),
            "state": "CLOSE_INTENT",
            "attemptId": attempt_id,
            "observerKey": key,
            "unit": identity["unit"],
            "manifestSha256": identity["manifestSha256"],
        },
    )
    stopped = _systemctl(["stop", identity["unit"]], timeout=45)
    if stopped.returncode != 0:
        raise ObservationBoundaryError("OBSERVER_STOP_OUTCOME_UNKNOWN", state="RECOVERY_REQUIRED")
    deadline = time.monotonic() + 35
    while True:
        state = _unit_properties(identity["unit"])
        if state["ActiveState"] in {"inactive", "failed"} and state["MainPID"] == "0":
            break
        if time.monotonic() >= deadline:
            raise ObservationBoundaryError(
                "OBSERVER_STOP_READBACK_TIMEOUT", state="RECOVERY_REQUIRED"
            )
        time.sleep(1)
    report = _final_report(identity)
    receipt = {
        **_receipt("CLOSE", release, selected, profile),
        "status": "OBSERVER_CLOSED",
        "attemptId": attempt_id,
        "observerKey": key,
        "unit": identity["unit"],
        "manifestSha256": identity["manifestSha256"],
        "observer": report,
        "appOrProviderCalls": False,
    }
    _write_once(receipt_path, receipt)
    return receipt


def start_post_deployment_observation(
    selection_path: Path, release_dir: Path, observer_policy: dict[str, Any]
) -> dict[str, Any]:
    """Run the fixed wrapper once, then require exact active/running selector readback."""

    if os.geteuid() != 0:
        raise ObservationBoundaryError("ROOT_OPERATOR_REQUIRED")
    profile = _source_profile(observer_policy)
    selected = _selection(selection_path)
    release = _release_dir(release_dir, profile)
    boundary = _boundary_dir(release)
    if _pending_intent(boundary, "start"):
        raise ObservationBoundaryError("START_INTENT_WITHOUT_RECEIPT", state="RECOVERY_REQUIRED")
    active = _active_observers(selected, profile)
    if active:
        identity = active[0]
        key = _instance_key(identity)
        receipt_path = boundary / f"start-receipt-{key}.json"
        if receipt_path.exists():
            receipt, _ = _json(receipt_path, maximum=256 * 1024)
            if (
                receipt.get("unit") != identity["unit"]
                or receipt.get("selectorSha256") != selected["selectorSha256"]
                or receipt.get("observerKey") != key
            ):
                raise ObservationBoundaryError("START_RECEIPT_CONFLICT", state="RECOVERY_REQUIRED")
            return {**receipt, "status": "ALREADY_RUNNING"}
        receipt = {
            **_receipt("START", release, selected, profile),
            "status": "ALREADY_RUNNING",
            "observerKey": key,
            "unit": identity["unit"],
            "manifestSha256": identity["manifestSha256"],
            "complete24h": False,
            "samplesPlanned": 1441,
            "evidenceClass": "HEALTH_ONLY_NOT_BUSINESS_OR_PROVIDER_ACCEPTANCE",
        }
        _write_once(receipt_path, receipt)
        return receipt
    attempt_id = uuid.uuid4().hex
    _write_once(
        boundary / f"start-intent-{attempt_id}.json",
        {
            **_receipt("START", release, selected, profile),
            "state": "START_INTENT",
            "attemptId": attempt_id,
        },
    )
    try:
        result = subprocess.run(
            [
                "/bin/bash",
                str(profile["directory"] / WRAPPER),
                "--expected-image-lock-sha256",
                selected["imageLockSha256"],
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            cwd="/",
            env=ENV,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ObservationBoundaryError(
            "OBSERVER_START_OUTCOME_UNKNOWN", state="RECOVERY_REQUIRED"
        ) from error
    try:
        lines = result.stdout.splitlines()
        summary = json.loads(lines[0], object_pairs_hook=_pairs) if len(lines) == 1 else None
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ObservationBoundaryError(
            "OBSERVER_START_SUMMARY_INVALID", state="RECOVERY_REQUIRED"
        ) from error
    expected_keys = {
        "status",
        "evidenceClass",
        "unit",
        "serviceCount",
        "samplesPlanned",
        "durationSeconds",
        "runtimeMaxSeconds",
        "manifestSha256",
        "selectorSha256",
        "imageLockSha256",
        "preflight",
    }
    if (
        result.returncode != 0
        or not isinstance(summary, dict)
        or set(summary) != expected_keys
        or summary.get("status") != "OBSERVER_STARTED"
        or summary.get("evidenceClass") != "HEALTH_ONLY_NOT_BUSINESS_OR_PROVIDER_ACCEPTANCE"
        or summary.get("serviceCount") != profile["expectedServiceCount"]
        or summary.get("samplesPlanned") != 1441
        or summary.get("durationSeconds") != 86400
        or summary.get("runtimeMaxSeconds") != 93600
        or summary.get("selectorSha256") != selected["selectorSha256"]
        or summary.get("imageLockSha256") != selected["imageLockSha256"]
        or summary.get("preflight") != "RUNNING_ZERO_FAILURES"
    ):
        raise ObservationBoundaryError("OBSERVER_START_NOT_CONFIRMED", state="RECOVERY_REQUIRED")
    summary_unit, manifest_sha = summary.get("unit"), summary.get("manifestSha256")
    if (
        not isinstance(summary_unit, str)
        or UNIT_SUMMARY_RE.fullmatch(summary_unit) is None
        or not isinstance(manifest_sha, str)
        or SHA_RE.fullmatch(manifest_sha) is None
        or summary_unit != f"{UNIT_PREFIX}{manifest_sha[:16]}"
    ):
        raise ObservationBoundaryError("OBSERVER_START_IDENTITY_INVALID", state="RECOVERY_REQUIRED")
    unit = summary_unit + ".service"
    deadline = time.monotonic() + 30
    identity = None
    while time.monotonic() < deadline:
        state = _unit_properties(unit)
        if state["ActiveState"] == "active" and state["SubState"] == "running":
            identity = _unit_identity(unit, state, selected, profile)
            break
        if state["ActiveState"] in {"inactive", "failed"}:
            break
        time.sleep(1)
    if (
        identity is None
        or identity["manifestSha256"] != manifest_sha
        or identity["selectorSha256"] != summary["selectorSha256"]
        or identity["imageLockSha256"] != summary["imageLockSha256"]
        or _selection(selection_path)["selectorSha256"] != selected["selectorSha256"]
    ):
        raise ObservationBoundaryError("OBSERVER_START_READBACK_FAILED", state="RECOVERY_REQUIRED")
    key = _instance_key(identity)
    receipt_path = boundary / f"start-receipt-{key}.json"
    receipt = {
        **_receipt("START", release, selected, profile),
        "status": "OBSERVER_STARTED",
        "attemptId": attempt_id,
        "observerKey": key,
        "unit": unit,
        "manifestSha256": manifest_sha,
        "complete24h": False,
        "samplesPlanned": 1441,
        "durationSeconds": 86400,
        "evidenceClass": "HEALTH_ONLY_NOT_BUSINESS_OR_PROVIDER_ACCEPTANCE",
        "appOrProviderCalls": False,
    }
    _write_once(receipt_path, receipt)
    return receipt
