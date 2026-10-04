#!/usr/bin/env python3
"""
┌─────────────────────────────────────────────────────────────────────┐
│ Module: tooling.acceptance.native_components_v2.native_acceptance   │
│ Role: Record real-host native component acceptance evidence.         │
│                                                                      │
│ 模块职责：记录原生组件真机验收证据，不执行安装或 GPU 工作负载。          │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

RUN_SCHEMA_VERSION = 1
DEFAULT_SSH_ALIAS = "RuanYun-3090-Tailscale"
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
RAW_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SSH_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
EVIDENCE_CLASSES = frozenset(
    {"REAL_HOST", "REAL_PRODUCT", "RELEASE_PROVENANCE", "HUMAN_REVIEW", "FIXTURE", "SIMULATED"}
)
REAL_EVIDENCE_CLASSES = frozenset({"REAL_HOST", "REAL_PRODUCT"})
PHASES = (
    "source_and_builder_provenance",
    "admin_initialization",
    "kernel_gpu_preflight",
    "yield_llama_factory_one_step",
    "active_task_stage_allowed",
    "active_task_apply_refused",
    "component_digest_plan_confirmation",
    "owner_worker_lease_resource_release",
    "systemd_candidate_failure_rollback",
    "interrupted_update_recovery",
    "forced_cyrene_relay_path",
    "control_client_host_protocol",
)
OBSERVATION_KINDS = frozenset({"artifact", "plan", "task", "resource"})
KERNEL_READONLY_TOOL_NAME = "cyrene-kernel-readonly-probe"
KERNEL_READONLY_REMOTE_DIRECTORY = ".local/share/cyrene/native-operator-acceptance/20261004"
NATIVE_RELEASE_HELPER = Path("scripts/native_installer_release.py")
NATIVE_RELEASE_REPOSITORY = "DoHorizon-AI/Cyrene-Workspace"
TARGET_PROFILES = {
    "22.04": "linux-ubuntu-22.04-x86_64-python-3.12",
    "24.04": "linux-ubuntu-24.04-x86_64-python-3.12",
}

REMOTE_PREPARE_SCRIPT = r"""set -eu
umask 077
base="$HOME/.local/share/cyrene/native-operator-acceptance"
target="$base/20261004"
for directory in "$HOME/.local/share" "$HOME/.local/share/cyrene" "$base"; do
    if [ -L "$directory" ]; then
        echo "REFUSED_SYMLINK:$directory" >&2
        exit 3
    fi
done
mkdir -m 700 -p "$base"
owner="$(stat -c '%u' "$base")"
current="$(id -u)"
mode="$(stat -c '%a' "$base")"
if [ "$owner" != "$current" ] || [ "$mode" != "700" ]; then
    echo "REFUSED_UNSAFE_BASE:$owner:$mode" >&2
    exit 4
fi
if [ -e "$target" ]; then
    if [ -L "$target" ] || [ ! -d "$target" ]; then
        echo "REFUSED_UNSAFE_TARGET" >&2
        exit 5
    fi
    target_owner="$(stat -c '%u' "$target")"
    target_mode="$(stat -c '%a' "$target")"
    if [ "$target_owner" != "$current" ] || [ "$target_mode" != "700" ]; then
        echo "REFUSED_UNSAFE_EXISTING_TARGET:$target_owner:$target_mode" >&2
        exit 6
    fi
    echo "EXISTING_UNMODIFIED:$target"
else
    mkdir -m 700 "$target"
    echo "CREATED:$target"
fi
"""

REMOTE_PROBE_SCRIPT = r"""import datetime
import json
import os
import platform
import pathlib
import stat
import subprocess


def run(args, timeout=4):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return {
            "exitCode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip()[:300],
        }
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return {"exitCode": None, "stdout": "", "stderr": type(error).__name__}


def present(path):
    return os.path.lexists(path)


os_release = {}
try:
    for line in open("/etc/os-release", encoding="utf-8"):
        key, separator, value = line.strip().partition("=")
        if separator and key in {"ID", "VERSION_ID", "PRETTY_NAME"}:
            os_release[key] = value.strip('"')
except OSError:
    pass

managed_units = (
    "cyrene-runtime-maintenance.service",
    "cyrene-kernel.service",
    "cyrene-linux-sys-adapter.service",
    "cyrene-nvidia-adapter.service",
    "cyrene-sandboxd.service",
    "cyrene-yield.service",
)
units = {}
for unit in managed_units:
    units[unit] = run(
        ["systemctl", "show", "--no-pager", "--property=LoadState,ActiveState", unit]
    )

runtime_root = pathlib.Path.home() / ".local/share/cyrene-platform-runtime"
known_process_names = {
    "cyrene-linux-sys-adapter",
    "cyrene-nvidia-adapter",
    "cyrene-sandboxd",
    "cyrene-kernel",
}
processes = {}
try:
    proc_entries = os.scandir("/proc")
except OSError:
    proc_entries = []
for entry in proc_entries:
    if not entry.name.isdecimal():
        continue
    pid = entry.name
    try:
        comm = open(f"/proc/{pid}/comm", encoding="utf-8").read().strip()
        if comm not in known_process_names:
            continue
        state = open(f"/proc/{pid}/stat", encoding="utf-8").read().split()[2]
        executable = os.readlink(f"/proc/{pid}/exe")
        processes[str(pid)] = {
            "present": True,
            "comm": comm,
            "state": state,
            "executable": executable,
            "underUserRuntimeRoot": executable.startswith(str(runtime_root) + "/"),
        }
    except (FileNotFoundError, PermissionError, IndexError):
        continue

kernel_socket = runtime_root / "run/kernel.sock"
try:
    socket_present = stat.S_ISSOCK(kernel_socket.stat().st_mode)
except OSError:
    socket_present = False

state_paths = {
    "/etc/cyrene": os.path.lexists("/etc/cyrene"),
    "/var/lib/cyrene": os.path.lexists("/var/lib/cyrene"),
    "/var/lib/cyrene/runtime/activity-sources.json": os.path.lexists(
        "/var/lib/cyrene/runtime/activity-sources.json"
    ),
    "/var/lib/cyrene-updates": os.path.lexists("/var/lib/cyrene-updates"),
    "/usr/share/cyrene/component-catalog-state.json": os.path.lexists(
        "/usr/share/cyrene/component-catalog-state.json"
    ),
    "/etc/cyrene/runtime-activity-source-tokens": os.path.lexists(
        "/etc/cyrene/runtime-activity-source-tokens"
    ),
    "/opt/cyrene": os.path.lexists("/opt/cyrene"),
}
package = run(
    [
        "dpkg-query",
        "-W",
        "-f=" + chr(36) + "{db:Status-Status}\t" + chr(36) + "{Version}\n",
        "cyrene",
    ]
)

gpu = run(
    [
        "nvidia-smi",
        "--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu",
        "--format=csv,noheader",
    ]
)
sudo_probe = run(["sudo", "-n", "true"])
docker_socket = "/var/run/docker.sock"
try:
    docker_access = os.access(docker_socket, os.R_OK | os.W_OK)
except OSError:
    docker_access = False

print(
    json.dumps(
        {
            "capturedAtUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "hostname": platform.node(),
            "os": os_release,
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "python3": run(["python3", "--version"]),
            "systemd": run(["systemctl", "--version"]),
            "managedSystemUnits": units,
            "existingManualRuntimeProcesses": processes,
            "knownUserRuntimeRoot": str(runtime_root),
            "kernelSocketPath": str(kernel_socket) if socket_present else None,
            "packageMetadata": package,
            "existingStatePaths": state_paths,
            "gpuTelemetry": gpu,
            "noninteractiveSudoAvailable": sudo_probe["exitCode"] == 0,
            "dockerSocketPresent": present(docker_socket),
            "dockerSocketAccessible": docker_access,
            "brokerUnitState": units["cyrene-runtime-maintenance.service"],
            "activityCatalogPresent": present("/var/lib/cyrene/runtime/activity-sources.json"),
            "formalUpdaterPresent": os.access("/usr/bin/cyrene", os.X_OK),
            "taskLeaseWorkerState": "UNKNOWN",
            "applyAdmission": "CLOSED",
            "gpuIdleConclusion": "UNKNOWN",
            "limits": [
                "Read-only inventory; no Product or Kernel task query was made.",
                "GPU telemetry and process presence do not prove Worker, Lease, or idle state.",
                "Kernel binding-aware GPU preflight was not run against this baseline.",
            ],
        },
        sort_keys=True,
    )
)
"""

REMOTE_KERNEL_QUERY_SCRIPT = r"""set -eu
probe="$HOME/.local/share/cyrene/native-operator-acceptance/20261004/$1"
expected_digest="$2"
socket_path="$3"
if [ -L "$probe" ] || [ ! -f "$probe" ]; then
    echo "REFUSED_UNSAFE_KERNEL_PROBE" >&2
    exit 12
fi
if [ "$(stat -c '%a' "$probe")" != "700" ]; then
    echo "REFUSED_KERNEL_PROBE_MODE" >&2
    exit 13
fi
actual_digest="$(sha256sum "$probe" | cut -d ' ' -f 1)"
if [ "$actual_digest" != "$expected_digest" ]; then
    echo "REFUSED_KERNEL_PROBE_DIGEST" >&2
    exit 14
fi
exec "$probe" --socket "$socket_path"
"""

REMOTE_KERNEL_COPY_GUARD_SCRIPT = r"""set -eu
probe="$HOME/.local/share/cyrene/native-operator-acceptance/20261004/$1"
expected_digest="$2"
if [ -L "$probe" ]; then
    echo "REFUSED_KERNEL_PROBE_SYMLINK" >&2
    exit 15
fi
if [ -e "$probe" ]; then
    if [ ! -f "$probe" ] || [ "$(stat -c '%a' "$probe")" != "700" ]; then
        echo "REFUSED_KERNEL_PROBE_EXISTING_PATH" >&2
        exit 16
    fi
    actual_digest="$(sha256sum "$probe" | cut -d ' ' -f 1)"
    if [ "$actual_digest" != "$expected_digest" ]; then
        echo "REFUSED_KERNEL_PROBE_EXISTING_DIGEST" >&2
        exit 17
    fi
    echo "EXISTING_VERIFIED"
else
    echo "READY"
fi
"""


class AcceptanceError(ValueError):
    """Raised when evidence is malformed or would overstate real acceptance.

    中文：证据格式错误或会夸大真实验收结论时抛出。
    """


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp for an evidence event.

    中文：生成证据事件使用的 ISO-8601 UTC 时间。
    """

    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    """Return a content digest with the schema's ``sha256:`` prefix.

    中文：返回带有 schema `sha256:` 前缀的内容摘要。
    """

    return "sha256:" + hashlib.sha256(data).hexdigest()


def validate_ssh_alias(value: str) -> str:
    """Accept an SSH config alias without shell metacharacters.

    中文：只接受不含 shell 元字符的 SSH config alias。
    """

    if SSH_ALIAS_PATTERN.fullmatch(value) is None:
        raise AcceptanceError("SSH alias contains unsupported characters")
    return value


def _git_output(path: Path, *arguments: str) -> str:
    """Read one Git identity value without fetching or changing repository state.

    中文：只读 Git 身份信息，不 fetch 或改动仓库状态。
    """

    completed = subprocess.run(
        ["git", "-C", str(path), *arguments], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise AcceptanceError(f"Cannot read Git source at {path}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def source_identity(name: str, source_path: Path) -> dict[str, Any]:
    """Capture a repository's exact local head and dirty-path summary.

    中文：记录仓库本地精确 HEAD 以及未提交路径清单。
    """

    path = source_path.expanduser().resolve(strict=True)
    commit = _git_output(path, "rev-parse", "HEAD")
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise AcceptanceError(f"{name}: Git returned an invalid commit SHA")
    branch = _git_output(path, "branch", "--show-current") or "DETACHED"
    status = _git_output(path, "status", "--porcelain=v1", "--untracked-files=normal")
    dirty_paths = [line[3:] for line in status.splitlines() if len(line) >= 4]
    return {
        "path": str(path),
        "branch": branch,
        "commit": commit,
        "workingTree": "DIRTY" if dirty_paths else "CLEAN",
        "dirtyPaths": dirty_paths,
    }


def _source_tree_fingerprint(source_path: Path) -> dict[str, str]:
    """Hash tracked diffs and untracked files around a candidate tool build.

    中文：在候选只读工具构建前后摘要跟踪差异与未跟踪源码。
    """

    path = source_path.expanduser().resolve(strict=True)
    patch = subprocess.run(
        ["git", "-C", str(path), "diff", "--binary", "HEAD"],
        capture_output=True,
        check=False,
    )
    if patch.returncode != 0:
        raise AcceptanceError(f"Cannot fingerprint Git source at {path}")
    untracked_result = subprocess.run(
        ["git", "-C", str(path), "ls-files", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        check=False,
    )
    if untracked_result.returncode != 0:
        raise AcceptanceError(f"Cannot list untracked Git source files at {path}")
    untracked_records: list[bytes] = []
    for raw_name in untracked_result.stdout.split(b"\0"):
        if not raw_name:
            continue
        relative = Path(os.fsdecode(raw_name))
        file_path = path / relative
        if file_path.is_symlink() or not file_path.is_file():
            untracked_records.append(raw_name + b"\0UNSAFE\0")
            continue
        untracked_records.append(
            raw_name + b"\0" + hashlib.sha256(file_path.read_bytes()).hexdigest().encode() + b"\0"
        )
    status_result = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        capture_output=True,
        check=False,
    )
    if status_result.returncode != 0:
        raise AcceptanceError(f"Cannot read Git source status at {path}")
    content = b"\0".join(
        [b"diff", patch.stdout, b"status", status_result.stdout, b"untracked", *untracked_records]
    )
    return {
        "commit": _git_output(path, "rev-parse", "HEAD"),
        "workingTreeDigest": sha256_bytes(content),
        "statusDigest": sha256_bytes(status_result.stdout),
    }


def compile_kernel_readonly_probe(
    platform_source: Path, target_directory: Path, binary_output: Path
) -> dict[str, Any]:
    """Compile an isolated client that only calls the read-only Kernel UDS RPCs.

    中文：隔离编译只调用 Kernel UDS 只读 RPC 的客户端。
    """

    platform_path = platform_source.expanduser().resolve(strict=True)
    proto_manifest = platform_path / "contracts" / "rust" / "cy-proto" / "Cargo.toml"
    if not proto_manifest.is_file() or proto_manifest.is_symlink():
        raise AcceptanceError("Platform source has no safe cy-proto manifest")
    output = binary_output.expanduser()
    if output.exists() or output.is_symlink():
        raise AcceptanceError("Kernel query binary output already exists")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.parent.is_symlink() or output.parent.stat().st_mode & 0o077:
        raise AcceptanceError("Kernel query output directory must be private and not a symlink")
    target = target_directory.expanduser()
    if target.exists() and (target.is_symlink() or not target.is_dir()):
        raise AcceptanceError("Kernel query build target must be a real directory")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)

    runner_source = Path(__file__).resolve().with_name("kernel_readonly_probe.rs")
    if runner_source.is_symlink() or not runner_source.is_file():
        raise AcceptanceError("Kernel query source is missing or unsafe")
    before = _source_tree_fingerprint(platform_path)
    with tempfile.TemporaryDirectory(prefix="cyrene-kernel-probe-build-") as temporary:
        manifest = Path(temporary) / "Cargo.toml"
        manifest.write_text(
            "\n".join(
                [
                    "[package]",
                    'name = "cyrene-kernel-readonly-probe"',
                    'version = "0.1.0"',
                    'edition = "2021"',
                    "",
                    "[[bin]]",
                    'name = "cyrene-kernel-readonly-probe"',
                    f"path = {json.dumps(str(runner_source))}",
                    "",
                    "[dependencies]",
                    f"cy-proto = {{ path = {json.dumps(str(proto_manifest.parent))} }}",
                    'hyper-util = { version = "0.1", features = ["tokio"] }',
                    'serde_json = "1"',
                    'tokio = { version = "1", features = ["macros", "rt-multi-thread", "net"] }',
                    'tonic = { version = "0.12", features = ["transport"] }',
                    'tower = "0.5"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["CARGO_TARGET_DIR"] = str(target.resolve())
        environment["CARGO_BUILD_JOBS"] = "3"
        lock_result = subprocess.run(
            [
                "cargo",
                "generate-lockfile",
                "--offline",
                "--manifest-path",
                str(manifest),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=environment,
        )
        if lock_result.returncode != 0:
            detail = lock_result.stderr.strip().splitlines()[-8:]
            raise AcceptanceError("Kernel read-only probe lock failed: " + " | ".join(detail))
        lock_path = Path(temporary) / "Cargo.lock"
        if lock_path.is_symlink() or not lock_path.is_file():
            raise AcceptanceError("Kernel read-only probe build produced no lockfile")
        cargo_lock_digest = sha256_bytes(lock_path.read_bytes())
        completed = subprocess.run(
            [
                "cargo",
                "build",
                "--locked",
                "--release",
                "--offline",
                "--manifest-path",
                str(manifest),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env=environment,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()[-30:]
            raise AcceptanceError("Kernel read-only probe build failed: " + " | ".join(detail))
    built_binary = target.resolve() / "release" / KERNEL_READONLY_TOOL_NAME
    if built_binary.is_symlink() or not built_binary.is_file():
        raise AcceptanceError("Kernel read-only probe build produced no regular binary")
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with built_binary.open("rb") as source, os.fdopen(descriptor, "wb") as destination:
        shutil.copyfileobj(source, destination)
        destination.flush()
        os.fsync(destination.fileno())
    os.chmod(output, 0o700)
    after = _source_tree_fingerprint(platform_path)
    return {
        "source": after,
        "sourceStableDuringBuild": before == after,
        "binaryPath": str(output.resolve()),
        "binarySha256": sha256_bytes(output.read_bytes()),
        "binarySizeBytes": output.stat().st_size,
        "sourceSha256": sha256_bytes(runner_source.read_bytes()),
        "cargoLockSha256": cargo_lock_digest,
        "buildCommand": "cargo generate-lockfile --offline; cargo build --locked --release --offline --manifest-path <isolated temporary manifest>",
        "rpcAllowlist": [
            "cyrene.core.v1.KernelService/GetKernelCapabilities",
            "cyrene.core.v2.KernelAuthorityService/GetUpdateReadiness",
        ],
    }


def _load_native_release_helper(workspace_root: Path) -> tuple[Any, str]:
    """Load the repository's canonical native release verifier without a shell.

    中文：直接载入仓库正式原生发行验证器，不经 shell 包装。
    """

    helper_path = workspace_root / NATIVE_RELEASE_HELPER
    if helper_path.is_symlink() or not helper_path.is_file():
        raise AcceptanceError("Canonical native release verifier is missing or unsafe")
    before = helper_path.read_bytes()
    module_name = "cyrene_native_installer_release_acceptance_verifier"
    spec = importlib.util.spec_from_file_location(module_name, helper_path)
    if spec is None or spec.loader is None:
        raise AcceptanceError("Canonical native release verifier cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    after = helper_path.read_bytes()
    if before != after:
        sys.modules.pop(module_name, None)
        raise AcceptanceError("Canonical native release verifier changed while loading")
    return module, sha256_bytes(after)


def _assert_release_helper_matches_source_commit(
    workspace_root: Path, expected_source_commit: str
) -> None:
    """Require the imported verifier bytes to match the immutable source pin.

    中文：确保本地正式 verifier 与不可变发行源码提交中的字节完全相同。
    """

    helper_path = workspace_root / NATIVE_RELEASE_HELPER
    try:
        pinned = subprocess.run(
            [
                "git",
                "-C",
                str(workspace_root),
                "show",
                f"{expected_source_commit}:{NATIVE_RELEASE_HELPER.as_posix()}",
            ],
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise AcceptanceError("Cannot read the pinned native release verifier source") from error
    if pinned.returncode != 0 or pinned.stdout != helper_path.read_bytes():
        raise AcceptanceError(
            "Canonical native release verifier differs from the pinned Workspace source commit"
        )


def verify_native_release_for_host(
    release_directory: Path,
    *,
    expected_source_ref: str,
    expected_source_commit: str,
    ubuntu_version: str,
) -> dict[str, Any]:
    """Verify immutable release assets and select the exact supported host DEB.

    Args:
        release_directory: Downloaded immutable assets, including detached bundles.
        expected_source_ref: Exact allowed Workspace branch ref to verify.
        expected_source_commit: Full lowercase Workspace source SHA.
        ubuntu_version: Host Ubuntu version, either ``22.04`` or ``24.04``.
    Returns:
        A structured receipt derived only after official verifier success.

    中文：校验不可变发行资产，并按受支持的 Ubuntu 版本选择精确 DEB。
    """

    if expected_source_ref not in {
        "refs/heads/develop",
        "refs/heads/main",
        "refs/heads/release",
    }:
        raise AcceptanceError("Expected Workspace source ref is not an allowed release branch")
    if COMMIT_PATTERN.fullmatch(expected_source_commit) is None:
        raise AcceptanceError("Expected Workspace source commit must be a full lowercase SHA")
    try:
        profile = TARGET_PROFILES[ubuntu_version]
    except KeyError as error:
        raise AcceptanceError(
            "Host Ubuntu version is not supported by the release contract"
        ) from error

    requested_root = release_directory.expanduser()
    if requested_root.is_symlink():
        raise AcceptanceError("Release asset directory must not be a symlink")
    root = requested_root.resolve(strict=True)
    if not root.is_dir():
        raise AcceptanceError("Release asset path must resolve to a directory")
    workspace_root = Path(__file__).resolve().parents[3]
    _assert_release_helper_matches_source_commit(workspace_root, expected_source_commit)
    helper, helper_digest = _load_native_release_helper(workspace_root)
    try:
        manifest = helper.verify_release_directory(
            root,
            expected_repository=NATIVE_RELEASE_REPOSITORY,
            expected_source_ref=expected_source_ref,
            expected_source_commit=expected_source_commit,
            verify_attestations=True,
        )
        receipt = helper._read_json_object(
            root / "native-installer-source-receipt-v1.json", "verified source receipt"
        )
    except Exception as error:
        raise AcceptanceError(f"Official native release verification failed: {error}") from error

    source = manifest.get("source")
    if (
        manifest.get("repository") != NATIVE_RELEASE_REPOSITORY
        or manifest.get("workflow")
        != f"{NATIVE_RELEASE_REPOSITORY}/.github/workflows/native-installer-release.yml"
        or not isinstance(source, dict)
        or source.get("ref") != expected_source_ref
        or source.get("commit") != expected_source_commit
    ):
        raise AcceptanceError("Official verifier returned a mismatched release identity")
    targets = manifest.get("targets")
    selected = [row for row in targets if isinstance(row, dict) and row.get("targetId") == profile]
    if len(selected) != 1:
        raise AcceptanceError("Verified release does not contain exactly one target for this host")
    target = selected[0]
    safe_initialization = receipt.get("safeInitialization")
    if (
        not isinstance(safe_initialization, dict)
        or safe_initialization.get("verified") is not True
        or safe_initialization.get("mode") != "stage-only-verified-published-bytes"
    ):
        raise AcceptanceError("Source receipt has no verified stage-only initialization proof")
    proof_rows = safe_initialization.get("targets")
    proof_matches = (
        [row for row in proof_rows if isinstance(row, dict) and row.get("targetId") == profile]
        if isinstance(proof_rows, list)
        else []
    )
    if len(proof_matches) != 1:
        raise AcceptanceError(
            "Source receipt has no unique safe-initialization proof for this host"
        )
    proof = proof_matches[0]

    required_services = {component_id for _repository, component_id in helper.PRODUCTS.values()}
    expected_checks = {
        "serviceActivation": "deferred",
        "brokerAction": "preserve-existing",
        "oldRuntimeAction": "preserve",
        "maintainerScriptsStaticScan": "passed",
        "verifiedServiceBytesPreserved": "passed",
        "freshBrokerUnavailable": "fail-closed",
        "upgradeState": "preserve-existing",
        "activeRuntimePointers": "preserve-existing",
        "pinnedPrivateRuntime": "passed",
    }
    script_hashes = proof.get("maintainerScriptsSha256")
    services = proof.get("services")
    if (
        proof.get("targetId") != profile
        or not RAW_SHA256_PATTERN.fullmatch(str(proof.get("debSha256", "")))
        or proof.get("markerPath") != "/usr/share/cyrene/native-install-contract-v1.json"
        or not RAW_SHA256_PATTERN.fullmatch(str(proof.get("markerSha256", "")))
        or proof.get("serviceArtifactsIndexPath")
        != "/usr/share/cyrene/service-artifacts/index.json"
        or not RAW_SHA256_PATTERN.fullmatch(str(proof.get("serviceArtifactsIndexSha256", "")))
        or not isinstance(script_hashes, dict)
        or set(script_hashes) != {"postinst", "prerm", "postrm"}
        or any(not RAW_SHA256_PATTERN.fullmatch(str(value)) for value in script_hashes.values())
        or not isinstance(services, dict)
        or set(services) != required_services
        or proof.get("checks") != expected_checks
    ):
        raise AcceptanceError("Verified stage-only DEB proof is incomplete or non-canonical")
    deb_path = root / target["assetName"]
    deb_data = deb_path.read_bytes()
    actual_deb_sha = hashlib.sha256(deb_data).hexdigest()
    if (
        target.get("sha256") != actual_deb_sha
        or target.get("sizeBytes") != len(deb_data)
        or proof.get("debSha256") != actual_deb_sha
        or proof.get("serviceArtifactsIndexSha256") is None
    ):
        raise AcceptanceError("Selected DEB bytes changed after official release verification")
    manifest_path = root / "native-installer-release-v1.json"
    receipt_path = root / "native-installer-source-receipt-v1.json"
    receipt_asset = manifest.get("sourceReceipt")
    receipt_sha = sha256_bytes(receipt_path.read_bytes())
    if (
        not isinstance(receipt_asset, dict)
        or receipt_asset.get("assetName") != receipt_path.name
        or receipt_asset.get("sha256") != receipt_sha.removeprefix("sha256:")
        or receipt_asset.get("sizeBytes") != receipt_path.stat().st_size
    ):
        raise AcceptanceError("Verified source receipt bytes changed after attestation checks")
    return {
        "verifiedAtUtc": utc_now(),
        "repository": manifest["repository"],
        "releaseId": manifest["releaseId"],
        "version": manifest["version"],
        "channel": manifest["channel"],
        "workflow": manifest["workflow"],
        "run": manifest["run"],
        "source": source,
        "target": {
            "targetId": profile,
            "assetName": target["assetName"],
            "debPath": str(deb_path),
            "debSha256": "sha256:" + actual_deb_sha,
            "debSizeBytes": len(deb_data),
            "serviceArtifactsIndexPath": proof["serviceArtifactsIndexPath"],
            "serviceArtifactsIndexSha256": proof["serviceArtifactsIndexSha256"],
            "maintainerScriptsSha256": proof["maintainerScriptsSha256"],
            "services": proof["services"],
            "checks": proof["checks"],
        },
        "manifest": {
            "path": str(manifest_path),
            "sha256": sha256_bytes(manifest_path.read_bytes()),
        },
        "sourceReceipt": {
            "path": str(receipt_path),
            "sha256": sha256_bytes(receipt_path.read_bytes()),
        },
        "verifier": {
            "path": str(workspace_root / NATIVE_RELEASE_HELPER),
            "sha256": helper_digest,
            "attestationsVerified": True,
        },
    }


def record_verified_release(
    run_path: Path,
    release_directory: Path,
    *,
    expected_source_ref: str,
    expected_source_commit: str,
) -> dict[str, Any]:
    """Store only official verifier-derived release and target evidence.

    中文：仅写入正式验证器推导的发行与目标制品证据。
    """

    ledger = load_run(run_path)
    inventory = ledger.get("target", {}).get("inventory")
    os_data = inventory.get("os", {}) if isinstance(inventory, dict) else {}
    if (
        not isinstance(os_data, dict)
        or os_data.get("ID") != "ubuntu"
        or inventory.get("architecture") != "x86_64"
    ):
        raise AcceptanceError("Collect an Ubuntu x86_64 host inventory before release selection")
    ubuntu_version = os_data.get("VERSION_ID")
    if ubuntu_version not in TARGET_PROFILES:
        raise AcceptanceError("Host inventory does not identify a supported Ubuntu version")
    proof = verify_native_release_for_host(
        release_directory,
        expected_source_ref=expected_source_ref,
        expected_source_commit=expected_source_commit,
        ubuntu_version=ubuntu_version,
    )
    if ledger.get("releaseVerification") is not None:
        raise AcceptanceError(
            "Release proof already exists in this run; preserve it and use a new run"
        )
    ledger["releaseVerification"] = proof
    ledger["releaseVerificationStatus"] = "VERIFIED_OFFICIAL_ATTESTATIONS"
    ledger["taskAdmission"]["status"] = "UNKNOWN"
    ledger["taskAdmission"]["applyAdmission"] = "CLOSED"
    store_run(run_path, ledger)
    return proof


def _phase_record() -> dict[str, Any]:
    """Create an explicitly unrun cold/warm timing record.

    中文：创建冷/暖耗时均明确标记为尚未运行的阶段记录。
    """

    return {
        "cold": {"status": "NOT_RUN", "elapsedMs": None, "evidence": None, "reason": ""},
        "warm": {"status": "NOT_RUN", "elapsedMs": None, "evidence": None, "reason": ""},
    }


def create_run(output_dir: Path, ssh_alias: str, source_specs: list[str]) -> Path:
    """Create a mode-0600 run ledger without replacing an existing ledger.

    中文：创建权限为 0600 的验收台账，不覆盖已有台账。
    """

    alias = validate_ssh_alias(ssh_alias)
    sources: dict[str, Any] = {}
    for item in source_specs:
        name, separator, raw_path = item.partition("=")
        if not separator or not name or not raw_path or name in sources:
            raise AcceptanceError("Each source must be a unique NAME=PATH pair")
        sources[name] = source_identity(name, Path(raw_path))
    if not sources:
        raise AcceptanceError("At least one exact local source repository is required")

    directory = output_dir.expanduser()
    if directory.exists() or directory.is_symlink():
        if directory.is_symlink() or not directory.is_dir():
            raise AcceptanceError("Run output must be a real directory")
        if directory.stat().st_mode & 0o077:
            raise AcceptanceError("Existing run output directory must be private")
    else:
        directory.mkdir(parents=True, mode=0o700)
    run_path = directory / "run.json"
    if run_path.exists() or run_path.is_symlink():
        raise AcceptanceError(f"Run ledger already exists: {run_path}")

    run_id = dt.datetime.now(dt.UTC).strftime("native-v2-%Y%m%dT%H%M%SZ-") + os.urandom(3).hex()
    value = {
        "schemaVersion": RUN_SCHEMA_VERSION,
        "runId": run_id,
        "createdAtUtc": utc_now(),
        "target": {
            "sshAlias": alias,
            "preparedUserDirectory": "NOT_RUN",
            "inventoryStatus": "NOT_RUN",
            "inventory": None,
            "inventoryEvidenceSha256": None,
        },
        "sources": sources,
        "latestSourceHeads": None,
        "artifacts": {"status": "NOT_RUN", "items": []},
        "releaseVerificationStatus": "NOT_RUN",
        "releaseVerification": None,
        "kernelReadonlyProbe": {"build": None, "remoteQuery": None},
        "plan": {"status": "NOT_RUN", "items": []},
        "taskAdmission": {
            "status": "UNKNOWN",
            "applyAdmission": "CLOSED",
            "reason": "No authoritative Kernel-bound task, Worker, Lease, and resource query has run.",
        },
        "tasks": {"status": "UNKNOWN", "items": []},
        "resources": {
            "status": "NOT_RUN",
            "gpuIdleConclusion": "UNKNOWN",
            "items": [],
        },
        "phases": {phase: _phase_record() for phase in PHASES},
        "events": [],
        "limits": [
            "A source commit is not a signed artifact or deployed runtime.",
            "GPU telemetry does not establish task, Worker, Lease, or idle state.",
            "Fixtures and simulations cannot establish real-host or real-Product acceptance.",
        ],
    }
    _write_new_json(run_path, value)
    return run_path


def _write_new_json(path: Path, value: dict[str, Any]) -> None:
    """Write JSON once with private mode and exclusive creation.

    中文：以独占方式写入权限受限的 JSON，拒绝覆盖已有文件。
    """

    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def load_run(path: Path) -> dict[str, Any]:
    """Load a private run ledger and reject symlinks or unsupported schemas.

    中文：读取权限受限的台账，并拒绝符号链接或不支持的 schema。
    """

    if path.is_symlink() or not path.is_file():
        raise AcceptanceError("Run ledger must be a regular file, not a symlink")
    if path.stat().st_mode & 0o077:
        raise AcceptanceError("Run ledger must not be accessible by group or other users")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AcceptanceError("Run ledger JSON cannot be read") from error
    if not isinstance(value, dict) or value.get("schemaVersion") != RUN_SCHEMA_VERSION:
        raise AcceptanceError("Run ledger schema is unsupported")
    return value


def store_run(path: Path, value: dict[str, Any]) -> None:
    """Atomically replace a regular private run ledger.

    中文：原子替换普通文件形式的私有台账。
    """

    if path.is_symlink() or not path.is_file():
        raise AcceptanceError("Run ledger must be a regular file, not a symlink")
    if path.stat().st_mode & 0o077:
        raise AcceptanceError("Run ledger must not be accessible by group or other users")
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=".run-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def refresh_source_heads(run_path: Path, source_specs: list[str] | None = None) -> dict[str, Any]:
    """Capture current source heads while preserving historical build evidence.

    中文：记录当前源码 HEAD，同时保留历史构建与查询的精确来源。
    """

    ledger = load_run(run_path)
    source_paths = {name: str(source["path"]) for name, source in ledger["sources"].items()}
    previous = ledger.get("latestSourceHeads")
    if isinstance(previous, dict) and isinstance(previous.get("items"), dict):
        for name, source in previous["items"].items():
            if isinstance(source, dict) and isinstance(source.get("path"), str):
                source_paths.setdefault(name, source["path"])
    for item in source_specs or []:
        name, separator, raw_path = item.partition("=")
        if not separator or not name or not raw_path:
            raise AcceptanceError("Each additional source must be a NAME=PATH pair")
        resolved = str(Path(raw_path).expanduser().resolve(strict=True))
        existing = source_paths.get(name)
        if existing is not None and str(Path(existing).resolve(strict=True)) != resolved:
            raise AcceptanceError(f"Source {name} is already bound to a different path")
        source_paths[name] = resolved
    latest = {
        name: source_identity(name, Path(source_path)) for name, source_path in source_paths.items()
    }
    snapshot = {"capturedAtUtc": utc_now(), "items": latest}
    ledger["latestSourceHeads"] = snapshot
    store_run(run_path, ledger)
    return snapshot


def _evidence_record(path: Path, evidence_class: str) -> dict[str, str]:
    """Hash a nonempty evidence file without copying its potentially sensitive contents.

    中文：仅计算非空证据文件摘要，不复制其中可能敏感的内容。
    """

    if evidence_class not in EVIDENCE_CLASSES:
        raise AcceptanceError("Evidence class is unsupported")
    resolved = path.expanduser()
    if resolved.is_symlink() or not resolved.is_file():
        raise AcceptanceError("Evidence must be a regular file, not a symlink")
    data = resolved.read_bytes()
    if not data:
        raise AcceptanceError("Evidence file is empty")
    return {
        "fileName": resolved.name,
        "sha256": sha256_bytes(data),
        "class": evidence_class,
    }


def record_phase(
    run_path: Path,
    phase: str,
    mode: str,
    status: str,
    elapsed_ms: int | None,
    evidence_path: Path | None,
    evidence_class: str | None,
    reason: str,
) -> None:
    """Record one cold/warm phase without promoting simulated evidence to PASS.

    中文：记录一个冷/暖阶段；不允许模拟证据将阶段升级为 PASS。
    """

    if phase not in PHASES:
        raise AcceptanceError("Unknown acceptance phase")
    if mode not in {"cold", "warm"}:
        raise AcceptanceError("Timing mode must be cold or warm")
    if status not in {"NOT_RUN", "BLOCKED", "PASS", "FAIL"}:
        raise AcceptanceError("Phase status is unsupported")
    if elapsed_ms is not None and (isinstance(elapsed_ms, bool) or elapsed_ms < 0):
        raise AcceptanceError("Elapsed milliseconds must be a nonnegative integer")
    evidence: dict[str, str] | None = None
    if evidence_path is not None:
        if evidence_class is None:
            raise AcceptanceError("Evidence class is required with an evidence file")
        evidence = _evidence_record(evidence_path, evidence_class)
    if status in {"PASS", "FAIL"} and (elapsed_ms is None or evidence is None):
        raise AcceptanceError("PASS/FAIL requires a measured duration and evidence file")
    if status == "PASS" and evidence and evidence["class"] not in REAL_EVIDENCE_CLASSES:
        raise AcceptanceError("Fixture or non-host evidence cannot mark a phase PASS")
    if status == "BLOCKED" and not reason.strip():
        raise AcceptanceError("BLOCKED requires a concrete reason")

    value = load_run(run_path)
    if status == "PASS":
        _validate_phase_prerequisites(value, phase)
    slot = value["phases"][phase][mode]
    if slot["status"] != "NOT_RUN" or slot["evidence"] is not None:
        raise AcceptanceError("This phase/mode already has a record; preserve it and use a new run")
    slot.update(
        {
            "status": status,
            "elapsedMs": elapsed_ms,
            "evidence": evidence,
            "reason": reason.strip(),
            "recordedAtUtc": utc_now(),
        }
    )
    store_run(run_path, value)


def _validate_phase_prerequisites(ledger: dict[str, Any], phase: str) -> None:
    """Require digest-linked observations before recording critical PASS states.

    中文：关键阶段记录 PASS 前，必须已有摘要关联的对应观察证据。
    """

    events = ledger["events"]
    if phase == "component_digest_plan_confirmation":
        if not any(event.get("kind") == "plan" for event in events):
            raise AcceptanceError("Plan confirmation PASS requires a digest-bound plan observation")
    elif phase == "kernel_gpu_preflight":
        if not any(
            event.get("kind") == "resource"
            and event.get("evidence", {}).get("class") in REAL_EVIDENCE_CLASSES
            and DIGEST_PATTERN.fullmatch(
                str(event.get("document", {}).get("kernelBindingProofSha256", ""))
            )
            for event in events
        ):
            raise AcceptanceError("Kernel GPU PASS requires real Kernel binding proof")
    elif phase == "yield_llama_factory_one_step":
        resource_proofs = {
            event.get("document", {}).get("kernelBindingProofSha256")
            for event in events
            if event.get("kind") == "resource"
            and event.get("evidence", {}).get("class") in REAL_EVIDENCE_CLASSES
        }
        if not any(
            event.get("kind") == "task"
            and event.get("evidence", {}).get("class") in REAL_EVIDENCE_CLASSES
            and event.get("document", {}).get("engine") == "training.llama-factory.v1"
            and event.get("document", {}).get("status") == "succeeded"
            and event.get("document", {}).get("completedSteps") == 1
            and event.get("document", {}).get("workerId")
            and event.get("document", {}).get("leaseId")
            and event.get("document", {}).get("kernelBindingProofSha256") in resource_proofs
            for event in events
        ):
            raise AcceptanceError(
                "Yield PASS requires a real one-step task bound to the recorded Kernel GPU proof"
            )
    elif phase in {"active_task_stage_allowed", "active_task_apply_refused"}:
        if not any(
            event.get("kind") == "task"
            and event.get("evidence", {}).get("class") in REAL_EVIDENCE_CLASSES
            and event.get("document", {}).get("state") in {"ACTIVE", "RUNNING"}
            and event.get("document", {}).get("workerId")
            and event.get("document", {}).get("leaseId")
            for event in events
        ):
            raise AcceptanceError(
                "Active-task update PASS requires real active task, Worker, and Lease evidence"
            )
    elif phase == "owner_worker_lease_resource_release" and not any(
        event.get("kind") == "task"
        and event.get("evidence", {}).get("class") in REAL_EVIDENCE_CLASSES
        and event.get("document", {}).get("state") == "RELEASED"
        and event.get("document", {}).get("ownerRelease") is True
        and event.get("document", {}).get("workerId")
        and event.get("document", {}).get("leaseId")
        for event in events
    ):
        raise AcceptanceError(
            "Release PASS requires an explicit owner release and closed Worker/Lease"
        )


def _contains_sensitive_key(value: Any) -> bool:
    """Reject credential-bearing JSON before saving it to the evidence ledger.

    中文：写入证据台账前拒绝包含凭据字段的 JSON。
    """

    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).lower().replace("_", "").replace("-", "")
            if any(
                marker in normalized
                for marker in (
                    "token",
                    "password",
                    "secret",
                    "authorization",
                    "cookie",
                    "privatekey",
                )
            ):
                return True
            if _contains_sensitive_key(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    return False


def record_observation(
    run_path: Path,
    kind: str,
    document_path: Path,
    evidence_path: Path,
    evidence_class: str,
) -> None:
    """Append a digest-bound source/artifact/plan/task/resource observation.

    中文：追加绑定文件摘要的源码、制品、计划、任务或资源观察记录。
    """

    if kind not in OBSERVATION_KINDS:
        raise AcceptanceError("Observation kind is unsupported")
    if document_path.is_symlink() or not document_path.is_file():
        raise AcceptanceError("Observation document must be a regular file")
    try:
        document = json.loads(document_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AcceptanceError("Observation document must contain JSON") from error
    if not isinstance(document, dict) or _contains_sensitive_key(document):
        raise AcceptanceError("Observation must be an object without credential-like fields")
    if kind == "artifact":
        digest = document.get("artifactDigest")
        if (
            not isinstance(digest, str)
            or DIGEST_PATTERN.fullmatch(digest) is None
            or not isinstance(document.get("componentId"), str)
            or COMMIT_PATTERN.fullmatch(str(document.get("sourceCommit", ""))) is None
        ):
            raise AcceptanceError(
                "Artifact observation requires componentId, sourceCommit, and exact artifactDigest"
            )
    elif kind == "plan":
        digest = document.get("planDigest")
        components = document.get("components")
        if (
            not isinstance(digest, str)
            or DIGEST_PATTERN.fullmatch(digest) is None
            or not isinstance(document.get("planId"), str)
            or not isinstance(components, list)
            or not components
        ):
            raise AcceptanceError("Plan observation requires planId, planDigest, and components")
        component_ids: set[str] = set()
        for component in components:
            if not isinstance(component, dict):
                raise AcceptanceError("Plan component identity must be an object")
            component_id = component.get("componentId")
            component_digest = component.get("artifactDigest")
            if (
                not isinstance(component_id, str)
                or component_id in component_ids
                or not isinstance(component_digest, str)
                or DIGEST_PATTERN.fullmatch(component_digest) is None
            ):
                raise AcceptanceError(
                    "Plan component must have unique id and exact artifact digest"
                )
            component_ids.add(component_id)
    elif kind == "task" and not isinstance(document.get("taskId"), str):
        raise AcceptanceError("Task observation requires the canonical taskId")

    ledger = load_run(run_path)
    if kind == "plan":
        recorded_artifacts = {
            event.get("document", {}).get("componentId"): event.get("document", {}).get(
                "artifactDigest"
            )
            for event in ledger["events"]
            if event.get("kind") == "artifact"
        }
        for component in document["components"]:
            if recorded_artifacts.get(component["componentId"]) != component["artifactDigest"]:
                raise AcceptanceError(
                    "Plan component and artifact digest do not match a recorded artifact observation"
                )
    event = {
        "kind": kind,
        "recordedAtUtc": utc_now(),
        "documentFileName": document_path.name,
        "documentSha256": sha256_bytes(document_path.read_bytes()),
        "document": document,
        "evidence": _evidence_record(evidence_path, evidence_class),
    }
    ledger["events"].append(event)
    if kind == "artifact":
        ledger["artifacts"]["status"] = "RECORDED"
        ledger["artifacts"]["items"].append(event)
    elif kind == "plan":
        ledger["plan"]["status"] = "RECORDED"
        ledger["plan"]["items"].append(event)
    elif kind == "task":
        ledger["tasks"]["items"].append(event)
        if (
            document.get("workerId")
            and document.get("leaseId")
            and evidence_class in REAL_EVIDENCE_CLASSES
        ):
            ledger["tasks"]["status"] = "OBSERVED"
        else:
            ledger["tasks"]["status"] = "UNKNOWN"
    elif kind == "resource":
        ledger["resources"]["items"].append(event)
        if (
            DIGEST_PATTERN.fullmatch(str(document.get("kernelBindingProofSha256", "")))
            and evidence_class in REAL_EVIDENCE_CLASSES
        ):
            ledger["resources"]["status"] = "OBSERVED"
        else:
            ledger["resources"]["status"] = "TELEMETRY_ONLY"
            ledger["resources"]["gpuIdleConclusion"] = "UNKNOWN"
    store_run(run_path, ledger)


def _ssh_command(alias: str, remote_command: str) -> list[str]:
    """Build a strict, noninteractive SSH invocation for a fixed remote script.

    中文：为固定远端脚本构造严格 host-key 和 noninteractive SSH 调用。
    """

    validate_ssh_alias(alias)
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=7",
        alias,
        remote_command,
    ]


def prepare_target(alias: str) -> str:
    """Create only the dedicated mode-0700 user-space acceptance directory.

    中文：仅创建专属权限 0700 的用户空间验收目录。
    """

    completed = subprocess.run(
        _ssh_command(alias, "sh -s"),
        input=REMOTE_PREPARE_SCRIPT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        raise AcceptanceError(
            "Remote user-space preparation failed closed: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    return completed.stdout.strip()


def collect_target(alias: str) -> dict[str, Any]:
    """Collect read-only host facts; do not query admission or infer idleness.

    中文：只读采集主机事实；不查询准入，也不推断 GPU 空闲。
    """

    completed = subprocess.run(
        _ssh_command(alias, "python3 -c " + shlex.quote(REMOTE_PROBE_SCRIPT)),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise AcceptanceError(
            "Remote read-only inventory failed: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AcceptanceError("Remote inventory returned malformed JSON") from error
    if not isinstance(value, dict):
        raise AcceptanceError("Remote inventory must be a JSON object")
    return value


def record_kernel_probe_build(
    run_path: Path,
    platform_source: Path,
    target_directory: Path,
    binary_output: Path,
) -> dict[str, Any]:
    """Compile and record the exact read-only Kernel UDS client.

    中文：编译并记录精确的 Kernel UDS 只读客户端。
    """

    build = compile_kernel_readonly_probe(platform_source, target_directory, binary_output)
    if not build["sourceStableDuringBuild"]:
        raise AcceptanceError("Platform source changed while compiling the read-only probe")
    ledger = load_run(run_path)
    previous = ledger.get("kernelReadonlyProbe", {}).get("build")
    if previous is not None:
        raise AcceptanceError("Kernel read-only probe build is already recorded; use a new run")
    ledger.setdefault("kernelReadonlyProbe", {})["build"] = build
    store_run(run_path, ledger)
    return build


def query_kernel_readiness(
    run_path: Path,
    *,
    alias: str,
    socket_path: str,
) -> dict[str, Any]:
    """Copy and run only the compiled read-only Kernel RPC client on the host.

    中文：仅把已编译只读客户端复制到主机，并调用限定的 Kernel 只读 RPC。
    """

    validate_ssh_alias(alias)
    if not socket_path.startswith("/") or "\0" in socket_path or len(socket_path) > 512:
        raise AcceptanceError("Kernel socket path must be an absolute bounded path")
    ledger = load_run(run_path)
    if ledger["target"]["sshAlias"] != alias:
        raise AcceptanceError("Kernel query SSH alias differs from the run's target")
    if ledger["target"].get("preparedUserDirectory") not in {"CREATED", "EXISTING_UNMODIFIED"}:
        raise AcceptanceError("Prepare the isolated target directory before copying a probe")
    build = ledger.get("kernelReadonlyProbe", {}).get("build")
    if not isinstance(build, dict) or build.get("sourceStableDuringBuild") is not True:
        raise AcceptanceError("Compile a stable read-only Kernel probe before querying the host")
    if ledger.get("kernelReadonlyProbe", {}).get("remoteQuery") is not None:
        raise AcceptanceError("Kernel query is already recorded; preserve it and use a new run")
    binary_path = Path(str(build.get("binaryPath", "")))
    if binary_path.is_symlink() or not binary_path.is_file():
        raise AcceptanceError("Recorded Kernel probe binary is missing or unsafe")
    local_sha = sha256_bytes(binary_path.read_bytes())
    if local_sha != build.get("binarySha256"):
        raise AcceptanceError("Kernel probe binary changed since its local build record")
    remote_name = f"{KERNEL_READONLY_TOOL_NAME}-{local_sha.removeprefix('sha256:')[:16]}"

    copy_guard = subprocess.run(
        _ssh_command(
            alias,
            "sh -s -- "
            + shlex.quote(remote_name)
            + " "
            + shlex.quote(local_sha.removeprefix("sha256:")),
        ),
        input=REMOTE_KERNEL_COPY_GUARD_SCRIPT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if copy_guard.returncode != 0:
        raise AcceptanceError(
            "Remote Kernel probe path is not safe: "
            + (copy_guard.stderr.strip() or copy_guard.stdout.strip())
        )
    copy_state = copy_guard.stdout.strip()
    if copy_state not in {"READY", "EXISTING_VERIFIED"}:
        raise AcceptanceError("Remote Kernel probe copy guard returned an unknown state")
    if copy_state == "READY":
        scp = subprocess.run(
            [
                "scp",
                "-p",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=7",
                str(binary_path),
                f"{alias}:~/{KERNEL_READONLY_REMOTE_DIRECTORY}/{remote_name}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    else:
        scp = None
    if scp is not None and scp.returncode != 0:
        raise AcceptanceError(
            "Copying the read-only Kernel probe failed: "
            + (scp.stderr.strip() or scp.stdout.strip())
        )
    command = (
        "sh -s -- "
        + shlex.quote(remote_name)
        + " "
        + shlex.quote(local_sha.removeprefix("sha256:"))
        + " "
        + shlex.quote(socket_path)
    )
    remote = subprocess.run(
        _ssh_command(alias, command),
        input=REMOTE_KERNEL_QUERY_SCRIPT,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    if remote.returncode != 0:
        raise AcceptanceError(
            "Read-only Kernel UDS query failed closed: "
            + (remote.stderr.strip() or remote.stdout.strip())
        )
    try:
        result = json.loads(remote.stdout)
    except json.JSONDecodeError as error:
        raise AcceptanceError("Kernel read-only probe returned malformed JSON") from error
    _validate_kernel_probe_result(result)
    value = {
        "capturedAtUtc": utc_now(),
        "sshAlias": alias,
        "socketPath": socket_path,
        "remoteToolPath": "~/" + KERNEL_READONLY_REMOTE_DIRECTORY + "/" + remote_name,
        "remoteCopyState": copy_state,
        "toolSourceSha256": build["sourceSha256"],
        "toolArtifactSha256": local_sha,
        "rpcAllowlist": build["rpcAllowlist"],
        "result": result,
        "taskLeaseWorkerState": "UNKNOWN",
        "gpuIdleConclusion": "UNKNOWN",
        "applyAdmissionDecision": "CLOSED",
    }
    ledger["kernelReadonlyProbe"]["remoteQuery"] = value
    ledger["taskAdmission"]["status"] = "UNKNOWN"
    ledger["taskAdmission"]["applyAdmission"] = "CLOSED"
    ledger["taskAdmission"]["reason"] = (
        "Kernel read-only readiness counters were captured; no admission decision was opened."
    )
    ledger["resources"]["gpuIdleConclusion"] = "UNKNOWN"
    store_run(run_path, ledger)
    return value


def _validate_kernel_probe_result(result: Any) -> None:
    """Reject any probe result that opens admission or infers GPU idleness.

    中文：拒绝任何打开准入或推断 GPU 空闲的探测结果。
    """

    if (
        not isinstance(result, dict)
        or result.get("schemaVersion") != 1
        or result.get("taskLeaseWorkerState") != "UNKNOWN"
        or result.get("gpuIdleConclusion") != "UNKNOWN"
        or result.get("applyAdmissionDecision") != "CLOSED"
        or not isinstance(result.get("capabilities"), dict)
        or not isinstance(result.get("readiness"), dict)
    ):
        raise AcceptanceError("Kernel probe result violates the read-only fail-closed contract")
    readiness = result["readiness"]
    if readiness.get("status") == "RESPONDED":
        for name in (
            "activeTaskCount",
            "activeWorkerCount",
            "activeLeaseOrAllocationCount",
            "inflightRuntimeAdmissionCount",
            "unknownActivitySourceCount",
        ):
            count = readiness.get(name)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise AcceptanceError("Kernel readiness returned malformed aggregate counters")
        if not isinstance(readiness.get("blockerCodes"), list):
            raise AcceptanceError("Kernel readiness omitted its blocker code list")
    elif readiness.get("status") != "UNKNOWN":
        raise AcceptanceError("Kernel readiness status is not recognized")


def attach_remote_result(
    run_path: Path, result: dict[str, Any], *, prepared: str | None = None
) -> None:
    """Attach a read-only inventory/preparation result and keep admission closed.

    中文：附加只读清单/目录准备结果，并保持准入关闭。
    """

    ledger = load_run(run_path)
    if prepared is not None:
        if not prepared.startswith(("CREATED:", "EXISTING_UNMODIFIED:")):
            raise AcceptanceError("Remote preparation result is unexpected")
        ledger["target"]["preparedUserDirectory"] = prepared.split(":", 1)[0]
        ledger["target"]["preparedPath"] = prepared.split(":", 1)[1]
    else:
        if result.get("taskLeaseWorkerState") != "UNKNOWN":
            raise AcceptanceError("Read-only inventory cannot set task/Lease/Worker state")
        if result.get("applyAdmission") != "CLOSED":
            raise AcceptanceError("Read-only inventory must preserve closed apply admission")
        encoded = (json.dumps(result, sort_keys=True, ensure_ascii=False) + "\n").encode()
        ledger["target"]["inventoryStatus"] = "OBSERVED_READ_ONLY"
        ledger["target"]["inventory"] = result
        ledger["target"]["inventoryEvidenceSha256"] = sha256_bytes(encoded)
        ledger["taskAdmission"]["status"] = "UNKNOWN"
        ledger["taskAdmission"]["applyAdmission"] = "CLOSED"
        ledger["resources"]["gpuIdleConclusion"] = "UNKNOWN"
    store_run(run_path, ledger)


def render_report(run_path: Path) -> str:
    """Render a concise Markdown evidence receipt with explicit open states.

    中文：生成明确列出未运行/阻塞状态的 Markdown 证据回执。
    """

    ledger = load_run(run_path)
    lines = [
        f"# Native components V2 acceptance: {ledger['runId']}",
        "",
        f"- Created: `{ledger['createdAtUtc']}`",
        f"- SSH alias: `{ledger['target']['sshAlias']}`",
        f"- Admission: `{ledger['taskAdmission']['status']}`; apply `{ledger['taskAdmission']['applyAdmission']}`",
        f"- Task/Worker/Lease: `{ledger['tasks']['status']}`",
        f"- GPU idle conclusion: `{ledger['resources']['gpuIdleConclusion']}`",
        f"- Isolated user directory: `{ledger['target']['preparedUserDirectory']}` (`{ledger['target'].get('preparedPath', 'not prepared')}`).",
        "",
        "## Exact source heads",
        "",
        "| Source | Branch | Commit | Tree | Dirty paths |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for name, source in sorted(ledger["sources"].items()):
        lines.append(
            f"| `{name}` | `{source['branch']}` | `{source['commit']}` | "
            f"`{source['workingTree']}` | {len(source['dirtyPaths'])} |"
        )
    latest_heads = ledger.get("latestSourceHeads")
    if isinstance(latest_heads, dict):
        lines.extend(
            [
                "",
                f"## Latest local integration heads (captured `{latest_heads.get('capturedAtUtc', 'unknown')}`)",
                "",
                "| Source | Branch | Commit | Tree | Dirty paths |",
                "| --- | --- | --- | --- | ---: |",
            ]
        )
        for name, source in sorted(latest_heads.get("items", {}).items()):
            lines.append(
                f"| `{name}` | `{source['branch']}` | `{source['commit']}` | "
                f"`{source['workingTree']}` | {len(source['dirtyPaths'])} |"
            )
    lines.extend(
        [
            "",
            "## Release and workload observations",
            "",
            f"- Official release verification: {chr(96)}{ledger.get('releaseVerificationStatus', 'NOT_RUN')}{chr(96)}.",
            f"- Kernel read-only query: {chr(96)}{'OBSERVED' if ledger.get('kernelReadonlyProbe', {}).get('remoteQuery') else 'NOT_RUN'}{chr(96)}; task/Worker/Lease remains {chr(96)}{ledger['tasks']['status']}{chr(96)} and GPU idle remains {chr(96)}{ledger['resources']['gpuIdleConclusion']}{chr(96)}.",
            f"- Artifacts: `{ledger['artifacts']['status']}`; records {len(ledger['artifacts']['items'])}.",
            f"- Plans: `{ledger['plan']['status']}`; records {len(ledger['plan']['items'])}.",
            f"- Tasks: `{ledger['tasks']['status']}`; records {len(ledger['tasks']['items'])}.",
            f"- Resources: `{ledger['resources']['status']}`; records {len(ledger['resources']['items'])}.",
            f"- Host inventory: `{ledger['target']['inventoryStatus']}`.",
            "",
            "## Cold/warm phase timings",
            "",
            "| Phase | Cold | Warm | Blocker reason |",
            "| --- | --- | --- | --- |",
        ]
    )
    for phase, modes in ledger["phases"].items():
        cells = []
        for mode in ("cold", "warm"):
            value = modes[mode]
            elapsed = "n/a" if value["elapsedMs"] is None else f"{value['elapsedMs']} ms"
            cells.append(f"`{value['status']}` / {elapsed}")
        reasons = list(
            dict.fromkeys(
                value["reason"]
                for value in modes.values()
                if value["status"] == "BLOCKED" and value["reason"]
            )
        )
        blocker = "; ".join(reasons) or "n/a"
        lines.append(f"| `{phase}` | {cells[0]} | {cells[1]} | {blocker} |")
    release = ledger.get("releaseVerification")
    if isinstance(release, dict):
        target = release.get("target", {})
        source = release.get("source", {})
        lines.extend(
            [
                "",
                "## Verified release identity",
                "",
                f"- Release: `{release.get('repository', 'unknown')}` / `{release.get('releaseId', 'unknown')}`; version `{release.get('version', 'unknown')}`; channel `{release.get('channel', 'unknown')}`.",
                f"- Source: `{source.get('ref', 'unknown')}` at `{source.get('commit', 'unknown')}`; workflow `{release.get('workflow', 'unknown')}`; attestations verified `{release.get('verifier', {}).get('attestationsVerified', False)}`.",
                f"- Selected target: `{target.get('targetId', 'unknown')}`; DEB `{target.get('assetName', 'unknown')}`; SHA-256 `{target.get('debSha256', 'unknown')}`.",
                f"- Release manifest SHA-256 `{release.get('manifest', {}).get('sha256', 'unknown')}`; source receipt SHA-256 `{release.get('sourceReceipt', {}).get('sha256', 'unknown')}`; verifier SHA-256 `{release.get('verifier', {}).get('sha256', 'unknown')}`.",
                f"- Service index: `{target.get('serviceArtifactsIndexPath', 'unknown')}`; SHA-256 `{target.get('serviceArtifactsIndexSha256', 'unknown')}`.",
                f"- Stage-only checks: `{json.dumps(target.get('checks', {}), sort_keys=True)}`.",
                "",
                "| Product component | Artifact SHA-256 | Source commit | Manifest digest | Attestation SHA-256 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for component_id, service in sorted(target.get("services", {}).items()):
            if not isinstance(service, dict):
                continue
            artifact = service.get("artifact", {})
            manifest = service.get("manifest", {})
            attestation = service.get("attestation", {})
            source_tuple = service.get("source", {})
            lines.append(
                f"| `{component_id}` | `{artifact.get('sha256', 'unknown')}` | `{source_tuple.get('commit', 'unknown')}` | `{manifest.get('manifestDigest', 'unknown')}` | `{attestation.get('sha256', 'unknown')}` |"
            )
        lines.append("")

    observation_fields = {
        "artifacts": ("componentId", "sourceCommit", "artifactDigest"),
        "plan": ("planId", "planDigest"),
        "tasks": ("taskId", "engine", "status", "state", "completedSteps", "workerId", "leaseId"),
        "resources": (
            "resourceId",
            "status",
            "kernelBindingProofSha256",
            "gpuIdleConclusion",
        ),
    }
    for collection, keys in observation_fields.items():
        label = collection.title()[:-1] if collection.endswith("s") else collection.title()
        events = ledger[collection]["items"]
        lines.extend(["", f"## {label} observations", ""])
        if not events:
            lines.extend(["- `NOT_RUN`; no records.", ""])
            continue
        for event in events:
            document = event.get("document", {})
            values = (
                "; ".join(
                    f"{key}=`{document[key]}`"
                    for key in keys
                    if key in document and isinstance(document[key], (str, int, bool))
                )
                or "no summary fields"
            )
            evidence = event.get("evidence", {})
            lines.append(
                f"- `{event.get('recordedAtUtc', 'unknown')}`; document SHA-256 `{event.get('documentSha256', 'unknown')}`; evidence SHA-256 `{evidence.get('sha256', 'unknown')}` ({evidence.get('class', 'unknown')}); {values}."
            )
        lines.append("")
    kernel_query = ledger.get("kernelReadonlyProbe", {}).get("remoteQuery")
    if isinstance(kernel_query, dict):
        query_result = kernel_query.get("result", {})
        readiness = query_result.get("readiness", {})
        capabilities = query_result.get("capabilities", {})
        probe_build = ledger.get("kernelReadonlyProbe", {}).get("build", {})
        probe_source = probe_build.get("source", {})
        quote = chr(96)
        lines.extend(
            [
                "",
                "## Read-only Kernel authority query",
                "",
                f"- Captured: {quote}{kernel_query.get('capturedAtUtc', 'unknown')}{quote}.",
                f"- Capabilities RPC: {quote}{capabilities.get('status', 'UNKNOWN')}{quote}; readiness RPC: {quote}{readiness.get('status', 'UNKNOWN')}{quote} ({quote}{readiness.get('rpcCode', 'no code')}{quote}).",
                f"- Candidate tool SHA-256: {quote}{kernel_query.get('toolArtifactSha256', 'unknown')}{quote}; source SHA-256: {quote}{kernel_query.get('toolSourceSha256', 'unknown')}{quote}.",
                f"- Platform source: HEAD {quote}{probe_source.get('commit', 'unknown')}{quote}; dirty-tree fingerprint {quote}{probe_source.get('workingTreeDigest', 'unknown')}{quote}; generated Cargo.lock {quote}{probe_build.get('cargoLockSha256', 'unknown')}{quote}.",
                "- The probe called only GetKernelCapabilities and GetUpdateReadiness; task/Worker/Lease remains UNKNOWN, GPU idle remains UNKNOWN, and apply remains CLOSED.",
            ]
        )
    lines.extend(["", "## Evidence boundaries", ""])
    lines.extend(f"- {item}" for item in ledger["limits"])
    if ledger["target"]["inventory"] is not None:
        inventory = ledger["target"]["inventory"]
        gpu = inventory.get("gpuTelemetry", {})
        gpu_detail = "unavailable"
        gpu_driver = "unavailable"
        gpu_fields = [part.strip() for part in gpu.get("stdout", "").split(",")]
        if len(gpu_fields) >= 5:
            gpu_detail = (
                f"{gpu_fields[0]}; {gpu_fields[2]} total / {gpu_fields[3]} used; "
                f"{gpu_fields[4]} utilization"
            )
            gpu_driver = gpu_fields[1]
        runtime_processes = inventory.get("existingManualRuntimeProcesses", {})
        process_summary = (
            ", ".join(
                f"PID {pid} `{value.get('executable', '').rsplit('/', 1)[-1]}`"
                for pid, value in sorted(runtime_processes.items())
                if value.get("present")
            )
            or "none observed"
        )
        unit_values = inventory.get("managedSystemUnits", {}).values()
        loaded_units = sum(
            "LoadState=loaded" in value.get("stdout", "")
            for value in unit_values
            if isinstance(value, dict)
        )
        unit_summary = f"{loaded_units} runner-listed Cyrene units loaded"
        lines.extend(
            [
                "",
                "## Read-only host inventory",
                "",
                f"- Captured: `{inventory.get('capturedAtUtc', 'unknown')}`.",
                f"- OS: `{inventory.get('os', {}).get('PRETTY_NAME', 'unknown')}`; kernel `{inventory.get('kernel', 'unknown')}`; architecture `{inventory.get('architecture', 'unknown')}`.",
                f"- Noninteractive sudo: `{inventory.get('noninteractiveSudoAvailable', 'unknown')}`.",
                f"- Broker unit: `{inventory.get('brokerUnitState', {}).get('stdout', 'unknown')}`; Workspace CLI: `{inventory.get('formalUpdaterPresent', 'unknown')}`. Broker binaries resolve through the verified active component release, not a global executable path.",
                f"- Activity catalog: `{inventory.get('activityCatalogPresent', 'unknown')}`.",
                f"- {unit_summary}; existing hand-started processes: {process_summary}.",
                f"- GPU observation (snapshot only): `{gpu_detail}`; idle remains `UNKNOWN`.",
                f"- NVIDIA driver version (read-only `nvidia-smi`): `{gpu_driver}`.",
                f"- Docker socket present/accessible: `{inventory.get('dockerSocketPresent', 'unknown')}` / `{inventory.get('dockerSocketAccessible', 'unknown')}`.",
                f"- Inventory SHA-256: `{ledger['target']['inventoryEvidenceSha256']}`.",
            ]
        )
        package = inventory.get("packageMetadata", {})
        if isinstance(package, dict):
            package_observation = package.get("stdout") or package.get("stderr") or "not observed"
            lines.append(f"- Package inventory observation: `{package_observation}`.")
    kernel_probe = ledger.get("kernelReadonlyProbe")
    remote_query = kernel_probe.get("remoteQuery") if isinstance(kernel_probe, dict) else None
    query_result = remote_query.get("result") if isinstance(remote_query, dict) else None
    readiness = query_result.get("readiness", {}) if isinstance(query_result, dict) else {}
    readiness_blocker = None
    if isinstance(readiness, dict) and readiness.get("status") != "READY":
        readiness_blocker = readiness.get("rpcCode") or readiness.get("status", "UNKNOWN")
    lines.extend(
        [
            "",
            "## Administrator handoff",
            "",
            "- Admin initialization command: `NOT_GENERATED`; no privileged install, catalog initialization, unit activation, or old-runtime switch was performed.",
            "- Minimum eventual steps: verify the immutable signed release and exact source tuple with the canonical detached-attestation verifier; inventory and read back a root-owned backup of existing state; verify the offline broker bootstrap and source-owner configuration; require authoritative task/Worker/Lease/resource readiness before activation.",
            "- The administrator must execute only a reviewed command rendered from the verified DEB SHA-256, signed source receipt, actual installed component IDs, protected owner identities, real endpoint and credential-file paths, and backup receipt. Do not hand-fill signature tuples or expose credential contents.",
            "- Keep stage-only package installation separate from initialization. Preserve existing catalog/tokens/state and active pointers on upgrade; an unavailable fresh broker must reject new task admission. Leave hand-started runtime processes untouched until authority and migration are verified.",
            "- The first workload acceptance requires a real Kernel-bound NVIDIA GPU preflight followed by exactly one Yield + LLaMA-Factory training step. A missing, unimplemented, or UNKNOWN readiness result keeps admission closed.",
            "- The control host is Ubuntu 24 and this target is Ubuntu 22; route the control client through the existing Cyrene Relay/Tailscale path. Do not add Azure or a new SSO path; record protocol and route acceptance only after observing both endpoints.",
        ]
    )
    if readiness_blocker is not None:
        lines.insert(
            len(lines) - 1,
            f"- Current Kernel readiness blocker: `{readiness_blocker}`; task/Worker/Lease and GPU idleness remain `UNKNOWN`.",
        )
    if ledger.get("releaseVerificationStatus", "NOT_RUN") != "VERIFIED_OFFICIAL_ATTESTATIONS":
        lines.insert(
            len(lines) - (7 if readiness_blocker is not None else 6),
            "- Current release blocker: immutable signed native-installer assets and their source/attestation tuple have not been verified.",
        )
    lines.extend(
        [
            "",
            "Evidence classes are labels supplied by the operator. A recorded phase is not automatically accepted; source review, signature verification, Kernel task/Lease/Worker ownership, and Product evidence remain separate gates.",
            "",
        ]
    )
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    """Build the command-line interface for real-host evidence preparation.

    中文：构造真机证据准备命令行接口。
    """

    result = argparse.ArgumentParser(
        description="Prepare and record native V2 real-host acceptance"
    )
    commands = result.add_subparsers(dest="command", required=True)
    create = commands.add_parser("new-run", help="Create a private acceptance ledger")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--ssh-alias", default=DEFAULT_SSH_ALIAS)
    create.add_argument("--source", action="append", default=[])

    refresh_sources = commands.add_parser(
        "record-latest-source-heads",
        help="Capture current source heads without rewriting historical evidence",
    )
    refresh_sources.add_argument("--run", type=Path, required=True)
    refresh_sources.add_argument(
        "--source",
        action="append",
        default=[],
        help="Include an additional NAME=PATH checkout in the latest-only snapshot",
    )

    prepare = commands.add_parser("prepare-target", help="Create only the isolated user directory")
    prepare.add_argument("--run", type=Path, required=True)
    prepare.add_argument("--ssh-alias", default=DEFAULT_SSH_ALIAS)

    collect = commands.add_parser("collect-target", help="Collect read-only host inventory")
    collect.add_argument("--run", type=Path, required=True)
    collect.add_argument("--ssh-alias", default=DEFAULT_SSH_ALIAS)

    release = commands.add_parser(
        "verify-release", help="Verify signed release assets with the canonical verifier"
    )
    release.add_argument("--run", type=Path, required=True)
    release.add_argument("--directory", type=Path, required=True)
    release.add_argument("--expected-source-ref", required=True)
    release.add_argument("--expected-source-commit", required=True)

    compile_probe = commands.add_parser(
        "compile-kernel-probe", help="Build an isolated read-only Kernel UDS client"
    )
    compile_probe.add_argument("--run", type=Path, required=True)
    compile_probe.add_argument("--platform-source", type=Path, required=True)
    compile_probe.add_argument("--target-directory", type=Path, required=True)
    compile_probe.add_argument("--output", type=Path, required=True)

    query_kernel = commands.add_parser(
        "query-kernel-readiness", help="Copy and run the read-only Kernel UDS client"
    )
    query_kernel.add_argument("--run", type=Path, required=True)
    query_kernel.add_argument("--ssh-alias", default=DEFAULT_SSH_ALIAS)
    query_kernel.add_argument("--socket", required=True)

    record_phase_parser = commands.add_parser("record-phase", help="Record one cold/warm phase")
    record_phase_parser.add_argument("--run", type=Path, required=True)
    record_phase_parser.add_argument("--phase", choices=PHASES, required=True)
    record_phase_parser.add_argument("--mode", choices=("cold", "warm"), required=True)
    record_phase_parser.add_argument(
        "--status", choices=("NOT_RUN", "BLOCKED", "PASS", "FAIL"), required=True
    )
    record_phase_parser.add_argument("--elapsed-ms", type=int)
    record_phase_parser.add_argument("--evidence", type=Path)
    record_phase_parser.add_argument("--evidence-class", choices=sorted(EVIDENCE_CLASSES))
    record_phase_parser.add_argument("--reason", default="")

    record_observation_parser = commands.add_parser(
        "record-observation", help="Record artifact, plan, task, or resource JSON evidence"
    )
    record_observation_parser.add_argument("--run", type=Path, required=True)
    record_observation_parser.add_argument(
        "--kind", choices=sorted(OBSERVATION_KINDS), required=True
    )
    record_observation_parser.add_argument("--document", type=Path, required=True)
    record_observation_parser.add_argument("--evidence", type=Path, required=True)
    record_observation_parser.add_argument(
        "--evidence-class", choices=sorted(EVIDENCE_CLASSES), required=True
    )

    report = commands.add_parser("report", help="Render a Markdown receipt")
    report.add_argument("--run", type=Path, required=True)
    report.add_argument("--output", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    """Execute only preparation, read-only collection, or evidence-ledger operations.

    中文：只执行准备、只读采集或证据台账操作。
    """

    args = parser().parse_args(argv)
    try:
        if args.command == "new-run":
            print(create_run(args.output, args.ssh_alias, args.source))
        elif args.command == "record-latest-source-heads":
            snapshot = refresh_source_heads(args.run, args.source)
            print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
        elif args.command == "prepare-target":
            result = prepare_target(args.ssh_alias)
            attach_remote_result(args.run, {}, prepared=result)
            print(result)
        elif args.command == "collect-target":
            result = collect_target(args.ssh_alias)
            attach_remote_result(args.run, result)
            print(
                "Read-only inventory recorded; GPU idle and task/Lease/Worker state remain UNKNOWN."
            )
        elif args.command == "verify-release":
            proof = record_verified_release(
                args.run,
                args.directory,
                expected_source_ref=args.expected_source_ref,
                expected_source_commit=args.expected_source_commit,
            )
            print(
                f"Official attestations verified for {proof['releaseId']} "
                f"target {proof['target']['targetId']}."
            )
        elif args.command == "compile-kernel-probe":
            proof = record_kernel_probe_build(
                args.run,
                args.platform_source,
                args.target_directory,
                args.output,
            )
            print(
                f"Read-only Kernel probe built: {proof['binarySha256']} "
                f"({proof['binarySizeBytes']} bytes)."
            )
        elif args.command == "query-kernel-readiness":
            proof = query_kernel_readiness(
                args.run,
                alias=args.ssh_alias,
                socket_path=args.socket,
            )
            readiness = proof["result"]["readiness"]
            print(
                f"Kernel readiness: {readiness['status']}; task/Worker/Lease remains UNKNOWN; "
                "GPU idle UNKNOWN; apply CLOSED."
            )
        elif args.command == "record-phase":
            record_phase(
                args.run,
                args.phase,
                args.mode,
                args.status,
                args.elapsed_ms,
                args.evidence,
                args.evidence_class,
                args.reason,
            )
            print("Phase evidence recorded; acceptance still requires review.")
        elif args.command == "record-observation":
            record_observation(
                args.run,
                args.kind,
                args.document,
                args.evidence,
                args.evidence_class,
            )
            print(f"{args.kind} evidence recorded with source digests.")
        elif args.command == "report":
            output = render_report(args.run)
            if args.output is None:
                sys.stdout.write(output)
            else:
                if args.output.exists() or args.output.is_symlink():
                    raise AcceptanceError("Report output already exists; choose a new path")
                args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _write_new_text(args.output, output)
                print(args.output)
        return 0
    except (AcceptanceError, OSError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


def _write_new_text(path: Path, value: str) -> None:
    """Create a new private text file without replacing existing evidence.

    中文：独占创建私有文本文件，不覆盖已有证据。
    """

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)


if __name__ == "__main__":
    raise SystemExit(main())
