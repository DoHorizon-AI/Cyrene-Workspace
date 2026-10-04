#!/usr/bin/env python3
"""
┌─────────────────────────────────────────────────────────────────────┐
│ Module: tooling.acceptance.native_components_v2.admin_initialize   │
│ Role: Run the pinned, stage-only native host initialization.          │
│                                                                      │
│ 模块职责：按已验证摘要执行一次性、仅暂存的主机初始化。                  │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import grp
import hashlib
import importlib.util
import json
import os
import platform
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from native_acceptance import AcceptanceError, verify_native_release_for_host

WORKSPACE_REPOSITORY = "DoHorizon-AI/Cyrene-Workspace"
BOOTSTRAP_COMPONENT = "cyrene-runtime-maintenance"
BOOTSTRAP_UNIT = "cyrene-runtime-maintenance.service"
PRIVATE_PYTHON = Path("/opt/cyrene/python/3.12.14/bin/python3.12")
BOOTSTRAP_HELPER_RELATIVE = PurePosixPath("share/cyrene-managed-runtime/cyrene_managed_runtime.py")
COMPONENT_UPDATE_HELPER = Path("/usr/libexec/cyrene-component-update-helper")
OPERATOR_TOOLS_LOCK = Path(__file__).with_name("operator-tools.lock.json")
PERSISTENT_GH = Path("/usr/libexec/cyrene-tools/gh")
OPERATOR_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
ADMIN_ROOT_UID = 0
ADMIN_ROOT_GID = 0
FIRST_PRODUCT_COHORT_RECEIPT = Path(
    "/var/lib/cyrene/native-initialization/first-product-cohort.json"
)
FIRST_PRODUCT_SERVICE_ORDER = ("navigator", "yield", "reactor", "exchange", "catalyst")
FIRST_PRODUCT_PLATFORM_TARGETS = {
    "linux-ubuntu-22.04-x86_64-python-3.12": "linux-ubuntu-22.04-x86_64-systemd",
    "linux-ubuntu-24.04-x86_64-python-3.12": "linux-ubuntu-24.04-x86_64-systemd",
}
SERVICE_REPOSITORIES = {
    "navigator": "Cyrene-Navigator",
    "yield": "Cyrene-Yield",
    "reactor": "Cyrene-Reactor",
    "exchange": "Cyrene-Exchange",
    "catalyst": "Cyrene-Catalyst",
}
PRODUCT_SOURCE_IDS = frozenset(
    {
        "cyrene-catalyst",
        "cyrene-exchange",
        "cyrene-navigator",
        "cyrene-reactor",
        "cyrene-yield",
    }
)
EXPECTED_INITIALIZATION_CHECKS = {
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
BACKUP_PATHS = (
    "/etc/cyrene",
    "/etc/systemd/system/cyrene-runtime-maintenance.service",
    "/etc/systemd/system/cyrene-runtime-maintenance.service.d",
    "/etc/systemd/system/cyrene-kernel.service.d",
    "/etc/systemd/system/cyrene-sandboxd.service.d",
    "/etc/systemd/system/cyrene-linux-sys-adapter.service.d",
    "/etc/systemd/system/cyrene-nvidia-adapter.service.d",
    "/usr/lib/systemd/system/cyrene-runtime-maintenance.service",
    "/usr/lib/systemd/system/cyrene-kernel.service",
    "/usr/lib/systemd/system/cyrene-sandboxd.service",
    "/usr/lib/systemd/system/cyrene-linux-sys-adapter.service",
    "/usr/lib/systemd/system/cyrene-nvidia-adapter.service",
    "/usr/libexec/cyrene-tools",
    "/usr/lib/cyrene/components",
    "/opt/cyrene/python/3.12.14",
    "/var/lib/cyrene",
    "/var/lib/cyrene-updates",
)
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RAW_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
PRODUCT_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


class AdminInitializationError(RuntimeError):
    """A pinned initialization precondition or trusted operation failed."""


def _sha256_bytes(payload: bytes) -> str:
    """Return a prefixed SHA-256 digest for immutable input bytes.

    中文：计算不可变输入字节的 SHA-256 摘要。
    """

    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    """Hash a regular file without exposing its contents.

    中文：只计算普通文件摘要，不输出文件内容。
    """

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _private_regular_file(path: Path, label: str) -> bytes:
    """Read one non-symlink regular input and return its exact bytes."""

    if path.is_symlink() or not path.is_file():
        raise AdminInitializationError(f"{label} must be a regular file, not a symlink")
    return path.read_bytes()


def _run(
    arguments: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 300,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Run one fixed argv without a shell and keep command output in memory."""

    try:
        result = runner(
            arguments,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AdminInitializationError(
            f"Trusted command could not complete: {Path(arguments[0]).name}"
        ) from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:500]
        raise AdminInitializationError(
            f"{Path(arguments[0]).name} failed ({result.returncode}): {detail}"
        )
    return result


def _json_result(result: subprocess.CompletedProcess[str], label: str) -> dict[str, Any]:
    """Parse one bounded JSON result and reject malformed command output."""

    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AdminInitializationError(f"{label} did not return valid JSON") from error
    if not isinstance(value, dict):
        raise AdminInitializationError(f"{label} returned an invalid result object")
    return value


def _verify_bootstrap_plan(plan: dict[str, Any], target_id: str) -> str:
    """Accept only the exact read-only plan shape returned by the helper."""

    plan_digest = plan.get("planDigest")
    if (
        plan.get("status") != "confirmation_required"
        or not isinstance(plan_digest, str)
        or not SHA256_RE.fullmatch(plan_digest)
        or plan.get("componentId") != BOOTSTRAP_COMPONENT
        or plan.get("targetId") != target_id
        or not isinstance(plan.get("version"), str)
        or not isinstance(plan.get("manifestDigest"), str)
        or not SHA256_RE.fullmatch(plan["manifestDigest"])
        or not isinstance(plan.get("artifactDigest"), str)
        or not SHA256_RE.fullmatch(plan["artifactDigest"])
        or not isinstance(plan.get("indexDigest"), str)
        or not SHA256_RE.fullmatch(plan["indexDigest"])
    ):
        raise AdminInitializationError("Broker bootstrap did not return a canonical exact plan")
    return plan_digest


def _require_root() -> None:
    """Require the explicit administrator invocation before reading target state."""

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise AdminInitializationError("Administrator initialization must run as root")


def _host_ubuntu_version(root: Path) -> str:
    """Derive the release target from the host's current OS facts."""

    release_path = root / "etc/os-release"
    try:
        release_info = os.lstat(release_path)
    except OSError as error:
        raise AdminInitializationError("Host OS release identity is unavailable") from error

    try:
        root_path = root.resolve(strict=True)
    except OSError as error:
        raise AdminInitializationError("Host OS release identity is unavailable") from error
    if stat.S_ISLNK(release_info.st_mode):
        # Ubuntu's canonical relative link is allowed; symlink mode bits are fixed at 0777.
        # Ubuntu 的标准链接模式位固定为 0777，因此校验所有者与精确目标。
        try:
            if release_info.st_uid != 0 or os.readlink(release_path) != "../usr/lib/os-release":
                raise AdminInitializationError("Host OS release identity is unavailable")
            if release_path.resolve(strict=True) != root_path / "usr/lib/os-release":
                raise AdminInitializationError("Host OS release identity is unavailable")
        except OSError as error:
            raise AdminInitializationError("Host OS release identity is unavailable") from error
        release_path = root / "usr/lib/os-release"
    elif not stat.S_ISREG(release_info.st_mode):
        raise AdminInitializationError("Host OS release identity is unavailable")
    else:
        try:
            if release_path.resolve(strict=True) != root_path / "etc/os-release":
                raise AdminInitializationError("Host OS release identity is unavailable")
        except OSError as error:
            raise AdminInitializationError("Host OS release identity is unavailable") from error

    try:
        target_info = os.lstat(release_path)
    except OSError as error:
        raise AdminInitializationError("Host OS release identity is unavailable") from error
    if not stat.S_ISREG(target_info.st_mode):
        raise AdminInitializationError("Host OS release identity is unavailable")

    try:
        descriptor = os.open(
            release_path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        with os.fdopen(descriptor, "r", encoding="utf-8") as release_file:
            file_info = os.fstat(release_file.fileno())
            if (
                not stat.S_ISREG(file_info.st_mode)
                or file_info.st_uid != 0
                or file_info.st_mode & 0o022
            ):
                raise AdminInitializationError("Host OS release identity is unavailable")
            release_text = release_file.read()
    except (OSError, UnicodeError) as error:
        raise AdminInitializationError("Host OS release identity is unavailable") from error

    fields: dict[str, str] = {}
    for line in release_text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value.strip().strip('"')
    if fields.get("ID") != "ubuntu" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise AdminInitializationError("Initialization supports only Ubuntu x86_64 hosts")
    version = fields.get("VERSION_ID", "")
    if version not in {"22.04", "24.04"}:
        raise AdminInitializationError("Host Ubuntu release is outside the signed DEB targets")
    return version


def _ensure_private_directory(path: Path) -> None:
    """Create or validate a root-owned private directory without following links."""

    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise AdminInitializationError(f"Private directory is unsafe: {path}")


def _ensure_operator_gh_config() -> None:
    """Create the pinned CLI's root-only config beneath trusted Cyrene state."""

    for parent in (Path("/var"), Path("/var/lib")):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or parent.is_symlink() or info.st_uid != 0:
            raise AdminInitializationError(f"GitHub CLI config parent is unsafe: {parent}")
    cyrene_state = Path("/var/lib/cyrene")
    if not cyrene_state.exists():
        cyrene_state.mkdir(mode=0o755)
        os.chown(cyrene_state, 0, 0)
        os.chmod(cyrene_state, 0o755)
    state_info = cyrene_state.lstat()
    if not stat.S_ISDIR(state_info.st_mode) or cyrene_state.is_symlink() or state_info.st_uid != 0:
        raise AdminInitializationError(f"GitHub CLI config parent is unsafe: {cyrene_state}")
    _ensure_private_directory(Path("/var/lib/cyrene/operator-tools/gh-config"))


def _locked_github_cli_digest(lock_path: Path = OPERATOR_TOOLS_LOCK) -> str:
    """Read the fixed GitHub CLI binary digest from the exact bundled lock."""

    try:
        value = json.loads(lock_path.read_text(encoding="utf-8"))
        gh = value["githubCli"]
        digest = gh["binarySha256"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise AdminInitializationError(
            "Pinned GitHub CLI lock is unavailable or invalid"
        ) from error
    if (
        value.get("schemaVersion") != 1
        or gh.get("version") != "2.97.0"
        or gh.get("repository") != "cli/cli"
        or gh.get("tag") != "v2.97.0"
        or not re.fullmatch(r"[0-9a-f]{64}", str(digest))
    ):
        raise AdminInitializationError("Pinned GitHub CLI lock is unsupported")
    return str(digest)


def _persist_locked_github_cli(
    source: Path, *, destination: Path = PERSISTENT_GH
) -> dict[str, str]:
    """Atomically install the exact pinned CLI for later restricted updates."""

    expected_digest = _locked_github_cli_digest()
    if (
        source.is_symlink()
        or not source.is_file()
        or _sha256_file(source) != f"sha256:{expected_digest}"
    ):
        raise AdminInitializationError("Packet GitHub CLI does not match the fixed binary SHA-256")
    directory = destination.parent
    for parent in (Path("/usr"), Path("/usr/libexec")):
        parent_info = parent.lstat()
        if not stat.S_ISDIR(parent_info.st_mode) or parent.is_symlink() or parent_info.st_uid != 0:
            raise AdminInitializationError(f"Persistent GitHub CLI parent is unsafe: {parent}")
    if directory.is_symlink():
        raise AdminInitializationError("Persistent GitHub CLI directory must not be a symlink")
    if not directory.exists():
        directory.mkdir(mode=0o755, parents=False, exist_ok=False)
        os.chown(directory, 0, 0)
        os.chmod(directory, 0o755)
    directory_info = directory.lstat()
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or directory_info.st_uid != 0
        or stat.S_IMODE(directory_info.st_mode) != 0o755
    ):
        raise AdminInitializationError("Persistent GitHub CLI directory is not root-owned mode 755")
    if destination.is_symlink():
        raise AdminInitializationError("Persistent GitHub CLI destination must not be a symlink")
    if destination.exists():
        info = destination.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o755
            or _sha256_file(destination) != f"sha256:{expected_digest}"
        ):
            raise AdminInitializationError(
                "Existing persistent GitHub CLI is not the verified root-owned pinned binary"
            )
        _ensure_operator_gh_config()
        return {"path": str(destination), "sha256": f"sha256:{expected_digest}"}

    _ensure_operator_gh_config()

    temporary: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=".gh.", dir=directory)
        temporary = Path(temporary_name)
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_stream:
            shutil.copyfileobj(input_stream, output)
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary, 0, 0)
        os.chmod(temporary, 0o755)
        if _sha256_file(temporary) != f"sha256:{expected_digest}":
            raise AdminInitializationError("Copied GitHub CLI changed before installation")
        os.replace(temporary, destination)
        temporary = None
        directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise AdminInitializationError(
            f"Persistent GitHub CLI could not be installed: {error}"
        ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"path": str(destination), "sha256": f"sha256:{expected_digest}"}


DEPENDENCY_RE = re.compile(r"^([a-z0-9][a-z0-9+.-]*)(?:\s*\((<<|<=|=|>=|>>)\s*([^()]+)\))?$")


def _preflight_deb_dependencies(
    deb_path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Require every generated DEB dependency to be installed before any host writes."""

    def invoke(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return runner(
                arguments,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AdminInitializationError("Cannot preflight signed DEB dependencies") from error

    metadata = invoke(["/usr/bin/dpkg-deb", "--field", str(deb_path), "Depends"])
    if metadata.returncode != 0:
        raise AdminInitializationError("Cannot read Depends from the signed DEB")
    package_architecture = invoke(["/usr/bin/dpkg-deb", "--field", str(deb_path), "Architecture"])
    if package_architecture.returncode != 0:
        raise AdminInitializationError("Cannot read Architecture from the signed DEB")
    package_architecture = package_architecture.stdout.strip()
    native_architecture = invoke(["/usr/bin/dpkg", "--print-architecture"])
    if native_architecture.returncode != 0 or not native_architecture.stdout.strip():
        raise AdminInitializationError("Cannot determine the host's native Debian architecture")
    native_architecture = native_architecture.stdout.strip()
    if package_architecture not in {native_architecture, "all"}:
        raise AdminInitializationError(
            "Signed DEB architecture does not match the host's native architecture"
        )
    expression = metadata.stdout.strip()
    if not expression:
        return
    for raw_group in expression.split(","):
        alternatives: list[tuple[str, str | None, str | None]] = []
        for raw_alternative in raw_group.split("|"):
            match = DEPENDENCY_RE.fullmatch(raw_alternative.strip())
            if match is None:
                raise AdminInitializationError(
                    f"Unsupported signed DEB dependency syntax: {raw_alternative.strip()}"
                )
            alternatives.append((match.group(1), match.group(2), match.group(3)))
        satisfied = False
        for package, operator, required_version in alternatives:
            query = invoke(
                [
                    "/usr/bin/dpkg-query",
                    "--show",
                    "--showformat=${Package}\\t${Architecture}\\t${db:Status-Abbrev}\\t${Version}\\n",
                    package,
                ]
            )
            if query.returncode != 0:
                continue
            for row in query.stdout.splitlines():
                fields = row.split("\t", maxsplit=3)
                if len(fields) != 4:
                    continue
                installed_name, architecture, status, version = fields
                if (
                    installed_name.split(":", maxsplit=1)[0] != package
                    or architecture not in {native_architecture, "all"}
                    or status.strip() != "ii"
                ):
                    continue
                if operator is not None:
                    compare = invoke(
                        [
                            "/usr/bin/dpkg",
                            "--compare-versions",
                            version,
                            operator,
                            required_version or "",
                        ]
                    )
                    if compare.returncode != 0:
                        continue
                satisfied = True
                break
            if satisfied:
                break
        if not satisfied:
            choices = " | ".join(item[0] for item in alternatives)
            raise AdminInitializationError(
                f"Signed DEB dependency is not installed; install before initialization: {choices}"
            )


def _backup_archive(archive_path: Path, *, root: Path = Path("/")) -> dict[str, Any]:
    """Create and reopen a root-only backup of installation-critical state.

    The archive preserves symlinks as links and never follows them. It excludes
    model payloads and task data outside the listed installation paths.
    中文：备份配置、原生指针、私有运行时与维护状态；不读取或打印凭据内容。
    """

    _require_root()
    if archive_path.is_symlink() or archive_path.exists():
        raise AdminInitializationError("Backup archive path must be new and not a symlink")
    _ensure_private_directory(archive_path.parent)
    selected: list[tuple[Path, str]] = []
    for absolute in BACKUP_PATHS:
        source = root / absolute.lstrip("/")
        if not os.path.lexists(source):
            continue
        if source.is_symlink():
            raise AdminInitializationError(f"Backup root path is a symlink: {absolute}")
        selected.append((source, absolute.lstrip("/")))

    try:
        with tarfile.open(archive_path, mode="x:gz", format=tarfile.PAX_FORMAT) as archive:
            for source, archive_name in selected:
                archive.add(source, arcname=archive_name, recursive=True)
        os.chmod(archive_path, 0o600)
        os.chown(archive_path, 0, 0)
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                if member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise AdminInitializationError("Backup archive has unreadable file data")
                    while stream.read(1024 * 1024):
                        pass
    except (OSError, tarfile.TarError) as error:
        archive_path.unlink(missing_ok=True)
        raise AdminInitializationError(f"Backup archive could not be verified: {error}") from error

    info = archive_path.lstat()
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise AdminInitializationError("Backup archive ownership or mode is unsafe")
    return {
        "path": str(archive_path),
        "sha256": _sha256_file(archive_path),
        "sizeBytes": info.st_size,
        "memberCount": len(members),
        "paths": [name for _, name in selected],
        "readback": "passed",
    }


def _fresh_activity_state(root: Path) -> None:
    """Require an empty fresh catalog/token/runtime state before initialization."""

    catalog = root / "var/lib/cyrene/runtime/activity-sources.json"
    token_dir = root / "etc/cyrene/runtime-activity-source-tokens"
    environment = root / "etc/cyrene/runtime-activity-sources.env"
    private_state = root / "var/lib/cyrene/runtime-maintenance-private"
    runtime_state = root / "var/lib/cyrene/runtime"
    for path in (catalog, environment):
        if os.path.lexists(path):
            raise AdminInitializationError(f"Existing trusted state must be preserved: {path}")
    for path in (token_dir, private_state, runtime_state):
        if not os.path.lexists(path):
            continue
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
            raise AdminInitializationError(f"Existing runtime state path is unsafe: {path}")
        try:
            next(path.iterdir())
        except StopIteration:
            continue
        raise AdminInitializationError(f"Existing runtime data must be preserved: {path}")


def _unit_service_fields(unit_bytes: bytes, unit_path: Path) -> dict[str, str]:
    """Extract required source identity and credential-file fields from a unit."""

    try:
        text = unit_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AdminInitializationError(
            f"Signed service unit is not UTF-8: {unit_path.name}"
        ) from error
    section = ""
    service: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if section != "Service" or not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if separator and key in {"User", "Group", "LoadCredential", "EnvironmentFile"}:
            service[key] = value
    credential = service.get("LoadCredential", "")
    credential_name, separator, credential_source = credential.partition(":")
    match = re.fullmatch(
        r"/etc/cyrene/runtime-activity-source-tokens/([a-z0-9._-]+)\.token",
        credential_source,
    )
    if (
        service.get("User") != "cyrene"
        or service.get("Group") != "cyrene"
        or credential_name != "activity-token"
        or not separator
        or match is None
        or service.get("EnvironmentFile") != "/etc/cyrene/runtime-activity-sources.env"
    ):
        raise AdminInitializationError(
            f"Product unit owner or credential path differs from the signed initialization contract: {unit_path.name}"
        )
    return {"sourceId": match.group(1), "user": service["User"], "group": service["Group"]}


def _activity_source_arguments(
    extracted_units: Path, installed_units: Path, *, root: Path = Path("/")
) -> tuple[list[str], dict[str, Any]]:
    """Derive activity-source principals from the exact signed Product units."""

    extracted: dict[str, bytes] = {}
    for path in extracted_units.glob("cyrene-*.service"):
        extracted[path.name] = _private_regular_file(path, f"signed unit {path.name}")
    if len(extracted) != len(PRODUCT_SOURCE_IDS):
        raise AdminInitializationError("Signed DEB must contain exactly five activity-source units")

    sources: dict[str, tuple[int, int]] = {}
    expected_names: set[str] = set()
    for name, unit_bytes in extracted.items():
        fields = _unit_service_fields(unit_bytes, Path(name))
        source_id = fields["sourceId"]
        if source_id not in PRODUCT_SOURCE_IDS or source_id in sources:
            raise AdminInitializationError(
                "Signed Product unit source IDs are incomplete or duplicated"
            )
        installed_path = installed_units / name
        installed = _private_regular_file(installed_path, f"installed unit {name}")
        if installed != unit_bytes:
            raise AdminInitializationError(
                f"Installed Product unit differs from verified DEB bytes: {name}"
            )
        uid = pwd.getpwnam(fields["user"]).pw_uid
        gid = grp.getgrnam(fields["group"]).gr_gid
        sources[source_id] = (uid, gid)
        expected_names.add(name)
    if set(sources) != PRODUCT_SOURCE_IDS:
        raise AdminInitializationError(
            "Installed units do not provide the complete trusted source set"
        )
    maintenance_gid = grp.getgrnam("cyrene-runtime-maintenance").gr_gid
    arguments = [
        item
        for source_id, (uid, gid) in sorted(sources.items())
        for item in ("--source", f"{source_id}={uid}:{gid}")
    ]
    return arguments, {
        "unitFiles": sorted(expected_names),
        "catalogGid": maintenance_gid,
        "sources": {source_id: list(identity) for source_id, identity in sorted(sources.items())},
    }


def _load_compiled_target_id(
    helper: Path = Path("/usr/lib/cyrene/scripts/component_updates.py"),
    catalog: Path = Path("/usr/share/cyrene/component-catalog-v1.json"),
) -> str:
    """Read the broker target from the installed compiled trusted catalog."""

    if helper.is_symlink() or not helper.is_file():
        raise AdminInitializationError("Installed component updater helper is unavailable")
    if catalog.is_symlink() or not catalog.is_file():
        raise AdminInitializationError("Compiled trusted component catalog is unavailable")
    if os.geteuid() == 0:
        helper_info = helper.lstat()
        catalog_info = catalog.lstat()
        if (
            helper_info.st_uid != 0
            or catalog_info.st_uid != 0
            or stat.S_IMODE(helper_info.st_mode) & 0o022
            or stat.S_IMODE(catalog_info.st_mode) & 0o022
        ):
            raise AdminInitializationError(
                "Installed trusted updater inputs are not root-controlled"
            )
    spec = importlib.util.spec_from_file_location("cyrene_admin_component_updates", helper)
    if spec is None or spec.loader is None:
        raise AdminInitializationError("Installed component updater helper cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    helper_directory = str(helper.parent)
    module_name = spec.name
    sys.modules[module_name] = module
    sys.path.insert(0, helper_directory)
    try:
        spec.loader.exec_module(module)
    finally:
        if sys.path[0] == helper_directory:
            sys.path.pop(0)
        sys.modules.pop(module_name, None)
    updater = module.ComponentUpdater(
        catalog_path=catalog,
        activity_catalog_path=Path("/var/lib/cyrene/runtime/activity-sources.json"),
        socket_path=Path("/run/cyrene/runtime-maintenance.sock"),
        broker_path=Path("/usr/bin/cyrene-runtime-maintenance"),
        install_root=Path("/usr/lib/cyrene"),
        state_root=Path("/var/lib/cyrene-updates"),
        release_lock_path=Path("/usr/lib/cyrene/release-lock.json"),
        trusted_catalog_digest=module.TRUSTED_CATALOG_DIGEST,
        load_active_catalog=False,
    )
    component = updater.components.get(BOOTSTRAP_COMPONENT)
    if not isinstance(component, dict):
        raise AdminInitializationError("Compiled trusted catalog omits the maintenance broker")
    target = updater._target_for(component)
    target_id = target.get("id") if isinstance(target, dict) else None
    if not isinstance(target_id, str) or not target_id:
        raise AdminInitializationError(
            "Compiled trusted catalog has no broker target for this host"
        )
    return target_id


def _load_admin_journal(path: Path, identity: dict[str, Any]) -> dict[str, Any]:
    """Load a matching root-owned recovery journal or create a new one."""

    _ensure_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise AdminInitializationError("Admin initialization journal path is unsafe")
        info = path.lstat()
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
            raise AdminInitializationError("Admin initialization journal is not private")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AdminInitializationError("Admin initialization journal is invalid") from error
        if not isinstance(value, dict) or value.get("identity") != identity:
            raise AdminInitializationError("Admin initialization journal belongs to other inputs")
        return value
    return {"schemaVersion": 1, "identity": identity, "phase": "verified", "evidence": {}}


def _write_admin_journal(path: Path, value: dict[str, Any]) -> None:
    """Atomically persist a root-only progress record without secret values."""

    descriptor, name = tempfile.mkstemp(prefix=".admin-init-", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, 0, 0)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _canonical_receipt_bytes(value: dict[str, Any]) -> bytes:
    """Encode this ASCII-only receipt subset using canonical JCS-compatible JSON."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _first_product_receipt_material(receipt: dict[str, Any]) -> dict[str, Any]:
    """Validate the closed first-Product receipt shape and return digest material."""

    if not isinstance(receipt, dict):
        raise AdminInitializationError("First-Product cohort receipt must be an object")
    if set(receipt) != {
        "schemaVersion",
        "kind",
        "installer",
        "products",
        "receiptDigest",
    }:
        raise AdminInitializationError("First-Product cohort receipt has unexpected fields")
    installer = receipt.get("installer")
    products = receipt.get("products")
    if (
        receipt.get("schemaVersion") != 1
        or receipt.get("kind") != "first-product-cohort"
        or not isinstance(installer, dict)
        or set(installer) != {"debSha256", "sourceCommit", "targetId"}
        or not isinstance(installer.get("debSha256"), str)
        or not SHA256_RE.fullmatch(installer["debSha256"])
        or not isinstance(installer.get("sourceCommit"), str)
        or COMMIT_RE.fullmatch(installer["sourceCommit"]) is None
        or not isinstance(installer.get("targetId"), str)
        or installer["targetId"] not in FIRST_PRODUCT_PLATFORM_TARGETS.values()
        or not isinstance(products, list)
        or len(products) != len(FIRST_PRODUCT_SERVICE_ORDER)
    ):
        raise AdminInitializationError("First-Product cohort receipt identity is invalid")
    expected_services = list(FIRST_PRODUCT_SERVICE_ORDER)
    actual_services: list[str] = []
    for product in products:
        if not isinstance(product, dict) or set(product) != {
            "service",
            "componentId",
            "version",
            "manifestDigest",
            "artifactDigest",
            "bundlePath",
        }:
            raise AdminInitializationError("First-Product cohort entry has unexpected fields")
        service = product.get("service")
        if (
            not isinstance(service, str)
            or product.get("componentId") != f"cyrene-{service}"
            or not isinstance(product.get("version"), str)
            or PRODUCT_VERSION_RE.fullmatch(product["version"]) is None
            or not isinstance(product.get("manifestDigest"), str)
            or not SHA256_RE.fullmatch(product["manifestDigest"])
            or not isinstance(product.get("artifactDigest"), str)
            or not SHA256_RE.fullmatch(product["artifactDigest"])
            or product.get("bundlePath")
            != f"/usr/share/cyrene/service-artifacts/{service}/{product['version']}"
        ):
            raise AdminInitializationError("First-Product cohort entry identity is invalid")
        actual_services.append(service)
    if actual_services != expected_services:
        raise AdminInitializationError("First-Product cohort services are not canonical")
    material = {key: value for key, value in receipt.items() if key != "receiptDigest"}
    expected_digest = _sha256_bytes(_canonical_receipt_bytes(material))
    if receipt.get("receiptDigest") != expected_digest:
        raise AdminInitializationError("First-Product cohort receipt digest is invalid")
    return material


def _load_staged_service_bundle_validator(extracted_deb: Path) -> Callable[..., dict[str, Any]]:
    """Load the validator shipped in the exact verified DEB extraction."""

    module_path = extracted_deb / "usr/lib/cyrene/scripts/service_bundle.py"
    release_lock = extracted_deb / "usr/lib/cyrene/release-lock.json"
    if (
        module_path.is_symlink()
        or not module_path.is_file()
        or release_lock.is_symlink()
        or not release_lock.is_file()
    ):
        raise AdminInitializationError("Verified DEB omits its service bundle verifier or lock")
    module_name = (
        "_cyrene_admin_service_bundle_"
        + hashlib.sha256(str(module_path).encode("utf-8")).hexdigest()[:12]
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise AdminInitializationError("Verified DEB service bundle validator cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    validate = getattr(module, "validate_bundle", None)
    if not callable(validate):
        raise AdminInitializationError("Verified DEB service bundle validator is incomplete")

    def validate_staged_bundle(
        bundle_path: Path, *, expected_service: str, expected_target_profile: str
    ) -> dict[str, Any]:
        try:
            return validate(
                bundle_path,
                expected_service=expected_service,
                expected_target_profile=expected_target_profile,
                release_lock_path=release_lock,
            )
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            raise AdminInitializationError(
                f"Staged {expected_service} bundle failed signed DEB validation: {error}"
            ) from error

    return validate_staged_bundle


def _derive_first_product_cohort(
    extracted_deb: Path,
    *,
    deb_sha256: str,
    source_commit: str,
    target_id: str,
    target_profile: str,
    service_artifacts_index_sha256: str,
    bundle_validator: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Derive the five staged Product identities from the verified DEB bytes."""

    if not SHA256_RE.fullmatch(deb_sha256) or COMMIT_RE.fullmatch(source_commit) is None:
        raise AdminInitializationError("Verified installer identity is malformed")
    if not RAW_SHA256_RE.fullmatch(service_artifacts_index_sha256):
        raise AdminInitializationError("Verified Product index digest is malformed")
    index_path = extracted_deb / "usr/share/cyrene/service-artifacts/index.json"
    if index_path.is_symlink() or not index_path.is_file():
        raise AdminInitializationError("Verified DEB omits the staged Product index")
    index_bytes = _private_regular_file(index_path, "staged Product index")
    if hashlib.sha256(index_bytes).hexdigest() != service_artifacts_index_sha256:
        raise AdminInitializationError("Staged Product index differs from signed DEB receipt")
    try:
        index = json.loads(index_bytes)
    except json.JSONDecodeError as error:
        raise AdminInitializationError("Staged Product index is invalid JSON") from error
    if (
        not isinstance(index, dict)
        or set(index) != {"schemaVersion", "targetProfile", "services"}
        or index.get("schemaVersion") != 1
        or index.get("targetProfile") != target_profile
        or not isinstance(index.get("services"), dict)
    ):
        raise AdminInitializationError(
            "Staged Product index does not match the verified host profile"
        )
    services = index["services"]
    if set(services) != PRODUCT_SOURCE_IDS:
        raise AdminInitializationError(
            "Staged Product index does not contain exactly five Products"
        )
    validate_bundle = bundle_validator or _load_staged_service_bundle_validator(extracted_deb)
    products: list[dict[str, str]] = []
    for service in FIRST_PRODUCT_SERVICE_ORDER:
        component_id = f"cyrene-{service}"
        record = services.get(component_id)
        if (
            not isinstance(record, dict)
            or set(record)
            != {
                "componentId",
                "repository",
                "releaseId",
                "source",
                "artifact",
                "manifest",
                "attestation",
            }
            or record.get("componentId") != component_id
            or record.get("repository") != f"DoHorizon-AI/{SERVICE_REPOSITORIES[service]}"
        ):
            raise AdminInitializationError(
                f"Staged Product index identity is invalid: {component_id}"
            )
        product_source = record.get("source")
        if (
            not isinstance(product_source, dict)
            or set(product_source) != {"ref", "commit"}
            or not isinstance(product_source.get("ref"), str)
            or not product_source["ref"]
            or not isinstance(product_source.get("commit"), str)
            or COMMIT_RE.fullmatch(product_source["commit"]) is None
        ):
            raise AdminInitializationError(
                f"Staged Product source identity is invalid: {component_id}"
            )
        service_root = extracted_deb / "usr/share/cyrene/service-artifacts" / service
        if service_root.is_symlink() or not service_root.is_dir():
            raise AdminInitializationError(f"Staged Product bundle is missing: {service}")
        versions = list(service_root.iterdir())
        if len(versions) != 1 or versions[0].is_symlink() or not versions[0].is_dir():
            raise AdminInitializationError(f"Staged Product bundle cohort is ambiguous: {service}")
        bundle_path = versions[0]
        manifest = validate_bundle(
            bundle_path,
            expected_service=service,
            expected_target_profile=target_profile,
        )
        manifest_path = bundle_path / "manifest.json"
        manifest_bytes = _private_regular_file(manifest_path, f"staged {service} bundle manifest")
        manifest_digest = _sha256_bytes(manifest_bytes)
        if not isinstance(manifest, dict):
            raise AdminInitializationError(
                f"Staged Product validator returned no manifest: {service}"
            )
        version = manifest.get("version")
        artifact_digest = manifest.get("artifact_digest")
        if (
            manifest.get("schema_version") != 2
            or manifest.get("service") != service
            or manifest.get("source_commit") != product_source["commit"]
            or not isinstance(version, str)
            or bundle_path.name != version
            or not isinstance(artifact_digest, str)
            or not SHA256_RE.fullmatch(artifact_digest)
        ):
            raise AdminInitializationError(f"Staged Product manifest identity differs: {service}")
        products.append(
            {
                "service": service,
                "componentId": component_id,
                "version": version,
                "manifestDigest": manifest_digest,
                "artifactDigest": artifact_digest,
                "bundlePath": f"/usr/share/cyrene/service-artifacts/{service}/{version}",
            }
        )
    receipt: dict[str, Any] = {
        "schemaVersion": 1,
        "kind": "first-product-cohort",
        "installer": {
            "debSha256": deb_sha256,
            "sourceCommit": source_commit,
            "targetId": target_id,
        },
        "products": products,
    }
    receipt["receiptDigest"] = _sha256_bytes(_canonical_receipt_bytes(receipt))
    _first_product_receipt_material(receipt)
    return receipt


def _write_first_product_cohort_receipt(
    receipt: dict[str, Any],
    *,
    path: Path = FIRST_PRODUCT_COHORT_RECEIPT,
) -> dict[str, Any]:
    """Atomically retain one root-private receipt and refuse nonmatching prior data."""

    _first_product_receipt_material(receipt)
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_info = parent.lstat()
    if (
        parent.is_symlink()
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != ADMIN_ROOT_UID
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise AdminInitializationError("First-Product receipt directory is unsafe")
    serialized = (
        json.dumps(receipt, ensure_ascii=True, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    )
    if os.path.lexists(path):
        if path.is_symlink() or not path.is_file():
            raise AdminInitializationError("Existing First-Product receipt path is unsafe")
        info = path.lstat()
        if (
            info.st_uid != ADMIN_ROOT_UID
            or info.st_gid != ADMIN_ROOT_GID
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise AdminInitializationError("Existing First-Product receipt is not root-private")
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AdminInitializationError("Existing First-Product receipt is invalid") from error
        _first_product_receipt_material(existing)
        if existing != receipt:
            raise AdminInitializationError("Existing First-Product receipt binds different inputs")
        return {
            "path": str(path),
            "receiptDigest": receipt["receiptDigest"],
            "status": "verified-existing",
        }

    descriptor, temporary_name = tempfile.mkstemp(prefix=".first-product-cohort-", dir=parent)
    temporary = Path(temporary_name)
    installed_new = False
    try:
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, ADMIN_ROOT_UID, ADMIN_ROOT_GID)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        installed_new = True
        temporary.unlink()
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        readback_info = path.lstat()
        readback = json.loads(path.read_text(encoding="utf-8"))
        if (
            path.is_symlink()
            or not stat.S_ISREG(readback_info.st_mode)
            or readback_info.st_uid != ADMIN_ROOT_UID
            or readback_info.st_gid != ADMIN_ROOT_GID
            or stat.S_IMODE(readback_info.st_mode) != 0o600
            or readback != receipt
        ):
            raise AdminInitializationError("First-Product receipt failed atomic readback")
    except Exception:
        temporary.unlink(missing_ok=True)
        if installed_new and path.is_file() and not path.is_symlink():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                if current == receipt:
                    path.unlink()
            except (OSError, json.JSONDecodeError):
                pass
        raise
    return {
        "path": str(path),
        "receiptDigest": receipt["receiptDigest"],
        "status": "written",
    }


def _write_verified_first_product_cohort(
    deb_path: Path,
    *,
    release_proof: dict[str, Any],
    target: dict[str, Any],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    receipt_path: Path = FIRST_PRODUCT_COHORT_RECEIPT,
) -> dict[str, Any]:
    """Extract the officially verified DEB and persist its validated staged Products."""

    source = release_proof.get("source")
    if not isinstance(source, dict):
        raise AdminInitializationError("Verified native release has no source identity")
    target_profile = target.get("targetId", "")
    platform_target = FIRST_PRODUCT_PLATFORM_TARGETS.get(target_profile)
    if platform_target is None:
        raise AdminInitializationError("Verified DEB does not identify a supported Product profile")
    extracted = Path(tempfile.mkdtemp(prefix="cyrene-first-product-deb-"))
    try:
        _run(["/usr/bin/dpkg-deb", "--extract", str(deb_path), str(extracted)], runner=runner)
        receipt = _derive_first_product_cohort(
            extracted,
            deb_sha256=target.get("debSha256", ""),
            source_commit=source.get("commit", ""),
            target_id=platform_target,
            target_profile=target_profile,
            service_artifacts_index_sha256=target.get("serviceArtifactsIndexSha256", ""),
        )
        return _write_first_product_cohort_receipt(receipt, path=receipt_path)
    finally:
        shutil.rmtree(extracted, ignore_errors=True)


def _operator_identity(
    username: str, lookup: Callable[[str], Any] = pwd.getpwnam
) -> tuple[str, int]:
    """Resolve one explicitly selected local account and reject root identities."""

    if not isinstance(username, str) or OPERATOR_NAME_RE.fullmatch(username) is None:
        raise AdminInitializationError("Operator username is not a safe local account name")
    try:
        account = lookup(username)
    except (KeyError, OSError) as error:
        raise AdminInitializationError("Selected operator account does not exist") from error
    if getattr(account, "pw_name", None) != username:
        raise AdminInitializationError("Selected operator account lookup was not exact")
    uid = getattr(account, "pw_uid", None)
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
        raise AdminInitializationError("Selected operator account must be a non-root user")
    return username, uid


def _verify_installed_update_helper(
    signed_helper: bytes, *, helper_path: Path = COMPONENT_UPDATE_HELPER
) -> str:
    """Require the installed fixed helper to be a root-owned byte-exact package file."""

    try:
        info = helper_path.lstat()
    except OSError as error:
        raise AdminInitializationError(
            "Installed fixed component-update helper is unavailable"
        ) from error
    if (
        helper_path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != ADMIN_ROOT_UID
        or info.st_gid != ADMIN_ROOT_GID
        or stat.S_IMODE(info.st_mode) != 0o755
    ):
        raise AdminInitializationError("Installed fixed component-update helper metadata is unsafe")
    installed = _private_regular_file(helper_path, "installed fixed component-update helper")
    if installed != signed_helper:
        raise AdminInitializationError("Installed fixed helper differs from verified DEB bytes")
    return _sha256_bytes(installed)


def _operator_sudoers_bytes(username: str) -> bytes:
    """Render the sole argument-free sudo command granted to the selected account."""

    return (
        f'{username} ALL=(root) NOPASSWD: /usr/libexec/cyrene-component-update-helper ""\n'
    ).encode("ascii")


def _authorize_component_update_operator(
    username: str,
    signed_helper: bytes,
    *,
    sudoers_directory: Path = Path("/etc/sudoers.d"),
    helper_path: Path = COMPONENT_UPDATE_HELPER,
    staging_parent: Path | None = None,
    lookup: Callable[[str], Any] = pwd.getpwnam,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Install or verify one narrow sudoers rule after validating account and helper."""

    operator, uid = _operator_identity(username, lookup)
    helper_digest = _verify_installed_update_helper(signed_helper, helper_path=helper_path)
    rule = _operator_sudoers_bytes(operator)
    destination = sudoers_directory / f"cyrene-component-update-{operator}"
    if os.path.lexists(destination):
        if destination.is_symlink() or not destination.is_file():
            raise AdminInitializationError("Existing operator sudoers path is unsafe")
        info = destination.lstat()
        existing = destination.read_bytes()
        if (
            info.st_uid != ADMIN_ROOT_UID
            or info.st_gid != ADMIN_ROOT_GID
            or stat.S_IMODE(info.st_mode) != 0o440
            or existing != rule
        ):
            raise AdminInitializationError(
                "Existing operator sudoers rule differs; refusing overwrite"
            )
        _run(["/usr/sbin/visudo", "-c", "-f", str(destination)], runner=runner)
        return {
            "username": operator,
            "uid": uid,
            "helper": str(helper_path),
            "helperSha256": helper_digest,
            "sudoers": str(destination),
            "sudoersSha256": _sha256_bytes(rule),
            "priorRuleState": "matching",
            "ruleReadback": "passed",
            "status": "verified-existing",
        }

    parent = staging_parent or sudoers_directory / ".cyrene-init-private"
    if (
        sudoers_directory.is_symlink()
        or not sudoers_directory.is_dir()
        or sudoers_directory.lstat().st_uid != ADMIN_ROOT_UID
    ):
        raise AdminInitializationError("Sudoers include directory is unsafe")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_info = parent.lstat()
    if (
        parent.is_symlink()
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != ADMIN_ROOT_UID
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise AdminInitializationError("Operator sudoers staging directory is unsafe")
    descriptor, staged_name = tempfile.mkstemp(prefix=".cyrene-operator-", dir=parent)
    staged = Path(staged_name)
    installed_new = False
    try:
        os.fchmod(descriptor, 0o440)
        os.fchown(descriptor, ADMIN_ROOT_UID, ADMIN_ROOT_GID)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(rule)
            stream.flush()
            os.fsync(stream.fileno())
        _run(["/usr/sbin/visudo", "-c", "-f", str(staged)], runner=runner)
        if os.path.lexists(destination):
            raise AdminInitializationError("Operator sudoers path appeared during initialization")
        os.link(staged, destination)
        staged.unlink()
        installed_new = True
        directory_fd = os.open(sudoers_directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        readback = destination.lstat()
        if (
            destination.is_symlink()
            or not stat.S_ISREG(readback.st_mode)
            or readback.st_uid != ADMIN_ROOT_UID
            or readback.st_gid != ADMIN_ROOT_GID
            or stat.S_IMODE(readback.st_mode) != 0o440
            or destination.read_bytes() != rule
        ):
            raise AdminInitializationError("Installed operator sudoers rule failed readback")
    except Exception:
        staged.unlink(missing_ok=True)
        if installed_new and not destination.is_symlink() and destination.is_file():
            try:
                if _sha256_file(destination) == _sha256_bytes(rule):
                    destination.unlink()
            except OSError:
                pass
        raise
    return {
        "username": operator,
        "uid": uid,
        "helper": str(helper_path),
        "helperSha256": helper_digest,
        "sudoers": str(destination),
        "sudoersSha256": _sha256_bytes(rule),
        "priorRuleState": "absent",
        "ruleReadback": "passed",
        "status": "installed",
    }


def _verified_broker_helper(
    activation: dict[str, Any], *, install_root: Path = Path("/usr/lib/cyrene")
) -> tuple[Path, dict[str, Any]]:
    """Resolve the helper only inside the just-activated signed broker release."""

    release_value = activation.get("releasePath")
    if not isinstance(release_value, str):
        raise AdminInitializationError("Broker activation omitted its releasePath")
    release = Path(release_value)
    expected_root = install_root / "components" / BOOTSTRAP_COMPONENT / "releases"
    try:
        if release.parent != expected_root or release.is_symlink() or not release.is_dir():
            raise AdminInitializationError(
                "Activated broker releasePath is outside its component root"
            )
    except OSError as error:
        raise AdminInitializationError("Activated broker releasePath is unavailable") from error
    manifest_path = release / "component-manifest.json"
    manifest_bytes = _private_regular_file(manifest_path, "activated broker manifest")
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as error:
        raise AdminInitializationError("Activated broker manifest is invalid") from error
    artifact = manifest.get("artifact") if isinstance(manifest, dict) else None
    files = artifact.get("files") if isinstance(artifact, dict) else None
    relative_name = BOOTSTRAP_HELPER_RELATIVE.as_posix()
    expected_digest = files.get(relative_name) if isinstance(files, dict) else None
    helper = release.joinpath(*BOOTSTRAP_HELPER_RELATIVE.parts)
    if (
        not isinstance(expected_digest, str)
        or not SHA256_RE.fullmatch(expected_digest)
        or manifest.get("manifestDigest") != activation.get("manifestDigest")
        or artifact.get("sha256") != activation.get("artifactDigest")
        or helper.is_symlink()
        or not helper.is_file()
        or _sha256_file(helper) != expected_digest
    ):
        raise AdminInitializationError(
            "Managed-runtime helper does not match the activated signed release"
        )
    return helper, manifest


def _broker_unit_and_entrypoint(
    activation: dict[str, Any], *, install_root: Path = Path("/usr/lib/cyrene")
) -> tuple[Path, Path]:
    """Resolve the unit and executable from the activated signed manifest."""

    release_path = activation.get("releasePath")
    if not isinstance(release_path, str):
        raise AdminInitializationError("Broker activation omitted its releasePath")
    release = Path(release_path)
    _helper, manifest = _verified_broker_helper(activation, install_root=install_root)
    artifact = manifest["artifact"]
    files = artifact["files"]
    unit_relative = f"systemd/{BOOTSTRAP_UNIT}"
    entrypoint_relative = artifact.get("entrypoint")
    unit_digest = files.get(unit_relative)
    binary_digest = files.get(entrypoint_relative) if isinstance(entrypoint_relative, str) else None
    unit_path = release / unit_relative
    binary_path = release / entrypoint_relative if isinstance(entrypoint_relative, str) else None
    if (
        not isinstance(unit_digest, str)
        or not SHA256_RE.fullmatch(unit_digest)
        or not isinstance(binary_digest, str)
        or not SHA256_RE.fullmatch(binary_digest)
        or binary_path is None
        or unit_path.is_symlink()
        or binary_path.is_symlink()
        or not unit_path.is_file()
        or not binary_path.is_file()
        or _sha256_file(unit_path) != unit_digest
        or _sha256_file(binary_path) != binary_digest
    ):
        raise AdminInitializationError("Broker unit or executable differs from its signed file map")
    return unit_path, binary_path


def _systemd_properties(unit: str) -> dict[str, str]:
    """Read order-independent systemd key/value properties for one unit."""

    output = _run(
        [
            "/usr/bin/systemctl",
            "show",
            "--no-pager",
            "--property=LoadState,ActiveState,FragmentPath,DropInPaths,MainPID",
            unit,
        ]
    ).stdout
    return {
        key: value
        for line in output.splitlines()
        if "=" in line
        for key, value in (line.split("=", 1),)
    }


def _start_fresh_broker(
    activation: dict[str, Any], runner: Callable[..., subprocess.CompletedProcess[str]]
) -> dict[str, str]:
    """Start only the new broker and prove systemd launched its signed binary."""

    unit_path, binary_path = _broker_unit_and_entrypoint(activation)
    installed_unit = Path("/usr/lib/systemd/system") / BOOTSTRAP_UNIT
    if (
        installed_unit.is_symlink()
        or not installed_unit.is_file()
        or _sha256_file(installed_unit) != _sha256_file(unit_path)
    ):
        raise AdminInitializationError(
            "Installed broker unit differs from the signed active release"
        )
    properties = _systemd_properties(BOOTSTRAP_UNIT)
    if (
        properties.get("LoadState") != "loaded"
        or Path(properties.get("FragmentPath", "")).resolve() != installed_unit.resolve()
        or properties.get("DropInPaths", "")
    ):
        raise AdminInitializationError("Loaded broker unit identity or drop-ins are not exact")
    if properties.get("ActiveState") != "active":
        _run(["/usr/bin/systemctl", "start", BOOTSTRAP_UNIT], runner=runner)
        properties = _systemd_properties(BOOTSTRAP_UNIT)
    if properties.get("ActiveState") != "active":
        raise AdminInitializationError("New maintenance broker did not become active")
    pid = properties.get("MainPID", "")
    if not pid.isdecimal() or int(pid) <= 1:
        raise AdminInitializationError("New broker unit has no valid active MainPID")
    try:
        process_exe = Path(os.readlink(f"/proc/{pid}/exe"))
    except OSError as error:
        raise AdminInitializationError("New broker executable identity is unreadable") from error
    if process_exe != binary_path:
        raise AdminInitializationError(
            "Broker MainPID is not executing the signed active entrypoint"
        )
    return {
        "unit": BOOTSTRAP_UNIT,
        "activeState": properties["ActiveState"],
        "mainPid": pid,
        "binaryPath": str(binary_path),
        "binarySha256": _sha256_file(binary_path),
        "unitSha256": _sha256_file(unit_path),
    }


def _read_os_release_version() -> str:
    """Read the real root OS release for the signed DEB target selection."""

    return _host_ubuntu_version(Path("/"))


def execute_initialization(args: argparse.Namespace) -> dict[str, Any]:
    """Verify, back up, stage the DEB, and prepare only the fresh broker.

    The maintenance broker can be started only when explicitly requested after
    a matching plan confirmation. Core/Product services are never started,
    enabled, stopped, or switched here. UNKNOWN authority stays CLOSED.
    中文：仅暂存签名包、初始化全新维护代理；不启动或切换 Kernel/Product。
    """

    _require_root()
    ref = args.expected_source_ref
    commit = args.expected_source_commit
    if (
        ref
        not in {
            "refs/heads/develop",
            "refs/heads/main",
            "refs/heads/release",
        }
        or COMMIT_RE.fullmatch(commit) is None
    ):
        raise AdminInitializationError(
            "Exact allowed Workspace source ref and full SHA are required"
        )
    version = _read_os_release_version()
    release_proof = verify_native_release_for_host(
        args.release_directory,
        expected_source_ref=ref,
        expected_source_commit=commit,
        ubuntu_version=version,
    )
    if release_proof["repository"] != WORKSPACE_REPOSITORY:
        raise AdminInitializationError(
            "Verified package repository does not match the fixed Workspace"
        )
    target = release_proof["target"]
    deb_path = Path(target["debPath"])
    deb_bytes = _private_regular_file(deb_path, "verified stage-only DEB")
    if _sha256_bytes(deb_bytes) != target["debSha256"]:
        raise AdminInitializationError("Verified DEB changed before initialization")
    if target.get("checks") != EXPECTED_INITIALIZATION_CHECKS:
        raise AdminInitializationError("Verified package does not satisfy the stage-only contract")

    operator_cli_source = args.github_cli
    locked_cli_digest = _locked_github_cli_digest()
    if (
        operator_cli_source.is_symlink()
        or not operator_cli_source.is_file()
        or _sha256_file(operator_cli_source) != f"sha256:{locked_cli_digest}"
    ):
        raise AdminInitializationError("Packet GitHub CLI does not match the fixed binary SHA-256")
    _preflight_deb_dependencies(deb_path)

    bootstrap_inputs: dict[str, Path] = {
        "index": args.index,
        "index-attestation": args.index_attestation,
        "manifest": args.manifest,
        "artifact": args.artifact,
        "artifact-attestation": args.artifact_attestation,
    }
    bootstrap_hashes = {
        name: _sha256_bytes(_private_regular_file(path, name))
        for name, path in bootstrap_inputs.items()
    }
    extract_root = Path(tempfile.mkdtemp(prefix="cyrene-init-catalog-"))
    try:
        _run(["/usr/bin/dpkg-deb", "--extract", str(deb_path), str(extract_root)])
        target_id = _load_compiled_target_id(
            extract_root / "usr/lib/cyrene/scripts/component_updates.py",
            extract_root / "usr/share/cyrene/component-catalog-v1.json",
        )
    finally:
        shutil.rmtree(extract_root, ignore_errors=True)
    bootstrap_command = [
        "/usr/bin/cyrene",
        "component-bootstrap-runtime-maintenance",
        "--index",
        str(args.index),
        "--index-attestation",
        str(args.index_attestation),
        "--manifest",
        str(args.manifest),
        "--artifact",
        str(args.artifact),
        "--artifact-attestation",
        str(args.artifact_attestation),
        "--channel",
        args.channel,
        "--target-id",
        target_id,
    ]
    identity = {
        "workspaceReleaseId": release_proof["releaseId"],
        "workspaceSource": {"ref": ref, "commit": commit},
        "debSha256": target["debSha256"],
        "persistentOperatorTool": {
            "path": str(PERSISTENT_GH),
            "sha256": f"sha256:{locked_cli_digest}",
        },
        "broker": {
            "channel": args.channel,
            "targetId": target_id,
            "inputSha256": bootstrap_hashes,
        },
    }
    if args.operator_user is not None:
        identity["operatorUser"] = args.operator_user
    journal_path = Path("/var/lib/cyrene/native-initialization") / (
        "operator-init-" + target["debSha256"].removeprefix("sha256:") + ".json"
    )
    journal = _load_admin_journal(journal_path, identity)
    evidence = journal.setdefault("evidence", {})

    backup_path = Path(args.backup_directory) / (
        "native-init-" + target["debSha256"].removeprefix("sha256:") + ".tar.gz"
    )
    if journal["phase"] == "verified":
        backup = _backup_archive(backup_path)
        evidence["backup"] = backup
        journal["phase"] = "backed-up"
        _write_admin_journal(journal_path, journal)
    else:
        backup = evidence.get("backup")
        if not isinstance(backup, dict) or backup.get("path") != str(backup_path):
            raise AdminInitializationError("Recovery journal has no matching verified backup")
        if _sha256_file(backup_path) != backup.get("sha256"):
            raise AdminInitializationError("Root-owned backup changed after verification")
        with tarfile.open(backup_path, mode="r:gz") as archive:
            member_count = len(archive.getmembers())
        if member_count != backup.get("memberCount"):
            raise AdminInitializationError("Root-owned backup failed readback validation")

    if journal["phase"] == "backed-up":
        evidence["persistentOperatorTool"] = _persist_locked_github_cli(operator_cli_source)
        _write_admin_journal(journal_path, journal)
        _run(["/usr/bin/dpkg", "-i", str(deb_path)])
        installed_target_id = _load_compiled_target_id()
        if installed_target_id != target_id:
            raise AdminInitializationError(
                "Installed compiled broker target differs from the signed DEB catalog target"
            )
        journal["phase"] = "deb-staged"
        _write_admin_journal(journal_path, journal)

    if journal["phase"] == "deb-staged":
        _fresh_activity_state(Path("/"))
        plan_result = _json_result(_run(bootstrap_command), "Broker bootstrap plan")
        plan_digest = _verify_bootstrap_plan(plan_result, target_id)
        journal["phase"] = "broker-plan-created"
        journal["planDigest"] = plan_digest
        journal["planIdentity"] = {
            key: plan_result.get(key)
            for key in (
                "componentId",
                "targetId",
                "version",
                "manifestDigest",
                "artifactDigest",
                "indexDigest",
            )
        }
        _write_admin_journal(journal_path, journal)

    if journal["phase"] == "broker-plan-created":
        expected_digest = journal.get("planDigest")
        confirmation = args.confirm_plan_digest
        if confirmation is None:
            print(
                "Verified broker plan "
                + json.dumps(journal["planIdentity"], sort_keys=True, separators=(",", ":")),
                flush=True,
            )
            confirmation = input(
                "Type the exact planDigest to confirm new broker activation: "
            ).strip()
        if confirmation != expected_digest:
            raise AdminInitializationError(
                "Human confirmation did not match the exact broker plan digest"
            )
        journal["confirmedPlanDigest"] = confirmation
        journal["phase"] = "broker-confirmed"
        _write_admin_journal(journal_path, journal)

    if journal["phase"] == "broker-confirmed":
        activation = _json_result(
            _run([*bootstrap_command, "--confirm-plan-digest", journal["confirmedPlanDigest"]]),
            "Broker bootstrap confirmation",
        )
        if (
            activation.get("status") != "activated"
            or activation.get("planDigest") != journal["confirmedPlanDigest"]
            or activation.get("componentId") != BOOTSTRAP_COMPONENT
            or activation.get("targetId") != target_id
            or any(activation.get(key) != value for key, value in journal["planIdentity"].items())
        ):
            raise AdminInitializationError("Broker activation did not match the confirmed plan")
        helper, helper_manifest = _verified_broker_helper(activation)
        journal["phase"] = "broker-activated"
        evidence["brokerActivation"] = {
            key: activation.get(key)
            for key in (
                "componentId",
                "targetId",
                "version",
                "manifestDigest",
                "artifactDigest",
                "indexDigest",
                "releasePath",
                "activePointer",
                "activePointerTarget",
            )
        }
        evidence["managedHelperSha256"] = _sha256_file(helper)
        evidence["brokerManifestDigest"] = helper_manifest["manifestDigest"]
        _write_admin_journal(journal_path, journal)
    else:
        activation = evidence.get("brokerActivation")
        if not isinstance(activation, dict):
            raise AdminInitializationError("Recovery journal has no verified broker activation")
        helper, _helper_manifest = _verified_broker_helper(activation)
        if _sha256_file(helper) != evidence.get("managedHelperSha256"):
            raise AdminInitializationError("Managed-runtime helper changed after activation")

    if journal["phase"] == "broker-activated":
        unit_result = _json_result(
            _run(["/usr/bin/cyrene", "component-install-missing-units"]),
            "Trusted unit installation",
        )
        if not isinstance(unit_result.get("installed"), list) or not isinstance(
            unit_result.get("alreadyPresent"), list
        ):
            raise AdminInitializationError("Trusted unit installer returned an invalid result")
        evidence["unitInstallation"] = unit_result
        _run([str(PRIVATE_PYTHON), str(helper), "prepare", "--root", "/"])
        _run(["/usr/bin/systemctl", "daemon-reload"])
        journal["phase"] = "identity-prepared"
        evidence["managedRuntimePrepare"] = "passed"
        evidence["daemonReload"] = "passed"
        _write_admin_journal(journal_path, journal)
    elif journal["phase"] not in {
        "identity-prepared",
        "catalog-initialized",
        "broker-started",
        "complete",
    }:
        raise AdminInitializationError("Admin initialization journal has an unsupported phase")

    evidence["firstProductCohort"] = _write_verified_first_product_cohort(
        deb_path,
        release_proof=release_proof,
        target=target,
    )
    _write_admin_journal(journal_path, journal)

    if journal["phase"] == "identity-prepared":
        _fresh_activity_state(Path("/"))
        extract_root = Path(tempfile.mkdtemp(prefix="cyrene-init-deb-"))
        try:
            _run(["/usr/bin/dpkg-deb", "--extract", str(deb_path), str(extract_root)])
            source_args, source_evidence = _activity_source_arguments(
                extract_root / "usr/lib/systemd/system",
                Path("/usr/lib/systemd/system"),
            )
        finally:
            shutil.rmtree(extract_root, ignore_errors=True)
        runtime_group_gid = grp.getgrnam("cyrene-runtime-maintenance").gr_gid
        catalog_command = [
            "/usr/bin/cyrene",
            "component-run",
            BOOTSTRAP_COMPONENT,
            "--",
            "init-catalog",
            "--catalog",
            "/var/lib/cyrene/runtime/activity-sources.json",
            "--token-dir",
            "/etc/cyrene/runtime-activity-source-tokens",
            "--catalog-gid",
            str(runtime_group_gid),
            *source_args,
        ]
        init_result = _json_result(_run(catalog_command), "Fresh activity catalog initialization")
        generation = init_result.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise AdminInitializationError(
                "Activity-source initializer returned no valid generation"
            )
        returned_sources = init_result.get("sources")
        if (
            not isinstance(returned_sources, list)
            or {row.get("source_id") for row in returned_sources if isinstance(row, dict)}
            != PRODUCT_SOURCE_IDS
            or any(
                row.get("token_file")
                != f"/etc/cyrene/runtime-activity-source-tokens/{row.get('source_id')}.token"
                for row in returned_sources
                if isinstance(row, dict)
            )
        ):
            raise AdminInitializationError(
                "Activity-source initializer returned an unexpected owner set"
            )
        environment_path = Path("/etc/cyrene/runtime-activity-sources.env")
        descriptor = os.open(
            environment_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o644,
        )
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(f"CYRENE_RUNTIME_ACTIVITY_CATALOG_GENERATION={generation}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(environment_path, 0, 0)
        os.chmod(environment_path, 0o644)
        catalog_path = Path("/var/lib/cyrene/runtime/activity-sources.json")
        catalog = json.loads(_private_regular_file(catalog_path, "fresh activity catalog"))
        expected_sources = [
            {
                "source_id": source_id,
                "uid": source_identity[0],
                "gid": source_identity[1],
            }
            for source_id, source_identity in sorted(source_evidence["sources"].items())
        ]
        actual_sources = [
            {key: row.get(key) for key in ("source_id", "uid", "gid")}
            for row in catalog.get("sources", [])
            if isinstance(row, dict)
        ]
        if catalog.get("generation") != generation or actual_sources != expected_sources:
            raise AdminInitializationError(
                "Activity catalog generation changed after initialization"
            )
        token_dir = Path("/etc/cyrene/runtime-activity-source-tokens")
        token_files: list[str] = []
        for source_id in sorted(PRODUCT_SOURCE_IDS):
            token_path = token_dir / f"{source_id}.token"
            info = token_path.lstat()
            if (
                token_path.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o400
            ):
                raise AdminInitializationError(
                    f"Generated source token metadata is unsafe: {source_id}"
                )
            token_files.append(str(token_path))
        evidence["activitySources"] = {
            "catalog": str(catalog_path),
            "generation": generation,
            "catalogSha256": _sha256_file(catalog_path),
            "environment": str(environment_path),
            "environmentSha256": _sha256_file(environment_path),
            "tokenFiles": token_files,
            "sourceUnits": source_evidence,
        }
        journal["phase"] = "catalog-initialized"
        _write_admin_journal(journal_path, journal)
    elif journal["phase"] in {"catalog-initialized", "broker-started", "complete"}:
        activity = evidence.get("activitySources")
        if not isinstance(activity, dict):
            raise AdminInitializationError("Recovery journal has no fresh activity-source receipt")
        for key, path_value in (
            ("catalogSha256", "/var/lib/cyrene/runtime/activity-sources.json"),
            ("environmentSha256", "/etc/cyrene/runtime-activity-sources.env"),
        ):
            if _sha256_file(Path(path_value)) != activity.get(key):
                raise AdminInitializationError(
                    "Fresh activity configuration changed after initialization"
                )

    if args.start_broker and journal["phase"] in {
        "catalog-initialized",
        "broker-started",
        "complete",
    }:
        broker_state = _start_fresh_broker(evidence["brokerActivation"], subprocess.run)
        journal["phase"] = "broker-started"
        evidence["brokerService"] = broker_state
        _write_admin_journal(journal_path, journal)

    operator_authorization: dict[str, Any] | None = None
    if args.operator_user is not None:
        extract_root = Path(tempfile.mkdtemp(prefix="cyrene-init-operator-deb-"))
        try:
            _run(["/usr/bin/dpkg-deb", "--extract", str(deb_path), str(extract_root)])
            signed_helper_path = extract_root / "usr/libexec/cyrene-component-update-helper"
            signed_helper = _private_regular_file(
                signed_helper_path, "verified DEB component-update helper"
            )
            operator_authorization = _authorize_component_update_operator(
                args.operator_user, signed_helper
            )
        finally:
            shutil.rmtree(extract_root, ignore_errors=True)

    result = {
        "status": (
            "BROKER_STARTED_AUTHORITY_UNKNOWN"
            if journal["phase"] in {"broker-started", "complete"}
            else "PREPARED"
        ),
        "releaseId": release_proof["releaseId"],
        "source": release_proof["source"],
        "targetId": target["targetId"],
        "debSha256": target["debSha256"],
        "persistentOperatorTool": evidence.get("persistentOperatorTool"),
        "planDigest": journal.get("confirmedPlanDigest"),
        "backup": evidence["backup"],
        "broker": evidence.get("brokerActivation"),
        "activitySources": evidence.get("activitySources"),
        "firstProductCohort": evidence.get("firstProductCohort"),
        "operatorAuthorization": operator_authorization,
        "readiness": "UNKNOWN",
        "applyAdmission": "CLOSED",
        "productAndCoreActivation": "NOT_RUN",
        "platformReadyManifest": "NOT_WRITTEN",
        "manualRuntimeProcesses": "PRESERVED",
    }
    journal["result"] = result
    journal["phase"] = "complete" if args.start_broker else journal["phase"]
    if operator_authorization is not None:
        evidence["operatorAuthorization"] = operator_authorization
    try:
        _write_admin_journal(journal_path, journal)
    except Exception:
        if (
            operator_authorization is not None
            and operator_authorization.get("status") == "installed"
        ):
            rule_path = Path(operator_authorization["sudoers"])
            try:
                if (
                    not rule_path.is_symlink()
                    and rule_path.is_file()
                    and _sha256_file(rule_path) == operator_authorization["sudoersSha256"]
                ):
                    rule_path.unlink()
            except OSError:
                pass
        raise
    return result


def build_parser() -> argparse.ArgumentParser:
    """Build the exact, pin-required administrator command interface."""

    parser = argparse.ArgumentParser(
        description="Run a signed, stage-only native host initialization"
    )
    parser.add_argument("--release-directory", type=Path, required=True)
    parser.add_argument("--expected-source-ref", required=True)
    parser.add_argument("--expected-source-commit", required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--index-attestation", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--artifact-attestation", type=Path, required=True)
    parser.add_argument("--github-cli", type=Path, required=True)
    parser.add_argument("--channel", choices=("stable", "preview"), required=True)
    parser.add_argument("--backup-directory", type=Path, default=Path("/var/backups/cyrene"))
    parser.add_argument("--confirm-plan-digest")
    parser.add_argument(
        "--operator-user",
        help="Optionally grant one existing non-root account the fixed component-update helper",
    )
    parser.add_argument(
        "--start-broker",
        action="store_true",
        help="Start only the newly verified runtime-maintenance broker after fresh-only initialization",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one root-only initialization and print a redacted receipt."""

    try:
        args = build_parser().parse_args(argv)
        result = execute_initialization(args)
    except (AdminInitializationError, AcceptanceError, OSError, ValueError) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
