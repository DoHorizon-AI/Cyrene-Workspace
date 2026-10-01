"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 service_bundle.py                                                │
│  Module: cyrene.packaging.service_bundle                             │
│  Role: Build, verify, stage, and select immutable service releases.  │
│                                                                      │
│  模块职责：构建并校验服务发布包，将其安装到不可变版本目录并切换活动版本。   │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import venv
from pathlib import Path, PurePosixPath
from typing import Any

SERVICES = ("navigator", "yield", "reactor", "exchange", "catalyst")
REPOSITORIES = {
    "navigator": "Cyrene-Navigator",
    "yield": "Cyrene-Yield",
    "reactor": "Cyrene-Reactor",
    "exchange": "Cyrene-Exchange",
    "catalyst": "Cyrene-Catalyst",
}
REQUIRED_DISTRIBUTIONS = {
    "navigator": ("cyrene-navigator",),
    "yield": ("cyrene-yield",),
    "reactor": ("cyrene-reactor-product",),
    "exchange": ("cyrene-exchange", "cyrene-exchange-product"),
    "catalyst": ("cyrene-catalyst",),
}
HEALTH_PATHS = {
    "navigator": "/api/v1/system/status",
    "yield": "/health",
    "reactor": "/openapi.json",
    "exchange": "/healthz",
    "catalyst": "/",
}
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
WHEEL_REQUIREMENT_PATTERN = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)\s*==\s*([A-Za-z0-9][A-Za-z0-9.+!_-]*)"
)


class ServiceBundleError(ValueError):
    """Raised when a service bundle is incomplete, unsafe, or inconsistent."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _service_root(service: str, install_root: Path | None = None) -> Path:
    if service not in SERVICES:
        raise ServiceBundleError(f"unsupported service name: {service}")
    root = Path(install_root) if install_root is not None else Path(
        os.environ.get("CYRENE_INSTALL_ROOT", "/usr/lib/cyrene")
    )
    return root / "services" / service


def _ensure_secure_directory(path: Path, *, create: bool) -> Path:
    """Require a root-owned, non-writable directory chain before using release paths."""

    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        created = False
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                raise ServiceBundleError(f"secure service directory is missing: {current}")
            try:
                current.mkdir(mode=0o755)
                created = True
            except FileExistsError:
                pass
            info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ServiceBundleError(f"service path contains a symlink or non-directory: {current}")
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ServiceBundleError(
                f"service directory must be root-owned and not group/world writable: {current}"
            )
        if created:
            current.chmod(0o755)
            info = current.lstat()
        if (info.st_mode & 0o005) != 0o005:
            raise ServiceBundleError(f"service directory must be traversable by the service user: {current}")
    return absolute


def ensure_secure_service_root(
    service: str, install_root: Path | None = None, create: bool = True
) -> Path:
    """Return a verified service root suitable for root-owned transaction state."""

    root = _service_root(service, install_root)
    _ensure_secure_directory(root, create=create)
    _ensure_secure_directory(root / "releases", create=create)
    return root


def _assert_secure_tree(root: Path) -> None:
    """Require an installed release tree to remain root-owned and immutable to service users."""

    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [".", *directory_names, *file_names]:
            path = current_path if name == "." else current_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ServiceBundleError(f"installed release contains a symbolic link: {path}")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ServiceBundleError(f"installed release contains a non-regular path: {path}")
            if info.st_uid != 0 or info.st_mode & (0o022 | 0o6000):
                raise ServiceBundleError(
                    f"installed release must be root-owned and not group/world writable: {path}"
                )
            if stat.S_ISDIR(info.st_mode) and (info.st_mode & 0o005) != 0o005:
                raise ServiceBundleError(f"installed release directory is not traversable: {path}")
            if stat.S_ISREG(info.st_mode) and not (info.st_mode & stat.S_IROTH):
                raise ServiceBundleError(f"installed release file is not readable by the service user: {path}")


def _normalize_release_modes(root: Path, entrypoint: str) -> None:
    """Set service-readable payload modes without granting service write access."""

    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        current_path.chmod(0o755)
        for name in directory_names:
            (current_path / name).chmod(0o755)
        for name in file_names:
            path = current_path / name
            path.chmod(0o755 if path.relative_to(root).as_posix() == entrypoint else 0o644)


def _expected_file_map(bundle_root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for current, directory_names, file_names in os.walk(bundle_root, followlinks=False):
        current_path = Path(current)
        for name in directory_names:
            path = current_path / name
            if path.is_symlink():
                raise ServiceBundleError(f"bundle contains a symbolic link: {path}")
            if not path.is_dir():
                raise ServiceBundleError(f"bundle contains a non-directory path: {path}")
        for name in file_names:
            path = current_path / name
            relative = path.relative_to(bundle_root).as_posix()
            if path.is_symlink():
                raise ServiceBundleError(f"bundle contains a symbolic link: {relative}")
            if not path.is_file():
                raise ServiceBundleError(f"bundle contains a non-regular file: {relative}")
            if relative != "manifest.json":
                files[relative] = _sha256_file(path)
    return dict(sorted(files.items()))


def _identity_fields(manifest: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "schema_version",
        "service",
        "version",
        "entrypoint",
        "health",
        "files",
        "source_repository",
        "source_commit",
        "dependencies",
        "target",
    )
    return {key: manifest[key] for key in fields if key != "version" and key in manifest}


def _artifact_digest(manifest: dict[str, Any]) -> str:
    return _sha256_bytes(_canonical_json(_identity_fields(manifest)))


def _relative_file_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise ServiceBundleError(f"manifest contains an unsafe file path: {value!r}")
    return path


def validate_bundle(bundle_dir: Path, expected_service: str | None = None) -> dict[str, Any]:
    """Validate a service bundle manifest and every payload file digest.

    Args:
        bundle_dir: Directory containing one manifest and its complete payload.
        expected_service: Optional service identity required by the caller.
    Returns:
        The verified manifest object.
    Raises:
        ServiceBundleError: If the bundle identity, paths, files, or digests differ.
    """

    root = Path(bundle_dir)
    if root.is_symlink() or not root.is_dir():
        raise ServiceBundleError(f"bundle directory is missing or unsafe: {root}")
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ServiceBundleError(f"bundle manifest is missing or unsafe: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ServiceBundleError(f"cannot read bundle manifest {manifest_path}: {error}") from error
    if not isinstance(manifest, dict):
        raise ServiceBundleError("bundle manifest must be a JSON object")

    expected_keys = {
        "schema_version",
        "service",
        "version",
        "entrypoint",
        "health",
        "files",
        "source_repository",
        "source_commit",
        "dependencies",
        "target",
        "artifact_digest",
    }
    if set(manifest) != expected_keys:
        missing = sorted(expected_keys - set(manifest))
        extra = sorted(set(manifest) - expected_keys)
        raise ServiceBundleError(f"manifest keys differ; missing={missing}, extra={extra}")
    if manifest["schema_version"] != 1:
        raise ServiceBundleError(f"unsupported bundle schema version: {manifest['schema_version']!r}")
    service = manifest["service"]
    if service not in SERVICES:
        raise ServiceBundleError(f"unsupported manifest service: {service!r}")
    if expected_service is not None and service != expected_service:
        raise ServiceBundleError(
            f"bundle service mismatch: expected {expected_service}, got {service}"
        )
    version = manifest["version"]
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ServiceBundleError(f"manifest version is not a safe path segment: {version!r}")

    entrypoint = manifest["entrypoint"]
    if not isinstance(entrypoint, str):
        raise ServiceBundleError("manifest entrypoint must be a relative path")
    entrypoint_path = _relative_file_path(entrypoint)

    health = manifest["health"]
    if (
        not isinstance(health, dict)
        or set(health) != {"path"}
        or not isinstance(health["path"], str)
        or not health["path"].startswith("/")
        or "?" in health["path"]
        or "#" in health["path"]
        or any(part == ".." for part in PurePosixPath(health["path"]).parts)
    ):
        raise ServiceBundleError("manifest health.path must be a safe absolute URL path")

    files = manifest["files"]
    if not isinstance(files, dict) or not files:
        raise ServiceBundleError("manifest files must be a non-empty path-to-SHA256 object")
    for file_name, digest in files.items():
        _relative_file_path(file_name)
        if file_name == "manifest.json":
            raise ServiceBundleError("manifest files must not contain manifest.json")
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
            raise ServiceBundleError(f"manifest has an invalid SHA-256 for {file_name!r}")
    normalized_files = dict(sorted(files.items()))
    if normalized_files != _expected_file_map(root):
        raise ServiceBundleError("bundle payload file list or SHA-256 values do not match manifest")
    if entrypoint not in files:
        raise ServiceBundleError(f"manifest entrypoint is not in the payload: {entrypoint}")
    executable = root.joinpath(*entrypoint_path.parts)
    if not executable.is_file() or not (executable.stat().st_mode & stat.S_IXOTH):
        raise ServiceBundleError(f"bundle entrypoint must be executable by the service user: {entrypoint}")

    source_repository = manifest["source_repository"]
    source_commit = manifest["source_commit"]
    if source_repository != REPOSITORIES[service]:
        raise ServiceBundleError(f"manifest source repository does not match {service}")
    if not isinstance(source_commit, str) or not COMMIT_PATTERN.fullmatch(source_commit):
        raise ServiceBundleError("manifest source_commit must be a lowercase 40-character Git SHA")
    dependencies = manifest["dependencies"]
    if (
        not isinstance(dependencies, dict)
        or set(dependencies) != {"lock_file", "lock_sha256"}
        or dependencies["lock_file"] != "requirements.lock"
        or not isinstance(dependencies["lock_sha256"], str)
        or not SHA256_PATTERN.fullmatch(dependencies["lock_sha256"])
        or files.get("requirements.lock") != dependencies["lock_sha256"]
    ):
        raise ServiceBundleError("manifest dependency lock identity is missing or inconsistent")
    target = manifest["target"]
    if (
        not isinstance(target, dict)
        or set(target) != {"debian_arch", "python"}
        or not isinstance(target["debian_arch"], str)
        or not isinstance(target["python"], str)
    ):
        raise ServiceBundleError("manifest target must declare debian_arch and python")
    if target["debian_arch"] != _debian_arch_for_host() or target["python"] != "3.12":
        raise ServiceBundleError(
            f"bundle target does not match this host: {target['debian_arch']}/Python {target['python']}"
        )

    source_record = _read_json_object(root / "source.json", "bundle source.json")
    if source_record != {
        "schema_version": 1,
        "service": service,
        "source_repository": source_repository,
        "source_commit": source_commit,
        "requirements_lock_sha256": dependencies["lock_sha256"],
    }:
        raise ServiceBundleError("bundle source.json does not match manifest provenance")

    digest = _artifact_digest(manifest)
    if version != digest or manifest["artifact_digest"] != digest:
        raise ServiceBundleError("manifest version/artifact_digest do not match verified content")
    return manifest


def resolve_active_release(
    service: str, install_root: Path | None = None
) -> tuple[Path, str] | None:
    """Return the verified active entrypoint and immutable release version, if present.

    Args:
        service: One of the five managed service names.
        install_root: Optional Cyrene installation root override.
    Returns:
        ``(entrypoint_path, version)`` for a valid active release, or ``None``.
    Raises:
        ServiceBundleError: If the active pointer or release is inconsistent.
    """

    root = _service_root(service, install_root)
    active = root / "active"
    _ensure_secure_directory(root, create=False)
    _ensure_secure_directory(root / "releases", create=False)
    if not active.exists() and not active.is_symlink():
        return None
    active_info = active.lstat()
    if not stat.S_ISLNK(active_info.st_mode) or active_info.st_uid != 0:
        raise ServiceBundleError(f"active release pointer is not a symbolic link: {active}")
    target = os.readlink(active)
    match = re.fullmatch(r"releases/([^/]+)", target)
    if match is None:
        raise ServiceBundleError(f"active release pointer has an unsafe target: {target!r}")
    version = match.group(1)
    if not VERSION_PATTERN.fullmatch(version):
        raise ServiceBundleError(f"active release pointer has an unsafe version: {version!r}")
    release = root / "releases" / version
    _ensure_secure_directory(release, create=False)
    manifest = validate_bundle(release, expected_service=service)
    _assert_secure_tree(release)
    if manifest["version"] != version:
        raise ServiceBundleError("active release directory and manifest version differ")
    entrypoint = release / manifest["entrypoint"]
    return entrypoint, version


def stage_release(bundle_dir: Path, install_root: Path | None = None) -> Path:
    """Copy a verified artifact into its immutable release directory.

    Re-staging byte-identical content is idempotent. Existing versions with a
    different manifest are rejected to preserve rollback history.
    """

    source = Path(bundle_dir)
    manifest = validate_bundle(source)
    service_root = _service_root(manifest["service"], install_root)
    releases = service_root / "releases"
    destination = releases / manifest["version"]
    _ensure_secure_directory(releases, create=True)
    if destination.exists() or destination.is_symlink():
        _ensure_secure_directory(destination, create=False)
        _assert_secure_tree(destination)
        existing = validate_bundle(destination, expected_service=manifest["service"])
        if existing["artifact_digest"] != manifest["artifact_digest"]:
            raise ServiceBundleError(f"immutable release already exists with different content: {destination}")
        return destination

    staging = Path(tempfile.mkdtemp(prefix=f".{manifest['version']}.stage-", dir=releases))
    try:
        shutil.copytree(source, staging, dirs_exist_ok=True, copy_function=shutil.copy2)
        _normalize_release_modes(staging, manifest["entrypoint"])
        copied = validate_bundle(staging, expected_service=manifest["service"])
        if copied["artifact_digest"] != manifest["artifact_digest"]:
            raise ServiceBundleError("staged release content changed during copy")
        try:
            os.rename(staging, destination)
        except OSError:
            if destination.exists() or destination.is_symlink():
                _ensure_secure_directory(destination, create=False)
                _assert_secure_tree(destination)
                existing = validate_bundle(destination, expected_service=manifest["service"])
                if existing["artifact_digest"] == manifest["artifact_digest"]:
                    return destination
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    _ensure_secure_directory(destination, create=False)
    _assert_secure_tree(destination)
    return destination


def activate_release(
    service: str,
    version: str,
    install_root: Path | None = None,
    expected_current_version: str | None = None,
) -> Path:
    """Atomically point ``active`` at an already validated immutable release."""

    if not VERSION_PATTERN.fullmatch(version):
        raise ServiceBundleError(f"unsafe service release version: {version!r}")
    service_root = _service_root(service, install_root)
    release = service_root / "releases" / version
    _ensure_secure_directory(service_root, create=True)
    _ensure_secure_directory(service_root / "releases", create=False)
    _ensure_secure_directory(release, create=False)
    _assert_secure_tree(release)
    manifest = validate_bundle(release, expected_service=service)
    if manifest["version"] != version:
        raise ServiceBundleError("release directory and manifest version differ")
    service_root.mkdir(parents=True, exist_ok=True)
    active = service_root / "active"
    prior_target: str | None = None
    if active.exists() or active.is_symlink():
        active_info = active.lstat()
        if not stat.S_ISLNK(active_info.st_mode) or active_info.st_uid != 0:
            raise ServiceBundleError(f"refusing to replace an unsafe active path: {active}")
        prior_target = os.readlink(active)
        expected_target = (
            f"releases/{expected_current_version}" if expected_current_version is not None else None
        )
        if prior_target != expected_target:
            raise ServiceBundleError(
                f"active release changed: expected {expected_target!r}, got {prior_target!r}"
            )
    elif expected_current_version is not None:
        raise ServiceBundleError("active release disappeared before activation")
    temporary = service_root / f".active-{os.getpid()}-{version}.tmp"
    try:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
        temporary.symlink_to(Path("releases") / version)
        if active.exists() or active.is_symlink():
            current_info = active.lstat()
            if not stat.S_ISLNK(current_info.st_mode) or current_info.st_uid != 0:
                raise ServiceBundleError("active release pointer changed before atomic activation")
            if os.readlink(active) != prior_target:
                raise ServiceBundleError("active release pointer changed before atomic activation")
        elif prior_target is not None:
            raise ServiceBundleError("active release pointer disappeared before atomic activation")
        os.replace(temporary, active)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
    return active


def _normalize_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _wheel_requirement_names(lock_path: Path) -> set[str]:
    names: set[str] = set()
    for raw_line in lock_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("--", "-e", "-r", "-c")):
            if line.startswith("--hash="):
                continue
            raise ServiceBundleError(
                f"requirements.lock must contain only pinned wheel requirements and hashes: {line}"
            )
        if "@" in line or "://" in line or "git+" in line:
            raise ServiceBundleError("requirements.lock must not contain direct URLs or VCS inputs")
        requirement_part = line.split(";", 1)[0].rstrip("\\").strip()
        requirement_tokens = [token for token in requirement_part.split() if not token.startswith("--hash=")]
        if len(requirement_tokens) != 1:
            raise ServiceBundleError(f"invalid hash-pinned requirement line: {line}")
        match = WHEEL_REQUIREMENT_PATTERN.fullmatch(requirement_tokens[0])
        if match is None:
            raise ServiceBundleError(
                f"requirements.lock entries must use exact package==version pins: {line}"
            )
        names.add(_normalize_distribution(match.group(1)))
    return names


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ServiceBundleError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise ServiceBundleError(f"{label} must contain a JSON object: {path}")
    return value


def _debian_arch_for_host() -> str:
    machine = platform.machine().lower()
    mapping = {"x86_64": "amd64"}
    try:
        return mapping[machine]
    except KeyError as error:
        raise ServiceBundleError(
            "release-lock.json supports Ubuntu 24.04 x86_64 (amd64) only; "
            f"unsupported native service bundle build CPU: {machine}"
        ) from error


def _entrypoint_text(service: str) -> str:
    commands = {
        "yield": (
            'exec python3 -s -m cy_exec.training.product_cli '
            '--state-directory "${CYRENE_DATA_DIR:-/var/lib/cyrene}/yield" '
            '--port "${CYRENE_PORT_YIELD:-8001}" "$@"'
        ),
        "reactor": (
            ""
        ),
        "exchange": (
            'exec python3 -s -m cyrene_exchange_product.cli '
            '--database "${CYRENE_DATA_DIR:-/var/lib/cyrene}/exchange.sqlite3" '
            'serve --port "${CYRENE_PORT_EXCHANGE:-8003}" "$@"'
        ),
        "catalyst": (
            'exec python3 -s -m cyrene_catalyst.cli serve '
            '--home "${CYRENE_DATA_DIR:-/var/lib/cyrene}/catalyst" '
            '--port "${CYRENE_PORT_CATALYST:-8004}" "$@"'
        ),
    }
    if service == "navigator":
        return """#!/bin/sh
set -eu
BUNDLE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
unset PYTHONHOME
export PYTHONPATH="$BUNDLE_DIR/python"
DATA_HOME="${CYRENE_DEV_HOME:-${CYRENE_DATA_DIR:-/var/lib/cyrene}}"
PAIRING_CODE_FILE="${CYRENE_PAIR_CODE_FILE:-$DATA_HOME/pair_code.txt}"
exec python3 -s "$BUNDLE_DIR/serve-web.py" \\
  --host "${CYRENE_WEB_HOST_HOST:-127.0.0.1}" \\
  --port "${CYRENE_PORT_NAVIGATOR:-7860}" \\
  --pairing-code-file "$PAIRING_CODE_FILE" \\
  --proxy "/api/v1/yield=http://127.0.0.1:${CYRENE_PORT_YIELD:-8001}" \\
  --proxy "/api/v1/reactor=http://127.0.0.1:${CYRENE_PORT_REACTOR:-8002}" \\
  --proxy "/api/v1/exchange=http://127.0.0.1:${CYRENE_PORT_EXCHANGE:-8003}" \\
  --proxy "/api/v1/catalyst=http://127.0.0.1:${CYRENE_PORT_CATALYST:-8004}" \\
  --insecure-http "$@"
"""
    if service == "reactor":
        return """#!/bin/sh
set -eu
BUNDLE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
unset PYTHONHOME
export PYTHONPATH="$BUNDLE_DIR/python"
DATA_HOME="${CYRENE_DATA_DIR:-/var/lib/cyrene}"
REACTOR_HOME="$DATA_HOME/reactor"
REACTOR_CONFIG="${CYRENE_REACTOR_CONFIG:-$REACTOR_HOME/control.json}"
mkdir -p "$REACTOR_HOME/private" "$(dirname -- "$REACTOR_CONFIG")"
chmod 700 "$REACTOR_HOME/private"
CYRENE_REACTOR_HOME="$REACTOR_HOME" CYRENE_REACTOR_CONFIG_PATH="$REACTOR_CONFIG" \\
  python3 -s - <<'PY'
import json
import os
import secrets
from pathlib import Path

home = Path(os.environ["CYRENE_REACTOR_HOME"])
private = home / "private"
private.mkdir(parents=True, exist_ok=True, mode=0o700)
for name in ("control.token", "serving.token"):
    token_path = private / name
    if not token_path.exists():
        descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(secrets.token_urlsafe(48))
    if token_path.stat().st_mode & 0o077:
        raise SystemExit(f"Reactor private credential has unsafe permissions: {token_path}")

config_path = Path(os.environ["CYRENE_REACTOR_CONFIG_PATH"])
if not config_path.exists():
    document = {
        "database_path": str(home / "reactor.sqlite3"),
        "credential_file": str(private / "control.token"),
        "public_base_url": f"http://127.0.0.1:{os.environ.get('CYRENE_PORT_REACTOR', '8002')}",
        "serving_bindings": [
            {
                "binding_id": "local-gpu",
                "control_url": os.environ.get(
                    "CYRENE_SERVING_CONTROL_URL", "http://127.0.0.1:19400"
                ),
                "credential_file": str(private / "serving.token"),
            }
        ],
        "exchange_receivers": [],
    }
    temporary = config_path.with_name(config_path.name + f".{os.getpid()}.new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.write("\\n")
    os.replace(temporary, config_path)
PY
exec python3 -s -m cyrene_reactor_product.cli control \\
  --config "$REACTOR_CONFIG" --port "${CYRENE_PORT_REACTOR:-8002}" "$@"
"""
    command = commands[service]
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        'BUNDLE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
        "unset PYTHONHOME\n"
        'export PYTHONPATH="$BUNDLE_DIR/python"\n'
        f"{command}\n"
    )


def _write_bundle_manifest(
    bundle_root: Path,
    *,
    service: str,
    source_commit: str,
    source_repository: str,
    debian_arch: str,
    python_version: str,
) -> dict[str, Any]:
    for current, directory_names, file_names in os.walk(bundle_root, followlinks=False):
        current_path = Path(current)
        current_path.chmod(0o755)
        for name in directory_names:
            (current_path / name).chmod(0o755)
        for name in file_names:
            path = current_path / name
            path.chmod(0o755 if path.name == "run-service" else 0o644)
    files = _expected_file_map(bundle_root)
    lock_digest = files.get("requirements.lock")
    if lock_digest is None:
        raise ServiceBundleError("service bundle has no requirements.lock")
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "service": service,
        "version": "",
        "entrypoint": "run-service",
        "health": {"path": HEALTH_PATHS[service]},
        "files": files,
        "source_repository": source_repository,
        "source_commit": source_commit,
        "dependencies": {"lock_file": "requirements.lock", "lock_sha256": lock_digest},
        "target": {"debian_arch": debian_arch, "python": python_version},
        "artifact_digest": "",
    }
    digest = _artifact_digest(manifest)
    manifest["version"] = digest
    manifest["artifact_digest"] = digest
    manifest_path = bundle_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _build_one_bundle(
    *,
    service: str,
    wheelhouse: Path,
    output_root: Path,
    source_commit: str,
    source_repository: str,
    debian_arch: str,
    python_executable: Path,
    builder_python: Path,
) -> Path:
    service_input = wheelhouse / service
    if service_input.is_symlink() or not service_input.is_dir():
        raise ServiceBundleError(f"missing {service} service wheelhouse: {service_input}")
    allowed_names = {"requirements.lock", "source.json"}
    if service == "navigator":
        allowed_names.add("serve-web.py")
    wheels: list[Path] = []
    for child in sorted(service_input.iterdir()):
        if child.is_symlink() or not child.is_file():
            raise ServiceBundleError(f"wheelhouse entries must be flat regular files: {child}")
        if child.name.endswith(".whl"):
            wheels.append(child)
        elif child.name not in allowed_names:
            raise ServiceBundleError(
                f"unexpected {service} wheelhouse input {child.name!r}; allowed metadata: "
                "requirements.lock, source.json, and flat .whl files"
            )
    if not wheels:
        raise ServiceBundleError(f"{service} wheelhouse has no .whl files: {service_input}")

    lock_path = service_input / "requirements.lock"
    source_path = service_input / "source.json"
    if not lock_path.is_file() or not source_path.is_file():
        raise ServiceBundleError(
            f"{service} wheelhouse requires requirements.lock and source.json: {service_input}"
        )
    if service == "navigator" and not (service_input / "serve-web.py").is_file():
        raise ServiceBundleError(
            "navigator wheelhouse requires serve-web.py from the pinned Navigator source revision"
        )
    if service == "navigator":
        launcher_source = (service_input / "serve-web.py").read_text(encoding="utf-8")
        if (
            '"--pairing-code-file"' not in launcher_source
            or '"pairingCodeFile"' not in launcher_source
            or '"pairingCode": pairing_code' in launcher_source
        ):
            raise ServiceBundleError(
                "Navigator pinned serve-web.py must accept --pairing-code-file and must not "
                "write the pairing secret to stdout; update release-lock.json to a secure revision"
            )
    source = _read_json_object(source_path, "service wheelhouse source.json")
    required_source_keys = {"schema_version", "service", "source_repository", "source_commit", "requirements_lock_sha256"}
    if set(source) != required_source_keys:
        raise ServiceBundleError(
            f"{service}/source.json keys must be exactly {sorted(required_source_keys)}"
        )
    if source["schema_version"] != 1 or source["service"] != service:
        raise ServiceBundleError(f"{service}/source.json identifies a different bundle")
    if source["source_repository"] != source_repository:
        raise ServiceBundleError(f"{service}/source.json has the wrong source repository")
    if source["source_commit"] != source_commit:
        raise ServiceBundleError(
            f"{service} wheelhouse source commit differs from release-lock.json: "
            f"expected {source_commit}, got {source['source_commit']}"
        )
    lock_digest = _sha256_file(lock_path)
    if source["requirements_lock_sha256"] != lock_digest:
        raise ServiceBundleError(f"{service}/source.json requirements lock digest does not match")
    requirement_names = _wheel_requirement_names(lock_path)
    missing_distributions = [
        distribution
        for distribution in REQUIRED_DISTRIBUTIONS[service]
        if _normalize_distribution(distribution) not in requirement_names
    ]
    if missing_distributions:
        raise ServiceBundleError(
            f"{service}/requirements.lock does not pin application wheel(s): "
            + ", ".join(missing_distributions)
        )

    bundle_root = output_root / service / ".build"
    if bundle_root.exists():
        shutil.rmtree(bundle_root)
    bundle_root.mkdir(parents=True)
    site_packages = bundle_root / "python"
    command = [
        str(builder_python),
        "-m",
        "pip",
        "install",
        "--no-index",
        "--no-deps",
        "--find-links",
        str(service_input),
        "--require-hashes",
        "--only-binary=:all:",
        "--no-compile",
        "--target",
        str(site_packages),
        "-r",
        str(lock_path),
    ]
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as error:
        raise ServiceBundleError(
            f"offline install failed for {service}; verify every exact wheel and hash in "
            f"{service_input} (pip exit {error.returncode})"
        ) from error

    env = os.environ.copy()
    env["PYTHONPATH"] = str(site_packages)
    distribution_check = (
        "import importlib.metadata as m; "
        f"required={REQUIRED_DISTRIBUTIONS[service]!r}; "
        "missing=[name for name in required if not any("
        "d.metadata.get('Name','').lower().replace('_','-') == name for d in m.distributions())]; "
        "raise SystemExit('missing installed distributions: '+', '.join(missing)) if missing else None"
    )
    subprocess.run([str(python_executable), "-c", distribution_check], env=env, check=True)

    (bundle_root / "requirements.lock").write_bytes(lock_path.read_bytes())
    (bundle_root / "source.json").write_bytes(source_path.read_bytes())
    if service == "navigator":
        shutil.copy2(service_input / "serve-web.py", bundle_root / "serve-web.py")
    entrypoint = bundle_root / "run-service"
    entrypoint.write_text(_entrypoint_text(service), encoding="utf-8")
    entrypoint.chmod(0o755)
    _write_bundle_manifest(
        bundle_root,
        service=service,
        source_commit=source_commit,
        source_repository=source_repository,
        debian_arch=debian_arch,
        python_version="3.12",
    )
    manifest = validate_bundle(bundle_root, expected_service=service)
    final_dir = output_root / service / manifest["version"]
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    if final_dir.exists():
        existing = validate_bundle(final_dir, expected_service=service)
        if existing["artifact_digest"] != manifest["artifact_digest"]:
            raise ServiceBundleError(f"output release collision at {final_dir}")
        shutil.rmtree(bundle_root)
        return final_dir
    os.rename(bundle_root, final_dir)
    return final_dir


def build_service_bundles(
    *,
    wheelhouse_root: Path,
    release_lock_path: Path,
    output_root: Path,
    debian_arch: str,
    python_executable: Path,
) -> list[Path]:
    """Build five self-contained runtime bundles from pinned offline wheelhouses."""

    wheelhouse = Path(wheelhouse_root)
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise ServiceBundleError(
            f"service wheelhouse is missing: {wheelhouse}; see packaging/service-bundle.md"
        )
    entries = sorted(wheelhouse.iterdir())
    if [entry.name for entry in entries] != sorted(SERVICES) or any(
        entry.is_symlink() or not entry.is_dir() for entry in entries
    ):
        raise ServiceBundleError(
            "service wheelhouse root must contain exactly these directories: "
            + ", ".join(SERVICES)
        )
    if debian_arch != "amd64":
        raise ServiceBundleError(
            "release-lock.json supports Ubuntu 24.04 x86_64 (amd64) only; "
            f"refusing service bundle target {debian_arch}"
        )
    host_arch = _debian_arch_for_host()
    if debian_arch != host_arch:
        raise ServiceBundleError(
            f"cross-architecture service bundles are unsupported: requested {debian_arch}, "
            f"build host is {host_arch}"
        )
    python = Path(python_executable)
    if not python.is_file():
        raise ServiceBundleError(f"build Python interpreter is missing: {python}")
    version = subprocess.run(
        [str(python), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if version != "3.12":
        raise ServiceBundleError(f"service wheelhouses require Python 3.12, got {version}")
    lock = _read_json_object(Path(release_lock_path), "Workspace release-lock.json")
    repositories = lock.get("repositories")
    if not isinstance(repositories, dict):
        raise ServiceBundleError("release-lock.json has no repositories object")

    output = Path(output_root)
    output.mkdir(parents=True, exist_ok=True)
    build_root = Path(tempfile.mkdtemp(prefix="cyrene-wheel-installer-"))
    try:
        builder_env = build_root / "venv"
        try:
            venv.EnvBuilder(with_pip=True, clear=True).create(builder_env)
        except (OSError, subprocess.CalledProcessError) as error:
            raise ServiceBundleError(
                "could not create the isolated packaging Python environment; install python3.12-venv"
            ) from error
        builder_python = builder_env / "bin" / "python"
        results: list[Path] = []
        for service in SERVICES:
            repository = REPOSITORIES[service]
            commit = repositories.get(repository)
            if not isinstance(commit, str) or not COMMIT_PATTERN.fullmatch(commit):
                raise ServiceBundleError(
                    f"release-lock.json has no immutable source commit for {repository}"
                )
            results.append(
                _build_one_bundle(
                    service=service,
                    wheelhouse=wheelhouse,
                    output_root=output,
                    source_commit=commit,
                    source_repository=repository,
                    debian_arch=debian_arch,
                    python_executable=python,
                    builder_python=builder_python,
                )
            )
        return results
    finally:
        shutil.rmtree(build_root, ignore_errors=True)


def bootstrap_package_bundles(activate_missing: bool) -> int:
    """Install package-provided artifacts and activate only missing services.

    Returns the number of immutable releases newly copied into the installation.
    Existing active releases remain untouched during Debian package upgrades.
    """

    artifact_root = Path(
        os.environ.get("CYRENE_SERVICE_ARTIFACTS_ROOT", "/usr/share/cyrene/service-artifacts")
    )
    _ensure_secure_directory(artifact_root, create=False)
    staged_count = 0
    for service in SERVICES:
        service_artifacts = artifact_root / service
        _ensure_secure_directory(service_artifacts, create=False)
        artifact_entries = sorted(service_artifacts.iterdir())
        if len(artifact_entries) != 1 or not artifact_entries[0].is_dir():
            raise ServiceBundleError(
                f"package must provide exactly one {service} bundle directory, "
                f"found {len(artifact_entries)} artifact entries"
            )
        candidates = artifact_entries
        _ensure_secure_directory(candidates[0], create=False)
        _assert_secure_tree(candidates[0])
        manifest = validate_bundle(candidates[0], expected_service=service)
        if candidates[0].name != manifest["version"]:
            raise ServiceBundleError(
                f"package artifact directory and manifest version differ for {service}"
            )
        destination = _service_root(service) / "releases" / manifest["version"]
        existed = destination.exists()
        stage_release(candidates[0])
        if not existed:
            staged_count += 1
        active = _service_root(service) / "active"
        if active.is_symlink():
            resolve_active_release(service)
        elif active.exists():
            raise ServiceBundleError(f"active release path is not a symbolic link: {active}")
        elif activate_missing:
            activate_release(
                service,
                manifest["version"],
                expected_current_version=None,
            )
    return staged_count


def _build_command(arguments: argparse.Namespace) -> int:
    results = build_service_bundles(
        wheelhouse_root=arguments.wheelhouse,
        release_lock_path=arguments.release_lock,
        output_root=arguments.output,
        debian_arch=arguments.arch,
        python_executable=arguments.python,
    )
    for bundle in results:
        manifest = validate_bundle(bundle)
        print(f"{manifest['service']}: {bundle} ({manifest['version']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and manage Cyrene service release bundles")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="build all five bundles from an offline wheelhouse")
    build.add_argument("--wheelhouse", type=Path, required=True)
    build.add_argument("--release-lock", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--arch", required=True)
    build.add_argument("--python", type=Path, default=Path(sys.executable))
    build.set_defaults(handler=_build_command)
    arguments = parser.parse_args(argv)
    try:
        return arguments.handler(arguments)
    except (ServiceBundleError, OSError, subprocess.CalledProcessError) as error:
        print(f"service-bundle: ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
