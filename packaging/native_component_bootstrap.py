"""Offline first-install bootstrap for the trusted native maintenance broker.

This module verifies immutable release bytes against the compiled Workspace
catalog, then reuses the component updater's extraction, receipt, and pointer
primitives. It is deliberately limited to first activation of the maintenance
broker; upgrades remain on the ordinary maintenance-gated updater path.
中文：仅使用编译目录验证离线发布字节，并首次激活维护代理。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

BOOTSTRAP_COMPONENT_ID = "cyrene-runtime-maintenance"
BOOTSTRAP_JOURNAL_NAME = "runtime-maintenance-first-install.json"
DEFAULT_BROKER_EXECUTABLE = Path("/usr/bin/cyrene-runtime-maintenance")
DEFAULT_PROC_ROOT = Path("/proc")
RUNTIME_SYSTEMD_UNIT_DIRECTORY = Path("/run/systemd/system")
COMPILED_CATALOG_DIGEST = "sha256:79866ed32c4393e5bbbd3e36144ae080f316e9bf2695a0768bdbbfa98bf76247"
BUNDLED_V1_CATALOG_PATH = Path("/usr/share/cyrene/component-catalog-v1.json")
COMPILED_V1_CATALOG_GENERATION = 13
_DELETED_EXE_SUFFIX = " (deleted)"


class _UpdateLockLease:
    """Opaque proof that this thread owns one updater transaction lock."""

    __slots__ = ("_thread_id", "_updater")

    def __init__(self, updater: Any, thread_id: int) -> None:
        self._updater = updater
        self._thread_id = thread_id


class _HeldUpdateLock:
    """Track same-thread nesting without opening a second flock descriptor."""

    __slots__ = ("depth", "lease")

    def __init__(self, lease: _UpdateLockLease) -> None:
        self.lease = lease
        self.depth = 1


_update_lock_context = threading.local()


@contextmanager
def exclusive_update_lock(updater: Any) -> Iterator[_UpdateLockLease]:
    """Take the official updater lock, reusing its lease for same-thread nesting.

    The OS-level updater lock remains the sole cross-process authority. The
    thread-local lease only prevents a trusted nested bootstrap from reopening
    the same lock file through another descriptor, which would self-deadlock.
    中文：复用官方更新锁，仅避免同线程内层重复打开 flock 文件。
    """

    contexts = getattr(_update_lock_context, "held", None)
    if contexts is None:
        contexts = {}
        _update_lock_context.held = contexts
    key = id(updater)
    held = contexts.get(key)
    if held is not None and held.lease._updater is updater:
        held.depth += 1
        try:
            yield held.lease
        finally:
            held.depth -= 1
        return

    with updater._exclusive_update_lock():
        lease = _UpdateLockLease(updater, threading.get_ident())
        held = _HeldUpdateLock(lease)
        contexts[key] = held
        try:
            yield lease
        finally:
            if contexts.get(key) is held:
                del contexts[key]


def _require_active_update_lock_lease(updater: Any, lease: Any) -> None:
    """Accept only the active lock lease for this updater and current thread."""

    contexts = getattr(_update_lock_context, "held", {})
    held = contexts.get(id(updater))
    if (
        not isinstance(lease, _UpdateLockLease)
        or lease._updater is not updater
        or lease._thread_id != threading.get_ident()
        or held is None
        or held.lease is not lease
    ):
        raise PermissionError("First broker bootstrap requires its active updater lock lease")


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _catalog_row(catalog: dict[str, Any], collection: str, key: str, value: str) -> dict[str, Any]:
    rows = catalog.get(collection)
    if not isinstance(rows, list):
        raise TypeError(f"Trusted catalog {collection} inventory is malformed")
    matches = [row for row in rows if isinstance(row, dict) and row.get(key) == value]
    if len(matches) != 1:
        raise ValueError(f"Trusted catalog has no unique {collection} identity for {value}")
    return matches[0]


def _revalidate_installed_bootstrap_binding(updater: Any) -> dict[str, Any]:
    """Reload the official installed binding and require it to match updater state."""

    loader = getattr(updater, "_load_installed_bootstrap_catalog_binding", None)
    if not callable(loader):
        raise TypeError("The installed bootstrap catalog binding verifier is unavailable")
    try:
        binding = loader()
    except Exception as error:
        raise ValueError(
            "The installed bootstrap catalog binding could not be revalidated"
        ) from error
    if not isinstance(binding, dict) or binding != getattr(
        updater, "bootstrap_catalog_binding", None
    ):
        raise ValueError("The installed bootstrap catalog binding differs from updater state")
    if set(binding) != {"schemaVersion", "catalog"} or binding.get("schemaVersion") != 1:
        raise ValueError("The installed bootstrap catalog binding is not a signed package binding")
    return binding


def _read_bundled_v1_baseline() -> bytes:
    """Read the fixed installed V1 baseline through the binding helper's root-safe reader."""

    helper = sys.modules.get("_cyrene_bootstrap_catalog_binding")
    expected_helper_path = Path(__file__).resolve().with_name("bootstrap_catalog_binding.py")
    try:
        helper_path = Path(getattr(helper, "__file__", "")).resolve(strict=True)
    except OSError as error:
        raise ValueError("The installed bootstrap catalog verifier is not loaded") from error
    if helper_path != expected_helper_path.resolve(strict=True):
        raise ValueError("The loaded bootstrap catalog verifier is not the installed helper")
    reader = getattr(helper, "_read_regular_file", None)
    if not callable(reader):
        raise TypeError("The installed bootstrap catalog verifier has no root-safe reader")
    try:
        helper_bytes = reader(
            expected_helper_path,
            "installed bootstrap catalog verifier source",
            require_root=True,
        )
        if not isinstance(helper_bytes, bytes) or not helper_bytes:
            raise ValueError("The installed bootstrap catalog verifier source is empty")
        return reader(
            BUNDLED_V1_CATALOG_PATH,
            "frozen generation-13 V1 baseline catalog",
            require_root=True,
        )
    except Exception as error:
        raise ValueError("The frozen V1 baseline catalog could not be read safely") from error


def _require_active_v2_catalog_context(updater: Any, active_catalog_digest: str) -> None:
    """Prove a V2 plan and the independently pinned V1 first-Broker authority."""

    if (
        not isinstance(active_catalog_digest, str)
        or len(active_catalog_digest) != 71
        or not active_catalog_digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in active_catalog_digest[7:])
        or active_catalog_digest != getattr(updater, "catalog_digest", None)
        or getattr(updater, "bootstrap_catalog_authorized", None) is not True
    ):
        raise ValueError("The selected V2 catalog is not bound to the updater's validated identity")

    bootstrap_bytes = getattr(updater, "bootstrap_catalog_bytes", None)
    bootstrap_digest = getattr(updater, "bootstrap_catalog_digest", None)
    if not isinstance(bootstrap_bytes, bytes) or _sha256(bootstrap_bytes) != bootstrap_digest:
        raise ValueError("Bootstrap catalog bytes differ from their selected digest")
    bootstrap = _parse_json(bootstrap_bytes, "Selected bootstrap catalog")
    if not isinstance(bootstrap, dict):
        raise TypeError("Selected bootstrap catalog is not an object")

    if bootstrap.get("schemaVersion") == 1:
        if (
            bootstrap_digest != COMPILED_CATALOG_DIGEST
            or bootstrap.get("generation") != COMPILED_V1_CATALOG_GENERATION
        ):
            raise ValueError("The V1 bootstrap catalog differs from the compiled baseline")
        baseline_bytes = bootstrap_bytes
        if getattr(updater, "bootstrap_catalog_binding", None) is not None:
            binding = _revalidate_installed_bootstrap_binding(updater)
            bound_catalog = binding.get("catalog")
            if (
                not isinstance(bound_catalog, dict)
                or bound_catalog.get("sha256") != COMPILED_CATALOG_DIGEST.removeprefix("sha256:")
                or bound_catalog.get("generation") != COMPILED_V1_CATALOG_GENERATION
                or bound_catalog.get("assetName") != "component-catalog-v1.json"
            ):
                raise ValueError("The installed bootstrap binding does not select frozen V1")
    elif bootstrap.get("schemaVersion") == 2:
        binding = _revalidate_installed_bootstrap_binding(updater)
        bound_catalog = binding.get("catalog")
        if (
            not isinstance(bound_catalog, dict)
            or bound_catalog.get("sha256") != bootstrap_digest.removeprefix("sha256:")
            or bound_catalog.get("generation") != bootstrap.get("generation")
            or bound_catalog.get("assetName") != "component-catalog-v2.json"
            or bootstrap.get("generation") != getattr(updater, "catalog_generation", None)
        ):
            raise ValueError("The signed V2 bootstrap binding differs from its catalog bytes")
        baseline_bytes = _read_bundled_v1_baseline()
        if _sha256(baseline_bytes) != COMPILED_CATALOG_DIGEST:
            raise ValueError("The bundled V1 baseline bytes differ from the compiled authority pin")
    else:
        raise ValueError("The selected bootstrap catalog schema is not supported")

    active_reader = getattr(updater, "_read_active_catalog", None)
    if not callable(active_reader):
        raise TypeError("The signed active catalog readback is unavailable")
    active_readback = active_reader()
    if active_readback is None:
        if (
            bootstrap.get("schemaVersion") != 2
            or bootstrap_digest != active_catalog_digest
            or getattr(updater, "catalog_source", None) is not None
        ):
            raise ValueError(
                "No signed active pointer or V2 package bootstrap authorizes the catalog"
            )
        active_bytes = bootstrap_bytes
    else:
        if (
            not isinstance(active_readback, tuple)
            or len(active_readback) != 2
            or not isinstance(active_readback[0], bytes)
            or not isinstance(active_readback[1], dict)
        ):
            raise ValueError("The signed active catalog has an invalid durable readback")
        active_bytes, active_receipt = active_readback
        if (
            _sha256(active_bytes) != active_catalog_digest
            or active_receipt.get("catalogSha256") != active_catalog_digest
            or type(active_receipt.get("generation")) is not int
            or active_receipt.get("generation") != getattr(updater, "catalog_generation", None)
            or active_receipt != getattr(updater, "catalog_source", None)
        ):
            raise ValueError("The active catalog digest differs from its signed live receipt")
    if active_bytes != getattr(updater, "catalog_bytes", None):
        raise ValueError("The selected active catalog bytes differ from updater state")

    baseline = _parse_json(baseline_bytes, "Compiled V1 baseline catalog")
    active = _parse_json(active_bytes, "Active component catalog")
    if (
        not isinstance(baseline, dict)
        or baseline.get("schemaVersion") != 1
        or type(baseline.get("generation")) is not int
        or baseline.get("generation") != COMPILED_V1_CATALOG_GENERATION
        or _sha256(baseline_bytes) != COMPILED_CATALOG_DIGEST
        or not isinstance(active, dict)
        or type(active.get("schemaVersion")) is not int
        or active.get("schemaVersion") != 2
        or type(active.get("generation")) is not int
        or active["generation"] <= baseline["generation"]
        or active.get("generation") != getattr(updater, "catalog_generation", None)
        or _sha256(active_bytes) != active_catalog_digest
        or active != getattr(updater, "catalog", None)
    ):
        raise ValueError("The active catalog is not a newer validated V2 catalog")

    broker_id = BOOTSTRAP_COMPONENT_ID
    target_id = "linux-ubuntu-24.04-x86_64-systemd"
    active_component = _catalog_row(active, "components", "componentId", broker_id)
    baseline_component = _catalog_row(baseline, "components", "componentId", broker_id)
    if active_component != baseline_component:
        raise ValueError("Active V2 Broker component trust metadata differs from compiled V1")
    repository = active_component.get("publisher")
    if not isinstance(repository, str) or baseline_component.get("publisher") != repository:
        raise ValueError("The first-Broker publisher identity is not stable across catalogs")
    active_publisher = _catalog_row(active, "publishers", "repository", repository)
    baseline_publisher = _catalog_row(baseline, "publishers", "repository", repository)
    if active_publisher != baseline_publisher:
        raise ValueError("Active V2 Broker publisher trust metadata differs from compiled V1")
    active_target = _catalog_row(active, "targets", "id", target_id)
    baseline_target = _catalog_row(baseline, "targets", "id", target_id)
    if active_target != baseline_target:
        raise ValueError("Active V2 Broker target metadata differs from compiled V1")
    if (
        getattr(updater, "components", {}).get(broker_id) != active_component
        or getattr(updater, "publishers", {}).get(repository) != active_publisher
        or getattr(updater, "targets", {}).get(target_id) != active_target
    ):
        raise ValueError("Updater Broker trust rows differ from its signed active catalog")


def _effective_uid() -> int:
    """Return the process identity used for root-wrapper authorization."""

    return os.geteuid()


def _parse_json(payload: bytes, label: str) -> Any:
    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from error


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate object keys so signed bytes have one unambiguous meaning."""

    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON object key: {key}")
        value[key] = item
    return value


def _write_bundle(directory: Path, name: str, payload: bytes) -> Path:
    path = directory / name
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _verify_detached(
    updater: Any,
    payload: bytes,
    bundle: bytes,
    *,
    subject_name: str,
    digest: str,
    repository: str,
    workflow: str,
    source_ref: str,
    source_commit: str,
    directory: Path,
) -> None:
    """Verify one subject and its detached GitHub attestation bundle."""

    bundle_path = _write_bundle(directory, subject_name + ".attestation.jsonl", bundle)
    updater._verify_attestation(
        payload,
        subject_name=subject_name,
        digest=digest,
        repository=repository,
        workflow=workflow,
        source_ref=source_ref,
        source_commit=source_commit,
        bundle_path=bundle_path,
    )


def _read_process_executable(exe_link: Path) -> tuple[str, str]:
    """Read a procfs executable link and validate its live inode.

    `/proc/PID/exe` can still stat a running executable after unlink. Do not
    resolve the displayed pathname back through the host filesystem.
    中文：通过 procfs magic link 校验运行中的 inode，不要求已删除的原路径仍存在。
    """

    target = os.readlink(exe_link)
    if not Path(target.removesuffix(_DELETED_EXE_SUFFIX)).is_absolute():
        raise OSError(errno.EINVAL, "process executable path is not absolute", str(exe_link))
    metadata = os.stat(exe_link)
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(errno.EINVAL, "process executable is not a regular file", str(exe_link))
    name = Path(target.removesuffix(_DELETED_EXE_SUFFIX)).name
    if not name:
        raise OSError(errno.EINVAL, "process executable name is empty", str(exe_link))
    return target, name


def _broker_process_exists(proc_root: Path) -> bool:
    """Fail closed if the process table is unavailable or contains the broker."""

    if not proc_root.is_dir() or proc_root.is_symlink():
        raise RuntimeError("Process table is unavailable; broker process state is UNKNOWN")
    try:
        processes = tuple(proc_root.iterdir())
    except OSError:
        raise RuntimeError("Process table is unreadable; broker process state is UNKNOWN") from None
    for process in processes:
        if not process.name.isdecimal():
            continue
        try:
            state = (process / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()[0]
            if state in {"Z", "X"}:
                continue
            command = (process / "cmdline").read_bytes().replace(b"\0", b" ")
            _executable_path, executable_name = _read_process_executable(process / "exe")
            if executable_name == DEFAULT_BROKER_EXECUTABLE.name:
                return True
        except FileNotFoundError:
            if not process.exists():
                continue  # PID disappeared while the process snapshot was read.
            try:
                command = (process / "cmdline").read_bytes()
            except FileNotFoundError:
                if not process.exists():
                    continue
                raise RuntimeError("Process entry changed while checking broker state") from None
            except OSError as error:
                raise RuntimeError(
                    "Process entry is unreadable; broker state is UNKNOWN"
                ) from error
            if not command:
                continue  # Linux kernel threads have no executable or command line.
            raise RuntimeError("Process executable is unreadable; broker state is UNKNOWN")
        except PermissionError as error:
            raise RuntimeError("Process entry is unreadable; broker state is UNKNOWN") from error
        except (OSError, IndexError) as error:
            if (
                isinstance(error, OSError)
                and error.errno in {errno.ENOENT, errno.ESRCH}
                and not process.exists()
            ):
                continue
            raise RuntimeError("Process entry is unreadable; broker state is UNKNOWN") from error
        if not command:
            continue
        if DEFAULT_BROKER_EXECUTABLE.name.encode() in command:
            return True
    return False


def _assert_fresh_broker(
    updater: Any,
) -> str | None:
    """Refuse to turn this bootstrap into a broker upgrade or service takeover."""

    component = updater.components.get(BOOTSTRAP_COMPONENT_ID)
    if not isinstance(component, dict):
        raise TypeError("The compiled trusted catalog omits the maintenance broker")
    current = updater._active_native_pointer_identity(BOOTSTRAP_COMPONENT_ID)
    if current is not None:
        raise ValueError("A maintenance broker release is already active; use normal update")
    if DEFAULT_BROKER_EXECUTABLE.exists() or DEFAULT_BROKER_EXECUTABLE.is_symlink():
        raise ValueError("A managed maintenance broker executable already exists")
    unit = component.get("systemdUnit")
    restart = component.get("restart")
    if not isinstance(unit, str) or not isinstance(restart, dict) or restart.get("unit") != unit:
        raise ValueError("The pinned maintenance broker unit identity is invalid")
    for directory in (*updater.systemd_unit_dirs, RUNTIME_SYSTEMD_UNIT_DIRECTORY):
        path = Path(directory) / unit
        if path.exists() or path.is_symlink():
            raise ValueError("A managed maintenance broker unit already exists")
    if _broker_process_exists(DEFAULT_PROC_ROOT):
        raise ValueError("A maintenance broker process is already running")
    return current


def _assert_completed_broker_plan_reusable(
    updater: Any,
    *,
    journal: dict[str, Any],
    plan_digest: str,
    identity: dict[str, Any],
    manifest: dict[str, Any],
    artifact_digest: str,
    payload_root: Path,
) -> None:
    """Allow read-only confirmation only for the exact completed inactive broker.

    中文：仅对完全相同且当前未运行的已完成 broker 计划开放只读再确认。
    """

    component_id = BOOTSTRAP_COMPONENT_ID
    component = updater.components.get(component_id)
    unit = component.get("systemdUnit") if isinstance(component, dict) else None
    expected_release = (
        f"{manifest['version']}--{manifest['manifestDigest'].removeprefix('sha256:')}"
    )
    if (
        journal.get("schemaVersion") != 1
        or journal.get("phase") != "complete"
        or journal.get("planDigest") != plan_digest
        or journal.get("identity") != identity
        or journal.get("releaseIdentity") != expected_release
        or not isinstance(unit, str)
        or component.get("restart", {}).get("unit") != unit
    ):
        raise ValueError("Completed maintenance broker journal does not match this exact plan")

    active_path = updater.install_root / "components" / component_id / "active"
    current = updater._active_native_pointer_identity(component_id)
    expected_pointer = f"releases/{expected_release}"
    if (
        current != expected_release
        or not active_path.is_symlink()
        or os.readlink(active_path) != expected_pointer
    ):
        raise ValueError("Completed maintenance broker active pointer differs from this plan")

    receipt = updater._read_active_receipt(component_id)
    if (
        not isinstance(receipt, dict)
        or receipt.get("componentId") != component_id
        or receipt.get("version") != manifest.get("version")
        or receipt.get("manifestDigest") != manifest.get("manifestDigest")
        or receipt.get("artifactDigest") != artifact_digest
        or receipt.get("releaseIdentity") != manifest.get("manifestDigest")
        or receipt.get("manifest") != manifest
    ):
        raise ValueError("Completed maintenance broker receipt differs from this plan")

    release = updater.install_root / "components" / component_id / "releases" / expected_release
    _verify_release_payload(updater, release, manifest)
    installed_manifest = release / "component-manifest.json"
    if (
        installed_manifest.is_symlink()
        or not installed_manifest.is_file()
        or json.loads(installed_manifest.read_text(encoding="utf-8")) != manifest
    ):
        raise ValueError("Completed maintenance broker release manifest differs from this plan")

    candidate_unit = payload_root / "systemd" / unit
    if candidate_unit.is_symlink() or not candidate_unit.is_file():
        raise ValueError("Verified broker payload omits its pinned systemd unit")
    candidate_unit_bytes = candidate_unit.read_bytes()
    if not updater._unit_uses_component_runner(candidate_unit, component_id):
        raise ValueError("Verified broker systemd unit is not the pinned component runner")
    installed_units: dict[tuple[int, int, str], Path] = {}
    for directory in dict.fromkeys((*updater.systemd_unit_dirs, RUNTIME_SYSTEMD_UNIT_DIRECTORY)):
        installed_unit = Path(directory) / unit
        if not installed_unit.exists() and not installed_unit.is_symlink():
            continue
        unit_info = installed_unit.lstat()
        if (
            not stat.S_ISREG(unit_info.st_mode)
            or unit_info.st_uid != os.geteuid()
            or stat.S_IMODE(unit_info.st_mode) & 0o022
            or installed_unit.read_bytes() != candidate_unit_bytes
            or not updater._unit_uses_component_runner(installed_unit, component_id)
        ):
            raise ValueError("Installed maintenance broker systemd unit differs from this plan")
        # Ubuntu's /lib and /usr/lib can name one unit through usrmerge; count
        # that file once only when inode and strict physical path both agree.
        physical_identity = (
            unit_info.st_dev,
            unit_info.st_ino,
            str(installed_unit.resolve(strict=True)),
        )
        installed_units.setdefault(physical_identity, installed_unit)
    if len(installed_units) != 1:
        raise ValueError("Completed maintenance broker systemd unit is missing or ambiguous")

    if _broker_process_exists(DEFAULT_PROC_ROOT):
        raise ValueError("A maintenance broker process is running; exact-plan reuse is denied")
    for property_name, expected_value in (("ActiveState", "inactive"), ("MainPID", "0")):
        result = updater.runner(
            ["systemctl", "show", f"--property={property_name}", "--value", unit],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != expected_value:
            raise RuntimeError(
                f"Maintenance broker {property_name} is not the exact inactive state"
            )


def _journal_path(updater: Any) -> Path:
    return Path(updater.state_root) / "native-first-bootstrap" / BOOTSTRAP_JOURNAL_NAME


def _read_journal(path: Path) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("The first-bootstrap recovery journal is unsafe")
    info = path.lstat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("The first-bootstrap recovery journal is not private")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("The first-bootstrap recovery journal is invalid") from error
    if not isinstance(value, dict):
        raise TypeError("The first-bootstrap recovery journal is invalid")
    return value


def _persist_journal(updater: Any, path: Path, value: dict[str, Any]) -> None:
    updater._ensure_state_root()
    path.parent.mkdir(mode=0o700, exist_ok=True)
    info = path.parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.parent.is_symlink()
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("The first-bootstrap journal directory is not private")
    updater._atomic_json_file(path, value, mode=0o600)


def _verify_release_payload(updater: Any, release: Path, manifest: dict[str, Any]) -> None:
    """Prove an interrupted release directory exactly matches its journal identity."""

    artifact = manifest.get("artifact")
    files = artifact.get("files") if isinstance(artifact, dict) else None
    if not isinstance(files, dict) or not files:
        raise ValueError("Interrupted release has no trusted native file map")
    release_info = release.lstat()
    if (
        not stat.S_ISDIR(release_info.st_mode)
        or release_info.st_uid != os.geteuid()
        or stat.S_IMODE(release_info.st_mode) & 0o022
    ):
        raise ValueError("Interrupted release directory is not controlled by the trusted owner")
    expected = set(files)
    found: set[str] = set()
    for current, directory_names, file_names in os.walk(release, followlinks=False):
        current_path = Path(current)
        for name in directory_names:
            path = current_path / name
            info = path.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise ValueError("Interrupted release contains an unsafe directory")
        for name in file_names:
            path = current_path / name
            info = path.lstat()
            relative = path.relative_to(release).as_posix()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o022
                or relative not in expected | {"component-manifest.json"}
            ):
                raise ValueError("Interrupted release contains an unexpected or unsafe file")
            found.add(relative)
            if relative in expected and _sha256(path.read_bytes()) != files[relative]:
                raise ValueError("Interrupted release payload digest differs from the manifest")
    if found - {"component-manifest.json"} != expected:
        raise ValueError("Interrupted release payload is incomplete")
    manifest_path = release / "component-manifest.json"
    if manifest_path.exists() or manifest_path.is_symlink():
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("Interrupted release manifest path is unsafe")
        info = manifest_path.lstat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError("Interrupted release manifest is not private to the trusted owner")
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Interrupted release manifest is invalid") from error
        if existing != manifest:
            raise ValueError("Interrupted release directory is occupied by a different manifest")


def _install_or_repair_release(updater: Any, candidate: Any, payload_root: Path) -> Path:
    """Install a verified release or repair only its journal-bound missing receipts."""

    component_id = candidate.component["componentId"]
    version = candidate.manifest["version"]
    digest = candidate.manifest_digest
    release = (
        updater.install_root
        / "components"
        / component_id
        / "releases"
        / f"{version}--{digest.removeprefix('sha256:')}"
    )
    if not release.exists() and not release.is_symlink():
        return updater._install_native_release(candidate, payload_root)
    if release.is_symlink() or not release.is_dir():
        raise ValueError("Interrupted release identity is occupied by an unsafe path")

    _verify_release_payload(updater, release, candidate.manifest)
    manifest_path = release / "component-manifest.json"
    if not manifest_path.exists():
        updater._atomic_json_file(manifest_path, candidate.manifest, mode=0o644)
    item = {
        "componentId": component_id,
        "version": version,
        "releaseIdentity": digest,
        "manifestDigest": digest,
        "artifactDigest": candidate.artifact_digest,
        "bundleIdentity": None,
        "manifest": candidate.manifest,
    }
    updater._write_release_receipt(item)
    return release


def _ensure_public_component_directories(updater: Any, component_id: str, release: Path) -> None:
    """Make the verified broker path traversable by its service account.

    Every directory is first checked as a real root-owned, non-group/world-writable
    directory. Only then are modes set to 0755 and read back. Explicit chmod is
    required because the reviewed launcher runs the initializer with umask 077.
    中文：先验证公开组件目录链，再显式设为 0755，避免继承私有暂存目录的 umask。
    """

    if component_id != BOOTSTRAP_COMPONENT_ID:
        raise ValueError("Public component-directory repair is limited to the broker")
    install_root = Path(updater.install_root)
    component_root = install_root / "components" / component_id
    releases = component_root / "releases"
    if release.parent != releases or release.is_symlink():
        raise ValueError("Activated broker release is outside its fixed component directory")

    directories = (install_root, install_root / "components", component_root, releases, release)
    for directory in directories:
        try:
            info = directory.lstat()
        except OSError as error:
            raise ValueError(f"Broker component directory is unavailable: {directory}") from error
        if (
            not stat.S_ISDIR(info.st_mode)
            or directory.is_symlink()
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise ValueError(f"Broker component directory is unsafe: {directory}")

    # Preflight the complete chain before changing any metadata.
    for directory in directories:
        os.chmod(directory, 0o755, follow_symlinks=False)
    for directory in directories:
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or directory.is_symlink()
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o755
        ):
            raise ValueError(f"Broker component directory failed permission readback: {directory}")


def _activate_confirmed(
    updater: Any,
    *,
    candidate: Any,
    identity: dict[str, Any],
    plan_digest: str,
    journal_path: Path,
    payload_root: Path,
) -> dict[str, Any]:
    """Apply one exact first-install plan while the updater-wide lock is held."""

    manifest = candidate.manifest
    component_id = candidate.component["componentId"]
    journal = _read_journal(journal_path)
    current = updater._active_native_pointer_identity(component_id)
    pointer_identity = (
        f"{manifest['version']}--{manifest['manifestDigest'].removeprefix('sha256:')}"
    )
    if journal is None:
        if current is not None:
            raise ValueError("A maintenance broker release is already active; use normal update")
        _assert_fresh_broker(updater)
        journal = {
            "schemaVersion": 1,
            "planDigest": plan_digest,
            "identity": identity,
            "phase": "verified",
            "expectedCurrent": None,
        }
        _persist_journal(updater, journal_path, journal)
    elif journal.get("planDigest") != plan_digest or journal.get("identity") != identity:
        raise ValueError("Interrupted bootstrap journal belongs to a different release")
    elif current not in {None, pointer_identity}:
        raise ValueError("Interrupted bootstrap found a different active broker pointer")
    elif current is None:
        _assert_fresh_broker(updater)

    release = _install_or_repair_release(updater, candidate, payload_root)
    _ensure_public_component_directories(updater, component_id, release)
    journal["phase"] = "installed"
    journal["releaseIdentity"] = release.name
    _persist_journal(updater, journal_path, journal)

    active_path = updater.install_root / "components" / component_id / "active"
    expected_active = f"releases/{release.name}"
    if current is None:
        updater._activate_native(component_id, release.name, expected_current=None)
    elif not active_path.is_symlink() or os.readlink(active_path) != expected_active:
        raise ValueError("Interrupted bootstrap active pointer does not match its journal")

    active_item = {
        "componentId": component_id,
        "releaseIdentity": manifest["manifestDigest"],
        "manifestDigest": manifest["manifestDigest"],
        "artifactDigest": candidate.artifact_digest,
        "bundleIdentity": None,
    }
    existing_active_receipt = updater._read_active_receipt(component_id)
    if existing_active_receipt is None:
        updater._write_active_receipt(active_item)
    elif (
        existing_active_receipt.get("manifest") != manifest
        or existing_active_receipt.get("artifactDigest") != candidate.artifact_digest
        or existing_active_receipt.get("releaseIdentity") != manifest["manifestDigest"]
    ):
        raise ValueError("Existing broker active receipt conflicts with the recovery journal")

    journal["phase"] = "complete"
    _persist_journal(updater, journal_path, journal)
    return {
        "status": "activated",
        "planDigest": plan_digest,
        "releasePath": str(release),
        "activePointer": str(active_path),
        "activePointerTarget": expected_active,
        "componentRun": ["/usr/bin/cyrene", "component-run", component_id],
        **identity,
    }


def _bootstrap_verified_runtime_maintenance(
    updater: Any,
    *,
    index_bytes: bytes,
    index_attestation_bytes: bytes,
    manifest_bytes: bytes,
    artifact_bytes: bytes,
    artifact_attestation_bytes: bytes,
    channel: str,
    target_id: str,
    confirm_plan_digest: str | None = None,
    _active_catalog_digest: str | None = None,
    _lock_lease: _UpdateLockLease | None = None,
) -> dict[str, Any]:
    """Verify and optionally activate the first maintenance broker release.

    The caller must provide the exact bytes returned by the reviewed offline
    release workflow. A first call without ``confirm_plan_digest`` is read-only
    and returns the digest to confirm. A matching second call installs one
    immutable broker release and activates only its pointer. Every trust check
    is repeated on the confirmed call; no caller-supplied acceptance flag is
    treated as authority.

    Args:
        updater: ComponentUpdater loaded from the compiled trusted catalog.
        index_bytes: Immutable publisher release-index bytes.
        index_attestation_bytes: Detached attestation bundle for the index.
        manifest_bytes: Exact component manifest bytes named by the index.
        artifact_bytes: Exact native release archive bytes.
        artifact_attestation_bytes: Detached attestation bundle for the archive.
        channel: Trusted release channel, either stable or preview.
        target_id: Exact compiled native target profile ID.
        confirm_plan_digest: Human-confirmed digest returned by the read-only plan call.

    Returns:
        A confirmation plan or the exact installed release and pointer identities.
    """

    if not all(
        isinstance(value, bytes)
        for value in (
            index_bytes,
            index_attestation_bytes,
            manifest_bytes,
            artifact_bytes,
            artifact_attestation_bytes,
        )
    ):
        raise TypeError("Verified release inputs must be immutable byte strings")
    if channel not in {"stable", "preview"}:
        raise ValueError("Channel must be stable or preview")
    if _lock_lease is not None:
        _require_active_update_lock_lease(updater, _lock_lease)
        if confirm_plan_digest is None:
            raise ValueError("A lock lease is only valid for confirmed broker activation")
    if _active_catalog_digest is None:
        if (
            updater.bootstrap_catalog_digest != COMPILED_CATALOG_DIGEST
            or updater.catalog_digest != updater.bootstrap_catalog_digest
        ):
            raise ValueError("First broker bootstrap requires the compiled trusted catalog")
    else:
        if _lock_lease is None or confirm_plan_digest is None:
            raise ValueError(
                "Active V2 Broker bootstrap requires its confirmed first-Core lock lease"
            )
        _require_active_v2_catalog_context(updater, _active_catalog_digest)

    component = updater.components.get(BOOTSTRAP_COMPONENT_ID)
    publisher = updater.publishers.get(component.get("publisher")) if component else None
    if not isinstance(component, dict) or not isinstance(publisher, dict):
        raise TypeError("The compiled catalog has no trusted maintenance broker publisher")
    target = updater.targets.get(target_id)
    component_target = updater._target_for(component)
    supported_targets = {
        item.get("targetId")
        for item in component.get("targets", [])
        if isinstance(item, dict)
        and item.get("artifactKind") == "native-binary"
        and item.get("support") == "supported"
    }
    if (
        not isinstance(target, dict)
        or target_id not in supported_targets
        or component_target is None
        or component_target.get("id") != target_id
        or component_target.get("artifactKind") != "native-binary"
    ):
        raise ValueError("Selected target is not the supported native host target")
    target = component_target

    index = _parse_json(index_bytes, "Release index")
    source = index.get("source") if isinstance(index, dict) else None
    if not isinstance(source, dict):
        raise TypeError("Release index has no trusted source identity")
    tag_prefix = updater._component_release_tag_prefix(component, channel)
    if tag_prefix is None:
        tag_prefix = "preview-" if channel == "preview" else "stable-"
    selected_release = {"tag_name": tag_prefix + str(source.get("commit", ""))}
    updater._validate_index(index, publisher, channel, selected_release, component)
    with tempfile.TemporaryDirectory(prefix="cyrene-bootstrap-index-") as index_temporary:
        _verify_detached(
            updater,
            index_bytes,
            index_attestation_bytes,
            subject_name=index["provenance"]["attestation"]["subjectName"],
            digest=_sha256(index_bytes),
            repository=publisher["repository"],
            workflow=publisher["workflow"],
            source_ref=source["ref"],
            source_commit=source["commit"],
            directory=Path(index_temporary),
        )

    entries = [
        item
        for item in index.get("releases", [])
        if isinstance(item, dict)
        and item.get("componentId") == BOOTSTRAP_COMPONENT_ID
        and item.get("target") == target["target"]
    ]
    if len(entries) != 1:
        raise ValueError("Attested release index has no unique broker entry for this target")
    entry = entries[0]
    updater._require_github_asset_uri(entry.get("manifestUri"), publisher["repository"])
    manifest = _parse_json(manifest_bytes, "Component manifest")
    if manifest.get("manifestDigest") != entry.get("manifestDigest"):
        raise ValueError("Manifest identity does not match the index manifest digest")
    updater._validate_manifest(manifest, entry, component, target, publisher, channel, index)
    artifact = manifest["artifact"]
    if (
        artifact.get("kind") != "native-binary"
        or artifact.get("sizeBytes") != len(artifact_bytes)
        or artifact.get("sha256") != _sha256(artifact_bytes)
    ):
        raise ValueError("Native artifact bytes do not match the verified manifest")
    updater._require_github_asset_uri(artifact.get("uri"), publisher["repository"])

    temporary = Path(tempfile.mkdtemp(prefix="cyrene-bootstrap-artifact-"))
    try:
        _verify_detached(
            updater,
            artifact_bytes,
            artifact_attestation_bytes,
            subject_name=manifest["provenance"]["attestation"]["subjectName"],
            digest=artifact["sha256"],
            repository=publisher["repository"],
            workflow=publisher["workflow"],
            source_ref=manifest["source"]["ref"],
            source_commit=manifest["source"]["commit"],
            directory=temporary,
        )

        archive_path = _write_bundle(temporary, "artifact.tar", artifact_bytes)
        payload_root = temporary / "payload"
        updater._extract_native(archive_path, payload_root, artifact)
        unit = component["systemdUnit"]
        unit_path = payload_root / "systemd" / unit
        if unit_path.is_symlink() or not unit_path.is_file():
            raise ValueError("Broker release omits its catalog-pinned systemd unit")
        if not updater._unit_uses_component_runner(unit_path, BOOTSTRAP_COMPONENT_ID):
            raise ValueError(
                "Broker systemd unit does not use the trusted component-run entrypoint"
            )

        plan_material = {
            "schemaVersion": 1,
            "componentId": BOOTSTRAP_COMPONENT_ID,
            "channel": channel,
            "targetId": target_id,
            "sourceRepository": source["repository"],
            "sourceRef": source["ref"],
            "sourceCommit": source["commit"],
            "indexDigest": _sha256(index_bytes),
            "manifestDigest": manifest["manifestDigest"],
            "artifactDigest": artifact["sha256"],
            "artifactSizeBytes": len(artifact_bytes),
        }
        plan_digest = _sha256(
            json.dumps(
                plan_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        identity = {
            "componentId": BOOTSTRAP_COMPONENT_ID,
            "targetId": target_id,
            "version": manifest["version"],
            "manifestDigest": manifest["manifestDigest"],
            "artifactDigest": artifact["sha256"],
            "indexDigest": _sha256(index_bytes),
        }

        journal_path = _journal_path(updater)
        journal = _read_journal(journal_path)
        if confirm_plan_digest is None:
            if journal is not None and journal.get("phase") == "complete":
                _assert_completed_broker_plan_reusable(
                    updater,
                    journal=journal,
                    plan_digest=plan_digest,
                    identity=identity,
                    manifest=manifest,
                    artifact_digest=artifact["sha256"],
                    payload_root=payload_root,
                )
            else:
                _assert_fresh_broker(updater)
            if journal is not None and journal.get("planDigest") != plan_digest:
                raise ValueError("A different interrupted bootstrap blocks this plan")
            return {"status": "confirmation_required", "planDigest": plan_digest, **identity}
        if confirm_plan_digest != plan_digest:
            raise ValueError("Confirmation digest does not match the verified broker identity")
        if _effective_uid() != 0:
            raise PermissionError("First broker activation requires the reviewed root wrapper")
        candidate = SimpleNamespace(
            component=component,
            manifest=manifest,
            manifest_digest=manifest["manifestDigest"],
            artifact_digest=artifact["sha256"],
            manifest_uri=entry["manifestUri"],
            index=index,
            index_uri="offline:verified-index",
            manifest_bytes=manifest_bytes,
        )

        def activate_under_lock() -> dict[str, Any]:
            confirmed_journal = _read_journal(journal_path)
            if confirmed_journal is not None and confirmed_journal.get("phase") == "complete":
                _assert_completed_broker_plan_reusable(
                    updater,
                    journal=confirmed_journal,
                    plan_digest=plan_digest,
                    identity=identity,
                    manifest=manifest,
                    artifact_digest=artifact["sha256"],
                    payload_root=payload_root,
                )
            return _activate_confirmed(
                updater,
                candidate=candidate,
                identity=identity,
                plan_digest=plan_digest,
                journal_path=journal_path,
                payload_root=payload_root,
            )

        if _lock_lease is None:
            with exclusive_update_lock(updater):
                return activate_under_lock()
        _require_active_update_lock_lease(updater, _lock_lease)
        return activate_under_lock()
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def bootstrap_verified_runtime_maintenance(
    updater: Any,
    *,
    index_bytes: bytes,
    index_attestation_bytes: bytes,
    manifest_bytes: bytes,
    artifact_bytes: bytes,
    artifact_attestation_bytes: bytes,
    channel: str,
    target_id: str,
    confirm_plan_digest: str | None = None,
) -> dict[str, Any]:
    """Verify a release and activate the first broker under the official lock.

    Ordinary CLI and API callers always acquire the updater lock internally.
    中文：普通入口始终自行获取官方更新锁。
    """

    return _bootstrap_verified_runtime_maintenance(
        updater,
        index_bytes=index_bytes,
        index_attestation_bytes=index_attestation_bytes,
        manifest_bytes=manifest_bytes,
        artifact_bytes=artifact_bytes,
        artifact_attestation_bytes=artifact_attestation_bytes,
        channel=channel,
        target_id=target_id,
        confirm_plan_digest=confirm_plan_digest,
    )


def bootstrap_verified_runtime_maintenance_under_lock(
    updater: Any,
    *,
    lock_lease: _UpdateLockLease,
    active_catalog_digest: str | None = None,
    **release_inputs: Any,
) -> dict[str, Any]:
    """Use a live first-Core lock lease while repeating every release check.

    Only fixed internal orchestration should call this function. Its opaque
    lease must be active on the current thread and bound to this exact updater.
    中文：仅持有本线程官方更新锁的内部首启流程可以复用该锁。
    """

    _require_active_update_lock_lease(updater, lock_lease)
    return _bootstrap_verified_runtime_maintenance(
        updater,
        **release_inputs,
        _active_catalog_digest=active_catalog_digest,
        _lock_lease=lock_lease,
    )
