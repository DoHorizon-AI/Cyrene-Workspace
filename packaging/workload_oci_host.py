"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 workload_oci_host.py                                            │
│  Module: packaging.workload_oci_host                                │
│  Role: Safely run the verified Echo OCI image on a Linux host.       │
│                                                                      │
│  模块职责：在 Linux 主机上安全运行已验证的 Echo OCI 镜像。               │
│  · 校验精确镜像摘要  · 管理受限容器  · 不接管安装计划或维护事务            │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

OFFICIAL_ECHO_REPOSITORY = "ghcr.io/dohorizon-ai/cyrene-echo"
ECHO_TARGET_ID = "linux-ubuntu-24.04-x86_64-oci"
ECHO_TARGET = {
    "os": "linux",
    "osVersion": "24.04",
    "distribution": "ubuntu",
    "distributionVersion": "24.04",
    "architecture": "x86_64",
    "runtime": "oci",
}
ECHO_PLATFORM = {"os": "linux", "architecture": "amd64"}
DEFAULT_DATA_DIRECTORY = Path("/var/lib/cyrene/echo")
DEFAULT_ARTIFACT_DIRECTORY = Path("/var/lib/cyrene/echo/artifacts")
DEFAULT_SOURCE_TOKEN_PATH = Path("/etc/cyrene/runtime-activity-source-tokens/cyrene-echo.token")
DEFAULT_MAINTENANCE_SOCKET_PATH = Path("/run/cyrene/runtime-maintenance.sock")
DEFAULT_TOKEN_STAGING_ROOT = Path("/var/lib/cyrene/runtime-activity-source-tokens/oci")
DEFAULT_PRIVATE_RUNTIME_ROOT = Path("/run/cyrene/workload-oci")
DEFAULT_DOCKER_BINARY = Path("/usr/bin/docker")
ROOT_OWNER_UID = 0
ROOT_OWNER_GID = 0
CONTAINER_TOKEN_PATH = Path("/run/secrets/cyrene-runtime-activity-token")
CONTAINER_MAINTENANCE_SOCKET_PATH = Path("/run/cyrene/runtime-maintenance.sock")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_RAW_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CONTAINER_NAME = re.compile(r"cyrene-echo(?:-[0-9a-f]{12})?\Z")
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")
_ENVIRONMENT_KEYS = frozenset(
    {
        "CYRENE_EVALUATION_RUNNER_CONNECTION_REF",
        "CYRENE_DATA_TOOLS_TOKEN",
    }
)
_RUNNER = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class WorkloadOciHostError(RuntimeError):
    """Raised when a verified OCI workload cannot be managed safely."""


@dataclass(frozen=True)
class OciHostSpec:
    """Validated host configuration passed by the workload transaction owner."""

    component_id: str
    target_id: str
    manifest_digest: str
    image_repository: str
    image_digest: str
    container_name: str
    host_uid: int
    host_gid: int
    host_port: int
    activity_source_id: str
    activity_catalog_generation: int
    activity_token_digest: str
    activity_token_path: Path
    maintenance_socket_path: Path
    data_directory: Path
    artifact_directory: Path
    private_environment: Mapping[str, str] = field(repr=False)

    @property
    def image_reference(self) -> str:
        """Return the immutable repository and manifest digest reference."""

        return f"{self.image_repository}@{self.image_digest}"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> OciHostSpec:
        """Validate one Echo OCI spec without retaining unchecked input fields."""

        expected_keys = {
            "schemaVersion",
            "componentId",
            "targetId",
            "target",
            "manifestDigest",
            "imageRepository",
            "imageDigest",
            "platform",
            "containerName",
            "hostUid",
            "hostGid",
            "hostPort",
            "activitySourceId",
            "activityCatalogGeneration",
            "activityTokenDigest",
            "activityTokenPath",
            "maintenanceSocketPath",
            "dataDirectory",
            "artifactDirectory",
            "environment",
        }
        if not isinstance(value, Mapping) or set(value) != expected_keys:
            raise WorkloadOciHostError("OCI host spec has an unsupported shape")
        if value.get("schemaVersion") != 1 or type(value.get("schemaVersion")) is not int:
            raise WorkloadOciHostError("OCI host spec version is unsupported")
        if value.get("componentId") != "cyrene-echo":
            raise WorkloadOciHostError("OCI host helper only manages the Echo Product")
        if value.get("targetId") != ECHO_TARGET_ID or value.get("target") != ECHO_TARGET:
            raise WorkloadOciHostError("OCI host target is not the supported Ubuntu 24.04 target")
        if value.get("platform") != ECHO_PLATFORM:
            raise WorkloadOciHostError("OCI image platform must be Linux amd64")
        if value.get("imageRepository") != OFFICIAL_ECHO_REPOSITORY:
            raise WorkloadOciHostError("OCI repository is not the official Echo image")

        manifest_digest = _require_digest(value.get("manifestDigest"), "manifestDigest")
        image_digest = _require_digest(value.get("imageDigest"), "imageDigest")
        container_name = value.get("containerName")
        if not isinstance(container_name, str) or _CONTAINER_NAME.fullmatch(container_name) is None:
            raise WorkloadOciHostError("OCI container name is invalid")

        host_uid = _require_integer(value.get("hostUid"), "hostUid", minimum=1)
        host_gid = _require_integer(value.get("hostGid"), "hostGid", minimum=1)
        host_port = _require_integer(value.get("hostPort"), "hostPort", minimum=1, maximum=65535)
        source_id = value.get("activitySourceId")
        if source_id != "cyrene-echo":
            raise WorkloadOciHostError("Echo ActivitySource identity is invalid")
        catalog_generation = _require_integer(
            value.get("activityCatalogGeneration"), "activityCatalogGeneration", minimum=1
        )
        token_digest = value.get("activityTokenDigest")
        if not isinstance(token_digest, str) or _RAW_DIGEST.fullmatch(token_digest) is None:
            raise WorkloadOciHostError("OCI host spec activityTokenDigest is invalid")
        paths = {
            "activityTokenPath": DEFAULT_SOURCE_TOKEN_PATH,
            "maintenanceSocketPath": DEFAULT_MAINTENANCE_SOCKET_PATH,
            "dataDirectory": DEFAULT_DATA_DIRECTORY,
            "artifactDirectory": DEFAULT_ARTIFACT_DIRECTORY,
        }
        for key, expected in paths.items():
            if _require_absolute_path(value.get(key), key) != expected:
                raise WorkloadOciHostError(f"OCI host spec {key} differs from the fixed host path")

        raw_environment = value.get("environment")
        if not isinstance(raw_environment, Mapping) or set(raw_environment) - _ENVIRONMENT_KEYS:
            raise WorkloadOciHostError("OCI Product environment contains unsupported keys")
        environment: dict[str, str] = {}
        for key, item in raw_environment.items():
            if not isinstance(item, str) or not item or any(ch in item for ch in "\x00\r\n"):
                raise WorkloadOciHostError("OCI Product environment contains an invalid value")
            environment[key] = item

        return cls(
            component_id="cyrene-echo",
            target_id=ECHO_TARGET_ID,
            manifest_digest=manifest_digest,
            image_repository=OFFICIAL_ECHO_REPOSITORY,
            image_digest=image_digest,
            container_name=container_name,
            host_uid=host_uid,
            host_gid=host_gid,
            host_port=host_port,
            activity_source_id=source_id,
            activity_catalog_generation=catalog_generation,
            activity_token_digest=token_digest,
            activity_token_path=paths["activityTokenPath"],
            maintenance_socket_path=paths["maintenanceSocketPath"],
            data_directory=paths["dataDirectory"],
            artifact_directory=paths["artifactDirectory"],
            private_environment=environment,
        )


@dataclass(frozen=True)
class OciContainerObservation:
    """Redacted container identity and state suitable for a transaction receipt."""

    component_id: str
    target_id: str
    manifest_digest: str
    image_digest: str
    image_reference: str
    container_name: str
    container_id: str | None
    state: str
    host_uid: int
    host_gid: int
    activity_source_id: str
    activity_catalog_generation: int
    data_directory: str
    artifact_directory: str

    def to_receipt_fields(self) -> dict[str, Any]:
        """Return non-secret fields for the caller-owned durable receipt."""

        return {
            "componentId": self.component_id,
            "targetId": self.target_id,
            "manifestDigest": self.manifest_digest,
            "imageDigest": self.image_digest,
            "imageReference": self.image_reference,
            "containerName": self.container_name,
            "containerId": self.container_id,
            "state": self.state,
            "hostUid": self.host_uid,
            "hostGid": self.host_gid,
            "activitySourceId": self.activity_source_id,
            "activityCatalogGeneration": self.activity_catalog_generation,
            "dataDirectory": self.data_directory,
            "artifactDirectory": self.artifact_directory,
        }


def pull_and_verify_image(
    spec: OciHostSpec | Mapping[str, Any], *, runner: _RUNNER | None = None
) -> dict[str, str]:
    """Pull only the exact verified digest and confirm its local platform identity.

    The caller must have verified the signed component manifest and OCI attestation
    before constructing this spec. This helper confirms Docker fetched that exact
    immutable reference; it does not replace signature or provenance verification.
    """

    checked = _coerce_spec(spec)
    _require_root()
    run = runner or _default_runner
    _run_docker(["pull", checked.image_reference], runner=run)
    image = _inspect_image(checked, runner=run)
    return {
        "imageReference": checked.image_reference,
        "imageDigest": checked.image_digest,
        "platform": "linux/amd64",
        "imageId": image["Id"],
    }


def start_container(
    spec: OciHostSpec | Mapping[str, Any], *, runner: _RUNNER | None = None
) -> OciContainerObservation:
    """Start the exact Echo image with private ActivitySource credentials and safe mounts.

    This function assumes its caller already holds the transaction's approved
    maintenance hold. It deliberately does not create or release Platform proof.
    """

    checked = _coerce_spec(spec)
    _require_root()
    run = runner or _default_runner
    pull_and_verify_image(checked, runner=run)
    _require_maintenance_socket(checked.maintenance_socket_path)
    _ensure_product_directories(checked)

    existing = _find_container_id(checked.container_name, runner=run)
    if existing is not None:
        observation, raw = _inspect_container(checked, existing, runner=run)
        _validate_container_config(
            checked,
            raw,
            _staged_token_path(checked),
            existing,
            runner=run,
            verify_staged_token=False,
        )
        if observation.state in {"created", "exited"}:
            token_path = _stage_activity_token(checked)
            _validate_container_config(checked, raw, token_path, existing, runner=run)
            _run_docker(["start", checked.container_name], runner=run)
            observation, raw = _inspect_container(checked, existing, runner=run)
            _validate_container_config(checked, raw, token_path, existing, runner=run)
        elif observation.state != "running":
            raise WorkloadOciHostError("OCI Product container is in an unsafe lifecycle state")
        else:
            _verify_staged_token(checked, _staged_token_path(checked))
        return observation

    token_path = _stage_activity_token(checked)
    environment_path = _write_private_environment(checked)
    try:
        _run_docker(_build_run_arguments(checked, token_path, environment_path), runner=run)
    finally:
        _unlink_private_environment(environment_path)

    container_id = _find_container_id(checked.container_name, runner=run)
    if container_id is None:
        raise WorkloadOciHostError("OCI runtime did not create the requested container")
    observation, raw = _inspect_container(checked, container_id, runner=run)
    _validate_container_config(checked, raw, token_path, container_id, runner=run)
    if observation.state != "running":
        raise WorkloadOciHostError("OCI Product container did not remain running")
    return observation


def container_status(
    spec: OciHostSpec | Mapping[str, Any], *, runner: _RUNNER | None = None
) -> OciContainerObservation:
    """Read and validate the managed container without exposing its environment."""

    checked = _coerce_spec(spec)
    _require_root()
    run = runner or _default_runner
    return _container_status(checked, runner=run, verify_staged_token=True)


def _container_status(
    spec: OciHostSpec, *, runner: _RUNNER, verify_staged_token: bool
) -> OciContainerObservation:
    """Read one exact container with an optional token check for recovery actions."""

    checked = spec
    container_id = _find_container_id(checked.container_name, runner=runner)
    if container_id is None:
        return _observation(checked, None, "not-installed")
    token_path = _staged_token_path(checked)
    observation, raw = _inspect_container(checked, container_id, runner=runner)
    _validate_container_config(
        checked,
        raw,
        token_path,
        container_id,
        runner=runner,
        verify_staged_token=verify_staged_token,
    )
    return observation


def stop_container(
    spec: OciHostSpec | Mapping[str, Any], *, runner: _RUNNER | None = None
) -> OciContainerObservation:
    """Stop only the named container after verifying its signed-image identity."""

    checked = _coerce_spec(spec)
    _require_root()
    run = runner or _default_runner
    observation = _container_status(checked, runner=run, verify_staged_token=False)
    if observation.state == "not-installed" or observation.state in {"created", "exited"}:
        return observation
    _run_docker(["stop", "--time", "30", checked.container_name], runner=run)
    observation = _container_status(checked, runner=run, verify_staged_token=False)
    if observation.state not in {"exited", "created"}:
        raise WorkloadOciHostError("OCI Product container did not stop cleanly")
    return observation


def remove_container(
    spec: OciHostSpec | Mapping[str, Any],
    *,
    runner: _RUNNER | None = None,
    remove_runtime_credentials: bool = False,
) -> OciContainerObservation:
    """Remove only the container object and optionally its staged source credential.

    Docker volumes are never removed. The Echo data and artifact directories are
    intentionally preserved so uninstall cannot erase user-owned training data.
    """

    checked = _coerce_spec(spec)
    _require_root()
    run = runner or _default_runner
    observation = stop_container(checked, runner=run)
    if observation.state != "not-installed":
        _run_docker(["rm", checked.container_name], runner=run)
    if remove_runtime_credentials:
        _remove_staged_credentials(checked)
    return _observation(checked, None, "not-installed")


def _coerce_spec(spec: OciHostSpec | Mapping[str, Any]) -> OciHostSpec:
    """Normalize an already validated spec or validate a plain mapping."""

    if not isinstance(spec, OciHostSpec):
        return OciHostSpec.from_mapping(spec)
    return OciHostSpec.from_mapping(
        {
            "schemaVersion": 1,
            "componentId": spec.component_id,
            "targetId": spec.target_id,
            "target": ECHO_TARGET,
            "manifestDigest": spec.manifest_digest,
            "imageRepository": spec.image_repository,
            "imageDigest": spec.image_digest,
            "platform": ECHO_PLATFORM,
            "containerName": spec.container_name,
            "hostUid": spec.host_uid,
            "hostGid": spec.host_gid,
            "hostPort": spec.host_port,
            "activitySourceId": spec.activity_source_id,
            "activityCatalogGeneration": spec.activity_catalog_generation,
            "activityTokenDigest": spec.activity_token_digest,
            "activityTokenPath": str(spec.activity_token_path),
            "maintenanceSocketPath": str(spec.maintenance_socket_path),
            "dataDirectory": str(spec.data_directory),
            "artifactDirectory": str(spec.artifact_directory),
            "environment": dict(spec.private_environment),
        }
    )


def _require_digest(value: Any, label: str) -> str:
    """Require one canonical typed SHA-256 digest."""

    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise WorkloadOciHostError(f"OCI host spec {label} is invalid")
    return value


def _require_integer(value: Any, label: str, *, minimum: int, maximum: int = 2**31 - 1) -> int:
    """Require a bounded integer while rejecting booleans."""

    if type(value) is not int or value < minimum or value > maximum:
        raise WorkloadOciHostError(f"OCI host spec {label} is invalid")
    return value


def _require_absolute_path(value: Any, label: str) -> Path:
    """Require a clean absolute path without traversal or control characters."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise WorkloadOciHostError(f"OCI host spec {label} is invalid")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise WorkloadOciHostError(f"OCI host spec {label} is invalid")
    return path


def _require_root() -> None:
    """Require the privileged host context used for container lifecycle changes."""

    if os.geteuid() != 0:
        raise WorkloadOciHostError("OCI workload lifecycle requires root")


def _default_runner(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run the system Docker client against the local root-controlled daemon."""

    binary = DEFAULT_DOCKER_BINARY
    if not binary.is_absolute() or not binary.exists() or not os.access(binary, os.X_OK):
        raise WorkloadOciHostError("The installed Docker client is unavailable")
    command = [str(binary), "--host", "unix:///var/run/docker.sock", *arguments]
    environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root"}
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkloadOciHostError("The local OCI runtime could not be reached") from error


def _run_docker(arguments: Sequence[str], *, runner: _RUNNER) -> str:
    """Run one Docker operation and suppress raw daemon output from exceptions."""

    result = runner(arguments)
    if result.returncode != 0:
        raise WorkloadOciHostError("The local OCI runtime operation failed")
    return result.stdout


def _inspect_image(spec: OciHostSpec, *, runner: _RUNNER) -> dict[str, Any]:
    """Require Docker's local image record to retain exact digest and platform."""

    output = _run_docker(
        ["image", "inspect", "--format", "{{json .}}", spec.image_reference], runner=runner
    )
    try:
        image = json.loads(output)
    except json.JSONDecodeError as error:
        raise WorkloadOciHostError("OCI image inspect returned malformed metadata") from error
    repository_digests = image.get("RepoDigests") if isinstance(image, dict) else None
    if (
        not isinstance(repository_digests, list)
        or spec.image_reference not in repository_digests
        or image.get("Os") != "linux"
        or image.get("Architecture") != "amd64"
        or not isinstance(image.get("Id"), str)
        or _DIGEST.fullmatch(image["Id"]) is None
    ):
        raise WorkloadOciHostError("Pulled OCI image does not match its exact digest and platform")
    return image


def _require_maintenance_socket(path: Path) -> None:
    """Require the exact Platform maintenance socket before Product activation."""

    try:
        info = path.lstat()
    except OSError as error:
        raise WorkloadOciHostError("Platform maintenance socket is unavailable") from error
    if not stat.S_ISSOCK(info.st_mode):
        raise WorkloadOciHostError("Platform maintenance path is not a Unix socket")


def _read_source_token(path: Path) -> bytes:
    """Read the root-only ActivitySource token without following links."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise WorkloadOciHostError("Echo ActivitySource token is unavailable") from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != ROOT_OWNER_UID
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise WorkloadOciHostError("Echo ActivitySource token permissions are unsafe")
        token = os.read(descriptor, 16385)
    finally:
        os.close(descriptor)
    if not token or len(token) > 16384 or b"\x00" in token or b"\n" in token or b"\r" in token:
        raise WorkloadOciHostError("Echo ActivitySource token content is invalid")
    return token


def _ensure_secure_directory(
    path: Path,
    *,
    mode: int,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> None:
    """Create or verify one private directory without traversing symbolic links."""

    owner_uid = ROOT_OWNER_UID if owner_uid is None else owner_uid
    owner_gid = ROOT_OWNER_GID if owner_gid is None else owner_gid
    _assert_no_symlink_components(path)
    try:
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as error:
        raise WorkloadOciHostError("OCI private runtime directory is unavailable") from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != owner_uid
        or info.st_gid != owner_gid
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise WorkloadOciHostError("OCI private runtime directory permissions are unsafe")
    os.chmod(path, mode)


def _assert_no_symlink_components(path: Path) -> None:
    """Reject an existing symbolic link in any fixed host path component."""

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise WorkloadOciHostError("OCI host path cannot be inspected safely") from error
        if stat.S_ISLNK(info.st_mode):
            raise WorkloadOciHostError("OCI host path contains a symbolic link")


def _atomic_write(
    path: Path, contents: bytes, *, mode: int, owner_uid: int, owner_gid: int
) -> None:
    """Atomically write a private runtime file with its final owner and mode."""

    _assert_no_symlink_components(path.parent)
    _ensure_secure_directory(path.parent, mode=0o700)
    try:
        current = path.lstat()
    except FileNotFoundError:
        current = None
    except OSError as error:
        raise WorkloadOciHostError("OCI private file cannot be inspected safely") from error
    if current is not None and (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1):
        raise WorkloadOciHostError("OCI private file path is occupied by an unsafe object")

    descriptor, temporary_name = tempfile.mkstemp(prefix=".cyrene-oci-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, owner_uid, owner_gid)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except OSError as error:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise WorkloadOciHostError("OCI private runtime file could not be staged") from error


def _staged_token_path(spec: OciHostSpec) -> Path:
    """Return the digest-scoped token mount source used by this container."""

    return DEFAULT_TOKEN_STAGING_ROOT / spec.container_name / "activity-token"


def _private_environment_path(spec: OciHostSpec) -> Path:
    """Return the transient root-only Docker env-file path for this container."""

    return DEFAULT_PRIVATE_RUNTIME_ROOT / spec.container_name / "product.env"


def _stage_activity_token(spec: OciHostSpec) -> Path:
    """Copy the verified host token into a digest-scoped mount readable by Echo."""

    token = _read_source_token(spec.activity_token_path)
    if hashlib.sha256(token).hexdigest() != spec.activity_token_digest:
        raise WorkloadOciHostError("Echo ActivitySource token differs from its catalog digest")
    path = _staged_token_path(spec)
    _ensure_secure_directory(path.parent, mode=0o700)
    _atomic_write(path, token, mode=0o400, owner_uid=spec.host_uid, owner_gid=spec.host_gid)
    return path


def _write_private_environment(spec: OciHostSpec) -> Path:
    """Stage non-public Product environment values in a transient root-only file."""

    environment = _container_environment(spec)
    data = "".join(f"{key}={value}\n" for key, value in sorted(environment.items())).encode("utf-8")
    path = _private_environment_path(spec)
    _ensure_secure_directory(path.parent, mode=0o700)
    _atomic_write(
        path,
        data,
        mode=0o600,
        owner_uid=ROOT_OWNER_UID,
        owner_gid=ROOT_OWNER_GID,
    )
    return path


def _container_environment(spec: OciHostSpec) -> dict[str, str]:
    """Build only the runtime and loopback settings required by Echo."""

    environment = dict(spec.private_environment)
    environment.update(
        {
            "CYRENE_RUNTIME_ACTIVITY_SOURCE_ID": spec.activity_source_id,
            "CYRENE_RUNTIME_ACTIVITY_SOURCE_TOKEN_FILE": str(CONTAINER_TOKEN_PATH),
            "CYRENE_RUNTIME_ACTIVITY_CATALOG_GENERATION": str(spec.activity_catalog_generation),
            "CYRENE_RUNTIME_MAINTENANCE_SOCKET": str(CONTAINER_MAINTENANCE_SOCKET_PATH),
            "ECHO_HOST": "127.0.0.1",
            "ECHO_PORT": str(spec.host_port),
            "ECHO_DATABASE_DIR": "/data/echo",
            "ECHO_ARTIFACT_ROOT": "/data/artifacts",
        }
    )
    return environment


def _container_labels(spec: OciHostSpec) -> dict[str, str]:
    """Bind one runtime object to its signed Product, target, and digest identity."""

    return {
        "io.cyrene.component-id": spec.component_id,
        "io.cyrene.target-id": spec.target_id,
        "io.cyrene.manifest-digest": spec.manifest_digest,
        "io.cyrene.image-digest": spec.image_digest,
        "io.cyrene.activity-source-id": spec.activity_source_id,
        "io.cyrene.activity-catalog-generation": str(spec.activity_catalog_generation),
    }


def _build_run_arguments(spec: OciHostSpec, token_path: Path, environment_path: Path) -> list[str]:
    """Construct the fixed least-privilege Docker invocation for Echo."""

    arguments = [
        "run",
        "--detach",
        "--name",
        spec.container_name,
        "--restart",
        "unless-stopped",
        "--network",
        "host",
        "--userns",
        "host",
        "--user",
        f"{spec.host_uid}:{spec.host_gid}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--read-only",
        "--pids-limit",
        "256",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=64m",
        "--env-file",
        str(environment_path),
    ]
    for key, value in sorted(_container_labels(spec).items()):
        arguments.extend(("--label", f"{key}={value}"))
    mounts = (
        (spec.data_directory, Path("/data/echo"), False),
        (spec.artifact_directory, Path("/data/artifacts"), False),
        (spec.maintenance_socket_path, CONTAINER_MAINTENANCE_SOCKET_PATH, True),
        (token_path, CONTAINER_TOKEN_PATH, True),
    )
    for source, target, readonly in mounts:
        argument = f"type=bind,source={source},target={target}"
        if readonly:
            argument += ",readonly"
        arguments.extend(("--mount", argument))
    arguments.append(spec.image_reference)
    return arguments


def _ensure_product_directories(spec: OciHostSpec) -> None:
    """Create only Echo's two persistent directories and preserve existing contents."""

    _assert_no_symlink_components(spec.data_directory.parent)
    parent = spec.data_directory.parent
    if not parent.exists():
        parent.mkdir(mode=0o755, parents=True)
    parent_info = parent.lstat()
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != ROOT_OWNER_UID
        or stat.S_IMODE(parent_info.st_mode) & 0o022
    ):
        raise WorkloadOciHostError("Echo data parent directory permissions are unsafe")

    _ensure_product_directory(spec.data_directory, spec.host_uid, spec.host_gid)
    _ensure_product_directory(spec.artifact_directory, spec.host_uid, spec.host_gid)


def _ensure_product_directory(path: Path, uid: int, gid: int) -> None:
    """Create a Product-owned directory or require existing write access for its identity."""

    _assert_no_symlink_components(path)
    created = False
    try:
        path.mkdir(mode=0o750)
        created = True
    except FileExistsError:
        pass
    except OSError as error:
        raise WorkloadOciHostError("Echo persistent directory is unavailable") from error
    try:
        info = path.lstat()
    except OSError as error:
        raise WorkloadOciHostError("Echo persistent directory cannot be inspected") from error
    if not stat.S_ISDIR(info.st_mode):
        raise WorkloadOciHostError("Echo persistent path is not a directory")
    if created:
        os.chown(path, uid, gid)
        os.chmod(path, 0o750)
        return
    owner_writable = info.st_uid == uid and stat.S_IMODE(info.st_mode) & 0o300 == 0o300
    group_writable = info.st_gid == gid and stat.S_IMODE(info.st_mode) & 0o030 == 0o030
    if not (owner_writable or group_writable):
        raise WorkloadOciHostError("Echo persistent directory is not writable by its host identity")


def _find_container_id(name: str, *, runner: _RUNNER) -> str | None:
    """Find exactly one object with the fixed container name."""

    output = _run_docker(
        ["container", "ls", "--all", "--quiet", "--filter", f"name=^/{name}$"],
        runner=runner,
    )
    identities = [line.strip() for line in output.splitlines() if line.strip()]
    if not identities:
        return None
    if len(identities) != 1 or _CONTAINER_ID.fullmatch(identities[0]) is None:
        raise WorkloadOciHostError("OCI runtime found an ambiguous container identity")
    return identities[0]


def _inspect_container(
    spec: OciHostSpec, container_id: str, *, runner: _RUNNER
) -> tuple[OciContainerObservation, dict[str, Any]]:
    """Parse one exact container record without including raw metadata in errors."""

    output = _run_docker(
        ["container", "inspect", "--format", "{{json .}}", container_id], runner=runner
    )
    try:
        raw = json.loads(output)
    except json.JSONDecodeError as error:
        raise WorkloadOciHostError("OCI container inspect returned malformed metadata") from error
    if not isinstance(raw, dict):
        raise WorkloadOciHostError("OCI container inspect returned an invalid identity")
    return _observe_container(spec, container_id, raw), raw


def _observe_container(
    spec: OciHostSpec, container_id: str, raw: Mapping[str, Any]
) -> OciContainerObservation:
    """Return only stable non-secret fields from Docker inspect metadata."""

    state_data = raw.get("State")
    state = state_data.get("Status") if isinstance(state_data, Mapping) else None
    if state not in {"created", "running", "paused", "restarting", "removing", "exited", "dead"}:
        raise WorkloadOciHostError("OCI container state is invalid")
    if _CONTAINER_ID.fullmatch(container_id) is None:
        raise WorkloadOciHostError("OCI container ID is invalid")
    return _observation(spec, container_id, state)


def _observation(
    spec: OciHostSpec, container_id: str | None, state: str
) -> OciContainerObservation:
    """Build the redacted operation result shared by status and lifecycle calls."""

    return OciContainerObservation(
        component_id=spec.component_id,
        target_id=spec.target_id,
        manifest_digest=spec.manifest_digest,
        image_digest=spec.image_digest,
        image_reference=spec.image_reference,
        container_name=spec.container_name,
        container_id=container_id,
        state=state,
        host_uid=spec.host_uid,
        host_gid=spec.host_gid,
        activity_source_id=spec.activity_source_id,
        activity_catalog_generation=spec.activity_catalog_generation,
        data_directory=str(spec.data_directory),
        artifact_directory=str(spec.artifact_directory),
    )


def _validate_container_config(
    spec: OciHostSpec,
    raw: Mapping[str, Any],
    token_path: Path,
    container_id: str,
    *,
    runner: _RUNNER,
    verify_staged_token: bool = True,
) -> None:
    """Reject a same-name container unless its image, identity, and isolation match."""

    config = raw.get("Config")
    host_config = raw.get("HostConfig")
    labels = config.get("Labels") if isinstance(config, Mapping) else None
    if (
        raw.get("Id") != container_id
        or _CONTAINER_ID.fullmatch(container_id) is None
        or raw.get("Name") != f"/{spec.container_name}"
        or not isinstance(config, Mapping)
        or not isinstance(host_config, Mapping)
        or config.get("Image") != spec.image_reference
        or config.get("User") != f"{spec.host_uid}:{spec.host_gid}"
        or not isinstance(labels, Mapping)
        or dict(labels) != _container_labels(spec)
    ):
        raise WorkloadOciHostError("OCI container identity differs from the verified workload")

    if (
        host_config.get("NetworkMode") != "host"
        or host_config.get("UsernsMode") != "host"
        or host_config.get("Privileged") is not False
        or host_config.get("ReadonlyRootfs") is not True
        or host_config.get("CapDrop") != ["ALL"]
        or bool(host_config.get("CapAdd"))
        or bool(host_config.get("Devices"))
        or bool(host_config.get("DeviceRequests"))
        or host_config.get("AutoRemove") is not False
        or "no-new-privileges:true" not in (host_config.get("SecurityOpt") or [])
        or not isinstance(host_config.get("PortBindings"), (dict, type(None)))
        or bool(host_config.get("PortBindings"))
    ):
        raise WorkloadOciHostError("OCI container isolation settings differ from policy")
    restart = host_config.get("RestartPolicy")
    if not isinstance(restart, Mapping) or restart.get("Name") != "unless-stopped":
        raise WorkloadOciHostError("OCI container restart policy differs from policy")

    image = _inspect_image(spec, runner=runner)
    image_config = image.get("Config")
    base_environment = image_config.get("Env") if isinstance(image_config, Mapping) else None
    if not isinstance(base_environment, list):
        raise WorkloadOciHostError("OCI image environment metadata is invalid")
    expected_environment: dict[str, str] = {}
    for entry in base_environment:
        if not isinstance(entry, str) or "=" not in entry:
            raise WorkloadOciHostError("OCI image environment metadata is invalid")
        key, value = entry.split("=", 1)
        expected_environment[key] = value
    expected_environment.update(_container_environment(spec))
    raw_environment = config.get("Env")
    actual_environment: dict[str, str] = {}
    if isinstance(raw_environment, list):
        for entry in raw_environment:
            if isinstance(entry, str) and "=" in entry:
                key, value = entry.split("=", 1)
                if key in expected_environment:
                    actual_environment[key] = value
    if actual_environment != expected_environment:
        raise WorkloadOciHostError("OCI container Product environment differs from policy")

    mounts = raw.get("Mounts")
    expected_mounts = {
        str(spec.data_directory): ("/data/echo", True),
        str(spec.artifact_directory): ("/data/artifacts", True),
        str(spec.maintenance_socket_path): (str(CONTAINER_MAINTENANCE_SOCKET_PATH), False),
        str(token_path): (str(CONTAINER_TOKEN_PATH), False),
    }
    actual_mounts: dict[str, tuple[str, bool]] = {}
    if isinstance(mounts, list):
        for mount in mounts:
            if isinstance(mount, Mapping):
                source, destination, writable = (
                    mount.get("Source"),
                    mount.get("Destination"),
                    mount.get("RW"),
                )
                if (
                    isinstance(source, str)
                    and isinstance(destination, str)
                    and type(writable) is bool
                ):
                    actual_mounts[source] = (destination, writable)
    if actual_mounts != expected_mounts:
        raise WorkloadOciHostError("OCI container mount set differs from policy")
    if verify_staged_token:
        _verify_staged_token(spec, token_path)


def _unlink_private_environment(path: Path) -> None:
    """Remove only the root-owned transient environment file created by this helper."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise WorkloadOciHostError("OCI private environment file cannot be inspected") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != ROOT_OWNER_UID
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise WorkloadOciHostError("OCI private environment file permissions are unsafe")
    path.unlink()


def _remove_staged_credentials(spec: OciHostSpec) -> None:
    """Delete only the staged token and empty digest-scoped helper directory."""

    token_path = _staged_token_path(spec)
    _assert_no_symlink_components(token_path)
    try:
        info = token_path.lstat()
    except FileNotFoundError:
        try:
            token_path.parent.rmdir()
        except OSError:
            pass
        return
    except OSError as error:
        raise WorkloadOciHostError(
            "Staged Echo ActivitySource token cannot be inspected"
        ) from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != spec.host_uid
        or info.st_gid != spec.host_gid
        or stat.S_IMODE(info.st_mode) != 0o400
        or info.st_nlink != 1
    ):
        raise WorkloadOciHostError("Staged Echo ActivitySource token permissions are unsafe")
    token_path.unlink()
    try:
        token_path.parent.rmdir()
    except OSError as error:
        raise WorkloadOciHostError("Staged Echo credential directory is not empty") from error


def _verify_staged_token(spec: OciHostSpec, token_path: Path) -> None:
    """Verify the bind-mounted ActivitySource credential still matches the catalog."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(token_path, flags)
    except OSError as error:
        raise WorkloadOciHostError("Staged Echo ActivitySource token is unavailable") from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != spec.host_uid
            or info.st_gid != spec.host_gid
            or stat.S_IMODE(info.st_mode) != 0o400
            or info.st_nlink != 1
        ):
            raise WorkloadOciHostError("Staged Echo ActivitySource token permissions are unsafe")
        token = os.read(descriptor, 16385)
    finally:
        os.close(descriptor)
    if (
        not token
        or len(token) > 16384
        or hashlib.sha256(token).hexdigest() != spec.activity_token_digest
    ):
        raise WorkloadOciHostError("Staged Echo ActivitySource token differs from the catalog")
