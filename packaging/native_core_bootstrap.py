"""First Core installation through the fixed native component updater.

The bootstrap keeps Kernel admission closed from the first Core pointer change
until the newly started Kernel reports known, empty runtime ownership counts.
中文：首次安装期间持久闭门；只有真实 Kernel 计数已知且为空时才解除闭门。
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import shutil
import socket
import stat
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CORE_COMPONENT_IDS = (
    "cyrene-linux-sys-adapter",
    "cyrene-nvidia-adapter",
    "cyrene-sandboxd",
    "cyrene-kernel",
)
PACKAGE_RUNTIME_GROUP_ID = "package-runtime-native-v1"
PACKAGE_RUNTIME_GROUP_COMPONENT_IDS = (
    "cyrene-runtime-maintenance",
    "cyrene-kernel",
    "cy-package-runtime",
)
C10_FIRST_CORE_COMPONENT_IDS = (
    "cyrene-linux-sys-adapter",
    "cyrene-nvidia-adapter",
    "cyrene-sandboxd",
    "cyrene-runtime-maintenance",
    "cyrene-kernel",
    "cy-package-runtime",
)
_PACKAGE_RUNTIME_PROTOCOLS = {
    "cyrene-runtime-maintenance": "cyrene.runtime-maintenance.broker.v1",
    "cyrene-kernel": "cyrene.runtime-maintenance.state.v2",
    "cy-package-runtime": "cy-package-runtime.control.v1",
}
CORE_BOOTSTRAP_MODE = "first-core"
JOURNAL_NAME = "first-core-bootstrap.json"
LEGACY_CORE_EXECUTABLE_NAMES = frozenset(
    {"cyrene-linux-sys-adapter", "cyrene-nvidia-adapter", "cyrene-sandboxd", "cyrene-kernel"}
)
CORE_EXECUTABLE_NAMES = frozenset(
    {
        "cyrene-linux-sys-adapter",
        "cyrene-nvidia-adapter",
        "cyrene-sandboxd",
        "cyrene-kernel",
        "cyrene-runtime-maintenance",
        "cy-package-runtime",
    }
)
PROC_ROOT = Path("/proc")
CORE_RUNTIME_ROOT = Path("/var/lib/cyrene/runtime")
CORE_RUN_ROOT = Path("/run/cyrene")
LINUX_SYS_ADAPTER_COMPONENT_ID = "cyrene-linux-sys-adapter"
LINUX_SYS_ADAPTER_UNIT = "cyrene-linux-sys-adapter.service"
LINUX_SYS_ADAPTER_SOCKET = Path("linux-sys-adapter.sock")
_DELETED_EXE_SUFFIX = " (deleted)"
CORE_EXEC_STARTUP_WAIT_SECONDS = 10.0
CORE_EXEC_STARTUP_POLL_SECONDS = 0.1
CORE_UNIT_QUIESCE_WAIT_SECONDS = 10.0
CORE_UNIT_QUIESCE_POLL_SECONDS = 0.1


@dataclass(frozen=True)
class _HeldAdapterSocketProof:
    """Snapshot the exact held adapter process and its fixed runtime socket."""

    socket_path: Path
    parent_identity: tuple[int, int, int, int, int]
    socket_identity: tuple[int, int, int, int, int]
    main_pid: str
    executable: Path
    executable_identity: tuple[int, int]


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _journal_path(updater: Any) -> Path:
    return Path(updater.state_root) / "native-first-bootstrap" / JOURNAL_NAME


def _resume_first_products_post_end(
    updater: Any,
    plan: dict[str, Any],
    transaction: dict[str, Any],
    journal_path: Path,
    *,
    recovery: bool = True,
) -> dict[str, Any]:
    """Resume only Product post-End work; never roll back the released Core cohort."""

    if transaction.get("gateReleaseConfirmed") is not True:
        raise ValueError("Post-End Product phase has no durable Core gate-release proof")
    addon = transaction.get("firstProducts")
    identity = plan.get("firstProducts")
    if (
        not isinstance(addon, dict)
        or not isinstance(identity, dict)
        or addon.get("receiptDigest") != identity.get("receiptDigest")
        or addon.get("products") != identity.get("products")
    ):
        raise ValueError("Post-End Product journal no longer matches the immutable plan")
    try:
        module = _first_products_module()
        callback = module.recover_post_end if recovery else module.complete_post_end
        result = callback(updater, plan, transaction)
        if not isinstance(result, dict) or result.get("status") not in {"complete", "pending"}:
            raise TypeError("Product post-End recovery returned an invalid status")
    except Exception as error:
        transaction["phase"] = "post_end_pending"
        transaction["postEndError"] = str(error)[:500]
        _write_private_json(updater, journal_path, transaction)
        raise RuntimeError(
            "Core gate release is confirmed; Product post-End work remains pending and was not rolled back"
        ) from error
    transaction["postEndResult"] = result
    transaction["phase"] = "succeeded" if result["status"] == "complete" else "post_end_pending"
    transaction.pop("postEndError", None)
    _write_private_json(updater, journal_path, transaction)
    return {
        "status": "installed" if result["status"] == "complete" else "post-end-pending",
        "planId": plan["planId"],
        "planDigest": plan["planDigest"],
        "postEndReadiness": transaction.get("postEndReadiness", "UNKNOWN"),
        "firstProducts": result,
    }


def _rollback_pre_end(
    updater: Any,
    plan: dict[str, Any],
    transaction: dict[str, Any],
    components: list[dict[str, Any]],
) -> None:
    """Remove Product-owned candidates first, then this transaction's Core cohort."""

    if plan.get("includeProducts") is True:
        record = transaction.get("firstProducts")
        if not isinstance(record, dict):
            raise ValueError("First-Product ownership journal is missing before cleanup")
        has_owned_products = (
            any(record.get(key) for key in ("ownedPointers", "ownedUnits", "ownedPids"))
            or record.get("pendingPointer") is not None
            or record.get("pendingUnit") is not None
        )
        if has_owned_products or record.get("phase") not in {"planned", "staged"}:
            _first_products_module().rollback_pre_end(updater, plan, transaction)
    _stop_candidate_services(updater, components)
    _remove_candidate_pointers_and_units(updater, components)
    _restore_initial_c10_broker(updater, transaction)


def _read_private_json(path: Path) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("First-Core journal or plan path is unsafe")
    info = path.lstat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("First-Core journal or plan is not private to the updater owner")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("First-Core journal or plan is malformed")
    return value


def _write_private_json(updater: Any, path: Path, value: dict[str, Any]) -> None:
    updater._ensure_state_root()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.parent.is_symlink()
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("First-Core private state directory is unsafe")
    updater._atomic_json_file(path, value, mode=0o600)


def _read_process_executable(exe_link: Path) -> tuple[str, str]:
    """Read a procfs executable link and validate its live inode.

    `/proc/PID/exe` remains stat-able after unlink, unlike the displayed host
    pathname. Keep the suffix in the returned path so deleted Core binaries
    can never match a planned candidate executable.
    中文：校验 procfs 中仍存活的 inode，并让已删除 Core 可执行文件继续触发旧进程门禁。
    """

    target = os.readlink(exe_link)
    path_for_name = target.removesuffix(_DELETED_EXE_SUFFIX)
    if not Path(path_for_name).is_absolute():
        raise OSError(errno.EINVAL, "process executable path is not absolute", str(exe_link))
    metadata = os.stat(exe_link)
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(errno.EINVAL, "process executable is not a regular file", str(exe_link))
    name = Path(path_for_name).name
    if not name:
        raise OSError(errno.EINVAL, "process executable name is empty", str(exe_link))
    return target, name


def _path_identity(path: Path, *, kind: str) -> tuple[int, int, int, int, int]:
    """Return a safe root-owned identity for the fixed adapter socket path."""

    try:
        info = path.lstat()
    except OSError as error:
        raise RuntimeError("Held System Adapter socket identity is unavailable") from error
    if path.is_symlink() or info.st_uid != 0:
        raise RuntimeError("Held System Adapter socket path is not root-owned and direct")
    if kind == "directory":
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022:
            raise RuntimeError("Held System Adapter socket parent is unsafe")
    elif kind == "socket":
        if not stat.S_ISSOCK(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o660:
            raise RuntimeError("Held System Adapter socket metadata is invalid")
    else:
        raise ValueError("Unsupported held adapter path identity kind")
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)


def _unix_socket_peer_credentials(path: Path) -> tuple[int, int, int]:
    """Read Linux SO_PEERCRED without sending a command to the adapter."""

    if not hasattr(socket, "SO_PEERCRED"):
        raise RuntimeError("Linux Unix-socket peer credentials are unavailable")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(1.0)
            connection.connect(str(path))
            value = connection.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("3i"),
            )
    except OSError as error:
        raise RuntimeError("Cannot read held System Adapter socket peer identity") from error
    if not isinstance(value, bytes) or len(value) != struct.calcsize("3i"):
        raise RuntimeError("Held System Adapter returned malformed peer credentials")
    return struct.unpack("3i", value)


def _adapter_unit_main_pid(updater: Any) -> str:
    """Require the fixed Linux System Adapter unit to remain active with one PID."""

    unit = updater.components[LINUX_SYS_ADAPTER_COMPONENT_ID].get("systemdUnit")
    if unit != LINUX_SYS_ADAPTER_UNIT:
        raise RuntimeError("Trusted catalog changed the fixed Linux System Adapter unit")
    values: dict[str, str] = {}
    for property_name in ("ActiveState", "MainPID"):
        try:
            result = updater.runner(
                ["systemctl", "show", f"--property={property_name}", "--value", unit],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("Cannot confirm held System Adapter unit identity") from error
        if result.returncode != 0:
            raise RuntimeError("Cannot confirm held System Adapter unit identity")
        values[property_name] = result.stdout.strip()
    pid = values["MainPID"]
    if values["ActiveState"] != "active" or not pid.isascii() or not pid.isdecimal() or pid == "0":
        raise RuntimeError("Held System Adapter is not the exact active unit process")
    return pid


def _verify_held_linux_sys_adapter_socket(
    updater: Any,
    planned_components: list[dict[str, Any]],
    *,
    proc_root: Path = PROC_ROOT,
    expected: _HeldAdapterSocketProof | None = None,
) -> _HeldAdapterSocketProof:
    """Prove the one permitted socket belongs to the exact staged adapter process.

    The caller may use this only after an idempotent BeginCoreBootstrap replay
    confirms the journal's active hold. Socket metadata alone is never ownership
    proof: the fixed unit PID, staged signed ELF inode, and SO_PEERCRED must agree.
    中文：只有同一计划的持久维护hold得到Broker确认后，才可识别这一个适配器socket。
    """

    candidates = [
        item
        for item in planned_components
        if item.get("componentId") == LINUX_SYS_ADAPTER_COMPONENT_ID
    ]
    if len(candidates) != 1:
        raise RuntimeError("Held plan does not identify one Linux System Adapter candidate")
    item = candidates[0]
    component = updater.components.get(LINUX_SYS_ADAPTER_COMPONENT_ID)
    if not isinstance(component, dict) or component.get("systemdUnit") != LINUX_SYS_ADAPTER_UNIT:
        raise RuntimeError("Trusted catalog does not identify the fixed Linux System Adapter unit")

    run_root = Path(getattr(updater, "core_run_root", CORE_RUN_ROOT))
    if not run_root.is_absolute() or run_root != Path(CORE_RUN_ROOT):
        raise RuntimeError(
            "Held System Adapter runtime path is not the fixed Cyrene runtime directory"
        )
    socket_path = run_root / LINUX_SYS_ADAPTER_SOCKET
    parent_identity = _path_identity(run_root, kind="directory")
    socket_identity = _path_identity(socket_path, kind="socket")
    if expected is not None and (
        expected.socket_path != socket_path
        or expected.parent_identity != parent_identity
        or expected.socket_identity != socket_identity
    ):
        raise RuntimeError("Held System Adapter socket or parent identity changed")

    executable = _candidate_executable(updater, item)
    executable_info = executable.stat()
    executable_identity = (executable_info.st_dev, executable_info.st_ino)
    pid = _adapter_unit_main_pid(updater)
    if expected is not None and (
        expected.main_pid != pid
        or expected.executable != executable
        or expected.executable_identity != executable_identity
    ):
        raise RuntimeError("Held System Adapter process differs from the recovery proof")
    if not _proc_executable_matches(pid, executable, proc_root=proc_root):
        raise RuntimeError("Held System Adapter MainPID is not the exact signed staged entrypoint")

    peer_pid, peer_uid, peer_gid = _unix_socket_peer_credentials(socket_path)
    if (
        peer_pid != int(pid)
        or peer_uid != 0
        or peer_uid != socket_identity[3]
        or peer_gid != socket_identity[4]
    ):
        raise RuntimeError("Held System Adapter socket peer differs from its signed unit process")

    after_parent = _path_identity(run_root, kind="directory")
    after_socket = _path_identity(socket_path, kind="socket")
    after_executable = _candidate_executable(updater, item)
    after_info = after_executable.stat()
    if (
        after_parent != parent_identity
        or after_socket != socket_identity
        or after_executable != executable
        or (after_info.st_dev, after_info.st_ino) != executable_identity
        or _adapter_unit_main_pid(updater) != pid
        or not _proc_executable_matches(pid, after_executable, proc_root=proc_root)
    ):
        raise RuntimeError("Held System Adapter identity changed during socket verification")

    proof = _HeldAdapterSocketProof(
        socket_path=socket_path,
        parent_identity=parent_identity,
        socket_identity=socket_identity,
        main_pid=pid,
        executable=executable,
        executable_identity=executable_identity,
    )
    if expected is not None and proof != expected:
        raise RuntimeError("Held System Adapter identity differs from the original recovery proof")
    return proof


def _core_process_snapshot(
    proc_root: Path = PROC_ROOT,
    *,
    executable_names: frozenset[str] = CORE_EXECUTABLE_NAMES,
) -> list[tuple[str, str, str]]:
    """Inspect every process and retain exact executable paths for recovery checks."""

    if not proc_root.is_dir() or proc_root.is_symlink():
        raise RuntimeError("Process inventory is unavailable; first-Core eligibility is UNKNOWN")
    try:
        entries = tuple(proc_root.iterdir())
    except OSError as error:
        raise RuntimeError(
            "Process inventory is unreadable; first-Core eligibility is UNKNOWN"
        ) from error
    found: list[tuple[str, str]] = []
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            state = (entry / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()[0]
            if state in {"Z", "X"}:
                continue
            command = (entry / "cmdline").read_bytes().split(b"\0")
        except FileNotFoundError:
            if not entry.exists():
                continue
            raise RuntimeError(
                "Process inventory changed while checking first-Core eligibility"
            ) from None
        except (OSError, IndexError) as error:
            if isinstance(error, OSError) and error.errno == 2 and not entry.exists():
                continue
            raise RuntimeError(
                "Process inventory is incomplete; first-Core eligibility is UNKNOWN"
            ) from error
        names: set[str] = set()
        try:
            executable_path, executable_name = _read_process_executable(entry / "exe")
            names.add(executable_name)
        except FileNotFoundError:
            if not entry.exists():
                continue
            if any(command):
                raise RuntimeError(
                    "Process executable is unavailable; first-Core eligibility is UNKNOWN"
                ) from None
            continue  # Linux kernel threads have no executable or command line.
        except OSError as error:
            raise RuntimeError(
                "Process executable is unreadable; first-Core eligibility is UNKNOWN"
            ) from error
        for argument in command:
            if argument:
                names.add(Path(os.fsdecode(argument)).name)
        found.extend(
            (name, executable_path, entry.name) for name in sorted(names & executable_names)
        )
    return found


def _core_processes(proc_root: Path = PROC_ROOT) -> list[str]:
    """Return legacy Core process names after validating the whole process table."""

    return sorted({name for name, _path, _pid in _core_process_snapshot(proc_root)})


def _validate_package_runtime_group(updater: Any) -> dict[str, Any] | None:
    """Validate the exact C10 group contract, or identify a legacy C9 catalog."""

    groups = updater.catalog.get("compatibilityGroups", [])
    if not isinstance(groups, list):
        raise TypeError("Trusted compatibility group catalog is malformed")
    matches = [
        group
        for group in groups
        if isinstance(group, dict) and group.get("groupId") == PACKAGE_RUNTIME_GROUP_ID
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("Trusted Package Runtime compatibility group is ambiguous")
    group = matches[0]
    expected_members = [
        {"componentId": component_id, "requiredForAdoption": True, "protocolVersion": protocol}
        for component_id, protocol in _PACKAGE_RUNTIME_PROTOCOLS.items()
    ]
    if (
        group.get("groupVersion") != "2"
        or group.get("wireApiVersion") != "cyrene.runtime-maintenance.binding-operations.v1"
        or group.get("contractApiVersion") != "0.1.0"
        or group.get("members") != expected_members
    ):
        raise ValueError(
            "Trusted Package Runtime compatibility group is not the fixed C10 contract"
        )
    for component_id, protocol in _PACKAGE_RUNTIME_PROTOCOLS.items():
        component = updater.components.get(component_id)
        if (
            not isinstance(component, dict)
            or component.get("compatibilityGroup") != PACKAGE_RUNTIME_GROUP_ID
            or component.get("protocolVersion") != protocol
        ):
            raise ValueError(f"Trusted catalog has an incomplete C10 member: {component_id}")
    return group


def _catalog_core_component_ids(updater: Any) -> tuple[str, ...]:
    """Select C9's fixed four or C10's complete six-component first-Core cohort."""

    return (
        C10_FIRST_CORE_COMPONENT_IDS
        if _validate_package_runtime_group(updater) is not None
        else CORE_COMPONENT_IDS
    )


def _component_cohort(
    updater: Any, component_ids: set[str] | list[str] | tuple[str, ...]
) -> tuple[str, ...]:
    """Reject partial or mixed first-Core sets and return their fixed start order."""

    actual = set(component_ids)
    legacy = set(CORE_COMPONENT_IDS)
    c10 = set(C10_FIRST_CORE_COMPONENT_IDS)
    if actual == legacy:
        if _validate_package_runtime_group(updater) is not None:
            raise ValueError("C10 first-Core candidates must include every required group member")
        return CORE_COMPONENT_IDS
    if actual == c10:
        _validate_package_runtime_group(updater)
        return C10_FIRST_CORE_COMPONENT_IDS
    raise ValueError("First-Core candidates are not an exact supported C9 or C10 cohort")


def _validate_staged_cohort(updater: Any, components: list[dict[str, Any]]) -> tuple[str, ...]:
    """Bind staged C10 manifests to the trusted group pins before activation."""

    cohort = _component_cohort(
        updater, {item.get("componentId") for item in components if isinstance(item, dict)}
    )
    if cohort == CORE_COMPONENT_IDS:
        return cohort
    group = _validate_package_runtime_group(updater)
    assert group is not None
    for item in components:
        component_id = item["componentId"]
        if component_id not in _PACKAGE_RUNTIME_PROTOCOLS:
            continue
        manifest = item.get("manifest")
        compatibility = manifest.get("compatibility") if isinstance(manifest, dict) else None
        if (
            manifest.get("schemaVersion") != 2
            or manifest.get("protocolVersion") != _PACKAGE_RUNTIME_PROTOCOLS[component_id]
            or not isinstance(compatibility, dict)
            or compatibility.get("groupId") != PACKAGE_RUNTIME_GROUP_ID
            or compatibility.get("groupVersion") != group["groupVersion"]
            or compatibility.get("contractApiVersion") != group["contractApiVersion"]
            or compatibility.get("wireApiVersion") != group["wireApiVersion"]
            or compatibility.get("contractLock") != group.get("contractLock")
        ):
            raise ValueError(f"Staged C10 compatibility pins differ for {component_id}")
    return cohort


def _validate_c10_broker_manifest(updater: Any, installed: dict[str, Any]) -> dict[str, Any]:
    """Require the current Broker's installed receipt to prove the exact C10 protocol."""

    group = _validate_package_runtime_group(updater)
    manifest = installed.get("manifest")
    component_id = "cyrene-runtime-maintenance"
    compatibility = manifest.get("compatibility") if isinstance(manifest, dict) else None
    if (
        group is None
        or installed.get("active") is not True
        or installed.get("identityAttested") is not True
        or manifest.get("schemaVersion") != 2
        or manifest.get("protocolVersion") != _PACKAGE_RUNTIME_PROTOCOLS[component_id]
        or not isinstance(compatibility, dict)
        or compatibility.get("groupId") != PACKAGE_RUNTIME_GROUP_ID
        or compatibility.get("groupVersion") != group["groupVersion"]
        or compatibility.get("contractApiVersion") != group["contractApiVersion"]
        or compatibility.get("wireApiVersion") != group["wireApiVersion"]
        or compatibility.get("contractLock") != group.get("contractLock")
    ):
        raise ValueError("Current maintenance Broker is not verified for the exact C10 contract")
    pointer = installed.get("pointerIdentity")
    expected_pointer = f"{manifest.get('version')}--{str(manifest.get('manifestDigest', '')).removeprefix('sha256:')}"
    if (
        not isinstance(pointer, str)
        or pointer != expected_pointer
        or installed.get("manifestDigest") != manifest.get("manifestDigest")
        or not isinstance(installed.get("artifactDigest"), str)
    ):
        raise ValueError("Current maintenance Broker receipt does not match its active pointer")
    return manifest


def _verify_process_executable(
    proc_root: Path, pid: str, expected: Path, *, description: str
) -> None:
    """Match one systemd PID to its exact live executable inode path."""

    try:
        process_path, _name = _read_process_executable(Path(proc_root) / pid / "exe")
    except OSError as error:
        raise RuntimeError(f"{description} process identity is unreadable") from error
    if process_path.endswith(_DELETED_EXE_SUFFIX) or Path(process_path) != expected:
        raise ValueError(f"{description} MainPID is not its signed executable")


def _verified_running_c10_broker(updater: Any, proc_root: Path) -> dict[str, Any]:
    """Prove the active signed C10 Broker is the sole permitted pre-Core process."""

    component_id = "cyrene-runtime-maintenance"
    component = updater.components[component_id]
    installed = updater._installed(component)
    manifest = _validate_c10_broker_manifest(updater, installed)
    pointer = installed["pointerIdentity"]
    receipt = updater._read_active_receipt(component_id)
    if (
        not isinstance(receipt, dict)
        or receipt.get("manifest") != manifest
        or receipt.get("manifestDigest") != installed["manifestDigest"]
        or receipt.get("artifactDigest") != installed["artifactDigest"]
        or updater._active_native_pointer_identity(component_id) != pointer
    ):
        raise ValueError("Current maintenance Broker active receipt is missing or inconsistent")

    release = updater.install_root / "components" / component_id / "releases" / pointer
    unit = component.get("systemdUnit")
    unit_source = release / "systemd" / unit
    if (
        not isinstance(unit, str)
        or component.get("restart", {}).get("unit") != unit
        or unit_source.is_symlink()
        or not unit_source.is_file()
        or not updater._unit_uses_component_runner(unit_source, component_id)
    ):
        raise ValueError("Current maintenance Broker has no verified catalog unit")
    unit_bytes = unit_source.read_bytes()
    installed_units: dict[tuple[int, int, str], Path] = {}
    for directory in updater.systemd_unit_dirs:
        path = Path(directory) / unit
        if not path.exists() and not path.is_symlink():
            continue
        info = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o022
            or path.read_bytes() != unit_bytes
            or not updater._unit_uses_component_runner(path, component_id)
        ):
            raise ValueError("Current maintenance Broker unit differs from its signed release")
        installed_units[(info.st_dev, info.st_ino, str(path.resolve(strict=True)))] = path
    if len(installed_units) != 1:
        raise ValueError("Current maintenance Broker unit is missing or ambiguous")

    executable = _candidate_executable(
        updater,
        {
            "componentId": component_id,
            "version": manifest["version"],
            "manifestDigest": manifest["manifestDigest"],
            "manifest": manifest,
        },
    )
    active = updater.runner(
        ["systemctl", "show", "--property=ActiveState", "--value", unit],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    main_pid = updater.runner(
        ["systemctl", "show", "--property=MainPID", "--value", unit],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    pid = main_pid.stdout.strip()
    if (
        active.returncode != 0
        or active.stdout.strip() != "active"
        or not pid.isdecimal()
        or pid == "0"
    ):
        raise RuntimeError("Current maintenance Broker is not confirmed active")
    _verify_process_executable(proc_root, pid, executable, description="Current maintenance Broker")
    return {
        "componentId": component_id,
        "pointerIdentity": pointer,
        "version": manifest["version"],
        "manifestDigest": installed["manifestDigest"],
        "artifactDigest": installed["artifactDigest"],
        "systemdUnit": unit,
        "mainPid": pid,
        "executable": str(executable),
    }


def _assert_fresh(
    updater: Any,
    *,
    proc_root: Path = PROC_ROOT,
    planned_components: list[dict[str, Any]] | None = None,
    require_empty_kernel_state: bool = True,
    expected_bootstrap_broker: dict[str, Any] | None = None,
    held_adapter_socket_proof: _HeldAdapterSocketProof | None = None,
) -> dict[str, Any] | None:
    """Require a genuine empty first install; never take over old Core state."""

    if held_adapter_socket_proof is not None and not require_empty_kernel_state:
        raise ValueError("Held adapter socket proof is valid only during full state validation")
    planned = {item["componentId"]: item for item in planned_components or []}
    planned_executables: dict[str, dict[str, Any]] = {}
    cohort = _catalog_core_component_ids(updater)
    current_broker = (
        _verified_running_c10_broker(updater, proc_root)
        if cohort == C10_FIRST_CORE_COMPONENT_IDS
        else None
    )
    if (
        current_broker is not None
        and expected_bootstrap_broker is not None
        and current_broker != expected_bootstrap_broker
    ):
        candidate = next(
            (
                item
                for item in planned_components or []
                if item.get("componentId") == "cyrene-runtime-maintenance"
            ),
            None,
        )
        if candidate is None or (
            current_broker.get("pointerIdentity")
            != f"{candidate['version']}--{candidate['manifestDigest'].removeprefix('sha256:')}"
        ):
            raise ValueError("Current C10 Broker differs from the checked first-Core identity")
    for component_id in cohort:
        component = updater.components.get(component_id)
        if not isinstance(component, dict):
            raise TypeError(f"Trusted catalog omits Core component {component_id}")
        item = planned.get(component_id)
        expected_pointer = None
        if item is not None:
            expected_pointer = (
                f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
            )
            entrypoint = item.get("manifest", {}).get("artifact", {}).get("entrypoint")
            if not isinstance(entrypoint, str):
                raise ValueError("Staged Core entrypoint identity is incomplete")
            executable_path = str(
                (
                    updater.install_root
                    / "components"
                    / component_id
                    / "releases"
                    / expected_pointer
                    / entrypoint
                ).resolve()
            )
            planned_executables[executable_path] = item
        current = updater._active_native_pointer_identity(component_id)
        if (
            current is not None
            and current != expected_pointer
            and not (
                component_id == "cyrene-runtime-maintenance"
                and current_broker is not None
                and current == current_broker["pointerIdentity"]
            )
        ):
            raise ValueError(
                f"Core component {component_id} already has an unrelated active pointer"
            )
        unit = component.get("systemdUnit")
        if not isinstance(unit, str):
            raise TypeError(f"Trusted catalog has no fixed unit for {component_id}")
        for directory in updater.systemd_unit_dirs:
            path = Path(directory) / unit
            if path.exists() or path.is_symlink():
                if component_id == "cyrene-runtime-maintenance" and current_broker is not None:
                    continue
                if item is None:
                    raise ValueError(f"Existing Core unit blocks first install: {unit}")
                release = (
                    updater.install_root
                    / "components"
                    / component_id
                    / "releases"
                    / expected_pointer
                )
                candidate_unit = release / "systemd" / unit
                if (
                    path.is_symlink()
                    or not candidate_unit.is_file()
                    or path.read_bytes() != candidate_unit.read_bytes()
                ):
                    raise ValueError(f"Existing Core unit differs from this bootstrap plan: {unit}")
    if require_empty_kernel_state:
        runtime_root = Path(getattr(updater, "core_runtime_root", CORE_RUNTIME_ROOT))
        run_root = Path(getattr(updater, "core_run_root", CORE_RUN_ROOT))
        for root in (runtime_root, run_root):
            try:
                info = root.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise RuntimeError(
                    "Kernel runtime ownership inventory is unavailable; first-Core eligibility is UNKNOWN"
                ) from error
            if root.is_symlink() or not stat.S_ISDIR(info.st_mode):
                raise RuntimeError(
                    "Kernel runtime ownership root is unsafe; first-Core eligibility is UNKNOWN"
                )
        kernel_journal = runtime_root / "journal.jsonl"
        if kernel_journal.exists() or kernel_journal.is_symlink():
            raise ValueError("Existing Kernel runtime ownership journal blocks first install")
        for relative in (
            Path("workers"),
            Path("kernel.sock"),
            Path("worker.sock"),
            Path("provider.sock"),
            LINUX_SYS_ADAPTER_SOCKET,
            Path("nvidia-adapter.sock"),
            Path("sandboxd.sock"),
        ):
            path = run_root / relative
            if path.exists() or path.is_symlink():
                if relative == LINUX_SYS_ADAPTER_SOCKET and held_adapter_socket_proof is not None:
                    parent_identity = _path_identity(run_root, kind="directory")
                    socket_identity = _path_identity(path, kind="socket")
                    if (
                        path != held_adapter_socket_proof.socket_path
                        or parent_identity != held_adapter_socket_proof.parent_identity
                        or socket_identity != held_adapter_socket_proof.socket_identity
                    ):
                        raise RuntimeError("Held System Adapter socket identity changed")
                    continue
                raise ValueError(
                    f"Existing Core runtime ownership path blocks first install: {relative}"
                )
    process_names = (
        CORE_EXECUTABLE_NAMES
        if cohort == C10_FIRST_CORE_COMPONENT_IDS
        else LEGACY_CORE_EXECUTABLE_NAMES
    )
    processes = _core_process_snapshot(proc_root, executable_names=process_names)
    for _name, executable, pid in processes:
        if current_broker is not None and (
            pid == current_broker["mainPid"] and executable == current_broker["executable"]
        ):
            continue
        item = planned_executables.get(executable)
        if item is None:
            raise ValueError("Legacy or manually started Core process blocks first install")
        component = updater.components[item["componentId"]]
        unit = component["systemdUnit"]
        main_pid = updater.runner(
            ["systemctl", "show", "--property=MainPID", "--value", unit],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        state = updater.runner(
            ["systemctl", "show", "--property=ActiveState", "--value", unit],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if (
            main_pid.returncode != 0
            or state.returncode != 0
            or main_pid.stdout.strip() != pid
            or state.stdout.strip() != "active"
        ):
            raise ValueError("A planned Core executable is not owned by its exact active unit")
    try:
        gpu = updater.runner(
            ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, TimeoutError) as error:
        raise RuntimeError(
            "GPU resource inventory is unavailable; first-Core eligibility is UNKNOWN"
        ) from error
    if gpu.returncode != 0:
        raise RuntimeError("GPU resource inventory failed; first-Core eligibility is UNKNOWN")
    if gpu.stdout.strip():
        raise ValueError("Existing GPU compute resources block first-Core installation")
    if held_adapter_socket_proof is not None:
        _verify_held_linux_sys_adapter_socket(
            updater,
            planned_components or [],
            proc_root=proc_root,
            expected=held_adapter_socket_proof,
        )
    return current_broker


def _health_snapshot(updater: Any, *, require_eligible: bool = True) -> dict[str, Any]:
    result = updater._broker_request("Health", {})
    if (
        result.get("status") != "SERVING"
        or not isinstance(result.get("gate_generation"), int)
        or isinstance(result.get("gate_generation"), bool)
        or not isinstance(result.get("catalog_generation"), int)
        or isinstance(result.get("catalog_generation"), bool)
    ):
        raise RuntimeError("Runtime maintenance broker Health identity is unknown")
    eligible = result.get("core_bootstrap_eligible")
    if not isinstance(eligible, bool):
        raise TypeError("Platform did not expose a known first-Core eligibility state")
    if require_eligible and not eligible:
        raise RuntimeError("Platform does not confirm a fresh first-Core bootstrap state")
    if _validate_package_runtime_group(updater) is not None:
        capabilities = result.get("capabilities")
        if (
            result.get("protocol_version") != "cyrene.runtime-maintenance.broker.v1"
            or not isinstance(capabilities, list)
            or any(not isinstance(value, str) or not value for value in capabilities)
            or len(capabilities) != len(set(capabilities))
            or "cyrene.runtime-maintenance.state.v2" not in capabilities
        ):
            raise RuntimeError("Current Broker Health does not prove the C10 state-v2 contract")
    activity_catalog, sources = updater._activity_catalog()
    if activity_catalog["generation"] != result["catalog_generation"]:
        raise RuntimeError("Broker and installed activity catalog generations differ")
    return {
        "gateGeneration": result["gate_generation"],
        "catalogGeneration": result["catalog_generation"],
        "activitySources": sources,
        "coreBootstrapEligible": eligible,
    }


def _plan_path(updater: Any, plan_id: str) -> Path:
    return Path(updater.state_root) / "plans" / (plan_id + ".first-core.json")


def _base_plan(updater: Any, plan_id: str, plan_digest: str) -> dict[str, Any]:
    """Load the ordinary updater plan bound as the Core portion of this plan."""

    path = Path(updater.state_root) / "plans" / (plan_id + ".json")
    value = _read_private_json(path)
    if value is None or value.get("planId") != plan_id or value.get("planDigest") != plan_digest:
        raise ValueError("The fixed Core update plan changed; check again")
    return value


def _first_products_module() -> Any:
    """Load the fixed Product first-start helper shipped beside this module."""

    name = "_cyrene_native_first_products"
    module = sys.modules.get(name)
    if module is not None:
        return module
    import importlib.util

    helper_path = Path(__file__).with_name("native_first_products.py")
    if not helper_path.is_file() or helper_path.is_symlink():
        raise RuntimeError("The fixed first-Product helper is unavailable")
    spec = importlib.util.spec_from_file_location(name, helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("The fixed first-Product helper cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _first_core_successor_module() -> Any:
    """Load the fixed helper that validates and stages held successor plans."""

    name = "_cyrene_native_core_successor"
    module = sys.modules.get(name)
    if module is not None:
        return module
    import importlib.util

    helper_path = Path(__file__).with_name("native_core_successor.py")
    if not helper_path.is_file() or helper_path.is_symlink():
        raise RuntimeError("The fixed held-successor helper is unavailable")
    spec = importlib.util.spec_from_file_location(name, helper_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("The fixed held-successor helper cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_plan(updater: Any, plan_id: Any, plan_digest: Any) -> dict[str, Any]:
    updater._validate_plan_identity(plan_id, plan_digest)
    value = _read_private_json(_plan_path(updater, plan_id))
    if value is None or value.get("planId") != plan_id or value.get("planDigest") != plan_digest:
        raise ValueError("First-Core plan identity changed; check again")
    if value.get("mode") != CORE_BOOTSTRAP_MODE:
        raise ValueError("Plan is not a first-Core bootstrap plan")
    updater_module = sys.modules.get(updater.__class__.__module__)
    canonical = getattr(updater_module, "canonical_jcs", None)
    if not callable(canonical):
        canonical = next(
            (
                module.canonical_jcs
                for module in tuple(sys.modules.values())
                if callable(getattr(module, "canonical_jcs", None))
            ),
            None,
        )
    if not callable(canonical):
        raise TypeError("Trusted plan canonicalization is unavailable")
    bootstrap_broker = None
    if "bootstrapBroker" in value:
        broker = value["bootstrapBroker"]
        if (
            not isinstance(broker, dict)
            or set(broker)
            != {
                "componentId",
                "pointerIdentity",
                "version",
                "manifestDigest",
                "artifactDigest",
                "systemdUnit",
                "mainPid",
                "executable",
            }
            or broker.get("componentId") != "cyrene-runtime-maintenance"
            or not isinstance(broker.get("mainPid"), str)
            or not broker["mainPid"].isdecimal()
            or broker["mainPid"] == "0"
            or not isinstance(broker.get("executable"), str)
        ):
            raise ValueError("First-Core Broker identity block is malformed")
        bootstrap_broker = broker
    include_products = value.get("includeProducts", False)
    if not isinstance(include_products, bool):
        raise TypeError("First-Core includeProducts plan field is not a boolean")
    material = {
        key: value[key]
        for key in (
            "schemaVersion",
            "mode",
            "basePlanId",
            "basePlanDigest",
            "channel",
            "catalogGeneration",
            "catalogDigest",
            "gateGeneration",
            "activitySources",
            "coreBootstrapEligible",
            "targetId",
            "components",
        )
    }
    if bootstrap_broker is not None:
        material["bootstrapBroker"] = bootstrap_broker
    # Accept Core-only plans written before the explicit Product opt-in field.
    if "includeProducts" in value:
        material["includeProducts"] = include_products
    if include_products:
        first_products = value.get("firstProducts")
        if not isinstance(first_products, dict) or first_products.get("schemaVersion") != 1:
            raise ValueError("First-Product identity block is missing from the immutable plan")
        material["firstProducts"] = first_products
    elif "firstProducts" in value:
        raise ValueError("Core-only plan unexpectedly contains Product cohort data")
    successor_binding = value.get("successorBinding")
    if successor_binding is not None:
        if value.get("includeProducts") is not False:
            raise ValueError("Held successor plan must be Core-only")
        successor = _first_core_successor_module()
        material = successor.plan_material(value)
    expected_digest = _digest(canonical(material))
    if expected_digest != plan_digest or plan_id != "plan-" + expected_digest.split(":", 1)[1][:32]:
        raise ValueError("First-Core plan digest does not match its immutable cohort data")
    expected_artifacts = {
        item["componentId"]: item["artifactDigest"] for item in value.get("components", [])
    }
    if value.get("componentArtifactDigests") != expected_artifacts:
        raise ValueError("First-Core artifact digest map differs from its immutable cohort")
    return value


def check(
    updater: Any,
    *,
    channel: Any = None,
    include_products: bool = False,
    proc_root: Path = PROC_ROOT,
) -> dict[str, Any]:
    """Create a digest-bound four-component bootstrap plan from trusted candidates."""

    updater._reload_catalog_for_operation()
    updater._ensure_state_root()
    if not isinstance(include_products, bool):
        raise TypeError("includeProducts must be a boolean")
    bootstrap_broker = _assert_fresh(updater, proc_root=proc_root)
    snapshot = _health_snapshot(updater)
    checked = updater.check(list(CORE_COMPONENT_IDS), channel=channel, include_readiness=False)
    normal = checked.get("plan")
    if not isinstance(normal, dict):
        raise TypeError("Trusted release index did not produce a complete Core cohort")
    items = normal.get("components")
    if not isinstance(items, list):
        raise TypeError("Trusted release index did not produce a component cohort")
    cohort = _component_cohort(
        updater, {item.get("componentId") for item in items if isinstance(item, dict)}
    )
    if cohort != _catalog_core_component_ids(updater):
        raise ValueError("Release candidates do not match the trusted first-Core cohort")
    targets = {
        updater._target_for(updater.components[component_id]).get("id") for component_id in cohort
    }
    if len(targets) != 1 or None in targets:
        raise ValueError("Core artifacts do not share one supported native target")
    first_products = _first_products_module().check(updater, normal) if include_products else None
    material = {
        "schemaVersion": 1,
        "mode": CORE_BOOTSTRAP_MODE,
        "basePlanId": normal["planId"],
        "basePlanDigest": normal["planDigest"],
        "channel": normal["channel"],
        "catalogGeneration": normal["catalogGeneration"],
        "catalogDigest": normal["catalogDigest"],
        **snapshot,
        "targetId": next(iter(targets)),
        "components": sorted(items, key=lambda item: item["componentId"]),
        "includeProducts": include_products,
    }
    if bootstrap_broker is not None:
        material["bootstrapBroker"] = bootstrap_broker
    if include_products:
        if not isinstance(first_products, dict) or first_products.get("schemaVersion") != 1:
            raise TypeError("First-Product helper returned an invalid immutable identity block")
        material["firstProducts"] = first_products
    updater_module = sys.modules.get(updater.__class__.__module__)
    canonical = getattr(updater_module, "canonical_jcs", None)
    if not callable(canonical):
        canonical = next(
            (
                module.canonical_jcs
                for module in tuple(sys.modules.values())
                if callable(getattr(module, "canonical_jcs", None))
            ),
            None,
        )
    if not callable(canonical):
        raise TypeError("Trusted updater canonical JSON implementation is unavailable")
    plan_digest = _digest(canonical(material))
    plan_id = "plan-" + plan_digest.split(":", 1)[1][:32]
    request_id = "bootstrap-" + plan_digest.split(":", 1)[1][:32]
    plan = {
        **material,
        "planId": plan_id,
        "planDigest": plan_digest,
        "requestId": request_id,
        "phase": "checked",
        "requiresRestart": True,
        "userConfirmedRestart": True,
        "componentArtifactDigests": {
            item["componentId"]: item["artifactDigest"] for item in material["components"]
        },
    }
    _write_private_json(updater, _plan_path(updater, plan_id), plan)
    return {"status": "checked", "mode": CORE_BOOTSTRAP_MODE, "plan": plan, "plans": [plan]}


def stage(updater: Any, plan_id: Any, plan_digest: Any, *, channel: Any = None) -> dict[str, Any]:
    """Stage every exact signed candidate while allowing runtime readiness UNKNOWN."""

    plan = _load_plan(updater, plan_id, plan_digest)
    if "bootstrapBroker" in plan:
        current_broker = _verified_running_c10_broker(updater, PROC_ROOT)
        if current_broker != plan["bootstrapBroker"]:
            raise ValueError("The verified C10 Broker changed after the first-Core check")
    snapshot = _health_snapshot(updater, require_eligible=False)
    if any(
        plan.get(key) != value
        for key, value in snapshot.items()
        if key in {"catalogGeneration", "activitySources"}
    ):
        raise ValueError("Core bootstrap gate or activity catalog changed; check again")
    staged = updater.stage(
        plan["basePlanId"], plan["basePlanDigest"], channel=channel or plan["channel"]
    )
    base_stage = Path(updater.state_root) / "staged" / plan["basePlanId"] / "stage.json"
    record = _read_private_json(base_stage)
    if record is None or record.get("phase") != "staged":
        raise ValueError("Verified Core cohort was not completely staged")
    digests = {
        item.get("componentId"): item.get("artifactDigest")
        for item in record.get("components", [])
        if isinstance(item, dict)
    }
    if digests != plan["componentArtifactDigests"]:
        raise ValueError("Staged Core artifact digests differ from the confirmed plan")
    _validate_staged_cohort(updater, record.get("components", []))
    first_products_stage = None
    if plan.get("includeProducts") is True:
        base_plan = _base_plan(updater, plan["basePlanId"], plan["basePlanDigest"])
        first_products_stage = _first_products_module().stage(
            updater, base_plan, plan["firstProducts"]
        )
        if (
            not isinstance(first_products_stage, dict)
            or first_products_stage.get("receiptDigest") != plan["firstProducts"]["receiptDigest"]
            or first_products_stage.get("products") != plan["firstProducts"]["products"]
        ):
            raise ValueError("Staged Product cohort differs from the immutable check plan")
    staged_plan = {**plan, "phase": "staged"}
    staged_record = {
        "schemaVersion": 1,
        "mode": CORE_BOOTSTRAP_MODE,
        "plan": staged_plan,
        "basePlanId": plan["basePlanId"],
        "basePlanDigest": plan["basePlanDigest"],
        "phase": "staged",
    }
    if first_products_stage is not None:
        staged_record["firstProductsStage"] = first_products_stage
    _write_private_json(
        updater, Path(updater.state_root) / "staged" / plan_id / "stage.json", staged_record
    )
    return {
        "status": "staged",
        "mode": CORE_BOOTSTRAP_MODE,
        "plan": staged_plan,
        "plans": [staged_plan],
        "components": staged.get("components", []),
        **({"firstProducts": first_products_stage} if first_products_stage is not None else {}),
    }


def _write_unit(updater: Any, component: dict[str, Any], release: Path) -> Path:
    unit = component["systemdUnit"]
    source = release / "systemd" / unit
    if (
        source.is_symlink()
        or not source.is_file()
        or not updater._unit_uses_component_runner(source, component["componentId"])
    ):
        raise ValueError(f"Verified Core release has no catalog-matched unit: {unit}")
    if not updater.systemd_unit_dirs:
        raise ValueError("First-Core bootstrap has no fixed systemd unit directory")
    # The normal catalog resolver searches /etc before vendor directories. Put
    # managed overrides in that first, effective location while preserving the
    # updater's full search path for conflict checks.
    destination = Path(updater.systemd_unit_dirs[0]) / unit
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if not destination.is_symlink() and destination.read_bytes() == source.read_bytes():
            return destination
        raise ValueError(f"Core unit path became occupied before installation: {unit}")
    temporary = destination.with_name("." + destination.name + ".first-core.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError("Interrupted Core unit temporary path requires operator review")
    shutil.copyfile(source, temporary)
    temporary.chmod(0o644)
    os.replace(temporary, destination)
    updater._fsync_directory(destination.parent)
    return destination


def _require_core_ready(updater: Any, plan: dict[str, Any]) -> dict[str, Any]:
    activity_catalog, sources = updater._activity_catalog()
    if (
        activity_catalog.get("generation") != plan["catalogGeneration"]
        or sources != plan["activitySources"]
    ):
        raise RuntimeError("Installed activity catalog changed while the first-Core hold is closed")
    result = updater._readiness_for("CORE_RUNTIME", requires_restart=True, force=True)
    blockers = result.get("blocker_codes")
    zero_counts = (
        "active_task_count",
        "inflight_runtime_admission_count",
        "active_worker_count",
        "active_allocation_count",
    )
    if (
        not isinstance(blockers, list)
        or "RUNTIME_ACTIVITY_UNKNOWN" in blockers
        or any(
            not isinstance(result.get(field), int)
            or isinstance(result.get(field), bool)
            or result.get(field) != 0
            for field in zero_counts
        )
        or not isinstance(result.get("active_tasks"), list)
        or result.get("active_tasks")
    ):
        raise RuntimeError(
            "Kernel runtime ownership counts are unknown or non-empty; the first-Core hold remains closed"
        )
    return result


def _require_c10_package_runtime_ready(updater: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """Require source-authenticated Package Runtime authority before C10 gate release."""

    if _component_cohort(updater, {item["componentId"] for item in plan["components"]}) != (
        C10_FIRST_CORE_COMPONENT_IDS
    ):
        return {}
    catalog, sources = updater._activity_catalog()
    if catalog.get("generation") != plan["catalogGeneration"] or sources != plan["activitySources"]:
        raise RuntimeError("Activity source identity changed before C10 readiness proof")
    helper = updater._load_native_package_runtime_bootstrap()
    try:
        result = helper.probe_runtime_authority(
            catalog, expected_catalog_generation=plan["catalogGeneration"]
        )
    except Exception as error:
        raise RuntimeError(
            "Package Runtime source-authenticated authority readiness is unavailable"
        ) from error
    expected = {
        "authority": "platform_package_runtime",
        "protocol_version": "cy-package-runtime.control.v1",
        "catalog_generation": plan["catalogGeneration"],
        "capabilities": ["cy-package-runtime.binding-operation-admission.v1"],
    }
    if result != expected:
        raise RuntimeError("Package Runtime authority identity differs from the C10 contract")
    return result


def _verify_live_core_cohort(
    updater: Any, transaction: dict[str, Any], plan: dict[str, Any]
) -> None:
    """Prove all four exact staged releases are active and their units are healthy."""

    cohort = _validate_staged_cohort(updater, transaction.get("components", []))
    if {item.get("componentId") for item in plan.get("components", [])} != set(cohort):
        raise RuntimeError("Bootstrap plan and journal no longer contain the same exact cohort")
    for item in transaction["components"]:
        component_id = item["componentId"]
        pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
        if updater._active_native_pointer_identity(component_id) != pointer:
            raise RuntimeError(
                f"Active pointer no longer matches the confirmed Core plan: {component_id}"
            )
        component = updater.components[component_id]
        units = [
            Path(directory) / component["systemdUnit"] for directory in updater.systemd_unit_dirs
        ]
        if not units or units[0].is_symlink() or not units[0].is_file():
            raise RuntimeError(
                f"Catalog-owned systemd unit is missing or ambiguous: {component_id}"
            )
        release = updater.install_root / "components" / component_id / "releases" / pointer
        source = release / "systemd" / component["systemdUnit"]
        if not source.is_file() or units[0].read_bytes() != source.read_bytes():
            raise RuntimeError(
                f"Installed systemd unit differs from its staged release: {component_id}"
            )
        for shadow in units[1:]:
            if (shadow.exists() or shadow.is_symlink()) and (
                shadow.is_symlink()
                or not shadow.is_file()
                or shadow.read_bytes() != source.read_bytes()
            ):
                raise RuntimeError(
                    f"A conflicting shadow unit exists in another systemd search path: {component_id}"
                )
    _verify_started_processes(updater, transaction["components"])
    updater._health_transaction(transaction)
    snapshot, sources = _health_snapshot(updater, require_eligible=False), plan["activitySources"]
    if (
        snapshot["catalogGeneration"] != plan["catalogGeneration"]
        or snapshot["activitySources"] != sources
    ):
        raise RuntimeError("Platform activity catalog changed while the Core hold is active")
    _require_c10_package_runtime_ready(updater, plan)


def _verify_started_processes(updater: Any, components: list[dict[str, Any]]) -> None:
    """Require each managed unit to own a live PID running its catalog binary."""

    for item in components:
        component = updater.components[item["componentId"]]
        unit = component["systemdUnit"]
        clock, _sleeper = _candidate_startup_clock(updater)
        started_at = _startup_time(clock)
        deadline = started_at + CORE_EXEC_STARTUP_WAIT_SECONDS
        timeout = _startup_remaining(clock, deadline, unit)
        completed = updater.runner(
            ["systemctl", "show", "--property=MainPID", "--value", unit],
            capture_output=True,
            text=True,
            timeout=min(10, timeout),
            check=False,
        )
        _startup_remaining(clock, deadline, unit)
        if completed.returncode != 0 or not completed.stdout.strip().isdecimal():
            raise RuntimeError(f"{unit} has no confirmed systemd MainPID")
        pid = completed.stdout.strip()
        _verify_candidate_pid(updater, item, pid, deadline=deadline)


def _candidate_executable(updater: Any, item: dict[str, Any]) -> Path:
    """Validate root-owned bytes at the exact attested manifest entrypoint."""

    manifest = item.get("manifest")
    artifact = manifest.get("artifact") if isinstance(manifest, dict) else None
    entrypoint = artifact.get("entrypoint") if isinstance(artifact, dict) else None
    files = artifact.get("files") if isinstance(artifact, dict) else None
    if not isinstance(entrypoint, str) or not isinstance(files, dict):
        raise TypeError("Staged native artifact has no exact entrypoint ownership proof")
    component_id = item["componentId"]
    pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
    executable = (
        updater.install_root / "components" / component_id / "releases" / pointer / entrypoint
    )
    info = executable.lstat()
    if (
        executable.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & 0o022
        or _digest(executable.read_bytes()) != files.get(entrypoint)
    ):
        raise RuntimeError("Core entrypoint ownership or digest differs from the staged artifact")
    return executable.resolve(strict=True)


def _candidate_startup_clock(updater: Any) -> tuple[Any, Any]:
    """Return the private clock seam used to bound candidate identity polling."""

    clock = getattr(updater, "monotonic", time.monotonic)
    sleeper = getattr(updater, "sleeper", time.sleep)
    if not callable(clock) or not callable(sleeper):
        raise TypeError("Core candidate startup clock seam is invalid")
    return clock, sleeper


def _startup_time(clock: Any) -> float:
    """Read a valid monotonic timestamp from the private bootstrap seam."""

    value = clock()
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise RuntimeError("Core candidate startup clock returned an invalid timestamp")
    return float(value)


def _startup_timeout(unit: str) -> RuntimeError:
    return RuntimeError(
        f"{unit} MainPID did not reach the exact staged release entrypoint within "
        f"{CORE_EXEC_STARTUP_WAIT_SECONDS:g} seconds"
    )


def _startup_remaining(clock: Any, deadline: float, unit: str) -> float:
    """Return remaining identity budget and reject observations beyond its deadline."""

    remaining = deadline - _startup_time(clock)
    if remaining <= 0:
        raise _startup_timeout(unit)
    return remaining


def _systemd_candidate_identity(
    updater: Any, unit: str, expected_pid: str, *, deadline: float, clock: Any
) -> None:
    """Require the same systemd unit to stay active with its original MainPID."""

    for property_name in ("ActiveState", "MainPID"):
        remaining = _startup_remaining(clock, deadline, unit)
        try:
            completed = updater.runner(
                ["systemctl", "show", f"--property={property_name}", "--value", unit],
                capture_output=True,
                text=True,
                timeout=min(5, remaining),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"Cannot confirm {unit} {property_name} during startup") from error
        _startup_remaining(clock, deadline, unit)
        value = completed.stdout.strip()
        if completed.returncode != 0:
            raise RuntimeError(f"Cannot confirm {unit} {property_name} during startup")
        if property_name == "ActiveState" and value != "active":
            raise RuntimeError(f"{unit} left active state during candidate startup")
        if property_name == "MainPID" and (
            not value.isascii() or not value.isdecimal() or value == "0" or value != expected_pid
        ):
            raise RuntimeError(f"{unit} MainPID changed during candidate startup")


def _proc_executable_matches(pid: str, expected: Path, *, proc_root: Path | None = None) -> bool:
    """Compare proc's live executable path and inode with the signed entrypoint."""

    proc_root = PROC_ROOT if proc_root is None else proc_root
    proc_executable = proc_root / pid / "exe"
    try:
        executable = proc_executable.resolve(strict=True)
        live_info = proc_executable.stat()
        expected_info = expected.stat()
    except OSError:
        return False
    return (
        executable == expected
        and stat.S_ISREG(live_info.st_mode)
        and (live_info.st_dev, live_info.st_ino) == (expected_info.st_dev, expected_info.st_ino)
    )


def _verify_candidate_pid(
    updater: Any,
    item: dict[str, Any],
    pid: str,
    *,
    deadline: float | None = None,
    proc_root: Path | None = None,
) -> None:
    """Wait briefly for exec, then require the exact signed candidate process identity.

    systemd Type=simple can report active while the trusted component runner is still
    validating and execing the staged ELF. Poll only that same active MainPID; never
    treat the runner or another executable as ready.
    中文：只等待原 MainPID 完成 exec，最终仍按签名入口路径、inode 与字节摘要核验。
    """

    if not pid.isascii() or not pid.isdecimal() or pid == "0":
        raise RuntimeError("Core candidate MainPID is invalid")
    component_id = item["componentId"]
    unit = updater.components[component_id]["systemdUnit"]
    expected = _candidate_executable(updater, item)
    clock, sleeper = _candidate_startup_clock(updater)
    if deadline is None:
        deadline = _startup_time(clock) + CORE_EXEC_STARTUP_WAIT_SECONDS
    if not math.isfinite(deadline):
        raise RuntimeError("Core candidate startup deadline is invalid")
    proc_root = PROC_ROOT if proc_root is None else proc_root

    # ── Phase 1: Observe only the original, active systemd MainPID.
    # 第一阶段：仅轮询原始且仍处于 active 的 MainPID。
    while True:
        _systemd_candidate_identity(updater, unit, pid, deadline=deadline, clock=clock)
        if _proc_executable_matches(pid, expected, proc_root=proc_root):
            # Revalidate immutable release bytes and the same process identity at success.
            confirmed = _candidate_executable(updater, item)
            if confirmed != expected:
                raise RuntimeError("Core staged entrypoint path changed during candidate startup")
            _startup_remaining(clock, deadline, unit)
            _systemd_candidate_identity(updater, unit, pid, deadline=deadline, clock=clock)
            if not _proc_executable_matches(pid, confirmed, proc_root=proc_root):
                raise RuntimeError(
                    f"{unit} MainPID changed executable during candidate startup verification"
                )
            _startup_remaining(clock, deadline, unit)
            return

        remaining = _startup_remaining(clock, deadline, unit)
        sleeper(min(CORE_EXEC_STARTUP_POLL_SECONDS, remaining))


def _quiesce_remaining(clock: Any, deadline: float, unit: str) -> float:
    """Return the remaining bounded unit-quiesce budget."""

    remaining = deadline - _startup_time(clock)
    if remaining <= 0:
        raise RuntimeError(f"{unit} did not reach inactive/dead with MainPID 0 in time")
    return remaining


def _systemd_unit_property(
    updater: Any,
    unit: str,
    property_name: str,
    *,
    deadline: float,
    clock: Any,
) -> str:
    """Read one systemd property within the caller's quiesce deadline."""

    timeout = min(5.0, _quiesce_remaining(clock, deadline, unit))
    try:
        completed = updater.runner(
            ["systemctl", "show", f"--property={property_name}", "--value", unit],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Cannot confirm {unit} {property_name} during quiesce") from error
    _quiesce_remaining(clock, deadline, unit)
    if completed.returncode != 0:
        raise RuntimeError(f"Cannot confirm {unit} {property_name} during quiesce")
    return completed.stdout.strip()


def _signed_candidate_unit_source(
    updater: Any, item: dict[str, Any]
) -> tuple[dict[str, Any], str, Path, bytes]:
    """Return the staged unit only when its bytes match the plan's signed file map."""

    component_id = item.get("componentId")
    component = updater.components.get(component_id)
    unit = component.get("systemdUnit") if isinstance(component, dict) else None
    manifest = item.get("manifest")
    artifact = manifest.get("artifact") if isinstance(manifest, dict) else None
    files = artifact.get("files") if isinstance(artifact, dict) else None
    if (
        not isinstance(component_id, str)
        or not isinstance(unit, str)
        or not isinstance(files, dict)
    ):
        raise TypeError("Staged Core candidate has no signed systemd unit identity")
    pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
    source = (
        updater.install_root / "components" / component_id / "releases" / pointer / "systemd" / unit
    )
    try:
        info = source.lstat()
        payload = source.read_bytes()
    except OSError as error:
        raise RuntimeError(
            f"Signed staged systemd unit is unavailable for {component_id}"
        ) from error
    expected = files.get(f"systemd/{unit}")
    if (
        source.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o022
        or expected != _digest(payload)
        or not updater._unit_uses_component_runner(source, component_id)
    ):
        raise RuntimeError(f"Staged systemd unit differs from its signed Core plan: {component_id}")
    return component, unit, source, payload


def _verify_candidate_unit_file(path: Path, expected_bytes: bytes, *, component_id: str) -> bool:
    """Require an installed unit file to be the exact private plan-owned bytes."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RuntimeError(f"Cannot inspect the candidate unit file for {component_id}") from error
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o022
        or path.read_bytes() != expected_bytes
    ):
        raise RuntimeError(f"Loaded systemd unit is not the exact signed plan file: {component_id}")
    return True


def _run_bounded_systemctl(
    updater: Any,
    arguments: list[str],
    *,
    unit: str,
    deadline: float,
    clock: Any,
) -> None:
    """Run a fixed systemctl operation without exceeding the unit deadline."""

    timeout = min(5.0, _quiesce_remaining(clock, deadline, unit))
    try:
        completed = updater.runner(
            ["systemctl", *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Cannot complete systemctl {arguments[0]} for {unit}") from error
    _quiesce_remaining(clock, deadline, unit)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"systemctl {arguments[0]} failed for {unit}: {detail}")


def _prove_candidate_unit_loaded(
    updater: Any,
    item: dict[str, Any],
    *,
    deadline: float,
    clock: Any,
    restore_missing: bool,
) -> str:
    """Bind systemd's loaded unit to the signed release before stopping it.

    The fragment must be the first fixed Cyrene unit path with no drop-ins. If an
    interrupted cleanup unlinked that fragment, restore the signed bytes and
    reload systemd before acting on its cached auto-restart state.
    中文：只操作同一计划签名unit；文件已被旧cleanup unlink时，先还原并重载再停止。
    """

    component, unit, source, payload = _signed_candidate_unit_source(updater, item)
    unit_dirs = getattr(updater, "systemd_unit_dirs", None)
    if not isinstance(unit_dirs, (list, tuple)) or not unit_dirs:
        raise RuntimeError("First-Core quiesce has no fixed systemd unit directory")
    destination = Path(unit_dirs[0]) / unit
    if not destination.is_absolute() or destination.is_symlink():
        raise RuntimeError(f"Candidate systemd unit path is unsafe for {component['componentId']}")

    fragment = _systemd_unit_property(updater, unit, "FragmentPath", deadline=deadline, clock=clock)
    drop_ins = _systemd_unit_property(updater, unit, "DropInPaths", deadline=deadline, clock=clock)
    if fragment != str(destination) or drop_ins:
        raise RuntimeError(
            f"Loaded systemd unit path or drop-ins differ from the signed plan: {unit}"
        )

    installed = _verify_candidate_unit_file(
        destination, payload, component_id=component["componentId"]
    )
    needs_reload = _systemd_unit_property(
        updater, unit, "NeedDaemonReload", deadline=deadline, clock=clock
    )
    if not installed:
        if not restore_missing:
            raise RuntimeError(
                f"Signed candidate unit file is absent for {component['componentId']}"
            )
        _write_unit(updater, component, source.parent.parent)
        _verify_candidate_unit_file(destination, payload, component_id=component["componentId"])
        needs_reload = "yes"
    if needs_reload not in {"yes", "no"}:
        raise RuntimeError(f"systemd reload state is unknown for {unit}")
    if needs_reload == "yes":
        _run_bounded_systemctl(
            updater, ["daemon-reload"], unit=unit, deadline=deadline, clock=clock
        )
        fragment = _systemd_unit_property(
            updater, unit, "FragmentPath", deadline=deadline, clock=clock
        )
        drop_ins = _systemd_unit_property(
            updater, unit, "DropInPaths", deadline=deadline, clock=clock
        )
        needs_reload = _systemd_unit_property(
            updater, unit, "NeedDaemonReload", deadline=deadline, clock=clock
        )
        _verify_candidate_unit_file(destination, payload, component_id=component["componentId"])
    if fragment != str(destination) or drop_ins or needs_reload != "no":
        raise RuntimeError(f"systemd did not load the exact signed unit for {unit}")
    return unit


def _candidate_unit_state(
    updater: Any,
    unit: str,
    *,
    deadline: float,
    clock: Any,
) -> tuple[str, str, str, str]:
    """Read one coherent unit state and both PIDs within the quiesce deadline."""

    timeout = min(5.0, _quiesce_remaining(clock, deadline, unit))
    try:
        completed = updater.runner(
            [
                "systemctl",
                "show",
                "--property=ActiveState,SubState,MainPID,ControlPID",
                "--value",
                unit,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Cannot confirm {unit} state during quiesce") from error
    _quiesce_remaining(clock, deadline, unit)
    if completed.returncode != 0:
        raise RuntimeError(f"Cannot confirm {unit} state during quiesce")
    values = completed.stdout.splitlines()
    if len(values) != 4:
        raise RuntimeError(f"Cannot parse coherent systemd state for {unit}")
    active, substate, main_pid, control_pid = values
    if (
        not main_pid.isascii()
        or not main_pid.isdecimal()
        or not control_pid.isascii()
        or not control_pid.isdecimal()
    ):
        raise RuntimeError(f"Cannot prove systemd PIDs for {unit}")
    return active, substate, main_pid, control_pid


def _wait_candidate_unit_dead(
    updater: Any,
    item: dict[str, Any],
    unit: str,
    *,
    deadline: float,
    clock: Any,
    sleeper: Any,
) -> None:
    """Wait until the exact candidate unit is fully inactive with no owned PID."""

    while True:
        state = _candidate_unit_state(updater, unit, deadline=deadline, clock=clock)
        active, substate, main_pid, control_pid = state
        if (active, substate, main_pid, control_pid) == ("inactive", "dead", "0", "0"):
            return
        if main_pid != "0":
            expected = _candidate_executable(updater, item)
            if not _proc_executable_matches(main_pid, expected):
                raise RuntimeError(f"Refusing an unexpected live MainPID for candidate unit {unit}")
        remaining = _quiesce_remaining(clock, deadline, unit)
        sleeper(min(CORE_UNIT_QUIESCE_POLL_SECONDS, remaining))


def _quiesce_candidate_unit(
    updater: Any,
    item: dict[str, Any],
    *,
    stop_running: bool,
    restore_missing: bool,
    proc_root: Path | None = None,
) -> None:
    """Stop a plan-owned unit, including a PID0 systemd auto-restart, with proof."""

    component = updater.components[item["componentId"]]
    unit = component["systemdUnit"]
    clock, sleeper = _candidate_startup_clock(updater)
    deadline = _startup_time(clock) + CORE_UNIT_QUIESCE_WAIT_SECONDS
    state = _candidate_unit_state(updater, unit, deadline=deadline, clock=clock)
    active, substate, main_pid, control_pid = state
    if (active, substate, main_pid, control_pid) == ("inactive", "dead", "0", "0"):
        return
    if main_pid != "0":
        _verify_candidate_pid(updater, item, main_pid, deadline=deadline, proc_root=proc_root)
        if not stop_running:
            return
    elif (active, substate, main_pid, control_pid) != ("activating", "auto-restart", "0", "0"):
        raise RuntimeError(f"Cannot prove a stoppable PID0 state for candidate unit {unit}")

    _prove_candidate_unit_loaded(
        updater,
        item,
        deadline=deadline,
        clock=clock,
        restore_missing=restore_missing,
    )
    # The loaded unit is now the exact plan file; recheck any live process before stopping it.
    state = _candidate_unit_state(updater, unit, deadline=deadline, clock=clock)
    active, substate, main_pid, control_pid = state
    if main_pid != "0":
        _verify_candidate_pid(updater, item, main_pid, deadline=deadline, proc_root=proc_root)
    elif (active, substate, control_pid) != ("activating", "auto-restart", "0"):
        if (active, substate, main_pid, control_pid) == ("inactive", "dead", "0", "0"):
            return
        raise RuntimeError(f"Candidate unit state changed before bounded stop: {unit}")
    _run_bounded_systemctl(updater, ["stop", unit], unit=unit, deadline=deadline, clock=clock)
    _wait_candidate_unit_dead(updater, item, unit, deadline=deadline, clock=clock, sleeper=sleeper)


def _quiesce_hold_recovery_units(
    updater: Any, components: list[dict[str, Any]], *, proc_root: Path | None = None
) -> None:
    """Quiesce only orphaned PID0 auto-restarts before rebuilding a held plan."""

    cohort = _validate_staged_cohort(updater, components)
    by_id = {item["componentId"]: item for item in components}
    for component_id in reversed(cohort):
        _quiesce_candidate_unit(
            updater,
            by_id[component_id],
            stop_running=False,
            restore_missing=True,
            proc_root=proc_root,
        )


def _stop_candidate_services(updater: Any, components: list[dict[str, Any]]) -> None:
    """Stop only exact plan-owned unit PIDs, in reverse dependency order."""

    cohort = _validate_staged_cohort(updater, components)
    by_id = {item["componentId"]: item for item in components}
    for component_id in reversed(cohort):
        _quiesce_candidate_unit(
            updater,
            by_id[component_id],
            stop_running=True,
            restore_missing=False,
        )


def _remove_verified_broker_units(
    updater: Any, manifest: dict[str, Any], *, allow_missing: bool = False
) -> None:
    """Remove only installed unit files matching the active signed Broker release."""

    component_id = "cyrene-runtime-maintenance"
    component = updater.components[component_id]
    unit = component["systemdUnit"]
    pointer = f"{manifest['version']}--{manifest['manifestDigest'].removeprefix('sha256:')}"
    source = updater.install_root / "components" / component_id / "releases" / pointer
    expected = source / "systemd" / unit
    if expected.is_symlink() or not expected.is_file():
        raise ValueError("Signed maintenance Broker release unit is unavailable")
    expected_bytes = expected.read_bytes()
    removed: set[tuple[int, int, str]] = set()
    for directory in updater.systemd_unit_dirs:
        path = Path(directory) / unit
        if not path.exists() and not path.is_symlink():
            continue
        info = path.lstat()
        if path.is_symlink():
            raise ValueError("Refusing to remove a symlinked maintenance Broker unit")
        identity = (info.st_dev, info.st_ino, str(path.resolve(strict=True)))
        if identity in removed:
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o022
            or path.read_bytes() != expected_bytes
            or not updater._unit_uses_component_runner(path, component_id)
        ):
            raise ValueError("Refusing to remove a Broker unit outside its signed release")
        path.unlink()
        updater._fsync_directory(path.parent)
        removed.add(identity)
    if not removed and not allow_missing:
        raise ValueError("Signed maintenance Broker unit disappeared before replacement")


def _stop_initial_c10_broker(updater: Any, transaction: dict[str, Any], journal_path: Path) -> None:
    """Stop the verified authority Broker only after its hold token is journaled."""

    original = transaction.get("bootstrapBroker")
    if not isinstance(original, dict):
        return
    component_id = "cyrene-runtime-maintenance"
    component = updater.components[component_id]
    candidate = next(
        item for item in transaction["components"] if item["componentId"] == component_id
    )
    candidate_pointer = (
        f"{candidate['version']}--{candidate['manifestDigest'].removeprefix('sha256:')}"
    )
    current_pointer = updater._active_native_pointer_identity(component_id)
    if current_pointer == candidate_pointer:
        return
    if current_pointer != original.get("pointerIdentity"):
        raise ValueError("Maintenance Broker pointer changed outside this first-Core plan")
    installed = updater._installed(component)
    manifest = _validate_c10_broker_manifest(updater, installed)
    if (
        installed.get("pointerIdentity") != original.get("pointerIdentity")
        or manifest.get("manifestDigest") != original.get("manifestDigest")
        or installed.get("artifactDigest") != original.get("artifactDigest")
    ):
        raise ValueError("Initial C10 Broker no longer matches the checked signed identity")

    phase = transaction.get("brokerRestartPhase")
    if phase != "stopped":
        if phase is None:
            transaction["brokerRestartPhase"] = "stop_pending"
            _write_private_json(updater, journal_path, transaction)
        if phase != "stop_pending" and phase is not None:
            raise ValueError("C10 Broker restart journal phase is unknown; keep the hold closed")
        state = updater.runner(
            ["systemctl", "show", "--property=MainPID", "--value", component["systemdUnit"]],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if state.returncode != 0 or not state.stdout.strip().isdecimal():
            raise RuntimeError("Cannot prove the current maintenance Broker PID")
        pid = state.stdout.strip()
        if pid != "0":
            current = _verified_running_c10_broker(updater, PROC_ROOT)
            if current != original:
                raise ValueError(
                    "Running maintenance Broker differs from its checked process identity"
                )
            updater._run_systemctl("stop", component["systemdUnit"])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = updater.runner(
                ["systemctl", "show", "--property=MainPID", "--value", component["systemdUnit"]],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            active = updater.runner(
                [
                    "systemctl",
                    "show",
                    "--property=ActiveState",
                    "--value",
                    component["systemdUnit"],
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if (
                state.returncode == 0
                and state.stdout.strip() == "0"
                and active.returncode == 0
                and active.stdout.strip() == "inactive"
            ):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Initial maintenance Broker did not stop under the held plan")
        remaining = _core_process_snapshot(PROC_ROOT, executable_names=frozenset({component_id}))
        if remaining:
            raise RuntimeError("Maintenance Broker process remains after its unit stopped")
        _remove_verified_broker_units(updater, manifest, allow_missing=phase == "stop_pending")
        transaction["brokerRestartPhase"] = "stopped"
        _write_private_json(updater, journal_path, transaction)
    else:
        state = updater.runner(
            ["systemctl", "show", "--property=MainPID", "--value", component["systemdUnit"]],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        active = updater.runner(
            ["systemctl", "show", "--property=ActiveState", "--value", component["systemdUnit"]],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        candidate_release = (
            updater.install_root / "components" / component_id / "releases" / candidate_pointer
        )
        candidate_unit = candidate_release / "systemd" / component["systemdUnit"]
        if candidate_unit.is_symlink() or not candidate_unit.is_file():
            raise ValueError("Staged C10 Broker unit is unavailable during restart recovery")
        unit_paths = [
            Path(directory) / component["systemdUnit"] for directory in updater.systemd_unit_dirs
        ]
        if (
            state.returncode != 0
            or state.stdout.strip() != "0"
            or active.returncode != 0
            or active.stdout.strip() != "inactive"
            or any(
                path.is_symlink()
                or (path.exists() and path.read_bytes() != candidate_unit.read_bytes())
                for path in unit_paths
            )
        ):
            raise RuntimeError("Journaled C10 Broker stop state no longer matches the host")


def _restore_initial_c10_broker(updater: Any, transaction: dict[str, Any]) -> None:
    """Restore the exact signed authority Broker while preserving the closed hold."""

    original = transaction.get("bootstrapBroker")
    if not isinstance(original, dict):
        return
    component_id = "cyrene-runtime-maintenance"
    component = updater.components[component_id]
    prior = next(item for item in transaction["previous"] if item["componentId"] == component_id)
    pointer = original["pointerIdentity"]
    current = updater._active_native_pointer_identity(component_id)
    if current != pointer:
        candidate_pointer = next(
            item["version"] + "--" + item["manifestDigest"].removeprefix("sha256:")
            for item in transaction["components"]
            if item["componentId"] == component_id
        )
        if current not in {None, candidate_pointer}:
            raise ValueError(
                "Cannot restore the original C10 Broker over an unknown active pointer"
            )
        updater._activate_native(component_id, pointer, expected_current=current)
    updater._restore_active_receipt(component_id, prior)
    receipt = updater._read_release_receipt(component_id, prior["releaseIdentity"])
    if not isinstance(receipt, dict) or receipt.get("manifestDigest") != original["manifestDigest"]:
        raise ValueError("Original C10 Broker receipt is unavailable for restoration")
    manifest = receipt["manifest"]
    release = updater.install_root / "components" / component_id / "releases" / pointer
    _remove_verified_broker_units(updater, manifest, allow_missing=True)
    _write_unit(updater, component, release)
    updater.runner(
        ["systemctl", "daemon-reload"], capture_output=True, text=True, timeout=30, check=False
    )
    updater._run_systemctl("start", component["systemdUnit"])
    updater._wait_unit_active(component["systemdUnit"])
    _verify_started_processes(
        updater,
        [
            {
                "componentId": component_id,
                "version": manifest["version"],
                "manifestDigest": manifest["manifestDigest"],
                "manifest": manifest,
            }
        ],
    )


def _activate_core_cohort(updater: Any, transaction: dict[str, Any]) -> None:
    """Activate only plan-bound pointers, including the exact initial Broker update."""

    previous_by_id = {entry["componentId"]: entry for entry in transaction["previous"]}
    initial_broker = transaction.get("bootstrapBroker")
    for item in transaction["components"]:
        component_id = item["componentId"]
        pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
        current = updater._active_native_pointer_identity(component_id)
        previous = previous_by_id[component_id]
        allowed_previous_broker = (
            component_id == "cyrene-runtime-maintenance"
            and isinstance(initial_broker, dict)
            and current == initial_broker.get("pointerIdentity")
            and previous.get("pointerIdentity") == current
            and previous.get("identityAttested") is True
        )
        if current not in {None, pointer} and not allowed_previous_broker:
            raise ValueError(
                f"Core pointer changed outside this bootstrap transaction: {component_id}"
            )
        if current is None or allowed_previous_broker:
            updater._activate_transaction(
                {**transaction, "components": [item], "previous": [previous]}
            )
        else:
            updater._write_active_receipt(item)


def _start_core_components(updater: Any, components: list[dict[str, Any]]) -> None:
    """Start the exact legacy or C10 first-Core cohort in its fixed dependency order."""

    cohort = _validate_staged_cohort(updater, components)
    by_id = {item["componentId"]: item for item in components}
    ordered = [by_id[component_id] for component_id in cohort]
    for item in ordered:
        unit = updater.components[item["componentId"]]["systemdUnit"]
        updater._run_systemctl("start", unit)
        updater._wait_unit_active(unit)
        _verify_started_processes(updater, [item])


def _remove_candidate_pointers_and_units(updater: Any, components: list[dict[str, Any]]) -> None:
    """Remove only pointers and units proven to belong to this staged cohort."""

    for item in reversed(components):
        component_id = item["componentId"]
        component = updater.components[component_id]
        pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
        current = updater._active_native_pointer_identity(component_id)
        if current == pointer:
            updater._activate_native(component_id, None, expected_current=pointer)
            updater._clear_active_receipt(component_id)
        unit_path = next(
            (
                Path(directory) / component["systemdUnit"]
                for directory in updater.systemd_unit_dirs
                if (Path(directory) / component["systemdUnit"]).exists()
            ),
            None,
        )
        if unit_path is not None and not unit_path.is_symlink():
            release = updater.install_root / "components" / component_id / "releases" / pointer
            source = release / "systemd" / component["systemdUnit"]
            if source.is_file() and unit_path.read_bytes() == source.read_bytes():
                unit_path.unlink()
                updater._fsync_directory(unit_path.parent)


def _core_bootstrap_begin_request(plan: dict[str, Any]) -> dict[str, Any]:
    """Build the exact idempotency payload persisted with a Core bootstrap plan."""

    return {
        "request_id": plan["requestId"],
        "target_kind": "CORE_RUNTIME",
        "requires_restart": True,
        "user_confirmed_restart": True,
        "expected_gate_generation": plan["gateGeneration"],
        "expected_catalog_generation": plan["catalogGeneration"],
        "expected_activity_sources": plan["activitySources"],
        "plan_id": plan["planId"],
        "plan_digest": plan["planDigest"],
        "component_artifact_digests": plan["componentArtifactDigests"],
    }


def _verify_held_core_bootstrap(
    updater: Any, plan: dict[str, Any], transaction: dict[str, Any]
) -> None:
    """Replay the exact BeginCoreBootstrap request and require its existing hold."""

    request = transaction.get("beginRequest")
    if (
        transaction.get("phase") != "hold_required"
        or transaction.get("requestId") != plan.get("requestId")
        or transaction.get("planId") != plan.get("planId")
        or transaction.get("planDigest") != plan.get("planDigest")
        or request != _core_bootstrap_begin_request(plan)
    ):
        raise RuntimeError("Held first-Core journal does not match the exact staged plan")
    token = transaction.get("maintenanceToken")
    gate_generation = transaction.get("maintenanceGateGeneration")
    if (
        not isinstance(token, str)
        or not token.strip()
        or not isinstance(gate_generation, int)
        or isinstance(gate_generation, bool)
    ):
        raise RuntimeError("Held first-Core journal has no exact maintenance hold identity")

    response = updater._broker_request("BeginCoreBootstrap", request, request_id=plan["requestId"])
    if (
        response.get("status") != "MAINTENANCE_ACTIVE"
        or response.get("maintenance_origin") != "CORE_BOOTSTRAP"
        or response.get("readiness_claimed") is not False
        or response.get("held") is not True
        or response.get("maintenance_token") != token
        or response.get("gate_generation") != gate_generation
        or isinstance(response.get("gate_generation"), bool)
    ):
        raise RuntimeError("Broker did not confirm the same active first-Core maintenance hold")


def _persist_successor_transaction(
    updater: Any,
    journal_path: Path,
    parent_plan: dict[str, Any],
    plan: dict[str, Any],
    transaction: dict[str, Any],
) -> None:
    """Persist child progress under the parent without rewriting its authority record."""

    current = _read_private_json(journal_path)
    if not isinstance(current, dict):
        raise TypeError("Held parent journal disappeared during successor apply")
    immutable = ("planId", "planDigest", "requestId", "componentArtifactDigests")
    if any(current.get(key) != parent_plan.get(key) for key in immutable) or current.get(
        "beginRequest"
    ) != _core_bootstrap_begin_request(parent_plan):
        raise ValueError("Original first-Core authority changed during successor apply")
    attempts = current.get("successorAttempts")
    if not isinstance(attempts, list) or len(attempts) != 1:
        raise ValueError("Held parent no longer contains its unique successor attempt")
    attempt = attempts[0]
    if (
        not isinstance(attempt, dict)
        or attempt.get("planId") != plan["planId"]
        or attempt.get("planDigest") != plan["planDigest"]
        or attempt.get("successorBinding") != plan["successorBinding"]
    ):
        raise ValueError("Successor attempt identity changed during apply")
    saved_transaction = dict(transaction)
    saved_transaction.pop("maintenanceToken", None)
    attempt["transaction"] = saved_transaction
    attempt["phase"] = transaction["phase"]
    if transaction.get("gateReleaseConfirmed") is True:
        current["gateReleaseConfirmed"] = True
        current["phase"] = "successor_end_confirmed"
        current["successorReleaseProof"] = {
            "planId": plan["planId"],
            "planDigest": plan["planDigest"],
            "requestId": transaction["endRequestId"],
            "maintenanceGateGeneration": current["maintenanceGateGeneration"],
            "unlocked": True,
            "status": transaction.get("endResultStatus", "UNKNOWN"),
        }
    if transaction.get("phase") == "succeeded":
        current["phase"] = "successor_succeeded"
    _write_private_json(updater, journal_path, current)


def _apply_held_successor(
    updater: Any,
    plan: dict[str, Any],
    confirmation: Any,
    selector_value: Any,
    *,
    channel: Any,
    proc_root: Path,
) -> dict[str, Any]:
    """Apply a checked successor using the original durable hold and End authority."""

    successor = _first_core_successor_module()
    selector = successor.validate_selector(selector_value)
    if selector != successor.selector_for_plan(plan):
        raise ValueError("Apply selector differs from the digest-bound successor parent")
    if plan.get("includeProducts") is not False:
        raise ValueError("Held successor apply supports only the four-member C9 Core cohort")
    expected_confirmation = successor.confirmation(plan)
    if "bootstrapBroker" in plan:
        expected_confirmation["bootstrapBroker"] = plan["bootstrapBroker"]
    if confirmation != expected_confirmation:
        raise ValueError(
            "Apply confirmation must bind the exact held successor and full artifact map"
        )
    if channel is not None and updater._resolve_channel(channel) != plan["channel"]:
        raise ValueError("Apply channel differs from the staged held successor plan")
    if not _is_root():
        raise PermissionError("Held successor activation requires the root-authorized updater")

    journal_path = _journal_path(updater)
    with updater._exclusive_update_lock():
        parent = _load_plan(updater, selector["parentPlanId"], selector["parentPlanDigest"])
        parent_transaction = _read_private_json(journal_path)
        if not isinstance(parent_transaction, dict):
            raise TypeError("Held successor parent journal is unavailable")
        parent_phase = parent_transaction.get("phase")
        if parent_phase not in {"hold_required", "successor_end_confirmed", "successor_succeeded"}:
            raise ValueError("Held successor parent journal is in an unsupported phase")
        if (
            parent.get("includeProducts", False) is not False
            or "firstProducts" in parent
            or parent_transaction.get("planId") != parent["planId"]
            or parent_transaction.get("planDigest") != parent["planDigest"]
            or parent_transaction.get("requestId") != parent["requestId"]
            or parent_transaction.get("componentArtifactDigests")
            != parent["componentArtifactDigests"]
            or parent_transaction.get("beginRequest") != _core_bootstrap_begin_request(parent)
            or parent_transaction.get("maintenanceGateGeneration")
            != selector["maintenanceGateGeneration"]
            or not isinstance(parent_transaction.get("maintenanceToken"), str)
            or not parent_transaction["maintenanceToken"].strip()
            or not successor._selector_matches_parent(
                selector,
                parent,
                parent_transaction.get("maintenanceGateGeneration"),
            )
        ):
            raise ValueError("Original held parent identity or Begin authority changed")
        attempts = parent_transaction.get("successorAttempts")
        if not isinstance(attempts, list) or len(attempts) != 1:
            raise ValueError("The exact successor attempt is not recorded under its parent")
        attempt = attempts[0]
        if (
            not isinstance(attempt, dict)
            or attempt.get("planId") != plan["planId"]
            or attempt.get("planDigest") != plan["planDigest"]
            or attempt.get("successorBinding") != plan["successorBinding"]
            or attempt.get("componentArtifactDigests") != plan["componentArtifactDigests"]
        ):
            raise ValueError("The exact signed successor is not the parent's selected attempt")
        staged_path = Path(updater.state_root) / "staged" / plan["planId"] / "stage.json"
        staged = _read_private_json(staged_path)
        if (
            not isinstance(staged, dict)
            or staged.get("phase") != "staged"
            or staged.get("plan") != {**plan, "phase": "staged"}
            or staged.get("successorBinding") != plan["successorBinding"]
        ):
            raise ValueError("The exact confirmed held successor is not staged")
        base_path = Path(updater.state_root) / "staged" / plan["basePlanId"] / "stage.json"
        base_record = _read_private_json(base_path)
        if not isinstance(base_record, dict):
            raise TypeError("Signed successor Core artifact stage is missing")
        updater._validate_staged_record(
            base_record,
            expected_plan_id=plan["basePlanId"],
            expected_plan_digest=plan["basePlanDigest"],
        )
        components = base_record["components"]
        cohort = _validate_staged_cohort(updater, components)
        if cohort != CORE_COMPONENT_IDS or {
            item.get("componentId") for item in plan["components"]
        } != set(CORE_COMPONENT_IDS):
            raise ValueError("Held successor is not the exact legacy C9 four-component cohort")
        digests = {
            item.get("componentId"): item.get("artifactDigest")
            for item in components
            if isinstance(item, dict)
        }
        if digests != plan["componentArtifactDigests"]:
            raise ValueError("Staged successor artifact map differs from explicit confirmation")
        planned_by_id = {
            item.get("componentId"): item
            for item in plan.get("components", [])
            if isinstance(item, dict)
        }
        for item in components:
            component_id = item.get("componentId")
            planned = planned_by_id.get(component_id)
            manifest = item.get("manifest")
            source = manifest.get("source") if isinstance(manifest, dict) else None
            if (
                not isinstance(planned, dict)
                or not isinstance(manifest, dict)
                or item.get("version") != planned.get("version")
                or item.get("manifestDigest") != planned.get("manifestDigest")
                or item.get("artifactDigest") != planned.get("artifactDigest")
                or manifest.get("releaseId") != planned.get("releaseId")
                or not isinstance(source, dict)
                or source.get("commit") != planned.get("sourceCommit")
            ):
                raise ValueError(
                    f"Staged signed manifest differs from successor selection: {component_id}"
                )

        transaction = attempt.get("transaction")
        if transaction is None:
            if parent_transaction.get("gateReleaseConfirmed") is True:
                raise ValueError("Released hold has no successor transaction to finalize")
            transaction = {
                "schemaVersion": 1,
                "requestId": plan["requestId"],
                "planId": plan["planId"],
                "planDigest": plan["planDigest"],
                "targetKind": "CORE_RUNTIME",
                "expectedGateGeneration": parent["gateGeneration"],
                "expectedCatalogGeneration": parent["catalogGeneration"],
                "expectedActivitySources": parent["activitySources"],
                "componentArtifactDigests": plan["componentArtifactDigests"],
                **(
                    {"bootstrapBroker": plan["bootstrapBroker"]}
                    if "bootstrapBroker" in plan
                    else {}
                ),
                "includeProducts": False,
                "components": components,
                "previous": updater._capture_active_versions(components),
                "successorBinding": plan["successorBinding"],
                "phase": "hold_required",
                "createdAt": int(time.time()),
            }
        else:
            if not isinstance(transaction, dict):
                raise TypeError("Successor transaction journal is malformed")
            for key, expected in {
                "requestId": plan["requestId"],
                "planId": plan["planId"],
                "planDigest": plan["planDigest"],
                "targetKind": "CORE_RUNTIME",
                "expectedGateGeneration": parent["gateGeneration"],
                "expectedCatalogGeneration": parent["catalogGeneration"],
                "expectedActivitySources": parent["activitySources"],
                "componentArtifactDigests": plan["componentArtifactDigests"],
                "components": components,
                "successorBinding": plan["successorBinding"],
            }.items():
                if transaction.get(key) != expected:
                    raise ValueError(f"Held successor transaction changed immutable field {key}")
            if transaction.get("maintenanceToken") not in {
                None,
                parent_transaction["maintenanceToken"],
            }:
                raise ValueError("Held successor transaction has a different authority token")
        transaction["maintenanceToken"] = parent_transaction["maintenanceToken"]
        transaction["components"] = components

        def persist() -> None:
            _persist_successor_transaction(updater, journal_path, parent, plan, transaction)

        if transaction.get("phase") == "succeeded":
            return {
                "status": "installed",
                "planId": plan["planId"],
                "planDigest": plan["planDigest"],
                "postEndReadiness": transaction.get("postEndReadiness", "UNKNOWN"),
            }
        if transaction.get("phase") == "end_confirmed":
            transaction["postEndReadiness"] = "UNKNOWN"
            try:
                post_end = updater._readiness_for("CORE_RUNTIME", requires_restart=True, force=True)
                transaction["postEndReadiness"] = post_end.get("status", "UNKNOWN")
            except Exception as error:  # noqa: BLE001 - End is already confirmed.
                transaction["postEndReadinessError"] = str(error)[:300]
            transaction["phase"] = "succeeded"
            persist()
            return {
                "status": "installed",
                "planId": plan["planId"],
                "planDigest": plan["planDigest"],
                "postEndReadiness": transaction["postEndReadiness"],
            }

        authority_released = parent_transaction.get("gateReleaseConfirmed") is True
        if transaction.get("phase") not in {"end_call_pending", "end_confirmed", "succeeded"}:
            if authority_released:
                raise ValueError("Parent records released admission without successor End proof")
            _verify_held_core_bootstrap(updater, parent, parent_transaction)
            snapshot = _health_snapshot(updater, require_eligible=False)
            if (
                snapshot["gateGeneration"] != selector["maintenanceGateGeneration"]
                or snapshot["catalogGeneration"] != plan["catalogGeneration"]
                or snapshot["activitySources"] != plan["activitySources"]
            ):
                raise RuntimeError("Live Broker changed while the held successor was pending")
            try:
                _assert_fresh(
                    updater,
                    proc_root=proc_root,
                    planned_components=components,
                    expected_bootstrap_broker=plan.get("bootstrapBroker"),
                )
                if transaction.get("phase") in {
                    "hold_required",
                    "installing",
                    "starting",
                    "started",
                }:
                    _quiesce_hold_recovery_units(updater, components, proc_root=proc_root)
            except Exception as error:
                transaction["failure"] = str(error)[:500]
                transaction["phase"] = "hold_required"
                persist()
                raise RuntimeError(
                    "Successor recovery identity is not proven; the original admission hold remains closed"
                ) from error
            if transaction.get("phase") == "hold_required":
                transaction["phase"] = "installing"
                persist()

        if transaction.get("phase") not in {
            "installing",
            "starting",
            "started",
            "end_pending",
            "end_call_pending",
        }:
            raise ValueError("Held successor transaction has an unsupported phase")

        try:
            if transaction.get("phase") == "installing":
                _stop_initial_c10_broker(updater, transaction, journal_path)
                for item in components:
                    component = updater.components[item["componentId"]]
                    pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
                    release = (
                        updater.install_root
                        / "components"
                        / item["componentId"]
                        / "releases"
                        / pointer
                    )
                    _write_unit(updater, component, release)
                _activate_core_cohort(updater, transaction)
                completed = updater.runner(
                    ["systemctl", "daemon-reload"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if completed.returncode != 0:
                    raise RuntimeError("systemd daemon-reload failed")
                transaction["phase"] = "starting"
                persist()
            if transaction.get("phase") in {"starting", "started"}:
                _start_core_components(updater, components)
                updater._health_transaction(transaction)
                transaction["phase"] = "end_pending"
                persist()
        except Exception as error:
            try:
                _stop_candidate_services(updater, components)
                _remove_candidate_pointers_and_units(updater, components)
            except Exception as cleanup_error:  # noqa: BLE001 - never release an unclear hold.
                transaction["cleanupError"] = str(cleanup_error)[:300]
            transaction["failure"] = str(error)[:500]
            transaction["phase"] = "hold_required"
            persist()
            raise RuntimeError(
                f"Held successor failed; original admission hold remains closed: {error}"
            ) from error

        try:
            _verify_live_core_cohort(updater, transaction, plan)
            _require_core_ready(updater, plan)
        except Exception as error:
            if transaction.get("phase") == "end_call_pending":
                transaction["endRecoveryError"] = str(error)[:500]
                persist()
                raise RuntimeError(
                    "Successor End outcome is uncertain and live Core proof failed; retain candidate and hold"
                ) from error
            try:
                _rollback_pre_end(updater, plan, transaction, components)
            except Exception as cleanup_error:  # noqa: BLE001 - retain all evidence under the hold.
                transaction["cleanupError"] = str(cleanup_error)[:300]
            transaction["failure"] = str(error)[:500]
            transaction["phase"] = "hold_required"
            persist()
            raise RuntimeError(
                f"Successor health proof failed; original admission hold remains closed: {error}"
            ) from error

        if transaction.get("phase") != "end_call_pending":
            transaction["phase"] = "end_call_pending"
            transaction["endRequestId"] = parent["requestId"]
            persist()
        end_request_id = "bootstrap-end-" + parent["planDigest"].split(":", 1)[1][:32]
        try:
            end_result = updater._broker_request(
                "EndMaintenance",
                {
                    "request_id": parent["requestId"],
                    "target_kind": "CORE_RUNTIME",
                    "maintenance_token": parent_transaction["maintenanceToken"],
                    "outcome": "SUCCESS",
                    "healthy": True,
                },
                request_id=end_request_id,
            )
        except Exception as error:
            transaction["endError"] = str(error)[:300]
            persist()
            raise RuntimeError(
                "Successor EndMaintenance result is uncertain; retry this same plan and original authority"
            ) from error
        if end_result.get("unlocked") is not True:
            transaction["endError"] = "Platform did not confirm the durable gate release"
            persist()
            raise RuntimeError(
                "Platform did not confirm successor gate release; hold remains recorded"
            )
        transaction["phase"] = "end_confirmed"
        transaction["gateReleaseConfirmed"] = True
        transaction["endRequestId"] = parent["requestId"]
        transaction["endResultStatus"] = end_result.get("status", "UNKNOWN")
        transaction["endGateGeneration"] = end_result.get("gate_generation")
        persist()
        transaction["postEndReadiness"] = "UNKNOWN"
        try:
            post_end = updater._readiness_for("CORE_RUNTIME", requires_restart=True, force=True)
            transaction["postEndReadiness"] = post_end.get("status", "UNKNOWN")
        except Exception as error:  # noqa: BLE001 - admission release is already proven.
            transaction["postEndReadinessError"] = str(error)[:300]
        transaction["phase"] = "succeeded"
        persist()
        return {
            "status": "installed",
            "planId": plan["planId"],
            "planDigest": plan["planDigest"],
            "components": list(cohort),
            "postEndReadiness": transaction["postEndReadiness"],
        }


def apply(
    updater: Any,
    plan_id: Any,
    plan_digest: Any,
    confirmation: Any,
    *,
    channel: Any = None,
    proc_root: Path = PROC_ROOT,
    held_recovery: Any = None,
) -> dict[str, Any]:
    """Install the confirmed Core cohort under a durable hold, preserving it on failure."""

    plan = _load_plan(updater, plan_id, plan_digest)
    if "successorBinding" in plan:
        if held_recovery is None:
            raise ValueError("Held successor apply requires its explicit parent selector")
        return _apply_held_successor(
            updater,
            plan,
            confirmation,
            held_recovery,
            channel=channel,
            proc_root=proc_root,
        )
    if held_recovery is not None:
        raise ValueError("heldRecovery cannot authorize an ordinary first-Core plan")
    expected_confirmation = {
        "mode": CORE_BOOTSTRAP_MODE,
        "planId": plan_id,
        "planDigest": plan_digest,
        "componentArtifactDigests": plan["componentArtifactDigests"],
        "catalogGeneration": plan["catalogGeneration"],
        "gateGeneration": plan["gateGeneration"],
        "confirmed": True,
    }
    if plan.get("includeProducts") is True:
        expected_confirmation["includeProducts"] = True
        expected_confirmation["firstProducts"] = plan["firstProducts"]
    if "bootstrapBroker" in plan:
        expected_confirmation["bootstrapBroker"] = plan["bootstrapBroker"]
    if confirmation != expected_confirmation:
        raise ValueError(
            "Apply confirmation must bind the exact first-Core plan and full artifact cohort"
        )
    if channel is not None and updater._resolve_channel(channel) != plan["channel"]:
        raise ValueError("Apply channel differs from the staged first-Core plan")
    if not _is_root():
        raise PermissionError("First-Core activation requires the root-authorized updater")
    journal_path = _journal_path(updater)
    with updater._exclusive_update_lock():
        staged_path = Path(updater.state_root) / "staged" / plan_id / "stage.json"
        staged = _read_private_json(staged_path)
        if (
            staged is None
            or staged.get("phase") != "staged"
            or staged.get("plan") != {**plan, "phase": "staged"}
        ):
            raise ValueError("The exact confirmed Core bootstrap plan is not staged")
        if plan.get("includeProducts") is True:
            product_stage = staged.get("firstProductsStage")
            if (
                not isinstance(product_stage, dict)
                or product_stage.get("receiptDigest") != plan["firstProducts"]["receiptDigest"]
                or product_stage.get("products") != plan["firstProducts"]["products"]
            ):
                raise ValueError("The exact confirmed Product cohort is not staged")
        base_path = Path(updater.state_root) / "staged" / plan["basePlanId"] / "stage.json"
        base_record = _read_private_json(base_path)
        if base_record is None:
            raise ValueError("Verified Core artifact stage is missing")
        updater._validate_staged_record(
            base_record,
            expected_plan_id=plan["basePlanId"],
            expected_plan_digest=plan["basePlanDigest"],
        )
        components = base_record["components"]
        cohort = _validate_staged_cohort(updater, components)
        if {item.get("componentId") for item in plan["components"]} != set(cohort):
            raise ValueError("Staged component cohort differs from the confirmed first-Core plan")
        transaction = {
            "schemaVersion": 1,
            "requestId": plan["requestId"],
            "planId": plan_id,
            "planDigest": plan_digest,
            "targetKind": "CORE_RUNTIME",
            "expectedGateGeneration": plan["gateGeneration"],
            "expectedCatalogGeneration": plan["catalogGeneration"],
            "expectedActivitySources": plan["activitySources"],
            "componentArtifactDigests": plan["componentArtifactDigests"],
            **({"bootstrapBroker": plan["bootstrapBroker"]} if "bootstrapBroker" in plan else {}),
            "includeProducts": plan.get("includeProducts", False),
            **(
                {
                    "firstProducts": {
                        "schemaVersion": 1,
                        "receiptDigest": plan["firstProducts"]["receiptDigest"],
                        "products": plan["firstProducts"]["products"],
                        "phase": "staged",
                    }
                }
                if plan.get("includeProducts") is True
                else {}
            ),
            "components": components,
            "previous": updater._capture_active_versions(components),
            "beginRequest": _core_bootstrap_begin_request(plan),
            "phase": "begin_pending",
            "createdAt": int(time.time()),
        }
        existing = _read_private_json(journal_path)
        if existing is not None and existing.get("successorAttempts"):
            raise ValueError(
                "An explicitly selected held successor owns recovery for the original first-Core hold"
            )
        recovering_hold_required = existing is not None and existing.get("phase") == "hold_required"
        if existing is not None:
            if (
                existing.get("planDigest") != plan_digest
                or existing.get("requestId") != plan["requestId"]
            ):
                raise ValueError(
                    "A different interrupted first-Core bootstrap keeps the gate closed"
                )
            for key in (
                "requestId",
                "planId",
                "planDigest",
                "targetKind",
                "expectedGateGeneration",
                "expectedCatalogGeneration",
                "expectedActivitySources",
                "componentArtifactDigests",
                "bootstrapBroker",
                "beginRequest",
                "components",
            ):
                if existing.get(key) != transaction.get(key):
                    raise ValueError(
                        f"Interrupted first-Core journal changed immutable field {key}"
                    )
            if (
                "includeProducts" in plan
                and existing.get("includeProducts", False) != plan["includeProducts"]
            ):
                raise ValueError("Interrupted first-Core journal changed Product selection")
            if plan.get("includeProducts") is True:
                previous_products = existing.get("firstProducts")
                expected_products = transaction["firstProducts"]
                if (
                    not isinstance(previous_products, dict)
                    or previous_products.get("receiptDigest") != expected_products["receiptDigest"]
                    or previous_products.get("products") != expected_products["products"]
                ):
                    raise ValueError("Interrupted Product journal changed its immutable identity")
            elif "firstProducts" in existing:
                raise ValueError("Core-only journal unexpectedly contains Product ownership")
            if existing.get("phase") == "succeeded":
                if plan.get("includeProducts") is True and (
                    not isinstance(existing.get("postEndResult"), dict)
                    or existing["postEndResult"].get("status") != "complete"
                ):
                    raise ValueError("Completed Product bootstrap journal lacks completion proof")
                return {
                    "status": "installed",
                    "planId": plan_id,
                    "planDigest": plan_digest,
                    "postEndReadiness": existing.get("postEndReadiness", "UNKNOWN"),
                    **(
                        {"firstProducts": existing["postEndResult"]}
                        if plan.get("includeProducts") is True
                        else {}
                    ),
                }
            if existing.get("phase") == "post_end_pending" and plan.get("includeProducts") is True:
                return _resume_first_products_post_end(updater, plan, existing, journal_path)
            if existing.get("phase") == "end_confirmed":
                if existing.get("gateReleaseConfirmed") is not True:
                    raise ValueError("End-confirmed journal has no durable unlock proof")
                if plan.get("includeProducts") is True:
                    existing["phase"] = "post_end_pending"
                    _write_private_json(updater, journal_path, existing)
                    return _resume_first_products_post_end(updater, plan, existing, journal_path)
                try:
                    post_end = updater._readiness_for(
                        "CORE_RUNTIME", requires_restart=True, force=True
                    )
                    existing["postEndReadiness"] = post_end.get("status", "UNKNOWN")
                except Exception as error:  # noqa: BLE001 - gate release is already confirmed.
                    existing["postEndReadiness"] = "UNKNOWN"
                    existing["postEndReadinessError"] = str(error)[:300]
                existing["phase"] = "succeeded"
                _write_private_json(updater, journal_path, existing)
                return {
                    "status": "installed",
                    "planId": plan_id,
                    "planDigest": plan_digest,
                    "postEndReadiness": existing["postEndReadiness"],
                }
            initial_broker = existing.get("bootstrapBroker")
            unexpected_previous = [
                item
                for item in existing.get("previous", [])
                if item.get("pointerIdentity") is not None
                and not (
                    item.get("componentId") == "cyrene-runtime-maintenance"
                    and isinstance(initial_broker, dict)
                    and item.get("pointerIdentity") == initial_broker.get("pointerIdentity")
                    and item.get("identityAttested") is True
                )
            ]
            if unexpected_previous:
                raise ValueError("First-Core journal records an unrelated old Core pointer")
            if not isinstance(existing.get("maintenanceToken"), str) and existing.get(
                "phase"
            ) not in {"begin_pending", "end_confirmed"}:
                raise ValueError("Interrupted first-Core journal has no durable hold token")
            transaction = existing
        if existing is None or existing.get("phase") not in {
            "end_call_pending",
            "end_confirmed",
            "succeeded",
        }:
            _assert_fresh(
                updater,
                proc_root=proc_root,
                planned_components=components
                if existing is not None and existing.get("phase") != "begin_pending"
                else None,
                require_empty_kernel_state=existing is None
                or existing.get("phase") == "begin_pending",
                expected_bootstrap_broker=plan.get("bootstrapBroker"),
            )
        if existing is None:
            _write_private_json(updater, journal_path, transaction)
            fresh_snapshot = _health_snapshot(updater)
            if (
                fresh_snapshot["gateGeneration"] != plan["gateGeneration"]
                or fresh_snapshot["catalogGeneration"] != plan["catalogGeneration"]
                or fresh_snapshot["activitySources"] != plan["activitySources"]
            ):
                raise RuntimeError("Fresh C10 Broker Health changed after first-Core confirmation")
        if transaction.get("phase") == "succeeded":
            return {"status": "installed", "planId": plan_id, "planDigest": plan_digest}
        if transaction.get("phase") == "begin_pending":
            request = transaction["beginRequest"]
            response = updater._broker_request(
                "BeginCoreBootstrap", request, request_id=plan["requestId"]
            )
            if (
                response.get("status") != "MAINTENANCE_ACTIVE"
                or response.get("maintenance_origin") != "CORE_BOOTSTRAP"
                or response.get("readiness_claimed") is not False
                or response.get("held") is not True
                or not isinstance(response.get("maintenance_token"), str)
                or not response.get("maintenance_token", "").strip()
                or not isinstance(response.get("gate_generation"), int)
                or isinstance(response.get("gate_generation"), bool)
            ):
                raise RuntimeError("Platform did not confirm a durable closed first-Core hold")
            transaction["maintenanceToken"] = response["maintenance_token"]
            transaction["maintenanceGateGeneration"] = response["gate_generation"]
            transaction["phase"] = "held"
            _write_private_json(updater, journal_path, transaction)
        if transaction.get("phase") not in {
            "held",
            "installing",
            "starting",
            "started",
            "end_pending",
            "end_call_pending",
            "hold_required",
        }:
            raise ValueError(
                "Interrupted first-Core journal has an unknown phase; hold remains closed"
            )

        recovery_socket_proof: _HeldAdapterSocketProof | None = None
        if recovering_hold_required:
            try:
                _verify_held_core_bootstrap(updater, plan, transaction)
                run_root = Path(getattr(updater, "core_run_root", CORE_RUN_ROOT))
                adapter_socket = run_root / LINUX_SYS_ADAPTER_SOCKET
                if adapter_socket.exists() or adapter_socket.is_symlink():
                    recovery_socket_proof = _verify_held_linux_sys_adapter_socket(
                        updater, components, proc_root=proc_root
                    )
                _assert_fresh(
                    updater,
                    proc_root=proc_root,
                    planned_components=components,
                    expected_bootstrap_broker=plan.get("bootstrapBroker"),
                    held_adapter_socket_proof=recovery_socket_proof,
                )
                _quiesce_hold_recovery_units(updater, components, proc_root=proc_root)
            except Exception as error:
                transaction["failure"] = str(error)[:500]
                transaction["phase"] = "hold_required"
                _write_private_json(updater, journal_path, transaction)
                raise RuntimeError(
                    "First-Core recovery identity is not proven; durable admission hold remains closed"
                ) from error
            transaction["phase"] = "installing"
            _write_private_json(updater, journal_path, transaction)

        # ── Phase 1: Install the exact staged cohort while admission is closed.
        # 第一阶段：先持久闭门，再安装并启动本次签名Core组。
        try:
            if transaction["phase"] == "held":
                transaction["phase"] = "installing"
                _write_private_json(updater, journal_path, transaction)
            if transaction["phase"] == "installing":
                if not recovering_hold_required:
                    _assert_fresh(
                        updater,
                        proc_root=proc_root,
                        planned_components=components,
                        expected_bootstrap_broker=plan.get("bootstrapBroker"),
                    )
                _stop_initial_c10_broker(updater, transaction, journal_path)
                for item in components:
                    component = updater.components[item["componentId"]]
                    pointer = f"{item['version']}--{item['manifestDigest'].removeprefix('sha256:')}"
                    release = (
                        updater.install_root
                        / "components"
                        / item["componentId"]
                        / "releases"
                        / pointer
                    )
                    _write_unit(updater, component, release)
                _activate_core_cohort(updater, transaction)
                completed = updater.runner(
                    ["systemctl", "daemon-reload"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if completed.returncode != 0:
                    raise RuntimeError("systemd daemon-reload failed")
                transaction["phase"] = "starting"
                _write_private_json(updater, journal_path, transaction)
            if transaction["phase"] in {"starting", "started"}:
                _start_core_components(updater, components)
                updater._health_transaction(transaction)
                transaction["phase"] = "end_pending"
                _write_private_json(updater, journal_path, transaction)
        except Exception as error:
            try:
                _stop_candidate_services(updater, components)
                _remove_candidate_pointers_and_units(updater, components)
            except Exception as cleanup_error:  # noqa: BLE001 - retain the hold if ownership is unclear.
                transaction["cleanupError"] = str(cleanup_error)[:300]
            transaction["failure"] = str(error)[:500]
            transaction["phase"] = "hold_required"
            _write_private_json(updater, journal_path, transaction)
            raise RuntimeError(
                f"First-Core bootstrap failed; durable admission hold remains closed: {error}"
            ) from error

        # ── Phase 2: Revalidate before every idempotent EndMaintenance attempt.
        # 第二阶段：每次解除闭门前重新验证PID、发布字节和真实Kernel计数。
        try:
            _verify_live_core_cohort(updater, transaction, plan)
            _require_core_ready(updater, plan)
            if plan.get("includeProducts") is True:
                product_helper = _first_products_module()
                if transaction.get("phase") == "end_call_pending":
                    product_helper.verify_held(updater, plan, transaction)
                else:
                    product_helper.activate_held(updater, plan, transaction)
                _write_private_json(updater, journal_path, transaction)
                _require_core_ready(updater, plan)
        except Exception as error:
            if transaction["phase"] == "end_call_pending":
                # The previous End RPC may have opened the gate before its
                # response was lost. Never tear down a possibly admitted Core.
                transaction["endRecoveryError"] = str(error)[:500]
                _write_private_json(updater, journal_path, transaction)
                raise RuntimeError(
                    "EndMaintenance outcome is uncertain and live Core proof failed; "
                    "preserve the journal and candidate services for operator recovery"
                ) from error
            try:
                _rollback_pre_end(updater, plan, transaction, components)
            except Exception as cleanup_error:  # noqa: BLE001 - preserve candidate state when ownership is unknown.
                transaction["cleanupError"] = str(cleanup_error)[:300]
            transaction["failure"] = str(error)[:500]
            transaction["phase"] = "hold_required"
            _write_private_json(updater, journal_path, transaction)
            raise RuntimeError(
                f"First-Core health proof failed; durable admission hold remains closed: {error}"
            ) from error
        if transaction["phase"] != "end_call_pending":
            transaction["phase"] = "end_call_pending"
            _write_private_json(updater, journal_path, transaction)

        # ── Phase 3: End the durable hold, then finalize without cleanup-on-error.
        # 第三阶段：End结果不确定时保留活动服务与原journal，以同请求幂等恢复。
        try:
            end_result = updater._broker_request(
                "EndMaintenance",
                {
                    "request_id": plan["requestId"],
                    "target_kind": "CORE_RUNTIME",
                    "maintenance_token": transaction["maintenanceToken"],
                    "outcome": "SUCCESS",
                    "healthy": True,
                },
                request_id="bootstrap-end-" + plan_digest.split(":", 1)[1][:32],
            )
        except Exception as error:
            transaction["endError"] = str(error)[:300]
            _write_private_json(updater, journal_path, transaction)
            raise RuntimeError(
                f"EndMaintenance result is uncertain; preserve the first-Core journal and retry this exact plan: {error}"
            ) from error
        if end_result.get("unlocked") is not True:
            transaction["endError"] = "Platform did not confirm the durable gate release"
            _write_private_json(updater, journal_path, transaction)
            raise RuntimeError(
                "Platform did not confirm gate release; preserve the first-Core journal and retry this exact plan"
            )

        transaction["phase"] = (
            "post_end_pending" if plan.get("includeProducts") is True else "end_confirmed"
        )
        transaction["gateReleaseConfirmed"] = True
        transaction.pop("maintenanceToken", None)
        if plan.get("includeProducts") is True:
            transaction["firstProducts"]["phase"] = "post_end_pending"
        _write_private_json(updater, journal_path, transaction)
        if plan.get("includeProducts") is True:
            return _resume_first_products_post_end(
                updater, plan, transaction, journal_path, recovery=False
            )
        try:
            post_end = updater._readiness_for("CORE_RUNTIME", requires_restart=True, force=True)
            transaction["postEndReadiness"] = post_end.get("status", "UNKNOWN")
        except Exception as error:  # noqa: BLE001 - release is confirmed; report unavailable post-release status.
            transaction["postEndReadiness"] = "UNKNOWN"
            transaction["postEndReadinessError"] = str(error)[:300]
        transaction["phase"] = "succeeded"
        _write_private_json(updater, journal_path, transaction)
        return {
            "status": "installed",
            "planId": plan_id,
            "planDigest": plan_digest,
            "components": list(cohort),
            "postEndReadiness": transaction["postEndReadiness"],
        }


def handle(updater: Any, request: dict[str, Any]) -> dict[str, Any] | None:
    """Handle an explicitly selected first-Core mode within the four fixed operations."""

    if "bootstrapMode" in request and request.get("bootstrapMode") != CORE_BOOTSTRAP_MODE:
        raise ValueError("Unsupported bootstrap mode")
    if request.get("bootstrapMode") != CORE_BOOTSTRAP_MODE:
        return None
    operation = request.get("operation")
    if operation == "status":
        if set(request) - {
            "protocolVersion",
            "operation",
            "bootstrapMode",
            "includeProducts",
        }:
            raise ValueError("Unsupported first-Core status field")
        include_products = request.get("includeProducts", False)
        if not isinstance(include_products, bool):
            raise TypeError("includeProducts must be a boolean")
        result = updater.status()
        try:
            _assert_fresh(updater)
            snapshot = _health_snapshot(updater, require_eligible=False)
            eligibility = {
                "status": "eligible" if snapshot["coreBootstrapEligible"] else "blocked",
                "blocker_codes": []
                if snapshot["coreBootstrapEligible"]
                else ["FIRST_CORE_ALREADY_USED"],
                "gateGeneration": snapshot["gateGeneration"],
                "catalogGeneration": snapshot["catalogGeneration"],
                "activitySources": snapshot["activitySources"],
            }
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            eligibility = {
                "status": "UNKNOWN",
                "blocker_codes": ["FIRST_CORE_NOT_FRESH"],
                "message": str(error)[:300],
            }
        result["mode"] = CORE_BOOTSTRAP_MODE
        result["includeProducts"] = include_products
        result["firstCoreEligibility"] = eligibility
        transaction = _read_private_json(_journal_path(updater))
        if transaction is not None and transaction.get("includeProducts") is True:
            phase = transaction.get("phase")
            product_status = (
                "complete"
                if phase == "succeeded"
                else "UNKNOWN"
                if phase == "end_call_pending"
                else "pending"
            )
            result["firstProductsBootstrap"] = {
                "status": product_status,
                "phase": phase,
                "gateReleaseConfirmed": transaction.get("gateReleaseConfirmed") is True,
            }
        return result
    if operation == "check":
        if set(request) - {
            "protocolVersion",
            "operation",
            "bootstrapMode",
            "channel",
            "includeProducts",
        }:
            raise ValueError("Unsupported first-Core check field")
        include_products = request.get("includeProducts", False)
        if not isinstance(include_products, bool):
            raise TypeError("includeProducts must be a boolean")
        return check(
            updater,
            channel=request.get("channel"),
            include_products=include_products,
        )
    if operation == "stage":
        if set(request) - {
            "protocolVersion",
            "operation",
            "bootstrapMode",
            "planId",
            "planDigest",
            "channel",
        }:
            raise ValueError("Unsupported first-Core stage field")
        return stage(
            updater,
            request.get("planId"),
            request.get("planDigest"),
            channel=request.get("channel"),
        )
    if operation == "apply":
        if set(request) - {
            "protocolVersion",
            "operation",
            "bootstrapMode",
            "planId",
            "planDigest",
            "confirmation",
            "channel",
        }:
            raise ValueError("Unsupported first-Core apply field")
        return apply(
            updater,
            request.get("planId"),
            request.get("planDigest"),
            request.get("confirmation"),
            channel=request.get("channel"),
        )
    return None
