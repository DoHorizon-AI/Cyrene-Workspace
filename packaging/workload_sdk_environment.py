"""Prepare the verified Runtime Maintenance SDK for Workspace operators.

The helper installs one attested SDK wheel into an immutable private venv and
atomically selects that release for the existing updater.
中文：为 Workspace updater 安装已验证的运行维护 SDK，并切换到版本化环境。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

WORKLOAD_SDK_COMPONENT_ID = "cyrene-runtime-maintenance-sdk"
WORKLOAD_SDK_ARTIFACT_KIND = "python-bundle"
WORKLOAD_SDK_TARGET_ID = "linux-ubuntu-24.04-x86_64-python-3.12-library"
WORKLOAD_SDK_DISTRIBUTION = "cyrene-runtime-maintenance"
WORKLOAD_SDK_WHEEL_NAME = "cyrene_runtime_maintenance-0.1.0-py3-none-any.whl"
WORKLOAD_OPERATOR_ROOT = Path("/opt/cyrene/workload-operator")
WORKLOAD_OPERATOR_PYTHON = Path("/opt/cyrene/python/3.12.14/bin/python3.12")
INSTALL_RECEIPT_NAME = ".workload-sdk-install.json"
SHA256_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
SOURCE_COMMIT_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
INSTALL_STABLE_FIELDS = (
    "schemaVersion",
    "componentId",
    "artifactKind",
    "version",
    "targetId",
    "releaseId",
    "manifestUri",
    "manifestDigest",
    "manifestAssetDigest",
    "digest",
    "artifactDigest",
    "releaseIdentity",
    "releaseTag",
    "indexIdentity",
    "publisherIdentity",
    "attestationRef",
    "wheelDigest",
    "releasePath",
    "pointerIdentity",
    "verification",
)

CommandRunner = Callable[[Sequence[str], Mapping[str, str]], subprocess.CompletedProcess[str]]


class WorkloadSdkEnvironmentError(RuntimeError):
    """Raised when SDK identity, private runtime, or activation is unsafe."""


def _require_text(value: Any, label: str) -> str:
    """Return one non-empty, NUL-free text field."""

    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise WorkloadSdkEnvironmentError(f"{label} must be non-empty text")
    return value


def _require_digest(value: Any, label: str) -> str:
    """Require and return the canonical SHA-256 representation."""

    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise WorkloadSdkEnvironmentError(f"{label} must be a sha256:<lowercase hex> digest")
    return value


def _require_absolute_path(value: Any, label: str) -> Path:
    """Parse a normalized absolute path without resolving filesystem links."""

    if not isinstance(value, (str, os.PathLike)):
        raise WorkloadSdkEnvironmentError(f"{label} must be an absolute path")
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise WorkloadSdkEnvironmentError(f"{label} must be an absolute path")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise WorkloadSdkEnvironmentError(f"{label} must be a normalized absolute path")
    return path


def _require_root_owned(info: os.stat_result, label: str) -> None:
    """Require ownership by the root account and group."""

    if info.st_uid != 0 or info.st_gid != 0:
        raise WorkloadSdkEnvironmentError(f"{label} must be owned by root")


def _lstat_without_links(path: Path, label: str) -> os.stat_result:
    """Walk an absolute path with lstat and reject symlinked components."""

    current = Path(path.anchor)
    parts = path.parts[1:]
    for index, part in enumerate(parts):
        current /= part
        try:
            info = current.lstat()
        except OSError as error:
            raise WorkloadSdkEnvironmentError(f"{label} is unavailable: {current}") from error
        if stat.S_ISLNK(info.st_mode):
            raise WorkloadSdkEnvironmentError(f"{label} contains a symbolic link: {current}")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise WorkloadSdkEnvironmentError(f"{label} has a non-directory path component")
    if not parts:
        return current.lstat()
    return info


def _require_root_directory(path: Path, label: str, *, exact_mode: int | None = 0o755) -> None:
    """Require one non-writable root-owned deployment directory."""

    info = _lstat_without_links(path, label)
    _require_root_owned(info, label)
    mode = stat.S_IMODE(info.st_mode)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_nlink < 2
        or mode & 0o022
        or info.st_mode & 0o500 != 0o500
        or (exact_mode is not None and mode != exact_mode)
    ):
        raise WorkloadSdkEnvironmentError(f"{label} directory metadata is unsafe")


def _require_root_directory_chain(path: Path, label: str) -> None:
    """Require a root-owned path chain with no writable non-sticky directory."""

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = _lstat_without_links(current, label)
        _require_root_owned(info, label)
        mode = stat.S_IMODE(info.st_mode)
        if not stat.S_ISDIR(info.st_mode) or (mode & 0o022 and not mode & stat.S_ISVTX):
            raise WorkloadSdkEnvironmentError(f"{label} path chain is writable or unsafe")


def _require_root_file(path: Path, label: str, *, executable: bool = False) -> os.stat_result:
    """Require a root-owned regular file with no hard-link aliases."""

    info = _lstat_without_links(path, label)
    _require_root_owned(info, label)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or not info.st_mode & 0o400
        or stat.S_IMODE(info.st_mode) & 0o022
        or (executable and not info.st_mode & 0o100)
    ):
        raise WorkloadSdkEnvironmentError(f"{label} file metadata is unsafe")
    return info


def _read_verified_wheel(path: Path, expected_digest: str) -> None:
    """Recheck the staged wheel digest through a no-follow file descriptor."""

    before = _require_root_file(path, "SDK wheel")
    descriptor: int | None = None
    digest = hashlib.sha256()
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        _require_root_owned(opened, "SDK wheel")
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            raise WorkloadSdkEnvironmentError("SDK wheel changed while it was opened")
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
        after = os.fstat(descriptor)
        if (
            after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
        ):
            raise WorkloadSdkEnvironmentError("SDK wheel changed while it was hashed")
    except OSError as error:
        raise WorkloadSdkEnvironmentError("SDK wheel cannot be read safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    actual_digest = "sha256:" + digest.hexdigest()
    if actual_digest != expected_digest:
        raise WorkloadSdkEnvironmentError("SDK wheel SHA-256 does not match the verified row")


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    """Require one input mapping from the already-verified assembly handoff."""

    if not isinstance(value, Mapping):
        raise WorkloadSdkEnvironmentError(f"{label} must be an object")
    return value


def _copy_json_mapping(value: Any, label: str) -> dict[str, Any]:
    """Copy a mapping through strict JSON encoding for durable identity fields."""

    mapping = _require_mapping(value, label)
    try:
        copied = json.loads(json.dumps(dict(mapping), allow_nan=False))
    except (TypeError, ValueError) as error:
        raise WorkloadSdkEnvironmentError(f"{label} is not a JSON object") from error
    if not isinstance(copied, dict):
        raise WorkloadSdkEnvironmentError(f"{label} is not a JSON object")
    return copied


def _https_uri(value: Any, label: str) -> str:
    """Require an HTTPS URI with a host and a non-empty final path segment."""

    uri = _require_text(value, label)
    parsed = urlsplit(uri)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.path.rsplit("/", 1)[-1]:
        raise WorkloadSdkEnvironmentError(f"{label} must be an HTTPS asset URI")
    return uri


def _validate_component_identity(component: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the selected SDK row's complete, already-verified provenance identity."""

    if component.get("componentId") != WORKLOAD_SDK_COMPONENT_ID:
        raise WorkloadSdkEnvironmentError("selected component is not the Runtime Maintenance SDK")
    if component.get("artifactKind") != WORKLOAD_SDK_ARTIFACT_KIND:
        raise WorkloadSdkEnvironmentError("Runtime Maintenance SDK must be a python-bundle")
    if component.get("targetId") != WORKLOAD_SDK_TARGET_ID:
        raise WorkloadSdkEnvironmentError("Runtime Maintenance SDK target is unsupported")

    version = _require_text(component.get("version"), "component.version")
    if VERSION_PATTERN.fullmatch(version) is None:
        raise WorkloadSdkEnvironmentError("component.version is not a safe release version")
    digest = _require_digest(component.get("digest"), "component.digest")
    manifest_digest = _require_digest(component.get("manifestDigest"), "component.manifestDigest")
    manifest_asset_digest = _require_digest(
        component.get("manifestAssetDigest"), "component.manifestAssetDigest"
    )
    release_id = _require_text(component.get("releaseId"), "component.releaseId")
    manifest_uri = _https_uri(component.get("manifestUri"), "component.manifestUri")

    publisher_identity = _copy_json_mapping(
        component.get("publisherIdentity"), "component.publisherIdentity"
    )
    for field in ("id", "repository", "workflow", "tagFormat"):
        _require_text(publisher_identity.get(field), f"publisherIdentity.{field}")
    index_identity = _copy_json_mapping(component.get("indexIdentity"), "component.indexIdentity")
    for field in ("repository", "assetName", "channel"):
        _require_text(index_identity.get(field), f"indexIdentity.{field}")
    for field in ("assetDigest", "indexDigest"):
        _require_digest(index_identity.get(field), f"indexIdentity.{field}")
    index_asset_uri = _https_uri(index_identity.get("assetUri"), "indexIdentity.assetUri")
    release_tag = _require_text(index_identity.get("releaseTag"), "indexIdentity.releaseTag")
    if (
        index_identity.get("publisherIdentity") != publisher_identity
        or index_identity.get("repository") != publisher_identity["repository"]
        or urlsplit(index_asset_uri).path.rsplit("/", 1)[-1] != index_identity["assetName"]
        or release_id != release_tag
    ):
        raise WorkloadSdkEnvironmentError("SDK release, index, and publisher identities disagree")

    attestation_ref = _copy_json_mapping(
        component.get("attestationRef"), "component.attestationRef"
    )
    for field in ("repository", "workflow", "sourceCommit", "subjectName"):
        _require_text(attestation_ref.get(field), f"attestationRef.{field}")
    _require_digest(attestation_ref.get("subjectDigest"), "attestationRef.subjectDigest")
    manifest_name = urlsplit(manifest_uri).path.rsplit("/", 1)[-1]
    if (
        attestation_ref.get("repository") != publisher_identity["repository"]
        or attestation_ref.get("workflow") != publisher_identity["workflow"]
        or SOURCE_COMMIT_PATTERN.fullmatch(str(attestation_ref.get("sourceCommit"))) is None
        or attestation_ref.get("subjectName") != manifest_name
        or attestation_ref.get("subjectDigest") != manifest_asset_digest
    ):
        raise WorkloadSdkEnvironmentError("SDK manifest attestation identity is inconsistent")

    return {
        "componentId": WORKLOAD_SDK_COMPONENT_ID,
        "artifactKind": WORKLOAD_SDK_ARTIFACT_KIND,
        "version": version,
        "targetId": WORKLOAD_SDK_TARGET_ID,
        "releaseId": release_id,
        "manifestUri": manifest_uri,
        "manifestDigest": manifest_digest,
        "manifestAssetDigest": manifest_asset_digest,
        "digest": digest,
        "artifactDigest": digest,
        "releaseIdentity": manifest_digest,
        "releaseTag": release_tag,
        "indexIdentity": index_identity,
        "publisherIdentity": publisher_identity,
        "attestationRef": attestation_ref,
    }


def _validate_candidate(
    component: Mapping[str, Any], staged_identity: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate fixed SDK selection and assemble exact source identity fields."""

    selected_identity = _validate_component_identity(component)
    verification = _copy_json_mapping(component.get("verification"), "component.verification")
    if set(verification) != {"identityAttested"} or verification["identityAttested"] is not True:
        raise WorkloadSdkEnvironmentError(
            "selected SDK component lacks verified release identity evidence"
        )
    selected_identity["verification"] = verification

    archive_path = _require_absolute_path(
        staged_identity.get("archivePath"), "stagedIdentity.archivePath"
    )
    bundle_path = _require_absolute_path(
        staged_identity.get("bundlePath"), "stagedIdentity.bundlePath"
    )
    wheel_path = _require_absolute_path(
        staged_identity.get("wheelPath"), "stagedIdentity.wheelPath"
    )
    wheel_digest = _require_digest(staged_identity.get("wheelDigest"), "wheelDigest")
    plan_id = _require_text(staged_identity.get("planId"), "planId")
    plan_digest = _require_digest(staged_identity.get("planDigest"), "planDigest")
    if wheel_path.parent != bundle_path or wheel_path.name != WORKLOAD_SDK_WHEEL_NAME:
        raise WorkloadSdkEnvironmentError("staged wheel path is not the exact SDK bundle wheel")

    return {
        **selected_identity,
        "wheelDigest": wheel_digest,
        "archivePath": archive_path,
        "bundlePath": bundle_path,
        "wheelPath": wheel_path,
        "planId": plan_id,
        "planDigest": plan_digest,
    }


def _source_identity(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve the exact selected row and stage source in the updater journal."""

    return {
        "archivePath": str(candidate["archivePath"]),
        "bundlePath": str(candidate["bundlePath"]),
        "wheelPath": str(candidate["wheelPath"]),
        "wheelDigest": candidate["wheelDigest"],
        "artifactDigest": candidate["digest"],
        "manifestDigest": candidate["manifestDigest"],
        "manifestAssetDigest": candidate["manifestAssetDigest"],
        "releaseId": candidate["releaseId"],
        "releaseTag": candidate["releaseTag"],
        "indexIdentity": candidate["indexIdentity"],
        "publisherIdentity": candidate["publisherIdentity"],
        "attestationRef": candidate["attestationRef"],
        "planId": candidate["planId"],
        "planDigest": candidate["planDigest"],
    }


def _install_identity(
    candidate: Mapping[str, Any], release_path: Path, source_identity: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the root-owned v1 receipt with the exact selected release provenance."""

    return {
        "schemaVersion": 1,
        **{
            field: candidate[field]
            for field in (
                "componentId",
                "artifactKind",
                "version",
                "targetId",
                "releaseId",
                "manifestUri",
                "manifestDigest",
                "manifestAssetDigest",
                "digest",
                "artifactDigest",
                "releaseIdentity",
                "releaseTag",
                "indexIdentity",
                "publisherIdentity",
                "attestationRef",
                "wheelDigest",
            )
        },
        "releasePath": str(release_path),
        "archivePath": str(candidate["archivePath"]),
        "pointerIdentity": release_path.name,
        "verification": dict(candidate["verification"]),
        "sourceIdentity": dict(source_identity),
    }


def _safe_process_environment() -> dict[str, str]:
    """Keep pip and Python isolated from user configuration and package paths."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PIP_") and key not in {"PYTHONHOME", "PYTHONPATH"}
    }
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PIP_NO_INPUT": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    return environment


def _run_command(
    argv: Sequence[str], environment: Mapping[str, str], runner: CommandRunner | None
) -> str:
    """Run one fixed-argument command and return stdout with bounded errors."""

    try:
        if runner is None:
            result = subprocess.run(
                list(argv),
                check=True,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=dict(environment),
                cwd="/",
            )
        else:
            result = runner(argv, environment)
    except subprocess.CalledProcessError as error:
        stderr = error.stderr if isinstance(error.stderr, str) else ""
        detail = stderr.strip()[:400]
        suffix = f": {detail}" if detail else ""
        raise WorkloadSdkEnvironmentError(f"SDK environment command failed{suffix}") from error
    except OSError as error:
        raise WorkloadSdkEnvironmentError("SDK environment command could not run") from error
    return result.stdout


def _read_probe_json(output: str, label: str) -> dict[str, Any]:
    """Decode a small JSON identity returned by a private interpreter probe."""

    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise WorkloadSdkEnvironmentError(f"{label} returned invalid JSON") from error
    if not isinstance(value, dict):
        raise WorkloadSdkEnvironmentError(f"{label} returned an invalid identity")
    return value


def _verify_private_python(python_executable: Path, runner: CommandRunner | None) -> dict[str, Any]:
    """Verify that the fixed root-owned interpreter is the expected CPython."""

    _require_root_directory_chain(python_executable.parent, "private CPython path")
    _require_root_file(python_executable, "private CPython", executable=True)
    environment = _safe_process_environment()
    script = (
        "import json, os, sys; "
        "print(json.dumps({'implementation': sys.implementation.name, "
        "'version': '.'.join(str(item) for item in sys.version_info[:3]), "
        "'executable': os.path.realpath(sys.executable)}))"
    )
    identity = _read_probe_json(
        _run_command([str(python_executable), "-I", "-c", script], environment, runner),
        "private CPython probe",
    )
    if (
        identity.get("implementation") != "cpython"
        or identity.get("version") != "3.12.14"
        or identity.get("executable") != str(python_executable)
    ):
        raise WorkloadSdkEnvironmentError("private interpreter is not the locked CPython 3.12.14")
    return environment


def _probe_sdk(
    python_path: Path,
    expected_version: str,
    environment: Mapping[str, str],
    runner: CommandRunner | None,
) -> None:
    """Check the installed SDK version and updater-facing client methods."""

    script = (
        "import importlib.metadata as metadata, json; "
        "from cyrene_runtime_maintenance import PackageRuntimeClient; "
        "required = ('from_source_secret', 'authority', 'runtime_status'); "
        "print(json.dumps({'version': metadata.version('cyrene-runtime-maintenance'), "
        "'methods': {name: callable(getattr(PackageRuntimeClient, name, None)) "
        "for name in required}}))"
    )
    identity = _read_probe_json(
        _run_command([str(python_path), "-I", "-c", script], environment, runner),
        "Runtime Maintenance SDK import probe",
    )
    methods = identity.get("methods")
    if identity.get("version") != expected_version or not isinstance(methods, dict):
        raise WorkloadSdkEnvironmentError("installed Runtime Maintenance SDK version is incorrect")
    if any(
        methods.get(name) is not True
        for name in ("from_source_secret", "authority", "runtime_status")
    ):
        raise WorkloadSdkEnvironmentError("installed Runtime Maintenance SDK API is incomplete")


def _require_venv_python(release_path: Path) -> Path:
    """Require the installed venv and interpreter to remain root-owned."""

    venv_path = release_path / "venv"
    _require_root_directory(venv_path, "SDK venv", exact_mode=None)
    _require_root_directory(venv_path / "bin", "SDK venv bin", exact_mode=None)
    python_path = venv_path / "bin" / "python"
    _require_root_file(python_path, "SDK venv Python", executable=True)
    return python_path


def _fsync_directory(path: Path) -> None:
    """Persist one directory entry update."""

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
    """Write one root-created receipt without exposing a partial JSON file."""

    descriptor: int | None = None
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(path.parent)
    except OSError as error:
        raise WorkloadSdkEnvironmentError(
            "SDK install identity receipt could not be written"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _ensure_deployment_root(root: Path) -> Path:
    """Create and validate the fixed root-owned updater release directories."""

    if not root.is_absolute() or ".." in root.parts:
        raise WorkloadSdkEnvironmentError(
            "workload operator root must be a normalized absolute path"
        )
    try:
        releases = root / "releases"
        for path in (root, releases):
            missing: list[Path] = []
            current = path
            while not current.exists() and not current.is_symlink():
                missing.append(current)
                current = current.parent
            for directory in reversed(missing):
                directory.mkdir(mode=0o755)
                os.chmod(directory, 0o755)
    except OSError as error:
        raise WorkloadSdkEnvironmentError(
            "workload operator release directories are unavailable"
        ) from error
    _require_root_directory_chain(root.parent, "workload operator parent path")
    _require_root_directory(root, "workload operator root")
    _require_root_directory(releases, "workload operator releases")
    return releases


def _existing_root_owned_directory_chain(path: Path, label: str) -> bool:
    """Validate an existing root-owned directory chain without creating anything."""

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise WorkloadSdkEnvironmentError(f"{label} cannot be inspected") from error
        if stat.S_ISLNK(info.st_mode):
            raise WorkloadSdkEnvironmentError(f"{label} contains a symbolic link: {current}")
        _require_root_owned(info, label)
        mode = stat.S_IMODE(info.st_mode)
        if not stat.S_ISDIR(info.st_mode) or (mode & 0o022 and not mode & stat.S_ISVTX):
            raise WorkloadSdkEnvironmentError(f"{label} path chain is writable or unsafe")
    return True


def _release_path(releases_root: Path, candidate: Mapping[str, Any]) -> Path:
    """Build the immutable path from the version and full manifest digest."""

    manifest_hex = str(candidate["manifestDigest"]).removeprefix("sha256:")
    return releases_root / f"{candidate['version']}--{manifest_hex}"


def _validate_source_identity(
    source_identity: Any, selected_identity: Mapping[str, Any], archive_path: str, wheel_digest: str
) -> dict[str, Any]:
    """Validate persisted staging provenance without requiring temporary stage files to remain."""

    source = _copy_json_mapping(source_identity, "SDK receipt sourceIdentity")
    required = {
        "archivePath",
        "bundlePath",
        "wheelPath",
        "wheelDigest",
        "artifactDigest",
        "manifestDigest",
        "manifestAssetDigest",
        "releaseId",
        "releaseTag",
        "indexIdentity",
        "publisherIdentity",
        "attestationRef",
        "planId",
        "planDigest",
    }
    if set(source) != required:
        raise WorkloadSdkEnvironmentError("SDK receipt sourceIdentity has an invalid field set")
    source_archive = _require_absolute_path(source.get("archivePath"), "sourceIdentity.archivePath")
    bundle_path = _require_absolute_path(source.get("bundlePath"), "sourceIdentity.bundlePath")
    wheel_path = _require_absolute_path(source.get("wheelPath"), "sourceIdentity.wheelPath")
    source_wheel_digest = _require_digest(source.get("wheelDigest"), "sourceIdentity.wheelDigest")
    _require_text(source.get("planId"), "sourceIdentity.planId")
    _require_digest(source.get("planDigest"), "sourceIdentity.planDigest")
    if (
        str(source_archive) != archive_path
        or source_wheel_digest != wheel_digest
        or wheel_path.parent != bundle_path
        or wheel_path.name != WORKLOAD_SDK_WHEEL_NAME
    ):
        raise WorkloadSdkEnvironmentError("SDK receipt staging paths or wheel identity disagree")
    expected = {
        "artifactDigest": selected_identity["digest"],
        "manifestDigest": selected_identity["manifestDigest"],
        "manifestAssetDigest": selected_identity["manifestAssetDigest"],
        "releaseId": selected_identity["releaseId"],
        "releaseTag": selected_identity["releaseTag"],
        "indexIdentity": selected_identity["indexIdentity"],
        "publisherIdentity": selected_identity["publisherIdentity"],
        "attestationRef": selected_identity["attestationRef"],
    }
    if any(source.get(field) != value for field, value in expected.items()):
        raise WorkloadSdkEnvironmentError(
            "SDK receipt sourceIdentity differs from release identity"
        )
    return source


def _validate_install_receipt(receipt: Any, path: Path) -> dict[str, Any]:
    """Validate all immutable release and provenance fields in the root-owned receipt."""

    value = _copy_json_mapping(receipt, "SDK install receipt")
    expected_fields = set(INSTALL_STABLE_FIELDS) | {"archivePath", "sourceIdentity"}
    if set(value) != expected_fields:
        raise WorkloadSdkEnvironmentError("SDK install receipt has an invalid field set")
    if type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1:
        raise WorkloadSdkEnvironmentError("SDK install receipt schema version is unsupported")

    selected = _validate_component_identity(value)
    if any(value.get(field) != expected for field, expected in selected.items()):
        raise WorkloadSdkEnvironmentError("SDK install receipt identity fields disagree")
    wheel_digest = _require_digest(value.get("wheelDigest"), "receipt.wheelDigest")
    declared_release_path = _require_absolute_path(value.get("releasePath"), "receipt.releasePath")
    archive_path = _require_absolute_path(value.get("archivePath"), "receipt.archivePath")
    if (
        declared_release_path != path
        or path.name != _release_path(path.parent, selected).name
        or value.get("pointerIdentity") != path.name
        or str(archive_path) != value.get("archivePath")
    ):
        raise WorkloadSdkEnvironmentError("SDK install receipt release paths are inconsistent")
    verification = value.get("verification")
    if (
        not isinstance(verification, dict)
        or set(verification) != {"identityAttested"}
        or type(verification.get("identityAttested")) is not bool
    ):
        raise WorkloadSdkEnvironmentError("SDK install receipt verification field is invalid")
    value["sourceIdentity"] = _validate_source_identity(
        value.get("sourceIdentity"), selected, str(archive_path), wheel_digest
    )
    return value


def _read_receipt_file(path: Path) -> dict[str, Any]:
    """Read one bounded root-owned receipt without following a final symlink."""

    info = _require_root_file(path, "SDK install receipt")
    if info.st_size < 1 or info.st_size > 32 * 1024:
        raise WorkloadSdkEnvironmentError("SDK install receipt has an invalid size")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        _require_root_owned(opened, "SDK install receipt")
        if opened.st_dev != info.st_dev or opened.st_ino != info.st_ino:
            raise WorkloadSdkEnvironmentError("SDK install receipt changed while it was opened")
        content = bytearray()
        while block := os.read(descriptor, 4096):
            content.extend(block)
            if len(content) > 32 * 1024:
                raise WorkloadSdkEnvironmentError("SDK install receipt has an invalid size")
    except OSError as error:
        raise WorkloadSdkEnvironmentError("SDK install receipt cannot be read safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        receipt = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkloadSdkEnvironmentError("SDK install receipt is malformed") from error
    if not isinstance(receipt, dict):
        raise WorkloadSdkEnvironmentError("SDK install receipt must be an object")
    return receipt


def _read_install_receipt(path: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    """Require an existing version directory to match the exact installed SDK."""

    _require_root_directory(path, "SDK release")
    receipt = _validate_install_receipt(_read_receipt_file(path / INSTALL_RECEIPT_NAME), path)
    if any(receipt.get(field) != expected.get(field) for field in INSTALL_STABLE_FIELDS):
        raise WorkloadSdkEnvironmentError("SDK release path is already bound to another identity")
    return receipt


def read_workload_sdk_environment() -> dict[str, Any] | None:
    """Read the active installed SDK identity from its immutable root-owned receipt.

    Returns the actual current release identity for resolver inventory. It does
    not compare against a candidate or recover identity fields from an updater
    journal.
    """

    if os.geteuid() != 0:
        raise PermissionError("workload SDK environment inspection requires root")
    root = WORKLOAD_OPERATOR_ROOT
    if not _existing_root_owned_directory_chain(root, "workload operator path"):
        return None
    _require_root_directory(root, "workload operator root")
    current = root / "current"
    try:
        current_info = current.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise WorkloadSdkEnvironmentError(
            "workload operator current pointer cannot be inspected"
        ) from error
    if not stat.S_ISLNK(current_info.st_mode):
        raise WorkloadSdkEnvironmentError("workload operator current pointer must be a symlink")
    _require_root_owned(current_info, "workload operator current pointer")

    releases_root = root / "releases"
    _require_root_directory(releases_root, "workload operator releases")
    target = _current_pointer_target(current, releases_root)
    if target is None:
        return None
    relative_target = Path(target)
    if (
        relative_target.is_absolute()
        or len(relative_target.parts) != 2
        or relative_target.parts[0] != "releases"
        or relative_target.parts[1] in {"", ".", ".."}
    ):
        raise WorkloadSdkEnvironmentError("workload operator current pointer is not canonical")
    release_path = root / relative_target
    if target != str(Path("releases") / release_path.name):
        raise WorkloadSdkEnvironmentError("workload operator current pointer is not canonical")

    receipt = _validate_install_receipt(
        _read_receipt_file(release_path / INSTALL_RECEIPT_NAME), release_path
    )
    python_path = _require_venv_python(release_path)
    _require_root_file(release_path / "venv" / "pyvenv.cfg", "SDK venv configuration")
    identity_attested = receipt["verification"]["identityAttested"]
    return {
        **receipt,
        "installed": True,
        "active": True,
        "identityAttested": identity_attested,
        "verification": {"identityAttested": identity_attested},
        "pythonPath": str(python_path),
        "receiptPath": str(release_path / INSTALL_RECEIPT_NAME),
    }


def _current_pointer_target(current: Path, releases_root: Path) -> str | None:
    """Validate and return the current symlink's literal target, if present."""

    try:
        info = current.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise WorkloadSdkEnvironmentError(
            "workload operator current pointer cannot be inspected"
        ) from error
    if not stat.S_ISLNK(info.st_mode):
        raise WorkloadSdkEnvironmentError("workload operator current pointer must be a symlink")
    _require_root_owned(info, "workload operator current pointer")
    target = os.readlink(current)
    target_path = Path(target)
    resolved_target = target_path if target_path.is_absolute() else current.parent / target_path
    if ".." in target_path.parts or not resolved_target.is_absolute():
        raise WorkloadSdkEnvironmentError("workload operator current pointer target is unsafe")
    normalized_target = Path(os.path.abspath(resolved_target))
    try:
        normalized_target.relative_to(releases_root)
    except ValueError as error:
        raise WorkloadSdkEnvironmentError(
            "workload operator current pointer escaped releases"
        ) from error
    _require_root_directory(normalized_target, "current SDK release")
    return target


def _replace_current_link(source: Path, destination: Path) -> None:
    """Atomically publish a staged current symlink."""

    os.replace(source, destination)


def _restore_current_pointer(current: Path, old_target: str | None) -> None:
    """Restore the previous symlink target after a failed activation sync."""

    if old_target is None:
        current.unlink(missing_ok=True)
        _fsync_directory(current.parent)
        return
    temporary = current.parent / f".current-restore-{uuid.uuid4().hex}"
    try:
        os.symlink(old_target, temporary)
        _replace_current_link(temporary, current)
        _fsync_directory(current.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _activate_current(current: Path, releases_root: Path, release_path: Path) -> None:
    """Atomically select one verified SDK release and restore the old pointer on error."""

    old_target = _current_pointer_target(current, releases_root)
    desired_target = os.path.relpath(release_path, current.parent)
    if old_target == desired_target:
        return
    temporary = current.parent / f".current-{uuid.uuid4().hex}"
    try:
        os.symlink(desired_target, temporary)
        _replace_current_link(temporary, current)
        _fsync_directory(current.parent)
    except OSError as error:
        try:
            _restore_current_pointer(current, old_target)
        except OSError as restore_error:
            raise WorkloadSdkEnvironmentError(
                "SDK activation failed and the previous current pointer could not be restored"
            ) from restore_error
        raise WorkloadSdkEnvironmentError(
            "SDK current pointer activation failed; previous version restored"
        ) from error
    finally:
        temporary.unlink(missing_ok=True)


def _install_new_release(
    release_path: Path,
    releases_root: Path,
    wheel_path: Path,
    candidate: Mapping[str, Any],
    identity: Mapping[str, Any],
    environment: Mapping[str, str],
    runner: CommandRunner | None,
) -> None:
    """Build, install, probe, and atomically publish one immutable venv tree."""

    created_release = False
    completed = False
    try:
        release_path.mkdir(mode=0o755)
        os.chmod(release_path, 0o755)
        created_release = True
        venv_path = release_path / "venv"
        python_path = venv_path / "bin" / "python"
        _run_command(
            [
                str(WORKLOAD_OPERATOR_PYTHON),
                "-I",
                "-m",
                "venv",
                "--copies",
                str(venv_path),
            ],
            environment,
            runner,
        )
        _require_venv_python(release_path)
        _run_command(
            [
                str(python_path),
                "-I",
                "-m",
                "pip",
                "install",
                "--isolated",
                "--no-deps",
                "--no-index",
                "--no-cache-dir",
                "--disable-pip-version-check",
                "--no-input",
                str(wheel_path),
            ],
            environment,
            runner,
        )
        _probe_sdk(python_path, str(candidate["version"]), environment, runner)
        receipt = (json.dumps(dict(identity), sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        _atomic_write(release_path / INSTALL_RECEIPT_NAME, receipt, mode=0o444)
        _fsync_directory(release_path)
        _fsync_directory(releases_root)
        completed = True
    except OSError as error:
        raise WorkloadSdkEnvironmentError("SDK venv could not be prepared safely") from error
    finally:
        if created_release and not completed:
            try:
                shutil.rmtree(release_path)
                _fsync_directory(releases_root)
            except OSError as error:
                raise WorkloadSdkEnvironmentError(
                    "partial SDK release could not be removed"
                ) from error


def prepare_workload_sdk_environment(
    component: Mapping[str, Any],
    staged_identity: Mapping[str, Any],
    *,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """Install an exact SDK wheel and return a journal-ready source identity.

    The caller has already validated the catalog manifest and archive
    attestations. This function rechecks the root-owned stage and wheel SHA,
    installs only that wheel into the locked private CPython venv, and switches
    the operator's ``current`` symlink after the import probe succeeds.

    Args:
        component: Selected SDK catalog row.
        staged_identity: Root-owned archive, extracted bundle, wheel, and plan identity.
        runner: Optional command runner for deterministic tests.
    Returns:
        Exact installed paths, digests, and source identity for the existing updater journal.
    """

    if os.geteuid() != 0:
        raise PermissionError("workload SDK environment preparation requires root")
    component_row = _require_mapping(component, "component")
    staged_row = _require_mapping(staged_identity, "stagedIdentity")
    candidate = _validate_candidate(component_row, staged_row)

    archive_path = candidate["archivePath"]
    bundle_path = candidate["bundlePath"]
    wheel_path = candidate["wheelPath"]
    _require_root_file(archive_path, "SDK archive")
    _require_root_directory_chain(archive_path.parent, "SDK archive path")
    _require_root_directory_chain(bundle_path, "SDK bundle path")
    _require_root_directory(bundle_path, "SDK bundle", exact_mode=None)
    _read_verified_wheel(wheel_path, str(candidate["wheelDigest"]))

    releases_root = _ensure_deployment_root(WORKLOAD_OPERATOR_ROOT)
    release_path = _release_path(releases_root, candidate)
    declared_release_path = staged_row.get("releasePath")
    if (
        declared_release_path is not None
        and _require_absolute_path(declared_release_path, "stagedIdentity.releasePath")
        != release_path
    ):
        raise WorkloadSdkEnvironmentError("staged release path differs from the immutable SDK path")

    python_environment = _verify_private_python(WORKLOAD_OPERATOR_PYTHON, runner)
    source_identity = _source_identity(candidate)
    identity = _install_identity(candidate, release_path, source_identity)
    if release_path.exists() or release_path.is_symlink():
        _read_install_receipt(release_path, identity)
        python_path = _require_venv_python(release_path)
        _probe_sdk(python_path, str(candidate["version"]), python_environment, runner)
    else:
        _install_new_release(
            release_path,
            releases_root,
            wheel_path,
            candidate,
            identity,
            python_environment,
            runner,
        )

    current = WORKLOAD_OPERATOR_ROOT / "current"
    _activate_current(current, releases_root, release_path)
    return {
        **identity,
        "releasePath": str(release_path),
        "pythonPath": str(release_path / "venv" / "bin" / "python"),
        "sourceIdentity": source_identity,
    }
