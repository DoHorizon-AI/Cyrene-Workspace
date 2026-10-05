"""Project the admitted Yield package binding into protected Product settings.

The Product activity credential remains owned by the existing source-token
initializer. This module adds only the fixed Package Runtime socket and binding,
plus a separate owner API bearer stored outside component release directories.
中文：为已准入的 Yield 包绑定生成受保护配置，并复用既有来源凭据。
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

YIELD_PACKAGE_ENVIRONMENT = Path("/etc/cyrene/yield-package-runtime.env")
YIELD_OWNER_TOKEN_DIRECTORY = Path("/etc/cyrene/runtime-owner-tokens")
YIELD_OWNER_TOKEN_FILE = YIELD_OWNER_TOKEN_DIRECTORY / "yield.token"
YIELD_ACTIVITY_TOKEN_FILE = Path("/etc/cyrene/runtime-activity-source-tokens/cyrene-yield.token")
ACTIVITY_CATALOG_ENVIRONMENT = Path("/etc/cyrene/runtime-activity-sources.env")


class ProductPackageEnvironmentError(ValueError):
    """Raised when trusted package identity or protected file metadata is invalid."""


@dataclass(frozen=True)
class ProductPackageEnvironmentResult:
    """Secret-free description of the generated Product projection."""

    environment_path: Path
    owner_token_path: Path
    changed: bool


def _read_regular_file(path: Path, *, mode: int, maximum: int) -> bytes:
    """Read a root-owned file without following links or accepting aliases."""

    descriptor: int | None = None
    try:
        before = path.lstat()
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        current = os.fstat(descriptor)
        if (
            path.is_symlink()
            or not stat.S_ISREG(current.st_mode)
            or current.st_uid != 0
            or current.st_gid != 0
            or stat.S_IMODE(current.st_mode) != mode
            or current.st_nlink != 1
            or current.st_dev != before.st_dev
            or current.st_ino != before.st_ino
            or current.st_size < 1
            or current.st_size > maximum
        ):
            raise ProductPackageEnvironmentError("Product package credential metadata is unsafe")
        content = os.read(descriptor, maximum + 1)
    except OSError as error:
        raise ProductPackageEnvironmentError(
            "Product package credential is unavailable or unsafe"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(content) > maximum:
        raise ProductPackageEnvironmentError("Product package credential is too large")
    return content


def _require_directory(path: Path, *, mode: int, create: bool) -> None:
    """Require or create a root-owned directory with exact private metadata."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            raise ProductPackageEnvironmentError("Product package directory is unavailable")
        try:
            path.mkdir(mode=mode)
            os.chown(path, 0, 0, follow_symlinks=False)
        except OSError as error:
            raise ProductPackageEnvironmentError(
                "Product package directory cannot be created safely"
            ) from error
        info = path.lstat()
    except OSError as error:
        raise ProductPackageEnvironmentError("Product package directory is unsafe") from error
    if (
        path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) != mode
        or info.st_nlink < 2
    ):
        raise ProductPackageEnvironmentError("Product package directory metadata is unsafe")


def _atomic_root_file(path: Path, content: bytes, *, mode: int) -> bool:
    """Atomically write a root-owned projection while preserving stable files."""

    existing: bytes | None = None
    if path.exists() or path.is_symlink():
        existing = _read_regular_file(path, mode=mode, maximum=4096)
    if existing == content:
        return False

    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, 0, 0)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        raise ProductPackageEnvironmentError(
            "Product package environment cannot be written safely"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def ensure_yield_package_environment(
    runtime_bootstrap: Any,
    candidate: Any,
    installation_record: Any,
    activity_catalog: Any,
    runtime_source_policy: Any,
    *,
    previous_catalog_generation: int,
    activity_token_file: Path = YIELD_ACTIVITY_TOKEN_FILE,
    activity_environment_path: Path = ACTIVITY_CATALOG_ENVIRONMENT,
    environment_path: Path = YIELD_PACKAGE_ENVIRONMENT,
    owner_token_directory: Path = YIELD_OWNER_TOKEN_DIRECTORY,
    owner_token_file: Path = YIELD_OWNER_TOKEN_FILE,
) -> ProductPackageEnvironmentResult:
    """Project the exact admitted binding and separate owner bearer for Yield.

    The existing activity token is read only to confirm that its on-disk value
    still matches the catalog. It is never returned, copied, or written here.
    The generated owner bearer is stable across retries and stored separately
    from the component version tree.
    """

    if os.geteuid() != 0:
        raise PermissionError("Product package environment projection requires root")
    if not environment_path.is_absolute() or not owner_token_file.is_absolute():
        raise ProductPackageEnvironmentError("Product package paths must be absolute")
    if owner_token_file.parent != owner_token_directory:
        raise ProductPackageEnvironmentError("Product owner token path is not canonical")

    catalog_generation, source = runtime_bootstrap._activity_catalog_identity(activity_catalog)
    if type(previous_catalog_generation) is not int or previous_catalog_generation < 1:
        raise ProductPackageEnvironmentError("Previous activity catalog generation is invalid")
    if catalog_generation not in {
        previous_catalog_generation,
        previous_catalog_generation + 1,
    }:
        raise ProductPackageEnvironmentError("Activity catalog generation advanced unexpectedly")
    policy = runtime_bootstrap.validate_runtime_source_policy(
        runtime_source_policy, candidate, installation_record, activity_catalog
    )
    if (
        policy["generation"] != catalog_generation
        or source["source_id"] != runtime_bootstrap.PACKAGE_SOURCE_ID
    ):
        raise ProductPackageEnvironmentError("Product package source generation is stale")
    if (
        source["uid"] != runtime_bootstrap._runtime_user_id()
        or source["gid"] != runtime_bootstrap._runtime_group_id()
    ):
        raise ProductPackageEnvironmentError("Yield activity source principal is not trusted")
    runtime_bootstrap._read_source_token(activity_token_file, source["source_token_sha256"])

    _require_directory(environment_path.parent, mode=0o755, create=False)
    _require_directory(activity_environment_path.parent, mode=0o755, create=False)
    _require_directory(owner_token_directory, mode=0o700, create=True)

    # All five Product units load this shared generation file. Keep it aligned
    # before the caller restarts those units under the still-active hold.
    current_activity_environment = _read_regular_file(
        activity_environment_path, mode=0o644, maximum=4096
    )
    expected_activity_environment = (
        f"CYRENE_RUNTIME_ACTIVITY_CATALOG_GENERATION={previous_catalog_generation}\n"
    ).encode("ascii")
    current_generation_environment = (
        f"CYRENE_RUNTIME_ACTIVITY_CATALOG_GENERATION={catalog_generation}\n"
    ).encode("ascii")
    if current_activity_environment not in {
        expected_activity_environment,
        current_generation_environment,
    }:
        raise ProductPackageEnvironmentError("Activity catalog environment is stale or ambiguous")
    activity_generation_changed = _atomic_root_file(
        activity_environment_path, current_generation_environment, mode=0o644
    )
    owner_token_changed = False
    if owner_token_file.exists() or owner_token_file.is_symlink():
        raw_owner_token = _read_regular_file(owner_token_file, mode=0o600, maximum=256)
    else:
        raw_owner_token = (secrets.token_urlsafe(48) + "\n").encode("ascii")
        owner_token_changed = _atomic_root_file(owner_token_file, raw_owner_token, mode=0o600)
        raw_owner_token = _read_regular_file(owner_token_file, mode=0o600, maximum=256)
    try:
        owner_token = raw_owner_token.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ProductPackageEnvironmentError("Product owner credential is malformed") from error
    if not owner_token or any(character.isspace() for character in owner_token):
        raise ProductPackageEnvironmentError("Product owner credential is malformed")

    owner_digest = hashlib.sha256(owner_token.encode("ascii")).hexdigest()
    environment = (
        f"CYRENE_PACKAGE_RUNTIME_SOCKET={runtime_bootstrap.PACKAGE_RUNTIME_CONTROL_SOCKET}\n"
        f"CYRENE_LLAMA_FACTORY_PACKAGE_BINDING_ID={runtime_bootstrap.PACKAGE_BINDING_ID}\n"
        f"YIELD_RUNTIME_OWNER_TOKEN_SHA256={owner_digest}\n"
    ).encode("ascii")
    changed = _atomic_root_file(environment_path, environment, mode=0o644)
    return ProductPackageEnvironmentResult(
        environment_path=environment_path,
        owner_token_path=owner_token_file,
        changed=changed or activity_generation_changed or owner_token_changed,
    )
