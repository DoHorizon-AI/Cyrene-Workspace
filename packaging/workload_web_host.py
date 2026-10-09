"""Install and supervise the verified static Workspace Client on Ubuntu.

This module owns one loopback-only Nginx instance and its systemd unit. It does
not own the static-web pointer, package receipts, Platform maintenance holds,
or transaction journal; callers provide the existing durable journal callback.
中文：管理已验证 Client 静态包的 loopback Nginx 服务，不另建状态库。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import pwd
import re
import socket
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

WEB_COMPONENT_ID = "cyrene-client-workspace-web"
WEB_ROOT = Path("/opt/cyrene/workloads/web/cyrene-client-workspace-web")
WEB_CURRENT = WEB_ROOT / "current"
UPDATER_STATE_ROOT = Path("/var/lib/cyrene-updates")
INSTALLED_RECEIPTS = UPDATER_STATE_ROOT / "installed" / WEB_COMPONENT_ID
HOST_CONFIG_DIRECTORY = Path("/etc/cyrene/workloads/web")
HOST_CONFIG_PATH = HOST_CONFIG_DIRECTORY / "nginx.conf"
HOST_UNIT_PATH = Path("/etc/systemd/system/cyrene-workspace-web.service")
HOST_UNIT = "cyrene-workspace-web.service"
NGINX = Path("/usr/sbin/nginx")
NGINX_MIME_TYPES = Path("/etc/nginx/mime.types")
SYSTEMCTL = Path("/usr/bin/systemctl")
APT_GET = Path("/usr/bin/apt-get")
APT_CACHE = Path("/usr/bin/apt-cache")
DPKG_QUERY = Path("/usr/bin/dpkg-query")
GLOBAL_NGINX_UNIT_PATHS = (
    Path("/etc/systemd/system/nginx.service"),
    Path("/usr/lib/systemd/system/nginx.service"),
    Path("/lib/systemd/system/nginx.service"),
)
RUNTIME_NGINX_UNIT_MASK = Path("/run/systemd/system/nginx.service")
PACKAGE_NGINX_UNIT = Path("/lib/systemd/system/nginx.service")
OS_RELEASE_PATH = Path("/usr/lib/os-release")
LISTENER_ADDRESS = "127.0.0.1"
LISTENER_PORT = 8100
CONTROL_ADDRESS = "127.0.0.1"
CONTROL_PORT = 5182
PUBLIC_ORIGIN = f"http://{LISTENER_ADDRESS}:{LISTENER_PORT}"
MAX_MANAGED_FILE_BYTES = 64 * 1024
SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
POINTER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
CONFIG_MARKER = b"# cyrene-workload-web-host:1\n"
UNIT_MARKER = b"# cyrene-workload-web-unit:1\n"
NGINX_CONFIG_TEMPLATE = Path(__file__).with_name("cyrene-workspace-web.nginx.conf.template")
SYSTEMD_UNIT_TEMPLATE = Path(__file__).with_name("cyrene-workspace-web.service.template")

CommandRunner = Callable[[Sequence[str], Mapping[str, str]], subprocess.CompletedProcess[str]]
DurableCallback = Callable[[Mapping[str, Any]], None]


class WorkloadWebHostError(RuntimeError):
    """Raised when verified static content cannot be hosted or restored safely."""


NGINX_CONFIG = CONFIG_MARKER + NGINX_CONFIG_TEMPLATE.read_bytes()
SYSTEMD_UNIT = UNIT_MARKER + SYSTEMD_UNIT_TEMPLATE.read_bytes()


def _runner(
    arguments: Sequence[str], environment: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run a fixed host command with a small deterministic environment."""

    return subprocess.run(
        list(arguments),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=dict(environment),
    )


def _run(runner: CommandRunner, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one absolute-path command without inheriting operator credentials."""

    result = runner(
        arguments,
        {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()[:400]
        raise WorkloadWebHostError(f"{Path(arguments[0]).name} failed: {detail}")
    return result


def _require_root() -> None:
    """Require the privileged updater context for host configuration changes."""

    if os.geteuid() != 0:
        raise WorkloadWebHostError("Web host mutation requires the root updater context")


def _require_root_owned(info: os.stat_result, label: str) -> None:
    """Require root ownership on receipts and managed host configuration."""

    if info.st_uid != 0 or info.st_gid != 0:
        raise WorkloadWebHostError(f"{label} must be owned by root")


def _safe_path_info(
    path: Path, label: str, *, allow_missing: bool = False
) -> os.stat_result | None:
    """Walk a fixed absolute path without following symlinks."""

    if not path.is_absolute() or ".." in path.parts:
        raise WorkloadWebHostError(f"{label} path is not a normalized absolute path")
    current = Path(path.anchor)
    parts = path.parts[1:]
    for index, part in enumerate(parts):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                return None
            raise WorkloadWebHostError(f"{label} is missing") from None
        if stat.S_ISLNK(info.st_mode):
            if index == len(parts) - 1 and label == "static web current pointer":
                return info
            raise WorkloadWebHostError(f"{label} path contains a symbolic link")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise WorkloadWebHostError(f"{label} path has a non-directory component")
        if index < len(parts) - 1:
            _require_root_owned(info, f"{label} parent directory")
    return info if parts else current.lstat()


def _read_regular(path: Path, label: str, *, max_bytes: int = MAX_MANAGED_FILE_BYTES) -> bytes:
    """Read a root-owned regular file without following links or hardlink aliases."""

    info = _safe_path_info(path, label)
    assert info is not None
    _require_root_owned(info, label)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o022
        or info.st_size > max_bytes
    ):
        raise WorkloadWebHostError(f"{label} file metadata is unsafe")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        opened = os.fstat(descriptor)
        _require_root_owned(opened, label)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_dev != info.st_dev
            or opened.st_ino != info.st_ino
        ):
            raise WorkloadWebHostError(f"{label} changed while it was opened")
        payload = bytearray()
        while chunk := os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(payload))):
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise WorkloadWebHostError(f"{label} exceeds the size limit")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys in installed identity receipts."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkloadWebHostError("Installed web receipt repeats a JSON field")
        result[key] = value
    return result


def _digest(payload: bytes) -> str:
    """Return the canonical typed SHA-256 representation."""

    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _load_active_web_receipt() -> dict[str, Any]:
    """Verify updater receipts and the fixed current pointer before serving files."""

    active_raw = _read_regular(INSTALLED_RECEIPTS / "active.json", "active web receipt")
    try:
        active = json.loads(active_raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkloadWebHostError("Active web receipt is malformed") from error
    if (
        not isinstance(active, dict)
        or set(active) != {"schemaVersion", "componentId", "releaseIdentity", "bundleIdentity"}
        or active.get("schemaVersion") != 1
        or active.get("componentId") != WEB_COMPONENT_ID
        or not isinstance(active.get("releaseIdentity"), str)
        or SHA256_PATTERN.fullmatch(active["releaseIdentity"]) is None
        or not isinstance(active.get("bundleIdentity"), str)
    ):
        raise WorkloadWebHostError("Active web receipt identity is invalid")

    release_identity = active["releaseIdentity"]
    release_path = (
        INSTALLED_RECEIPTS / "releases" / f"{release_identity.removeprefix('sha256:')}.json"
    )
    release_raw = _read_regular(release_path, "verified web release receipt")
    try:
        receipt = json.loads(release_raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkloadWebHostError("Verified web release receipt is malformed") from error
    if (
        not isinstance(receipt, dict)
        or receipt.get("schemaVersion") != 2
        or receipt.get("componentId") != WEB_COMPONENT_ID
        or receipt.get("releaseIdentity") != release_identity
        or receipt.get("manifestDigest") != release_identity
        or receipt.get("bundleIdentity") != active["bundleIdentity"]
        or not isinstance(receipt.get("pointerIdentity"), str)
        or POINTER_PATTERN.fullmatch(receipt["pointerIdentity"]) is None
        or receipt.get("releasePath") != str(WEB_ROOT / "releases" / receipt["pointerIdentity"])
    ):
        raise WorkloadWebHostError("Verified web release receipt identity is invalid")
    manifest = receipt.get("manifest")
    artifact = manifest.get("artifact") if isinstance(manifest, dict) else None
    files = artifact.get("files") if isinstance(artifact, dict) else None
    expected_index = files.get("index.html") if isinstance(files, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("componentId") != WEB_COMPONENT_ID
        or artifact.get("kind") != "static-web"
        or artifact.get("entrypoint") != "index.html"
        or not isinstance(expected_index, str)
        or SHA256_PATTERN.fullmatch(expected_index) is None
    ):
        raise WorkloadWebHostError("Verified web manifest has no authenticated index.html")

    pointer_info = _safe_path_info(WEB_CURRENT, "static web current pointer")
    assert pointer_info is not None
    if not stat.S_ISLNK(pointer_info.st_mode):
        raise WorkloadWebHostError("Static web current pointer is not a root-owned symlink")
    _require_root_owned(pointer_info, "static web current pointer")
    target = os.readlink(WEB_CURRENT)
    if target != f"releases/{receipt['pointerIdentity']}":
        raise WorkloadWebHostError("Static web current pointer differs from its active receipt")
    release_directory = WEB_ROOT / "releases" / receipt["pointerIdentity"]
    release_info = _safe_path_info(release_directory, "active static web release")
    assert release_info is not None
    if not stat.S_ISDIR(release_info.st_mode):
        raise WorkloadWebHostError("Active static web release directory is unsafe")
    _require_root_owned(release_info, "active static web release")
    index_raw = _read_regular(
        release_directory / "index.html", "active web index", max_bytes=8 * 1024 * 1024
    )
    if _digest(index_raw) != expected_index:
        raise WorkloadWebHostError("Active web index digest differs from the verified manifest")
    return {
        "componentId": WEB_COMPONENT_ID,
        "version": receipt.get("version"),
        "releaseIdentity": release_identity,
        "pointerIdentity": receipt["pointerIdentity"],
        "sourceReceiptDigest": _digest(release_raw),
        "activeReceiptDigest": _digest(active_raw),
        "indexDigest": expected_index,
        "indexBytes": index_raw,
    }


def _managed_snapshot(path: Path, marker: bytes, label: str) -> bytes | None:
    """Read one owned file or reject an unmanaged collision."""

    try:
        payload = _read_regular(path, label)
    except WorkloadWebHostError as error:
        if "is missing" in str(error):
            return None
        raise
    if not payload.startswith(marker):
        raise WorkloadWebHostError(f"Refusing to replace an unmanaged {label}")
    return payload


def _systemctl_value(runner: CommandRunner, property_name: str) -> str:
    """Read one systemd property, treating a missing unit as an empty state."""

    result = runner(
        [str(SYSTEMCTL), "show", f"--property={property_name}", "--value", HOST_UNIT],
        {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _state_digest(payload: bytes | None) -> str | None:
    """Hash a snapshot file without exposing configuration content in status."""

    return _digest(payload) if payload is not None else None


def capture_web_host_state(*, runner: CommandRunner = _runner) -> dict[str, Any]:
    """Capture exact managed files and service state for the parent transaction journal."""

    _require_root()
    config = _managed_snapshot(HOST_CONFIG_PATH, CONFIG_MARKER, "Nginx configuration")
    unit = _managed_snapshot(HOST_UNIT_PATH, UNIT_MARKER, "systemd unit")
    active_state = _systemctl_value(runner, "ActiveState")
    if (config is None) != (unit is None):
        raise WorkloadWebHostError("Managed web service files are only partially installed")
    if config is None and unit is None and active_state == "active":
        raise WorkloadWebHostError("Web service is active without its managed unit files")
    enabled_result = runner(
        [str(SYSTEMCTL), "is-enabled", HOST_UNIT],
        {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )
    enabled = enabled_result.returncode == 0 and enabled_result.stdout.strip() in {
        "enabled",
        "enabled-runtime",
    }
    return {
        "schemaVersion": 1,
        "componentId": WEB_COMPONENT_ID,
        "configBytes": base64.b64encode(config).decode("ascii") if config is not None else None,
        "unitBytes": base64.b64encode(unit).decode("ascii") if unit is not None else None,
        "configDigest": _state_digest(config),
        "unitDigest": _state_digest(unit),
        "serviceState": active_state or "inactive",
        "serviceEnabled": enabled,
    }


def _decode_snapshot(state: Mapping[str, Any]) -> tuple[bytes | None, bytes | None]:
    """Validate journal-captured bytes before using them for rollback."""

    expected = {
        "schemaVersion",
        "componentId",
        "configBytes",
        "unitBytes",
        "configDigest",
        "unitDigest",
        "serviceState",
        "serviceEnabled",
    }
    if (
        set(state) != expected
        or state.get("schemaVersion") != 1
        or state.get("componentId") != WEB_COMPONENT_ID
    ):
        raise WorkloadWebHostError("Web host journal snapshot schema is invalid")
    decoded: list[bytes | None] = []
    for name, marker, digest_name in (
        ("configBytes", CONFIG_MARKER, "configDigest"),
        ("unitBytes", UNIT_MARKER, "unitDigest"),
    ):
        encoded = state.get(name)
        if encoded is None:
            if state.get(digest_name) is not None:
                raise WorkloadWebHostError("Web host journal snapshot digest is inconsistent")
            decoded.append(None)
            continue
        if not isinstance(encoded, str):
            raise WorkloadWebHostError("Web host journal snapshot is malformed")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise WorkloadWebHostError("Web host journal snapshot encoding is invalid") from error
        if (
            len(payload) > MAX_MANAGED_FILE_BYTES
            or not payload.startswith(marker)
            or _digest(payload) != state.get(digest_name)
        ):
            raise WorkloadWebHostError("Web host journal snapshot digest does not match")
        decoded.append(payload)
    if (decoded[0] is None) != (decoded[1] is None):
        raise WorkloadWebHostError("Web host journal snapshot contains a partial installation")
    if not isinstance(state.get("serviceState"), str) or not isinstance(
        state.get("serviceEnabled"), bool
    ):
        raise WorkloadWebHostError("Web host journal service state is malformed")
    return decoded[0], decoded[1]


def _ensure_directory(path: Path) -> None:
    """Create the fixed managed directory with root ownership and safe modes."""

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o755)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WorkloadWebHostError(f"Managed directory is unsafe: {current}")
        _require_root_owned(info, "managed directory")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise WorkloadWebHostError(
                f"Managed directory is writable by non-root users: {current}"
            )


def _fsync_directory(path: Path) -> None:
    """Persist an atomic file replacement in its parent directory."""

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o644) -> None:
    """Atomically create or replace a fixed managed file after collision checks."""

    _ensure_directory(path.parent)
    existing = _safe_path_info(path, "managed host file", allow_missing=True)
    if existing is not None:
        _require_root_owned(existing, "managed host file")
        if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
            raise WorkloadWebHostError("Managed host file is not a regular unaliased file")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        if os.geteuid() == 0:
            os.fchown(descriptor, 0, 0)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _remove_managed_file(path: Path, marker: bytes) -> None:
    """Unlink one exact managed file without touching parent data or siblings."""

    payload = _managed_snapshot(path, marker, path.name)
    if payload is None:
        return
    path.unlink()
    _fsync_directory(path.parent)


def _check_supported_host() -> None:
    """Require the fixed Ubuntu 24.04 x86_64 deployment target."""
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise WorkloadWebHostError("Web host supports Linux x86_64 only")
    try:
        os_release = _read_regular(
            OS_RELEASE_PATH, "Ubuntu release metadata", max_bytes=16 * 1024
        ).decode("utf-8")
    except (UnicodeDecodeError, WorkloadWebHostError) as error:
        raise WorkloadWebHostError("Ubuntu 24.04 release metadata is unavailable") from error
    fields = dict(
        match.groups()
        for line in os_release.splitlines()
        if (match := re.fullmatch(r'([A-Z_]+)="?([^"\n]*)"?', line)) is not None
    )
    if fields.get("ID") != "ubuntu" or fields.get("VERSION_ID") != "24.04":
        raise WorkloadWebHostError("Web host supports Ubuntu 24.04 only")


def _check_host_prerequisites(runner: CommandRunner) -> str:
    """Require Ubuntu 24.04, the system Nginx package, and the Cyrene account."""

    _check_supported_host()
    if not NGINX.is_file() or not NGINX_MIME_TYPES.is_file():
        raise WorkloadWebHostError(
            "Ubuntu nginx package is missing; install the trusted nginx apt package before maintenance"
        )
    if not Path("/run/systemd/system").exists() or not SYSTEMCTL.is_file():
        raise WorkloadWebHostError("systemd is required for the managed web service")
    try:
        pwd.getpwnam("cyrene")
    except KeyError as error:
        raise WorkloadWebHostError(
            "The Cyrene service account must exist before web activation"
        ) from error
    result = _run(runner, [str(NGINX), "-v"])
    version_text = result.stderr.strip() or result.stdout.strip()
    match = re.search(r"nginx/[0-9][A-Za-z0-9.+-]*", version_text)
    if match is None:
        raise WorkloadWebHostError("The installed nginx binary did not report a usable version")
    return match.group(0)


def check_web_host_prerequisites(*, runner: CommandRunner = _runner) -> dict[str, Any]:
    """Check host readiness so the caller can install Ubuntu's trusted Nginx package pre-hold."""

    return prepare_host_prerequisites(install_authorized=False, runner=runner)


def _apt_command(
    runner: CommandRunner, arguments: Sequence[str], *, noninteractive: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run fixed APT inspection or install commands with bounded package-manager env."""

    environment = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    if noninteractive:
        environment.update(
            {"DEBIAN_FRONTEND": "noninteractive", "APT_LISTCHANGES_FRONTEND": "none"}
        )
    return runner(arguments, environment)


def _apt_policy_sources(policy: str, version: str) -> list[dict[str, str]]:
    """Return signed Ubuntu Noble origins listed for one exact APT package version."""

    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in policy.splitlines():
        version_line = re.match(r"^\s+(?:\*\*\*\s+)?(\S+)\s+\d+\s*$", line)
        if version_line is not None:
            current = version_line.group(1)
            blocks.setdefault(current, [])
        elif current is not None:
            blocks[current].append(line)
    if version not in blocks:
        return []
    trusted_sources: set[tuple[str, str]] = set()
    for line in blocks[version]:
        source = re.search(
            r"https?://(?P<host>(?:archive|security)\.ubuntu\.com)/ubuntu\s+"
            r"(?P<suite>noble(?:-updates|-security)?)/main\s+amd64\s+Packages\b",
            line,
        )
        if source is not None:
            trusted_sources.add((source.group("host"), source.group("suite")))
    return [
        {"host": host, "suite": suite, "component": "main", "architecture": "amd64"}
        for host, suite in sorted(trusted_sources)
    ]


def _apt_policy_candidate(policy: str) -> tuple[str, list[dict[str, str]]]:
    """Select an exact candidate backed by a canonical signed Ubuntu Noble archive."""

    candidate_match = re.search(r"^\s+Candidate:\s+(\S+)\s*$", policy, re.MULTILINE)
    if candidate_match is None or candidate_match.group(1) == "(none)":
        raise WorkloadWebHostError(
            "Ubuntu APT has no nginx candidate; refresh trusted Ubuntu package indexes"
        )
    candidate = candidate_match.group(1)
    sources = _apt_policy_sources(policy, candidate)
    if not sources:
        raise WorkloadWebHostError(
            "nginx candidate is not available from an official Ubuntu Noble archive"
        )
    return candidate, sources


def _installed_package_version(runner: CommandRunner) -> str | None:
    """Read the installed Debian package version without shell evaluation."""

    result = runner(
        [str(DPKG_QUERY), "-W", "-f=${db:Status-Abbrev}|${Version}\\n", "nginx"],
        {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )
    status, separator, version = result.stdout.strip().partition("|")
    return (
        version
        if result.returncode == 0 and separator and status.startswith("ii ") and version
        else None
    )


def _nginx_service_state(runner: CommandRunner) -> dict[str, Any]:
    """Capture global nginx.service state without changing it."""

    return {
        "loadState": _systemctl_value_for_unit(runner, "LoadState", "nginx.service"),
        "activeState": _systemctl_value_for_unit(runner, "ActiveState", "nginx.service"),
        "enabled": _is_enabled(runner, "nginx.service"),
    }


def _systemctl_value_for_unit(runner: CommandRunner, property_name: str, unit: str) -> str:
    """Read a systemd property for one fixed package service."""

    result = runner(
        [str(SYSTEMCTL), "show", f"--property={property_name}", "--value", unit],
        {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _is_enabled(runner: CommandRunner, unit: str) -> bool:
    """Report whether a fixed systemd unit is enabled without modifying it."""

    result = runner(
        [str(SYSTEMCTL), "is-enabled", unit],
        {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )
    return result.returncode == 0 and result.stdout.strip() in {"enabled", "enabled-runtime"}


def _safe_apt_diagnostic(text: str) -> str:
    """Remove credential-bearing URL userinfo before persisting package errors."""

    return re.sub(r"(https?://)[^/@\s]+@", r"\1<redacted>@", text.strip())[:400]


def _remove_temporary_nginx_mask(runner: CommandRunner, *, owned: bool) -> list[str]:
    """Remove only this operation's runtime mask and reload systemd."""

    if not owned:
        return []
    runtime_mask = RUNTIME_NGINX_UNIT_MASK
    if not runtime_mask.exists() and not runtime_mask.is_symlink():
        return []
    if not runtime_mask.is_symlink() or os.readlink(runtime_mask) != "/dev/null":
        return ["temporary nginx.service mask path changed outside this operation"]
    environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    unmask = runner([str(SYSTEMCTL), "unmask", "--runtime", "nginx.service"], environment)
    if unmask.returncode != 0:
        return ["temporary nginx.service mask could not be removed"]
    reload = runner([str(SYSTEMCTL), "daemon-reload"], environment)
    if reload.returncode != 0:
        return ["systemd could not reload after removing the temporary mask"]
    return []


def prepare_host_prerequisites(
    *,
    install_authorized: bool,
    durable_callback: DurableCallback | None = None,
    runner: CommandRunner = _runner,
) -> dict[str, Any]:
    """Check or install the official Nginx host prerequisite before a maintenance hold.

    The caller decides whether the workload's explicit install authorization permits
    the bounded APT install. A newly installed package's default global service is
    prevented from starting during postinst and then left disabled; pre-existing
    nginx.service state and configuration are never changed.
    """

    _require_root()
    _check_supported_host()
    installed_version = _installed_package_version(runner)
    policy_result = _apt_command(runner, [str(APT_CACHE), "policy", "nginx"])
    if policy_result.returncode != 0:
        raise WorkloadWebHostError("Cannot read the configured Ubuntu APT policy for nginx")
    policy_digest = _digest(policy_result.stdout.encode("utf-8"))
    candidate, candidate_sources = _apt_policy_candidate(policy_result.stdout)
    if installed_version is not None:
        if not NGINX.is_file() or not NGINX_MIME_TYPES.is_file():
            raise WorkloadWebHostError("Installed nginx package files are incomplete")
        installed_sources = _apt_policy_sources(policy_result.stdout, installed_version)
        if not installed_sources:
            raise WorkloadWebHostError(
                "Installed nginx version has no matching trusted Ubuntu APT source"
            )
        version_result = _run(runner, [str(NGINX), "-v"])
        version_text = version_result.stderr.strip() or version_result.stdout.strip()
        version_match = re.search(r"nginx/[0-9][A-Za-z0-9.+-]*", version_text)
        if version_match is None:
            raise WorkloadWebHostError("Installed nginx binary version cannot be verified")
        result = {
            "schemaVersion": 1,
            "ready": True,
            "installedByThisOperation": False,
            "package": "nginx",
            "packageVersion": installed_version,
            "nginxVersion": version_match.group(0),
            "aptSource": installed_sources,
            "aptPolicyDigest": policy_digest,
            "defaultServicePreserved": _nginx_service_state(runner),
        }
        if durable_callback is not None:
            durable_callback(
                {"schemaVersion": 1, "kind": "host-prerequisite", "phase": "observed", **result}
            )
        return result

    if not install_authorized:
        return {
            "schemaVersion": 1,
            "ready": False,
            "installRequired": True,
            "requiresAuthorization": True,
            "package": "nginx",
            "candidateVersion": candidate,
            "aptSource": candidate_sources,
            "listenerAddress": LISTENER_ADDRESS,
            "listenerPort": LISTENER_PORT,
        }
    if durable_callback is None:
        raise WorkloadWebHostError(
            "A durable parent-journal callback is required for APT installation"
        )

    prior_service = _nginx_service_state(runner)
    if prior_service["loadState"] not in {"", "not-found"} or any(
        path.exists() for path in GLOBAL_NGINX_UNIT_PATHS
    ):
        raise WorkloadWebHostError(
            "nginx.service exists before the package install; refusing to alter a pre-existing global service"
        )

    simulation = _apt_command(
        runner,
        [
            str(APT_GET),
            "-s",
            "--no-install-recommends",
            "install",
            f"nginx={candidate}",
        ],
    )
    if simulation.returncode != 0:
        raise WorkloadWebHostError(
            "Trusted Ubuntu APT could not simulate the nginx prerequisite install"
        )
    simulation_lines = simulation.stdout.splitlines()
    install_lines = [line for line in simulation_lines if line.startswith("Inst ")]
    if (
        not install_lines
        or any(line.startswith("Remv ") for line in simulation_lines)
        or any(
            not re.search(r"\bUbuntu:24\.04/noble(?:-updates|-security)?\b", line)
            for line in install_lines
        )
    ):
        raise WorkloadWebHostError(
            "APT install plan contains non-Ubuntu or destructive package changes"
        )
    simulation_digest = _digest(simulation.stdout.encode("utf-8"))
    runtime_mask = Path("/run/systemd/system/nginx.service")
    mask_before = (
        runtime_mask.lstat() if runtime_mask.exists() or runtime_mask.is_symlink() else None
    )
    if mask_before is not None:
        if not stat.S_ISLNK(mask_before.st_mode) or os.readlink(runtime_mask) != "/dev/null":
            raise WorkloadWebHostError("A runtime nginx.service override exists; preserving it")
        owns_runtime_mask = False
    else:
        owns_runtime_mask = True
    durable_callback(
        {
            "schemaVersion": 1,
            "kind": "host-prerequisite",
            "phase": "prepared",
            "package": "nginx",
            "candidateVersion": candidate,
            "aptSource": candidate_sources,
            "aptPolicyDigest": policy_digest,
            "simulationDigest": simulation_digest,
            "priorGlobalService": prior_service,
            "temporaryServiceMaskOwned": owns_runtime_mask,
        }
    )
    install: subprocess.CompletedProcess[str] | None = None
    install_exception: Exception | None = None
    try:
        if owns_runtime_mask:
            _run(runner, [str(SYSTEMCTL), "mask", "--runtime", "nginx.service"])
            _run(runner, [str(SYSTEMCTL), "daemon-reload"])
        install = _apt_command(
            runner,
            [
                str(APT_GET),
                "--yes",
                "--no-install-recommends",
                "-o",
                "Dpkg::Options::=--force-confold",
                "install",
                f"nginx={candidate}",
            ],
            noninteractive=True,
        )
    except (WorkloadWebHostError, OSError, subprocess.SubprocessError) as error:
        install_exception = error
    cleanup_errors = _remove_temporary_nginx_mask(runner, owned=owns_runtime_mask)

    installed_version = _installed_package_version(runner)
    package_error = None
    if install_exception is not None:
        package_error = _safe_apt_diagnostic(str(install_exception))
    elif install is not None and install.returncode != 0:
        package_error = _safe_apt_diagnostic(
            install.stderr or install.stdout or "APT install failed"
        )
    elif installed_version != candidate:
        package_error = "installed package version differs from the pinned Ubuntu candidate"

    package_unit = PACKAGE_NGINX_UNIT
    service_after = _nginx_service_state(runner)
    if installed_version is not None and service_after["loadState"] not in {"", "not-found"}:
        fragment = _systemctl_value_for_unit(runner, "FragmentPath", "nginx.service")
        ownership = runner(
            [str(DPKG_QUERY), "-S", fragment or str(package_unit)],
            {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        )
        package_owned = (
            fragment.startswith(("/lib/systemd/system/", "/usr/lib/systemd/system/"))
            and ownership.returncode == 0
            and re.match(r"^nginx(?:-[A-Za-z0-9.+-]+)?:\s+", ownership.stdout) is not None
        )
        if package_owned:
            disable = runner(
                [str(SYSTEMCTL), "disable", "nginx.service"],
                {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            )
            if disable.returncode != 0:
                cleanup_errors.append("newly installed global nginx.service could not be disabled")
            if _systemctl_value_for_unit(runner, "ActiveState", "nginx.service") == "active":
                stop = runner(
                    [str(SYSTEMCTL), "stop", "nginx.service"],
                    {
                        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                        "LANG": "C.UTF-8",
                        "LC_ALL": "C.UTF-8",
                    },
                )
                if stop.returncode != 0:
                    cleanup_errors.append(
                        "newly installed global nginx.service could not be stopped"
                    )
        elif service_after["activeState"] == "active" or service_after["enabled"]:
            cleanup_errors.append(
                "new global nginx.service was not verified as owned by the installed Ubuntu package"
            )

    if cleanup_errors:
        durable_callback(
            {
                "schemaVersion": 1,
                "kind": "host-prerequisite",
                "phase": "cleanup-required",
                "package": "nginx",
                "packageVersion": installed_version,
                "cleanupErrors": cleanup_errors,
            }
        )
        raise WorkloadWebHostError(
            "APT completed but the temporary service-mask cleanup needs recovery"
        )
    if package_error is not None:
        durable_callback(
            {
                "schemaVersion": 1,
                "kind": "host-prerequisite",
                "phase": "failed",
                "package": "nginx",
                "packageVersion": installed_version,
                "diagnostic": package_error,
            }
        )
        raise WorkloadWebHostError(f"Trusted Ubuntu nginx installation failed: {package_error}")
    if not NGINX.is_file() or not NGINX_MIME_TYPES.is_file():
        raise WorkloadWebHostError("APT reported success but the nginx runtime files are missing")
    version_result = _run(runner, [str(NGINX), "-v"])
    version_text = version_result.stderr.strip() or version_result.stdout.strip()
    version_match = re.search(r"nginx/[0-9][A-Za-z0-9.+-]*", version_text)
    if version_match is None:
        raise WorkloadWebHostError("Installed nginx binary version cannot be verified")
    result = {
        "schemaVersion": 1,
        "ready": True,
        "installedByThisOperation": True,
        "package": "nginx",
        "packageVersion": installed_version,
        "nginxVersion": version_match.group(0),
        "aptSource": candidate_sources,
        "aptPolicyDigest": policy_digest,
        "simulationDigest": simulation_digest,
        "defaultServicePreserved": prior_service,
        "newDefaultServiceDisabled": True,
    }
    durable_callback(
        {"schemaVersion": 1, "kind": "host-prerequisite", "phase": "installed", **result}
    )
    return result


def _port_is_open() -> bool:
    """Probe the fixed loopback listener without binding or changing host state."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.2)
        return connection.connect_ex((LISTENER_ADDRESS, LISTENER_PORT)) == 0


def _http_get(
    url: str, *, headers: Mapping[str, str] | None = None, timeout: float = 3.0
) -> tuple[int, bytes, str]:
    """Read one bounded loopback HTTP response, including explicit error statuses."""

    request = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    except (OSError, TimeoutError) as error:
        raise WorkloadWebHostError(f"Loopback web probe failed for {url}") from error
    with response:
        body = response.read(8 * 1024 * 1024 + 1)
        if len(body) > 8 * 1024 * 1024:
            raise WorkloadWebHostError("Loopback web probe response exceeded the size limit")
        return int(response.status), body, response.headers.get("Content-Type", "")


def _probe_host(version: str, source: Mapping[str, Any]) -> dict[str, Any]:
    """Prove static delivery, Control readiness, and its local session projection."""

    root_status, root_body, _ = _http_get(
        f"{PUBLIC_ORIGIN}/", headers={"Accept-Encoding": "identity", "Origin": PUBLIC_ORIGIN}
    )
    if root_status != 200 or _digest(root_body) != source["indexDigest"]:
        raise WorkloadWebHostError("Web root did not return the verified index.html")
    health_status, health_body, _ = _http_get(f"{PUBLIC_ORIGIN}/healthz")
    if health_status != 200 or json.loads(health_body).get("status") != "ok":
        raise WorkloadWebHostError("Web health endpoint is not ready")

    ready_status, ready_body, _ = _http_get(f"http://{CONTROL_ADDRESS}:{CONTROL_PORT}/health/ready")
    if ready_status != 200 or json.loads(ready_body).get("status") != "ready":
        raise WorkloadWebHostError("Studio Control readiness probe failed")
    session_status, session_body, _ = _http_get(
        f"{PUBLIC_ORIGIN}/api/v1/auth/session",
        headers={"Host": f"{LISTENER_ADDRESS}:{LISTENER_PORT}", "Origin": PUBLIC_ORIGIN},
    )
    try:
        session = json.loads(session_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkloadWebHostError("Local Control session response is not JSON") from error
    if (
        session_status != 200
        or not isinstance(session, dict)
        or session.get("authenticated") is not True
        or session.get("state") != "AUTHENTICATED"
        or session.get("refreshable") is not False
    ):
        raise WorkloadWebHostError("Same-origin local Control session contract is unavailable")

    return {
        "root": {"status": root_status, "indexDigest": _digest(root_body)},
        "health": {"status": health_status, "bodyStatus": "ok"},
        "controlReady": {"status": ready_status, "bodyStatus": "ready"},
        "localSession": {
            "status": session_status,
            "authenticated": True,
            "state": "AUTHENTICATED",
            "refreshable": False,
        },
        "nginxVersion": version,
    }


def _receipt_for_state(source: Mapping[str, Any], probes: Mapping[str, Any]) -> dict[str, Any]:
    """Build the safe, serializable Web host receipt stored by the updater."""

    return {
        "schemaVersion": 1,
        "componentId": WEB_COMPONENT_ID,
        "version": source["version"],
        "releaseIdentity": source["releaseIdentity"],
        "sourceReceiptDigest": source["sourceReceiptDigest"],
        "activeReceiptDigest": source["activeReceiptDigest"],
        "documentRoot": str(WEB_CURRENT),
        "clientUrl": f"{PUBLIC_ORIGIN}/",
        "listenerAddress": LISTENER_ADDRESS,
        "listenerPort": LISTENER_PORT,
        "configPath": str(HOST_CONFIG_PATH),
        "configDigest": _digest(NGINX_CONFIG),
        "unitPath": str(HOST_UNIT_PATH),
        "unitDigest": _digest(SYSTEMD_UNIT),
        "serviceUnit": HOST_UNIT,
        "serviceState": "active",
        "serviceEnabled": True,
        "nginxVersion": probes["nginxVersion"],
        "probes": dict(probes),
    }


def _restore_snapshot(
    prior_state: Mapping[str, Any],
    *,
    runner: CommandRunner,
    expected_current_digest: str | None = None,
) -> None:
    """Restore exact prior files and service state after a failed transition."""

    previous_config, previous_unit = _decode_snapshot(prior_state)
    if expected_current_digest is not None:
        actual = _managed_snapshot(HOST_CONFIG_PATH, CONFIG_MARKER, "Nginx configuration")
        if actual is None or _digest(actual) != expected_current_digest:
            raise WorkloadWebHostError(
                "Current Web host configuration changed outside this transaction"
            )
    if previous_config is None:
        _run(runner, [str(SYSTEMCTL), "stop", HOST_UNIT])
        _run(runner, [str(SYSTEMCTL), "disable", HOST_UNIT])
        _remove_managed_file(HOST_CONFIG_PATH, CONFIG_MARKER)
        _remove_managed_file(HOST_UNIT_PATH, UNIT_MARKER)
    else:
        assert previous_unit is not None
        _atomic_write(HOST_CONFIG_PATH, previous_config)
        _atomic_write(HOST_UNIT_PATH, previous_unit)
        _run(runner, [str(SYSTEMCTL), "daemon-reload"])
        if prior_state["serviceEnabled"]:
            _run(runner, [str(SYSTEMCTL), "enable", HOST_UNIT])
        else:
            runner(
                [str(SYSTEMCTL), "disable", HOST_UNIT],
                {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            )
        if prior_state["serviceState"] == "active":
            _run(runner, [str(SYSTEMCTL), "restart", HOST_UNIT])
        else:
            runner(
                [str(SYSTEMCTL), "stop", HOST_UNIT],
                {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            )


def apply_web_host(
    *,
    expected_state: Mapping[str, Any],
    durable_callback: DurableCallback,
    runner: CommandRunner = _runner,
) -> dict[str, Any]:
    """Install the fixed Nginx service and verify the real local Client routes."""

    _require_root()
    if not callable(durable_callback):
        raise WorkloadWebHostError("A durable parent-journal callback is required")
    source = _load_active_web_receipt()
    prior_state = capture_web_host_state(runner=runner)
    _decode_snapshot(expected_state)
    if dict(expected_state) != prior_state:
        raise WorkloadWebHostError("Managed Web host state changed after workload planning")
    version = _check_host_prerequisites(runner)
    if _port_is_open() and prior_state["serviceState"] != "active":
        raise WorkloadWebHostError("Loopback port 8100 is already occupied by an unmanaged service")
    intended_receipt = _receipt_for_state(source, {"nginxVersion": version, "pending": True})
    durable_callback(
        {
            "schemaVersion": 1,
            "kind": "web-host",
            "phase": "prepared",
            "priorState": dict(prior_state),
            "intendedReceipt": {
                key: value for key, value in intended_receipt.items() if key != "probes"
            },
        }
    )
    try:
        _atomic_write(HOST_CONFIG_PATH, NGINX_CONFIG)
        _atomic_write(HOST_UNIT_PATH, SYSTEMD_UNIT)
        _run(runner, [str(NGINX), "-t", "-q", "-c", str(HOST_CONFIG_PATH)])
        durable_callback(
            {
                "schemaVersion": 1,
                "kind": "web-host",
                "phase": "configured",
                "configDigest": _digest(NGINX_CONFIG),
                "unitDigest": _digest(SYSTEMD_UNIT),
            }
        )
        _run(runner, [str(SYSTEMCTL), "daemon-reload"])
        _run(runner, [str(SYSTEMCTL), "enable", HOST_UNIT])
        _run(
            runner,
            [
                str(SYSTEMCTL),
                "restart" if prior_state["serviceState"] == "active" else "start",
                HOST_UNIT,
            ],
        )
        durable_callback(
            {"schemaVersion": 1, "kind": "web-host", "phase": "started", "serviceUnit": HOST_UNIT}
        )
        probes = _probe_host(version, source)
        receipt = _receipt_for_state(source, probes)
        durable_callback(
            {"schemaVersion": 1, "kind": "web-host", "phase": "healthy", "receipt": receipt}
        )
        return receipt
    except Exception as failure:
        try:
            _restore_snapshot(
                prior_state, runner=runner, expected_current_digest=_digest(NGINX_CONFIG)
            )
        except Exception as rollback_error:
            raise WorkloadWebHostError(
                f"Web host apply failed ({failure}); previous host state could not be restored ({rollback_error})"
            ) from rollback_error
        raise WorkloadWebHostError(
            f"Web host apply failed and prior state was restored: {failure}"
        ) from failure


def rollback_web_host(
    *,
    prior_state: Mapping[str, Any],
    expected_current_digest: str,
    runner: CommandRunner = _runner,
) -> dict[str, Any]:
    """Restore a parent transaction's exact pre-apply Web host snapshot."""

    _require_root()
    if SHA256_PATTERN.fullmatch(expected_current_digest) is None:
        raise WorkloadWebHostError("Expected current configuration digest is malformed")
    _restore_snapshot(prior_state, runner=runner, expected_current_digest=expected_current_digest)
    return capture_web_host_state(runner=runner)


def remove_web_host(
    *,
    expected_state: Mapping[str, Any],
    durable_callback: DurableCallback,
    runner: CommandRunner = _runner,
) -> dict[str, Any]:
    """Stop and remove only this managed service configuration; preserve all data."""

    _require_root()
    if not callable(durable_callback):
        raise WorkloadWebHostError("A durable parent-journal callback is required")
    prior_state = capture_web_host_state(runner=runner)
    _decode_snapshot(expected_state)
    if dict(expected_state) != prior_state:
        raise WorkloadWebHostError("Managed Web host state changed before uninstall")
    if prior_state["configBytes"] is None:
        return {
            "schemaVersion": 1,
            "componentId": WEB_COMPONENT_ID,
            "removed": False,
            "dataPreserved": True,
        }
    durable_callback(
        {
            "schemaVersion": 1,
            "kind": "web-host",
            "phase": "remove-prepared",
            "priorState": dict(prior_state),
        }
    )
    try:
        _run(runner, [str(SYSTEMCTL), "stop", HOST_UNIT])
        _run(runner, [str(SYSTEMCTL), "disable", HOST_UNIT])
        _remove_managed_file(HOST_CONFIG_PATH, CONFIG_MARKER)
        _remove_managed_file(HOST_UNIT_PATH, UNIT_MARKER)
        _run(runner, [str(SYSTEMCTL), "daemon-reload"])
        durable_callback(
            {"schemaVersion": 1, "kind": "web-host", "phase": "removed", "serviceUnit": HOST_UNIT}
        )
    except Exception as failure:
        try:
            _restore_snapshot(prior_state, runner=runner)
        except Exception as rollback_error:
            raise WorkloadWebHostError(
                f"Web host removal failed ({failure}); service restoration failed ({rollback_error})"
            ) from rollback_error
        raise WorkloadWebHostError(
            f"Web host removal failed and prior service was restored: {failure}"
        ) from failure
    return {
        "schemaVersion": 1,
        "componentId": WEB_COMPONENT_ID,
        "removed": True,
        "dataPreserved": True,
    }


def read_web_host_status(
    *,
    component_id: str = WEB_COMPONENT_ID,
    expected_source_receipt: Mapping[str, Any] | None = None,
    runner: CommandRunner = _runner,
) -> dict[str, Any]:
    """Read actual unit, receipt, fixed listener, and browser routes without journaling."""

    if component_id != WEB_COMPONENT_ID:
        raise WorkloadWebHostError("Only the official Workspace web component is supported")
    prior = capture_web_host_state(runner=runner)
    if prior["configBytes"] is None:
        return {
            "schemaVersion": 1,
            "componentId": WEB_COMPONENT_ID,
            "installed": False,
            "available": False,
            "clientUrl": f"{PUBLIC_ORIGIN}/",
            "serviceUnit": HOST_UNIT,
            "serviceState": prior["serviceState"],
            "serviceEnabled": prior["serviceEnabled"],
        }
    if _decode_snapshot(prior)[0] != NGINX_CONFIG or _decode_snapshot(prior)[1] != SYSTEMD_UNIT:
        raise WorkloadWebHostError("Managed Web host configuration differs from this release")
    source = _load_active_web_receipt()
    if expected_source_receipt is not None:
        for key in ("componentId", "releaseIdentity", "sourceReceiptDigest"):
            if expected_source_receipt.get(key) != source.get(key):
                raise WorkloadWebHostError(
                    "Active web receipt differs from the workload status record"
                )
    if prior["serviceState"] != "active" or not prior["serviceEnabled"]:
        raise WorkloadWebHostError("Managed Web host service is not active and enabled")
    version_result = _run(runner, [str(NGINX), "-v"])
    version_text = version_result.stderr.strip() or version_result.stdout.strip()
    match = re.search(r"nginx/[0-9][A-Za-z0-9.+-]*", version_text)
    if match is None:
        raise WorkloadWebHostError("Installed Nginx version cannot be read")
    probes = _probe_host(match.group(0), source)
    receipt = _receipt_for_state(source, probes)
    if (
        receipt["configDigest"] != prior["configDigest"]
        or receipt["unitDigest"] != prior["unitDigest"]
    ):
        raise WorkloadWebHostError(
            "Managed Web host receipt differs from the running configuration"
        )
    return {**receipt, "installed": True, "available": True}


__all__ = [
    "HOST_UNIT",
    "LISTENER_ADDRESS",
    "LISTENER_PORT",
    "PUBLIC_ORIGIN",
    "WEB_COMPONENT_ID",
    "WorkloadWebHostError",
    "apply_web_host",
    "capture_web_host_state",
    "check_web_host_prerequisites",
    "read_web_host_status",
    "remove_web_host",
    "rollback_web_host",
]
