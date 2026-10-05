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
import email.parser
import fnmatch
import grp
import hashlib
import json
import os
import platform
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile
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
WHEEL_TAG_PATTERN = re.compile(r"[A-Za-z0-9_]+-[A-Za-z0-9_]+-[A-Za-z0-9_]+\Z")
WHEEL_REQUIREMENT_PATTERN = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)\s*==\s*([A-Za-z0-9][A-Za-z0-9.+!_-]*)"
)
RUNTIME_SDK_COMPONENT_ID = "cyrene-runtime-maintenance-sdk"
RUNTIME_SDK_DISTRIBUTION = "cyrene-runtime-maintenance"
PRIVATE_PYTHON_EXECUTABLE = "/opt/cyrene/python/3.12.14/bin/python3.12"
PRIVATE_UV_EXECUTABLE = "/opt/cyrene/uv/0.12.21/uv"
PRIVATE_UV_VERSION = "0.12.21"
EXECUTION_RUNTIME_KEYS = {
    "schema_version",
    "runtime_id",
    "source_root",
    "project_file",
    "lock_file",
    "bootstrap_script",
    "probe_script",
    "protocol_files",
    "profile",
    "platform_profile",
    "python_executable",
    "python_version",
    "uv_executable",
    "uv_version",
    "runtime_home_relative_path",
    "runtime_manifest_file",
    "runtime_arguments",
    "api_only_arguments",
}
NATIVE_PYTHON_TARGET_FIELDS = (
    "os",
    "osVersion",
    "distribution",
    "distributionVersion",
    "architecture",
    "abi",
    "runtime",
)
INSTALLED_RELEASE_LOCK = Path("/usr/lib/cyrene/release-lock.json")
DEFAULT_RELEASE_LOCK = (
    INSTALLED_RELEASE_LOCK
    if INSTALLED_RELEASE_LOCK.is_file()
    else Path(__file__).resolve().parents[1] / "release-lock.json"
)
INSTALLED_PYTHON_RUNTIME_LOCK = Path("/usr/share/cyrene/python-runtime/python-runtime.lock.json")
DEFAULT_PYTHON_RUNTIME_LOCK = (
    INSTALLED_PYTHON_RUNTIME_LOCK
    if INSTALLED_PYTHON_RUNTIME_LOCK.is_file()
    else Path(__file__).resolve().parents[1] / "packaging" / "python-runtime.lock.json"
)


class ServiceBundleError(ValueError):
    """Raised when a service bundle is incomplete, unsafe, or inconsistent."""


def _native_python_profile(release_lock_path: Path, profile_id: str) -> dict[str, Any]:
    """Load one exact Python runtime and wheel compatibility profile from release-lock."""

    lock_path = Path(release_lock_path)
    if lock_path.is_symlink() or not lock_path.is_file():
        raise ServiceBundleError(f"release-lock.json is missing or unsafe: {lock_path}")
    lock = _read_json_object(lock_path, "Workspace release-lock.json")
    profiles = lock.get("nativePythonProfiles")
    profile = profiles.get(profile_id) if isinstance(profiles, dict) else None
    expected_keys = set(NATIVE_PYTHON_TARGET_FIELDS) | {
        "pythonVersion",
        "pythonExecutable",
        "pythonInput",
        "wheelResolver",
    }
    if not isinstance(profile, dict) or set(profile) != expected_keys:
        raise ServiceBundleError(f"release-lock.json has no complete Python profile {profile_id!r}")
    resolver = profile["wheelResolver"]
    if not isinstance(resolver, dict):
        raise ServiceBundleError(f"release-lock.json Python profile is invalid: {profile_id}")
    allowed = resolver.get("allowedWheelTags")
    if not isinstance(allowed, dict):
        raise ServiceBundleError(f"release-lock.json Python profile is invalid: {profile_id}")
    pep600 = allowed.get("pep600")
    if not isinstance(pep600, dict):
        raise ServiceBundleError(f"release-lock.json Python profile is invalid: {profile_id}")
    if (
        set(resolver) != {"tool", "version", "arguments", "allowedWheelTags"}
        or set(allowed) != {"purePython", "pep600"}
        or set(pep600) != {"architecture", "maxGlibc"}
        or profile.get("pythonVersion") != "3.12.14"
        or profile.get("pythonExecutable") != PRIVATE_PYTHON_EXECUTABLE
        or profile.get("pythonInput") != "packaging/python-runtime.lock.json"
        or profile.get("runtime") != "python:3.12"
        or profile.get("os") != "linux"
        or profile.get("distribution") != "ubuntu"
        or profile.get("osVersion") not in {"22.04", "24.04"}
        or profile.get("distributionVersion") != profile.get("osVersion")
        or profile.get("architecture") != "x86_64"
        or (profile.get("osVersion"), profile.get("abi"))
        not in {("22.04", "glibc-2.35"), ("24.04", "glibc-2.39")}
        or profile_id != f"linux-ubuntu-{profile.get('osVersion')}-x86_64-python-3.12"
        or resolver.get("tool") != "uv"
        or resolver.get("version") != "0.12.21"
        or resolver.get("arguments") != ["--python-platform", "x86_64-unknown-linux-gnu"]
        or allowed.get("purePython") != ["*-none-any"]
        or pep600.get("architecture") != profile.get("architecture")
        or pep600.get("maxGlibc") != profile["abi"].removeprefix("glibc-")
    ):
        raise ServiceBundleError(f"release-lock.json Python profile is invalid: {profile_id}")
    return dict(profile)


def _bundle_target(
    profile_id: str,
    profile: dict[str, Any],
    source_wheel_tags: dict[str, list[str]],
    installed_wheel_tags: list[str],
) -> dict[str, Any]:
    """Return the canonical inner-manifest target bound to the selected profile."""

    resolver = profile["wheelResolver"]
    allowed = resolver["allowedWheelTags"]
    return {
        "profile_id": profile_id,
        **{key: profile[key] for key in NATIVE_PYTHON_TARGET_FIELDS},
        "debian_arch": "amd64",
        "python": "3.12",
        "python_version": profile["pythonVersion"],
        "python_executable": profile["pythonExecutable"],
        "python_input": profile["pythonInput"],
        "wheel_resolver": {
            "tool": resolver["tool"],
            "version": resolver["version"],
            "arguments": resolver["arguments"],
        },
        "wheel_tags": {
            "policy": {
                "pure_python": allowed["purePython"],
                "pep600": allowed["pep600"],
            },
            "artifacts": source_wheel_tags,
            "installed": installed_wheel_tags,
        },
    }


def _require_profile_host(profile: dict[str, Any]) -> None:
    """Refuse profile builds unless OS, CPU architecture, and libc all match exactly."""

    try:
        os_release = platform.freedesktop_os_release()
    except OSError as error:
        raise ServiceBundleError(f"cannot determine service bundle build OS: {error}") from error
    machine = platform.machine().lower()
    libc_name, libc_version = platform.libc_ver()
    abi = f"{libc_name}-{libc_version}" if libc_name and libc_version else None
    if (
        not sys.platform.startswith("linux")
        or os_release.get("ID") != profile["distribution"]
        or os_release.get("VERSION_ID") != profile["distributionVersion"]
        or machine not in {"x86_64", "amd64"}
        or abi != profile["abi"]
    ):
        raise ServiceBundleError(
            "service bundle target profile does not match the build host: "
            f"expected {profile['distribution']} {profile['distributionVersion']} "
            f"x86_64/{profile['abi']}, got {os_release.get('ID')} "
            f"{os_release.get('VERSION_ID')} {machine}/{abi}"
        )


def _python_runtime_version(executable: Path) -> str:
    """Read a build interpreter's full CPython version without using ambient Python state."""

    path = Path(executable)
    if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        raise ServiceBundleError(f"build Python interpreter is missing or unsafe: {path}")
    try:
        result = subprocess.run(
            [
                str(path),
                "-I",
                "-c",
                (
                    "import platform,sys; "
                    "print(platform.python_implementation()); "
                    "print('.'.join(map(str, sys.version_info[:3]))); "
                    "print(platform.machine())"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ServiceBundleError(
            f"cannot execute build Python interpreter {path}: {error}"
        ) from error
    lines = result.stdout.strip().splitlines()
    if len(lines) != 3 or lines[0] != "CPython" or lines[2] not in {"x86_64", "amd64"}:
        raise ServiceBundleError(
            f"build Python must be x86_64 CPython, got {result.stdout.strip()!r}"
        )
    return lines[1]


def _validate_python_input(profile: dict[str, Any], release_lock_path: Path) -> None:
    """Match the profile's Python archive and resolver to their separate immutable lock."""

    relative = PurePosixPath(profile["pythonInput"])
    if (
        relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.as_posix() != profile["pythonInput"]
    ):
        raise ServiceBundleError("Python profile input must be a normalized relative lock path")
    current = Path(release_lock_path).resolve().parent
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ServiceBundleError(
                f"Python profile input lock path contains a symlink: {current}"
            )
    path = current
    if path.is_symlink() or not path.is_file():
        raise ServiceBundleError(f"Python profile input lock is missing or unsafe: {path}")
    lock = _read_json_object(path, "Python runtime input lock")
    python = lock.get("python")
    resolver = lock.get("buildResolver")
    archive = python.get("archive") if isinstance(python, dict) else None
    uv_archive = resolver.get("binaryArchive") if isinstance(resolver, dict) else None
    if (
        not isinstance(python, dict)
        or python.get("implementation") != "CPython"
        or python.get("version") != profile["pythonVersion"]
        or python.get("target") != "x86_64-unknown-linux-gnu"
        or python.get("executable") != profile["pythonExecutable"]
        or not isinstance(archive, dict)
        or not isinstance(archive.get("sha256"), str)
        or not SHA256_PATTERN.fullmatch(archive["sha256"])
        or type(archive.get("size")) is not int
        or archive["size"] < 1
        or not isinstance(resolver, dict)
        or resolver.get("tool") != profile["wheelResolver"]["tool"]
        or resolver.get("version") != profile["wheelResolver"]["version"]
        or not isinstance(uv_archive, dict)
        or not isinstance(uv_archive.get("sha256"), str)
        or not SHA256_PATTERN.fullmatch(uv_archive["sha256"])
        or not isinstance(uv_archive.get("executableSha256"), str)
        or not SHA256_PATTERN.fullmatch(uv_archive["executableSha256"])
        or type(uv_archive.get("size")) is not int
        or uv_archive["size"] < 1
    ):
        raise ServiceBundleError("Python archive/resolver does not match the selected profile")


def _relative_path(value: Any, label: str) -> PurePosixPath:
    """Require a normalized non-empty relative path from signed bundle metadata."""

    if not isinstance(value, str):
        raise ServiceBundleError(f"{label} must be a relative path string")
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ServiceBundleError(f"{label} must be a normalized relative path")
    return path


def _validate_execution_runtime(
    value: Any,
    *,
    root: Path,
    files: dict[str, str],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Validate Product-declared locked execution-runtime source and launch arguments."""

    if not isinstance(value, dict) or set(value) != EXECUTION_RUNTIME_KEYS:
        raise ServiceBundleError("execution_runtime must have the complete v1 descriptor shape")
    if (
        type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or not isinstance(value.get("runtime_id"), str)
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value["runtime_id"]) is None
        or value.get("source_root") != "execution-runtime"
        or not isinstance(value.get("profile"), str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", value["profile"]) is None
        or not isinstance(value.get("platform_profile"), str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", value["platform_profile"]) is None
        or value.get("python_executable") != profile["pythonExecutable"]
        or value.get("python_version") != profile["pythonVersion"]
        or value.get("uv_executable") != PRIVATE_UV_EXECUTABLE
        or value.get("uv_version") != profile["wheelResolver"]["version"]
        or value.get("uv_version") != PRIVATE_UV_VERSION
    ):
        raise ServiceBundleError(
            "execution_runtime profile or locked runtime tools are inconsistent"
        )

    source_root = _relative_path(value["source_root"], "execution_runtime.source_root")
    project_file = _relative_path(value["project_file"], "execution_runtime.project_file")
    lock_file = _relative_path(value["lock_file"], "execution_runtime.lock_file")
    bootstrap_script = _relative_path(
        value["bootstrap_script"], "execution_runtime.bootstrap_script"
    )
    probe_script = _relative_path(value["probe_script"], "execution_runtime.probe_script")
    _relative_path(value["runtime_manifest_file"], "execution_runtime.runtime_manifest_file")
    runtime_home_relative = _relative_path(
        value["runtime_home_relative_path"], "execution_runtime.runtime_home_relative_path"
    )
    if runtime_home_relative.parts[0] in {"services", "runtime", "system"}:
        raise ServiceBundleError("execution runtime home must be outside immutable/system releases")

    def payload_file(relative: PurePosixPath, label: str) -> Path:
        bundle_path = PurePosixPath(*source_root.parts, *relative.parts).as_posix()
        if bundle_path not in files:
            raise ServiceBundleError(f"execution_runtime.{label} is not a hashed bundle file")
        path = root.joinpath(*PurePosixPath(bundle_path).parts)
        if path.is_symlink() or not path.is_file():
            raise ServiceBundleError(f"execution_runtime.{label} is missing or unsafe: {path}")
        return path

    declared_payloads: set[str] = set()
    for label, path in (
        ("source declaration", PurePosixPath("execution-runtime.json")),
        ("project_file", project_file),
        ("lock_file", lock_file),
        ("bootstrap_script", bootstrap_script),
        ("probe_script", probe_script),
    ):
        resolved_payload = payload_file(path, label)
        declared_payloads.add(PurePosixPath(*source_root.parts, *path.parts).as_posix())
        if label == "source declaration":
            declaration = _read_json_object(
                resolved_payload, "Product execution-runtime.json declaration"
            )
            if declaration != value:
                raise ServiceBundleError(
                    "hashed Product execution-runtime.json differs from the manifest descriptor"
                )
    if project_file.parent != lock_file.parent:
        raise ServiceBundleError("execution runtime project and uv lock must share a directory")

    protocol_files = value.get("protocol_files")
    if (
        not isinstance(protocol_files, list)
        or not protocol_files
        or any(not isinstance(item, str) for item in protocol_files)
        or protocol_files != sorted(set(protocol_files))
    ):
        raise ServiceBundleError(
            "execution_runtime.protocol_files must be a sorted unique path list"
        )
    for item in protocol_files:
        protocol_path = _relative_path(item, "execution_runtime.protocol_files entry")
        payload_file(protocol_path, "protocol file")
        declared_payloads.add(PurePosixPath(*source_root.parts, *protocol_path.parts).as_posix())
    actual_runtime_payloads = {
        file_name for file_name in files if file_name.startswith("execution-runtime/")
    }
    if actual_runtime_payloads != declared_payloads:
        raise ServiceBundleError(
            "execution runtime payload must contain exactly its declared source files"
        )

    try:
        project = tomllib.loads(
            payload_file(project_file, "project_file").read_text(encoding="utf-8")
        )
        lock = tomllib.loads(payload_file(lock_file, "lock_file").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ServiceBundleError(
            f"execution runtime project or lock is invalid: {error}"
        ) from error
    project_table = project.get("project")
    requires_python = (
        project_table.get("requires-python") if isinstance(project_table, dict) else None
    )
    if (
        not isinstance(requires_python, str)
        or "3.12" not in requires_python
        or lock.get("requires-python") != requires_python
    ):
        raise ServiceBundleError(
            "execution runtime pyproject and uv.lock Python constraints differ"
        )

    runtime_arguments = value.get("runtime_arguments")
    api_only_arguments = value.get("api_only_arguments")

    def valid_argument_template(arguments: Any, placeholders: set[str]) -> bool:
        return (
            isinstance(arguments, list)
            and bool(arguments)
            and all(
                isinstance(item, str)
                and (
                    item in placeholders or re.fullmatch(r"--[a-z0-9][a-z0-9-]*", item) is not None
                )
                for item in arguments
            )
        )

    if (
        not valid_argument_template(
            runtime_arguments, {"{platform_runtime_config}", "{trainer_runtime_config}"}
        )
        or runtime_arguments.count("{platform_runtime_config}") != 1
        or runtime_arguments.count("{trainer_runtime_config}") != 1
        or not valid_argument_template(api_only_arguments, {"{artifact_root}"})
        or api_only_arguments.count("{artifact_root}") != 1
    ):
        raise ServiceBundleError("execution_runtime launch argument templates are invalid")
    return dict(value)


def _wheel_tag_is_compatible(tag: str, profile: dict[str, Any]) -> bool:
    """Accept pure-Python or PEP 600 x86_64 wheels within the profile's glibc ceiling."""

    parts = tag.split("-")
    if len(parts) != 3:
        return False
    python_tags, abi_tags, platform_tags = parts
    python_set = set(python_tags.split("."))
    abi_set = set(abi_tags.split("."))
    platform_set = set(platform_tags.split("."))
    pure_patterns = profile["wheelResolver"]["allowedWheelTags"]["purePython"]
    if (
        python_set
        and abi_set
        and platform_set
        and any(
            fnmatch.fnmatchcase(f"{python_tag}-{abi_tag}-{platform_tag}", pattern)
            for python_tag in python_set
            for abi_tag in abi_set
            for platform_tag in platform_set
            for pattern in pure_patterns
        )
    ):
        return bool(python_set & {"py3", "py312", "cp312"})
    compatible_abi3 = abi_set == {"abi3"} and any(
        (match := re.fullmatch(r"cp3([0-9]+)", tag)) is not None and int(match.group(1)) <= 12
        for tag in python_set
    )
    compatible_current_abi = python_set == {"cp312"} and abi_set == {"cp312"}
    if not compatible_abi3 and not compatible_current_abi:
        return False
    allowed = profile["wheelResolver"]["allowedWheelTags"]["pep600"]
    target_major, target_minor = map(int, allowed["maxGlibc"].split("."))
    if allowed["architecture"] != "x86_64" or target_major != 2:
        return False
    legacy_floors = {"manylinux1_x86_64": 5, "manylinux2010_x86_64": 12, "manylinux2014_x86_64": 17}
    for platform_tag in platform_set:
        if platform_tag in legacy_floors and legacy_floors[platform_tag] <= target_minor:
            return True
        match = re.fullmatch(r"manylinux_2_([0-9]+)_x86_64", platform_tag)
        if match is not None and int(match.group(1)) <= target_minor:
            return True
    return False


def _wheel_tags_are_compatible(tags: list[str], profile: dict[str, Any]) -> bool:
    """Require valid tag syntax and at least one tag supported by the target."""

    return (
        bool(tags)
        and all(WHEEL_TAG_PATTERN.fullmatch(tag) is not None for tag in tags)
        and any(_wheel_tag_is_compatible(tag, profile) for tag in tags)
    )


def _verify_site_package_wheel_tags(site_packages: Path, profile: dict[str, Any]) -> list[str]:
    """Inspect installed WHEEL metadata and reject artifacts outside the selected ABI profile."""

    dist_info_dirs = sorted(path for path in site_packages.rglob("*.dist-info") if path.is_dir())
    if not dist_info_dirs:
        raise ServiceBundleError(f"service bundle has no installed wheel metadata: {site_packages}")
    observed: set[str] = set()
    for dist_info in dist_info_dirs:
        wheel_metadata = dist_info / "WHEEL"
        if wheel_metadata.is_symlink() or not wheel_metadata.is_file():
            raise ServiceBundleError(f"installed wheel is missing WHEEL metadata: {wheel_metadata}")
        tags = [
            line.removeprefix("Tag:").strip()
            for line in wheel_metadata.read_text(encoding="utf-8").splitlines()
            if line.startswith("Tag:")
        ]
        if not tags or len(tags) != len(set(tags)):
            raise ServiceBundleError(
                f"installed wheel has missing or duplicate Tag entries: {wheel_metadata}"
            )
        if not _wheel_tags_are_compatible(tags, profile):
            invalid_tag = next(
                (tag for tag in tags if WHEEL_TAG_PATTERN.fullmatch(tag) is None), None
            )
            if invalid_tag is not None:
                raise ServiceBundleError(
                    f"installed wheel has invalid Tag entry {invalid_tag!r}: {wheel_metadata}"
                )
            incompatible_tag = next(
                tag for tag in tags if not _wheel_tag_is_compatible(tag, profile)
            )
            raise ServiceBundleError(
                f"wheel tag {incompatible_tag!r} is not compatible with {profile['abi']} "
                f"and CPython {profile['pythonVersion']}: {wheel_metadata}"
            )
        observed.update(tags)
    return sorted(observed)


def _validate_source_wheel_tags(
    value: Any,
    profile: dict[str, Any],
    installed_wheel_tags: list[str],
) -> dict[str, list[str]]:
    """Validate wheelhouse artifact tags and match their union to installed WHEEL tags."""

    if not isinstance(value, dict) or not value:
        raise ServiceBundleError("bundle source wheel_tags must map wheel files to their tags")
    result: dict[str, list[str]] = {}
    all_tags: set[str] = set()
    for filename, tags in value.items():
        if (
            not isinstance(filename, str)
            or not filename.endswith(".whl")
            or Path(filename).name != filename
            or "\\" in filename
            or not isinstance(tags, list)
            or not tags
            or any(not isinstance(tag, str) for tag in tags)
            or tags != sorted(set(tags))
        ):
            raise ServiceBundleError("bundle source wheel_tags contains an invalid wheel record")
        if not _wheel_tags_are_compatible(tags, profile):
            invalid_tag = next(
                (tag for tag in tags if WHEEL_TAG_PATTERN.fullmatch(tag) is None), None
            )
            if invalid_tag is not None:
                raise ServiceBundleError(f"source wheel tag {invalid_tag!r} is malformed")
            incompatible_tag = next(
                tag for tag in tags if not _wheel_tag_is_compatible(tag, profile)
            )
            raise ServiceBundleError(
                f"source wheel tag {incompatible_tag!r} is incompatible with profile"
            )
        result[filename] = tags
        all_tags.update(tags)
    if list(result) != sorted(result) or sorted(all_tags) != installed_wheel_tags:
        raise ServiceBundleError("bundle source wheel tags differ from installed wheel metadata")
    return result


def _wheelhouse_wheel_tags(wheels: list[Path], profile: dict[str, Any]) -> dict[str, list[str]]:
    """Read and verify every archived wheel tag before installing the wheelhouse."""

    result: dict[str, list[str]] = {}
    for wheel in sorted(wheels):
        fields = wheel.name.removesuffix(".whl").split("-")
        if len(fields) not in {5, 6}:
            raise ServiceBundleError(
                f"wheel filename does not follow the binary wheel format: {wheel.name}"
            )
        python_tags, abi_tags, platform_tags = (set(value.split(".")) for value in fields[-3:])
        filename_tags = sorted(
            f"{python_tag}-{abi_tag}-{platform_tag}"
            for python_tag in python_tags
            for abi_tag in abi_tags
            for platform_tag in platform_tags
        )
        try:
            with zipfile.ZipFile(wheel) as archive:
                wheel_paths = [
                    name
                    for name in archive.namelist()
                    if name.endswith(".dist-info/WHEEL") and name.count("/") == 1
                ]
                if len(wheel_paths) != 1:
                    raise ServiceBundleError(f"wheel must contain one dist-info/WHEEL: {wheel}")
                content = archive.read(wheel_paths[0]).decode("utf-8")
        except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as error:
            raise ServiceBundleError(f"cannot inspect wheel tags in {wheel}: {error}") from error
        tags = [
            line.removeprefix("Tag:").strip()
            for line in content.splitlines()
            if line.startswith("Tag:")
        ]
        if not tags or len(tags) != len(set(tags)):
            raise ServiceBundleError(f"wheel has missing or duplicate WHEEL tags: {wheel.name}")
        if filename_tags != sorted(tags):
            raise ServiceBundleError(
                f"wheel filename tags differ from WHEEL metadata: {wheel.name}"
            )
        if not _wheel_tags_are_compatible(tags, profile):
            invalid_tag = next(
                (tag for tag in tags if WHEEL_TAG_PATTERN.fullmatch(tag) is None), None
            )
            if invalid_tag is not None:
                raise ServiceBundleError(f"wheel has an invalid WHEEL Tag entry {invalid_tag!r}")
            incompatible_tag = next(
                tag for tag in tags if not _wheel_tag_is_compatible(tag, profile)
            )
            raise ServiceBundleError(
                f"wheel tag {incompatible_tag!r} is incompatible with the profile"
            )
        result[wheel.name] = sorted(tags)
    return result


def _verify_runtime_launcher(
    path: Path,
    profile: dict[str, Any],
    service: str,
    execution_runtime: dict[str, Any] | None,
) -> None:
    """Require the generated entrypoint to use only the profile's fixed interpreter."""

    text = path.read_text(encoding="utf-8")
    expected = _entrypoint_text(
        service, profile["pythonExecutable"], execution_runtime=execution_runtime
    )
    legacy_yield = (
        "#!/bin/sh\n"
        "set -eu\n"
        'BUNDLE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
        "unset PYTHONHOME\n"
        'export PYTHONPATH="$BUNDLE_DIR/python"\n'
        "exec " + shlex.quote(profile["pythonExecutable"]) + " -s -m cy_exec.training.product_cli "
        '--state-directory "${CYRENE_DATA_DIR:-/var/lib/cyrene}/yield" '
        '--port "${CYRENE_PORT_YIELD:-8001}" "$@"\n'
    )
    if text != expected and not (
        service == "yield" and execution_runtime is None and text == legacy_yield
    ):
        raise ServiceBundleError(
            "bundle entrypoint differs from the generated launcher for its locked private CPython"
        )


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


def _validate_runtime_dependencies(value: Any) -> list[dict[str, str]]:
    """Require one digest-pinned Runtime Maintenance SDK build dependency."""

    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ServiceBundleError("runtime_dependencies must contain exactly the pinned runtime SDK")
    record = value[0]
    required = {
        "component_id",
        "manifest_digest",
        "artifact_digest",
        "distribution",
        "version",
        "wheel_sha256",
    }
    if (
        set(record) != required
        or record.get("component_id") != RUNTIME_SDK_COMPONENT_ID
        or record.get("distribution") != RUNTIME_SDK_DISTRIBUTION
        or not isinstance(record.get("manifest_digest"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", record["manifest_digest"])
        or not isinstance(record.get("artifact_digest"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", record["artifact_digest"])
        or not isinstance(record.get("version"), str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}", record["version"])
        or not isinstance(record.get("wheel_sha256"), str)
        or not SHA256_PATTERN.fullmatch(record["wheel_sha256"])
    ):
        raise ServiceBundleError("runtime SDK provenance is incomplete or malformed")
    return [dict(record)]


def _service_root(service: str, install_root: Path | None = None) -> Path:
    if service not in SERVICES:
        raise ServiceBundleError(f"unsupported service name: {service}")
    root = (
        Path(install_root)
        if install_root is not None
        else Path(os.environ.get("CYRENE_INSTALL_ROOT", "/usr/lib/cyrene"))
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
            raise ServiceBundleError(
                f"service directory must be traversable by the service user: {current}"
            )
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
                raise ServiceBundleError(
                    f"installed release file is not readable by the service user: {path}"
                )


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


def _assert_execution_runtime_source_tree(root: Path) -> None:
    """Require a Product execution-runtime source tree to contain regular files only."""

    source_root = Path(root)
    if source_root.is_symlink() or not source_root.is_dir():
        raise ServiceBundleError(
            f"execution runtime source tree is missing or unsafe: {source_root}"
        )
    seen_file = False
    for current, directory_names, file_names in os.walk(source_root, followlinks=False):
        current_path = Path(current)
        for name in directory_names:
            path = current_path / name
            if path.is_symlink() or not path.is_dir():
                raise ServiceBundleError(f"execution runtime contains an unsafe directory: {path}")
        for name in file_names:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise ServiceBundleError(f"execution runtime contains an unsafe file: {path}")
            seen_file = True
    if not seen_file:
        raise ServiceBundleError("execution runtime source tree must contain files")


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
        "execution_runtime",
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


def validate_bundle(
    bundle_dir: Path,
    expected_service: str | None = None,
    *,
    expected_target_profile: str | None = None,
    release_lock_path: Path = DEFAULT_RELEASE_LOCK,
) -> dict[str, Any]:
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
    if "execution_runtime" in manifest:
        expected_keys.add("execution_runtime")
    if set(manifest) != expected_keys:
        missing = sorted(expected_keys - set(manifest))
        extra = sorted(set(manifest) - expected_keys)
        raise ServiceBundleError(f"manifest keys differ; missing={missing}, extra={extra}")
    schema_version = manifest["schema_version"]
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise ServiceBundleError(
            f"unsupported bundle schema version: {manifest['schema_version']!r}"
        )
    if schema_version != 2 and "execution_runtime" in manifest:
        raise ServiceBundleError("execution_runtime requires bundle schema v2")
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
        raise ServiceBundleError(
            f"bundle entrypoint must be executable by the service user: {entrypoint}"
        )

    source_repository = manifest["source_repository"]
    source_commit = manifest["source_commit"]
    if source_repository != REPOSITORIES[service]:
        raise ServiceBundleError(f"manifest source repository does not match {service}")
    if not isinstance(source_commit, str) or not COMMIT_PATTERN.fullmatch(source_commit):
        raise ServiceBundleError("manifest source_commit must be a lowercase 40-character Git SHA")
    dependencies = manifest["dependencies"]
    expected_dependency_keys = {"lock_file", "lock_sha256"}
    if schema_version == 2:
        expected_dependency_keys.add("runtime")
    if (
        not isinstance(dependencies, dict)
        or set(dependencies) != expected_dependency_keys
        or dependencies["lock_file"] != "requirements.lock"
        or not isinstance(dependencies["lock_sha256"], str)
        or not SHA256_PATTERN.fullmatch(dependencies["lock_sha256"])
        or files.get("requirements.lock") != dependencies["lock_sha256"]
    ):
        raise ServiceBundleError("manifest dependency lock identity is missing or inconsistent")
    runtime_dependencies: list[dict[str, str]] = []
    if schema_version == 2:
        runtime_dependencies = _validate_runtime_dependencies(dependencies["runtime"])
    source_record = _read_json_object(root / "source.json", "bundle source.json")
    target = manifest["target"]
    execution_runtime: dict[str, Any] | None = None
    if not isinstance(target, dict):
        raise ServiceBundleError("manifest target must be an object")
    if schema_version == 1:
        if (
            set(target) != {"debian_arch", "python"}
            or target["debian_arch"] != _debian_arch_for_host()
            or target["python"] != "3.12"
        ):
            raise ServiceBundleError(
                "legacy bundle target does not match this host's architecture/Python"
            )
    else:
        profile_id = target.get("profile_id")
        if not isinstance(profile_id, str) or (
            expected_target_profile is not None and profile_id != expected_target_profile
        ):
            raise ServiceBundleError(
                f"bundle target profile differs from the selected release: {profile_id!r}"
            )
        profile = _native_python_profile(release_lock_path, profile_id)
        installed_wheel_tags = _verify_site_package_wheel_tags(root / "python", profile)
        execution_runtime = None
        if "execution_runtime" in manifest:
            execution_runtime = _validate_execution_runtime(
                manifest["execution_runtime"],
                root=root,
                files=normalized_files,
                profile=profile,
            )
        _verify_runtime_launcher(root / entrypoint_path, profile, service, execution_runtime)
        source_wheel_tags = _validate_source_wheel_tags(
            source_record.get("wheel_tags"), profile, installed_wheel_tags
        )
        expected_target = _bundle_target(
            profile_id, profile, source_wheel_tags, installed_wheel_tags
        )
        if target != expected_target:
            raise ServiceBundleError(
                "bundle target metadata does not match its locked Python target profile"
            )
        _require_profile_host(profile)

    expected_source_record: dict[str, Any] = {
        "schema_version": schema_version,
        "service": service,
        "source_repository": source_repository,
        "source_commit": source_commit,
        "requirements_lock_sha256": dependencies["lock_sha256"],
    }
    if schema_version == 2:
        expected_source_record["runtime_dependencies"] = runtime_dependencies
        expected_source_record["target_profile"] = manifest["target"].get("profile_id")
        expected_source_record["wheel_tags"] = source_wheel_tags
        if "execution_runtime" in manifest:
            expected_source_record["execution_runtime"] = manifest["execution_runtime"]
            expected_source_record["execution_runtime_source_sha256"] = {
                file_name: digest
                for file_name, digest in normalized_files.items()
                if file_name.startswith("execution-runtime/")
            }
    if source_record != expected_source_record:
        raise ServiceBundleError("bundle source.json does not match manifest provenance")
    if schema_version == 2:
        lock_pins = _wheel_requirement_hashes(root / "requirements.lock")
        sdk_identity = (
            _normalize_distribution(runtime_dependencies[0]["distribution"]),
            runtime_dependencies[0]["version"],
        )
        if runtime_dependencies[0]["wheel_sha256"] not in lock_pins.get(sdk_identity, set()):
            raise ServiceBundleError(
                "requirements.lock does not contain the pinned runtime SDK wheel"
            )

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


def stage_release(
    bundle_dir: Path,
    install_root: Path | None = None,
    *,
    release_lock_path: Path = DEFAULT_RELEASE_LOCK,
) -> Path:
    """Copy a verified artifact into its immutable release directory.

    Re-staging byte-identical content is idempotent. Existing versions with a
    different manifest are rejected to preserve rollback history.
    """

    source = Path(bundle_dir)
    manifest = validate_bundle(source, release_lock_path=release_lock_path)
    if manifest["schema_version"] != 2:
        raise ServiceBundleError("only schema v2 bundles may be staged as new releases")
    service_root = _service_root(manifest["service"], install_root)
    releases = service_root / "releases"
    destination = releases / manifest["version"]
    _ensure_secure_directory(releases, create=True)
    if destination.exists() or destination.is_symlink():
        _ensure_secure_directory(destination, create=False)
        _assert_secure_tree(destination)
        existing = validate_bundle(
            destination,
            expected_service=manifest["service"],
            release_lock_path=release_lock_path,
        )
        if existing["artifact_digest"] != manifest["artifact_digest"]:
            raise ServiceBundleError(
                f"immutable release already exists with different content: {destination}"
            )
        return destination

    staging = Path(tempfile.mkdtemp(prefix=f".{manifest['version']}.stage-", dir=releases))
    try:
        shutil.copytree(source, staging, dirs_exist_ok=True, copy_function=shutil.copy2)
        _normalize_release_modes(staging, manifest["entrypoint"])
        copied = validate_bundle(
            staging,
            expected_service=manifest["service"],
            expected_target_profile=manifest["target"].get("profile_id"),
            release_lock_path=release_lock_path,
        )
        if copied["artifact_digest"] != manifest["artifact_digest"]:
            raise ServiceBundleError("staged release content changed during copy")
        try:
            os.rename(staging, destination)
        except OSError:
            if destination.exists() or destination.is_symlink():
                _ensure_secure_directory(destination, create=False)
                _assert_secure_tree(destination)
                existing = validate_bundle(
                    destination,
                    expected_service=manifest["service"],
                    release_lock_path=release_lock_path,
                )
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


def _execution_runtime_owner_policy() -> bool:
    """Return whether packaged root ownership is mandatory for trusted runtime tools."""

    return Path(__file__).resolve().parent == Path("/usr/lib/cyrene/scripts")


def _verify_execution_runtime_tools(
    descriptor: dict[str, Any],
    profile: dict[str, Any],
    *,
    runtime_lock_path: Path,
) -> tuple[Path, Path]:
    """Verify the installed CPython and uv against the separate immutable runtime lock."""

    lock_path = Path(runtime_lock_path)
    if lock_path.is_symlink() or not lock_path.is_file():
        raise ServiceBundleError(f"private Python runtime lock is missing or unsafe: {lock_path}")
    packaged = _execution_runtime_owner_policy()
    lock_info = lock_path.lstat()
    if packaged and (lock_info.st_uid != 0 or lock_info.st_mode & 0o022):
        raise ServiceBundleError("private Python runtime lock is not root-controlled")
    lock = _read_json_object(lock_path, "private Python runtime lock")
    python = lock.get("python")
    resolver = lock.get("buildResolver")
    archive = python.get("archive") if isinstance(python, dict) else None
    uv_archive = resolver.get("binaryArchive") if isinstance(resolver, dict) else None
    if (
        not isinstance(python, dict)
        or not isinstance(resolver, dict)
        or not isinstance(archive, dict)
        or not isinstance(uv_archive, dict)
        or python.get("implementation") != "CPython"
        or python.get("version") != profile["pythonVersion"]
        or python.get("executable") != descriptor["python_executable"]
        or resolver.get("tool") != "uv"
        or resolver.get("version") != descriptor["uv_version"]
        or resolver.get("installedPath") != descriptor["uv_executable"]
        or not isinstance(uv_archive.get("executableSha256"), str)
        or SHA256_PATTERN.fullmatch(uv_archive["executableSha256"]) is None
    ):
        raise ServiceBundleError(
            "installed Python runtime lock does not match the signed descriptor"
        )

    def verify_executable(path_value: str, label: str) -> Path:
        executable = Path(path_value)
        if not executable.is_absolute() or executable.is_symlink() or not executable.is_file():
            raise ServiceBundleError(
                f"locked {label} executable is missing or unsafe: {executable}"
            )
        info = executable.lstat()
        if not (info.st_mode & 0o111) or info.st_mode & 0o022:
            raise ServiceBundleError(
                f"locked {label} executable has unsafe permissions: {executable}"
            )
        if packaged and info.st_uid != 0:
            raise ServiceBundleError(f"locked {label} executable is not root-owned: {executable}")
        current = executable.parent
        while current != current.parent:
            parent_info = current.lstat()
            if (
                stat.S_ISLNK(parent_info.st_mode)
                or not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_mode & 0o022
                or (packaged and parent_info.st_uid != 0)
            ):
                raise ServiceBundleError(f"locked {label} path has an unsafe directory: {current}")
            current = current.parent
        return executable

    python_executable = verify_executable(descriptor["python_executable"], "CPython")
    if _python_runtime_version(python_executable) != descriptor["python_version"]:
        raise ServiceBundleError("installed private CPython does not have the locked version")

    uv_executable = verify_executable(descriptor["uv_executable"], "uv")
    if _sha256_file(uv_executable) != uv_archive["executableSha256"]:
        raise ServiceBundleError("installed private uv executable differs from its locked SHA-256")
    try:
        result = subprocess.run(
            [str(uv_executable), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ServiceBundleError(f"cannot verify locked uv executable: {error}") from error
    output = result.stdout.strip().split()
    if len(output) < 2 or output[0] != "uv" or output[1] != descriptor["uv_version"]:
        raise ServiceBundleError("installed private uv does not have the locked version")
    return python_executable, uv_executable


def _verify_data_root(data_root: Path, service_uid: int) -> Path:
    """Require a real service-owned data directory before creating runtime state."""

    root = Path(os.path.abspath(data_root))
    if not root.is_absolute():
        raise ServiceBundleError("CYRENE_DATA_DIR must be an absolute path")
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise ServiceBundleError(f"service data directory is missing: {current}") from error
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ServiceBundleError(
                f"service data path contains a symlink or non-directory: {current}"
            )
        sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        if current == root:
            if info.st_uid != service_uid or info.st_mode & 0o022:
                raise ServiceBundleError(
                    "service data directory is not privately controlled by cyrene"
                )
        elif info.st_uid not in {0, service_uid} or (info.st_mode & 0o022 and not sticky_root):
            raise ServiceBundleError(f"service data path has an unsafe directory: {current}")
    return root


def _runtime_home_state(
    data_root: Path,
    relative_parts: tuple[str, ...],
    version: str,
    manifest_name: str,
    *,
    service_uid: int,
    expected_profile: str,
) -> tuple[Path, Path] | None:
    """Return a complete, private runtime home or reject partial state."""

    home = data_root.joinpath(*relative_parts, version)
    current = data_root
    directory_parts = (*relative_parts, version)
    missing = False
    for index, part in enumerate(directory_parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            missing = True
            continue
        if missing:
            raise ServiceBundleError(f"runtime home path is occupied unsafely: {current}")
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != service_uid
            or info.st_mode & 0o022
        ):
            raise ServiceBundleError(f"runtime home directory is not private to cyrene: {current}")
        if index == len(directory_parts) - 1 and stat.S_IMODE(info.st_mode) != 0o700:
            raise ServiceBundleError("runtime home must have mode 0700")
    if missing:
        return None

    manifest_path = home.joinpath(*PurePosixPath(manifest_name).parts)
    manifest_parent = home
    for part in PurePosixPath(manifest_name).parts[:-1]:
        manifest_parent = manifest_parent / part
        info = manifest_parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != service_uid
            or info.st_mode & 0o022
        ):
            raise ServiceBundleError(f"runtime manifest directory is unsafe: {manifest_parent}")
    try:
        manifest_info = manifest_path.lstat()
    except FileNotFoundError as error:
        raise ServiceBundleError(
            f"runtime home is incomplete; refusing an in-place repair: {home}"
        ) from error
    if (
        not stat.S_ISREG(manifest_info.st_mode)
        or stat.S_ISLNK(manifest_info.st_mode)
        or manifest_info.st_uid != service_uid
        or stat.S_IMODE(manifest_info.st_mode) != 0o600
    ):
        raise ServiceBundleError(
            f"runtime manifest is not a private cyrene-owned file: {manifest_path}"
        )
    try:
        runtime_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ServiceBundleError(f"runtime manifest is invalid: {manifest_path}") from error
    if (
        not isinstance(runtime_manifest, dict)
        or runtime_manifest.get("status") != "READY"
        or runtime_manifest.get("profile") != expected_profile
    ):
        raise ServiceBundleError("existing runtime manifest is not READY for the selected profile")
    return home, manifest_path


def _reserve_runtime_home(
    data_root: Path,
    relative_parts: tuple[str, ...],
    version: str,
    *,
    service_uid: int,
    service_gid: int,
) -> Path:
    """Atomically create a fresh user-owned runtime home without following symlinks."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(data_root, flags)
    try:
        root_info = os.fstat(directory_fd)
        if root_info.st_uid != service_uid or root_info.st_mode & 0o022:
            raise ServiceBundleError("service data directory is not privately controlled by cyrene")
        for part in relative_parts:
            try:
                os.mkdir(part, 0o700, dir_fd=directory_fd)
                created = True
            except FileExistsError:
                created = False
            child_fd = os.open(part, flags, dir_fd=directory_fd)
            child_info = os.fstat(child_fd)
            if (
                not stat.S_ISDIR(child_info.st_mode)
                or child_info.st_uid != service_uid
                or child_info.st_mode & 0o022
            ):
                os.close(child_fd)
                raise ServiceBundleError(f"runtime home parent is unsafe: {part}")
            if created and os.geteuid() == 0:
                os.fchown(child_fd, service_uid, service_gid)
                os.fchmod(child_fd, 0o700)
            os.close(directory_fd)
            directory_fd = child_fd
        os.mkdir(version, 0o700, dir_fd=directory_fd)
        runtime_fd = os.open(version, flags, dir_fd=directory_fd)
        runtime_info = os.fstat(runtime_fd)
        if os.geteuid() == 0:
            os.fchown(runtime_fd, service_uid, service_gid)
            os.fchmod(runtime_fd, 0o700)
            runtime_info = os.fstat(runtime_fd)
        os.close(runtime_fd)
        if runtime_info.st_uid != service_uid or stat.S_IMODE(runtime_info.st_mode) != 0o700:
            raise ServiceBundleError("new runtime home ownership or mode is invalid")
    finally:
        os.close(directory_fd)
    return data_root.joinpath(*relative_parts, version)


def _select_runtime_release(
    service: str,
    *,
    version: str | None,
    install_root: Path,
    release_lock_path: Path,
) -> tuple[Path, dict[str, Any]]:
    """Select and verify exactly one staged/active immutable service release."""

    service_root = _service_root(service, install_root)
    _ensure_secure_directory(service_root, create=False)
    releases_root = service_root / "releases"
    _ensure_secure_directory(releases_root, create=False)
    selected_version = version
    active = service_root / "active"
    if selected_version is None and (active.exists() or active.is_symlink()):
        active_info = active.lstat()
        if not stat.S_ISLNK(active_info.st_mode) or active_info.st_uid != 0:
            raise ServiceBundleError(f"active release pointer is unsafe: {active}")
        match = re.fullmatch(r"releases/([^/]+)", os.readlink(active))
        if match is None or not VERSION_PATTERN.fullmatch(match.group(1)):
            raise ServiceBundleError("active release pointer has an unsafe target")
        selected_version = match.group(1)
    if selected_version is None:
        candidates: list[str] = []
        for path in sorted(releases_root.iterdir()):
            if path.is_symlink() or not path.is_dir() or not VERSION_PATTERN.fullmatch(path.name):
                raise ServiceBundleError(f"release directory contains an unsafe entry: {path}")
            manifest = validate_bundle(
                path, expected_service=service, release_lock_path=release_lock_path
            )
            if manifest["version"] != path.name:
                raise ServiceBundleError(f"release directory and manifest version differ: {path}")
            candidates.append(path.name)
        if len(candidates) != 1:
            raise ServiceBundleError(
                "select one staged runtime release with --version; no active release or a single candidate is available"
            )
        selected_version = candidates[0]
    if not isinstance(selected_version, str) or not VERSION_PATTERN.fullmatch(selected_version):
        raise ServiceBundleError("service release version is not a safe path segment")
    release = releases_root / selected_version
    _ensure_secure_directory(release, create=False)
    _assert_secure_tree(release)
    manifest = validate_bundle(
        release,
        expected_service=service,
        release_lock_path=release_lock_path,
    )
    if manifest["version"] != selected_version:
        raise ServiceBundleError("selected release directory and manifest version differ")
    return release, manifest


def prepare_execution_runtime(
    service: str,
    *,
    version: str | None = None,
    install_root: Path | None = None,
    data_root: Path | None = None,
    release_lock_path: Path = DEFAULT_RELEASE_LOCK,
    runtime_lock_path: Path = DEFAULT_PYTHON_RUNTIME_LOCK,
    runner: Any | None = None,
    service_uid: int | None = None,
    service_gid: int | None = None,
) -> dict[str, Any]:
    """Prepare one signed Product-declared runtime beside its immutable bundle.

    This operation selects one already staged immutable release, verifies its
    source file hashes and explicit base tools, then invokes its signed bootstrap
    as the ``cyrene`` account. It never activates a release or touches systemd.

    Args:
        service: Managed service whose bundle declares ``execution_runtime``.
        version: Optional exact staged bundle version; defaults to active or a
            single staged release.
        install_root: Optional immutable service install root override.
        data_root: Optional persistent data directory override.
        release_lock_path: Trusted lock containing the selected native profile.
        runtime_lock_path: Root-controlled CPython/uv lock installed by the DEB.
        runner: Optional subprocess runner used by focused tests.
        service_uid: Optional service UID override used by focused tests.
        service_gid: Optional service GID override used by focused tests.

    Returns:
        A JSON-compatible record identifying the prepared external runtime.
    """

    if runner is None:
        runner = subprocess.run
    try:
        account = pwd.getpwnam("cyrene")
        group = grp.getgrnam("cyrene")
    except KeyError as error:
        raise ServiceBundleError("the cyrene service account is not installed") from error
    uid = account.pw_uid if service_uid is None else service_uid
    gid = group.gr_gid if service_gid is None else service_gid
    if os.geteuid() not in {0, uid}:
        raise ServiceBundleError("service-prepare must run as root or the cyrene service account")

    root = Path(install_root or os.environ.get("CYRENE_INSTALL_ROOT", "/usr/lib/cyrene"))
    release, manifest = _select_runtime_release(
        service,
        version=version,
        install_root=root,
        release_lock_path=release_lock_path,
    )
    descriptor = manifest.get("execution_runtime")
    if not isinstance(descriptor, dict):
        raise ServiceBundleError(f"{service} release does not declare an execution runtime")
    profile = _native_python_profile(release_lock_path, manifest["target"]["profile_id"])
    python_executable, uv_executable = _verify_execution_runtime_tools(
        descriptor, profile, runtime_lock_path=runtime_lock_path
    )

    configured_data_root = data_root or Path(os.environ.get("CYRENE_DATA_DIR", "/var/lib/cyrene"))
    data_directory = _verify_data_root(Path(configured_data_root).expanduser(), uid)
    home_relative = _relative_path(
        descriptor["runtime_home_relative_path"], "execution_runtime.runtime_home_relative_path"
    )
    home = data_directory.joinpath(*home_relative.parts, manifest["version"])
    absolute_install_root = Path(os.path.abspath(root))
    if home == absolute_install_root or home.is_relative_to(absolute_install_root):
        raise ServiceBundleError("execution runtime home must remain outside immutable releases")

    manifest_relative = _relative_path(
        descriptor["runtime_manifest_file"], "execution_runtime.runtime_manifest_file"
    )
    existing = _runtime_home_state(
        data_directory,
        home_relative.parts,
        manifest["version"],
        manifest_relative.as_posix(),
        service_uid=uid,
        expected_profile=descriptor["profile"],
    )
    if existing is not None:
        existing_home, existing_manifest = existing
        return {
            "status": "already_prepared",
            "service": service,
            "version": manifest["version"],
            "runtimeId": descriptor["runtime_id"],
            "profile": descriptor["profile"],
            "runtimeHome": str(existing_home),
            "runtimeManifest": str(existing_manifest),
        }

    runtime_home = _reserve_runtime_home(
        data_directory,
        home_relative.parts,
        manifest["version"],
        service_uid=uid,
        service_gid=gid,
    )
    source_root = release.joinpath(*PurePosixPath(descriptor["source_root"]).parts)
    bootstrap = source_root.joinpath(*PurePosixPath(descriptor["bootstrap_script"]).parts)
    command = [
        str(python_executable),
        "-I",
        str(bootstrap),
        "bootstrap",
        "--runtime-home",
        str(runtime_home),
        "--repository",
        str(source_root),
        "--python-executable",
        descriptor["python_executable"],
        "--python-version",
        descriptor["python_version"],
        "--uv-executable",
        str(uv_executable),
        "--uv-version",
        descriptor["uv_version"],
    ]
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(runtime_home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "CYRENE_TRAINER_RUNTIME_HOME": str(runtime_home),
        "UV_PROJECT_ENVIRONMENT": str(runtime_home / "venv"),
        "UV_CACHE_DIR": str(runtime_home / "cache"),
    }
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        if os.environ.get(key):
            environment[key] = os.environ[key]
    for key in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        if os.environ.get(key):
            environment[key] = os.environ[key]
    runner_options: dict[str, Any] = {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": 3600,
        "cwd": str(source_root),
        "env": environment,
    }
    if os.geteuid() == 0:
        runner_options.update({"user": uid, "group": gid, "extra_groups": [], "umask": 0o077})
    try:
        completed = runner(command, **runner_options)
    except (OSError, subprocess.SubprocessError) as error:
        raise ServiceBundleError(
            f"execution runtime preparation could not start: {error}"
        ) from error
    if completed.returncode != 0:
        code = ""
        for output in (completed.stdout, completed.stderr):
            try:
                report = json.loads(output)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(report, dict) and isinstance(report.get("code"), str):
                code = f" ({report['code']})"
                break
        raise ServiceBundleError(
            f"execution runtime preparation failed with exit {completed.returncode}{code}; "
            f"partial home remains for review: {runtime_home}"
        )
    prepared = _runtime_home_state(
        data_directory,
        home_relative.parts,
        manifest["version"],
        manifest_relative.as_posix(),
        service_uid=uid,
        expected_profile=descriptor["profile"],
    )
    if prepared is None:
        raise ServiceBundleError("trainer bootstrap completed without its versioned runtime home")
    prepared_home, prepared_manifest = prepared
    return {
        "status": "prepared",
        "service": service,
        "version": manifest["version"],
        "runtimeId": descriptor["runtime_id"],
        "profile": descriptor["profile"],
        "runtimeHome": str(prepared_home),
        "runtimeManifest": str(prepared_manifest),
        "pythonExecutable": descriptor["python_executable"],
        "uvExecutable": descriptor["uv_executable"],
    }


def _normalize_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _wheel_metadata(path: Path) -> tuple[str, str]:
    """Read the single validated Name and Version pair inside one wheel."""

    try:
        with zipfile.ZipFile(path) as archive:
            metadata_paths = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA") and name.count("/") == 1
            ]
            if len(metadata_paths) != 1:
                raise ServiceBundleError(f"wheel must contain one dist-info/METADATA: {path}")
            metadata = email.parser.Parser().parsestr(
                archive.read(metadata_paths[0]).decode("utf-8", errors="replace")
            )
    except (OSError, zipfile.BadZipFile) as error:
        raise ServiceBundleError(f"cannot inspect wheel metadata {path}: {error}") from error
    names = metadata.get_all("Name", [])
    versions = metadata.get_all("Version", [])
    if (
        len(names) != 1
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", names[0]) is None
        or len(versions) != 1
        or VERSION_PATTERN.fullmatch(versions[0]) is None
    ):
        raise ServiceBundleError(f"wheel metadata must contain one valid Name and Version: {path}")
    return names[0], versions[0]


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
        requirement_tokens = [
            token for token in requirement_part.split() if not token.startswith("--hash=")
        ]
        if len(requirement_tokens) != 1:
            raise ServiceBundleError(f"invalid hash-pinned requirement line: {line}")
        match = WHEEL_REQUIREMENT_PATTERN.fullmatch(requirement_tokens[0])
        if match is None:
            raise ServiceBundleError(
                f"requirements.lock entries must use exact package==version pins: {line}"
            )
        names.add(_normalize_distribution(match.group(1)))
    return names


def _wheel_requirement_hashes(lock_path: Path) -> dict[tuple[str, str], set[str]]:
    """Read exact wheel hash pins from the generated pip lock."""

    pins: dict[tuple[str, str], set[str]] = {}
    current: tuple[str, str] | None = None
    for raw_line in lock_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash=sha256:"):
            digest = line.removeprefix("--hash=sha256:").rstrip("\\").strip()
            if current is None or SHA256_PATTERN.fullmatch(digest) is None:
                raise ServiceBundleError(f"invalid hash continuation in requirements.lock: {line}")
            pins.setdefault(current, set()).add(digest)
            continue
        requirement = line.split(";", 1)[0].rstrip("\\").strip()
        match = WHEEL_REQUIREMENT_PATTERN.fullmatch(requirement)
        if match is None:
            raise ServiceBundleError(
                f"requirements.lock entries must use exact package==version pins: {line}"
            )
        current = (_normalize_distribution(match.group(1)), match.group(2))
        pins.setdefault(current, set())
    if any(not hashes for hashes in pins.values()):
        raise ServiceBundleError("requirements.lock contains a pin without a SHA-256 hash")
    return pins


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
            "release-lock.json supports x86_64 (amd64) only; "
            f"unsupported native service bundle build CPU: {machine}"
        ) from error


def _execution_runtime_prelude(
    execution_runtime: dict[str, Any] | None,
    *,
    service: str,
) -> str:
    """Select paired runtime inputs or the Product-declared API-only arguments."""

    if execution_runtime is None:
        return ""
    bindings = {
        "{platform_runtime_config}": '"$PLATFORM_RUNTIME_CONFIG"',
        "{trainer_runtime_config}": '"$TRAINER_RUNTIME_CONFIG"',
        "{artifact_root}": '"$ARTIFACT_ROOT"',
    }

    def render_arguments(arguments: list[str]) -> str:
        return " ".join(bindings.get(argument, shlex.quote(argument)) for argument in arguments)

    runtime_arguments = render_arguments(execution_runtime["runtime_arguments"])
    api_only_arguments = render_arguments(execution_runtime["api_only_arguments"])
    runtime_home = PurePosixPath(execution_runtime["runtime_home_relative_path"])
    trainer_manifest = PurePosixPath(execution_runtime["runtime_manifest_file"])
    trainer_runtime_default = PurePosixPath(*runtime_home.parts, "$BUNDLE_VERSION")
    trainer_runtime_default = PurePosixPath(
        *trainer_runtime_default.parts, *trainer_manifest.parts
    ).as_posix()
    return f"""BUNDLE_VERSION="${{BUNDLE_DIR##*/}}"
DATA_HOME="${{CYRENE_DATA_DIR:-/var/lib/cyrene}}"
ARTIFACT_ROOT="${{CYRENE_ARTIFACT_ROOT:-$DATA_HOME/artifacts}}"
PLATFORM_RUNTIME_CONFIG="${{CYRENE_PLATFORM_RUNTIME_CONFIG:-/etc/cyrene/runtime/platform.json}}"
TRAINER_RUNTIME_CONFIG="${{CYRENE_TRAINER_RUNTIME_CONFIG:-$DATA_HOME/{trainer_runtime_default}}}"
if [ -L "$PLATFORM_RUNTIME_CONFIG" ] || [ -L "$TRAINER_RUNTIME_CONFIG" ]; then
  echo "ERROR: execution runtime manifests must not be symbolic links." >&2
  exit 1
fi
if [ -e "$PLATFORM_RUNTIME_CONFIG" ] && [ ! -f "$PLATFORM_RUNTIME_CONFIG" ]; then
  echo "ERROR: Platform runtime manifest is not a regular file." >&2
  exit 1
fi
if [ -e "$TRAINER_RUNTIME_CONFIG" ] && [ ! -f "$TRAINER_RUNTIME_CONFIG" ]; then
  echo "ERROR: trainer runtime manifest is not a regular file." >&2
  exit 1
fi
if [ -f "$PLATFORM_RUNTIME_CONFIG" ] && [ -f "$TRAINER_RUNTIME_CONFIG" ]; then
  set -- {runtime_arguments} "$@"
elif [ -e "$TRAINER_RUNTIME_CONFIG" ]; then
  echo "ERROR: trainer runtime manifest exists without a Platform runtime manifest." >&2
  exit 1
elif [ -n "${{CYRENE_PLATFORM_RUNTIME_CONFIG:-}}" ] || [ -n "${{CYRENE_TRAINER_RUNTIME_CONFIG:-}}" ]; then
  echo "ERROR: explicitly configured execution runtime manifests must exist as a pair." >&2
  exit 1
else
  if [ -e "$PLATFORM_RUNTIME_CONFIG" ] || [ -e "$TRAINER_RUNTIME_CONFIG" ]; then
    echo "WARNING: execution runtime manifests are incomplete; starting API-only without training readiness." >&2
  else
    echo "WARNING: execution runtime is not prepared; starting API-only. Run cyrene service-prepare {service} after staging its release." >&2
  fi
  mkdir -p "$ARTIFACT_ROOT"
  set -- {api_only_arguments} "$@"
fi
"""


def _entrypoint_text(
    service: str,
    python_executable: str,
    *,
    execution_runtime: dict[str, Any] | None = None,
) -> str:
    commands = {
        "yield": (
            "exec python3 -s -m cy_exec.training.product_cli "
            '--state-directory "${CYRENE_DATA_DIR:-/var/lib/cyrene}/yield" '
            '--port "${CYRENE_PORT_YIELD:-8001}" "$@"'
        ),
        "reactor": (""),
        "exchange": (
            "exec python3 -s -m cyrene_exchange_product.cli "
            '--database "${CYRENE_DATA_DIR:-/var/lib/cyrene}/exchange.sqlite3" '
            'serve --port "${CYRENE_PORT_EXCHANGE:-8003}" "$@"'
        ),
        "catalyst": (
            "exec python3 -s -m cyrene_catalyst.cli serve "
            '--home "${CYRENE_DATA_DIR:-/var/lib/cyrene}/catalyst" '
            '--port "${CYRENE_PORT_CATALYST:-8004}" "$@"'
        ),
    }
    execution_prelude = _execution_runtime_prelude(execution_runtime, service=service)
    if service == "yield":
        text = f"""#!/bin/sh
set -eu
BUNDLE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
unset PYTHONHOME
export PYTHONPATH="$BUNDLE_DIR/python"
{execution_prelude}exec python3 -s -m cy_exec.training.product_cli \\
  --state-directory "${{CYRENE_DATA_DIR:-/var/lib/cyrene}}/yield" \\
  --port "${{CYRENE_PORT_YIELD:-8001}}" "$@"
"""
        return text.replace("python3", shlex.quote(python_executable))
    if service == "navigator":
        text = """#!/bin/sh
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
        if execution_prelude:
            text = text.replace(
                'export PYTHONPATH="$BUNDLE_DIR/python"\n',
                'export PYTHONPATH="$BUNDLE_DIR/python"\n' + execution_prelude,
            )
        return text.replace("python3", shlex.quote(python_executable))
    if service == "reactor":
        text = """#!/bin/sh
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
        if execution_prelude:
            text = text.replace(
                'export PYTHONPATH="$BUNDLE_DIR/python"\n',
                'export PYTHONPATH="$BUNDLE_DIR/python"\n' + execution_prelude,
            )
        return text.replace("python3", shlex.quote(python_executable))
    command = commands[service]
    text = (
        "#!/bin/sh\n"
        "set -eu\n"
        'BUNDLE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
        "unset PYTHONHOME\n"
        'export PYTHONPATH="$BUNDLE_DIR/python"\n'
        f"{execution_prelude}"
        f"{command}\n"
    )
    return text.replace("python3", shlex.quote(python_executable))


def _write_bundle_manifest(
    bundle_root: Path,
    *,
    service: str,
    source_commit: str,
    source_repository: str,
    target_profile_id: str,
    target_profile: dict[str, Any],
    source_wheel_tags: dict[str, list[str]],
    installed_wheel_tags: list[str],
    runtime_dependencies: list[dict[str, str]],
    execution_runtime: dict[str, Any] | None = None,
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
        "schema_version": 2,
        "service": service,
        "version": "",
        "entrypoint": "run-service",
        "health": {"path": HEALTH_PATHS[service]},
        "files": files,
        "source_repository": source_repository,
        "source_commit": source_commit,
        "dependencies": {
            "lock_file": "requirements.lock",
            "lock_sha256": lock_digest,
            "runtime": runtime_dependencies,
        },
        "target": _bundle_target(
            target_profile_id, target_profile, source_wheel_tags, installed_wheel_tags
        ),
        "artifact_digest": "",
    }
    if execution_runtime is not None:
        manifest["execution_runtime"] = dict(execution_runtime)
    digest = _artifact_digest(manifest)
    manifest["version"] = digest
    manifest["artifact_digest"] = digest
    manifest_path = bundle_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _build_one_bundle(
    *,
    service: str,
    wheelhouse: Path,
    output_root: Path,
    source_commit: str,
    source_repository: str,
    target_profile_id: str,
    target_profile: dict[str, Any],
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
    execution_runtime_root = service_input / "execution-runtime"
    for child in sorted(service_input.iterdir()):
        if child.is_symlink():
            raise ServiceBundleError(f"wheelhouse entries must be flat regular files: {child}")
        if child.name == "execution-runtime" and child.is_dir():
            _assert_execution_runtime_source_tree(child)
            continue
        if not child.is_file():
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
    required_source_keys = {
        "schema_version",
        "service",
        "source_repository",
        "source_commit",
        "requirements_lock_sha256",
        "runtime_dependencies",
        "target_profile",
        "wheel_tags",
    }
    if frozenset(source) not in {
        frozenset(required_source_keys),
        frozenset(required_source_keys | {"execution_runtime", "execution_runtime_source_sha256"}),
    }:
        raise ServiceBundleError(
            f"{service}/source.json keys must be exactly {sorted(required_source_keys)}"
        )
    if source["schema_version"] != 2 or source["service"] != service:
        raise ServiceBundleError(f"{service}/source.json identifies a different bundle")
    if source["target_profile"] != target_profile_id:
        raise ServiceBundleError(f"{service}/source.json was built for a different target profile")
    has_execution_runtime = "execution_runtime" in source
    execution_runtime = source.get("execution_runtime")
    if not has_execution_runtime:
        if execution_runtime_root.exists() or execution_runtime_root.is_symlink():
            raise ServiceBundleError(
                "wheelhouse execution-runtime tree lacks its source descriptor"
            )
    else:
        if not execution_runtime_root.is_dir() or execution_runtime_root.is_symlink():
            raise ServiceBundleError("wheelhouse descriptor has no execution-runtime source tree")
        _validate_execution_runtime(
            execution_runtime,
            root=service_input,
            files=_expected_file_map(service_input),
            profile=target_profile,
        )
        execution_runtime_source_hashes = {
            file_name: digest
            for file_name, digest in _expected_file_map(service_input).items()
            if file_name.startswith("execution-runtime/")
        }
        if source.get("execution_runtime_source_sha256") != execution_runtime_source_hashes:
            raise ServiceBundleError(
                "wheelhouse source.json execution runtime hashes do not match source files"
            )
    if source["source_repository"] != source_repository:
        raise ServiceBundleError(f"{service}/source.json has the wrong source repository")
    if source["source_commit"] != source_commit:
        raise ServiceBundleError(
            f"{service} wheelhouse source commit differs from release-lock.json: "
            f"expected {source_commit}, got {source['source_commit']}"
        )
    actual_source_wheel_tags = _wheelhouse_wheel_tags(wheels, target_profile)
    if source["wheel_tags"] != actual_source_wheel_tags:
        raise ServiceBundleError(
            f"{service}/source.json wheel tags do not match the exact wheelhouse contents"
        )
    lock_digest = _sha256_file(lock_path)
    if source["requirements_lock_sha256"] != lock_digest:
        raise ServiceBundleError(f"{service}/source.json requirements lock digest does not match")
    runtime_dependencies = _validate_runtime_dependencies(source["runtime_dependencies"])
    if runtime_dependencies != source["runtime_dependencies"]:
        raise ServiceBundleError(f"{service}/source.json runtime dependencies are not canonical")
    sdk = runtime_dependencies[0]
    sdk_identity = (_normalize_distribution(sdk["distribution"]), sdk["version"])
    sdk_wheels = []
    for wheel in wheels:
        if not wheel.name.endswith(".whl"):
            continue
        wheel_name, wheel_version = _wheel_metadata(wheel)
        if (_normalize_distribution(wheel_name), wheel_version) == sdk_identity and _sha256_file(
            wheel
        ) == sdk["wheel_sha256"]:
            sdk_wheels.append(wheel)
    if len(sdk_wheels) != 1:
        raise ServiceBundleError(
            f"{service} wheelhouse does not contain the exact pinned Runtime Maintenance SDK wheel"
        )
    lock_pins = _wheel_requirement_hashes(lock_path)
    sdk_identity = (_normalize_distribution(sdk["distribution"]), sdk["version"])
    if sdk["wheel_sha256"] not in lock_pins.get(sdk_identity, set()):
        raise ServiceBundleError(
            f"{service}/requirements.lock does not pin the exact runtime SDK wheel"
        )
    requirement_names = {name for name, _ in lock_pins}
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
    build_env = os.environ.copy()
    build_env.pop("PYTHONHOME", None)
    build_env.pop("PYTHONPATH", None)
    build_env["PYTHONNOUSERSITE"] = "1"
    try:
        subprocess.run(command, check=True, env=build_env)
    except subprocess.CalledProcessError as error:
        raise ServiceBundleError(
            f"offline install failed for {service}; verify every exact wheel and hash in "
            f"{service_input} (pip exit {error.returncode})"
        ) from error
    installed_wheel_tags = _verify_site_package_wheel_tags(site_packages, target_profile)
    source_wheel_tags = _validate_source_wheel_tags(
        actual_source_wheel_tags,
        target_profile,
        installed_wheel_tags,
    )

    distribution_check = (
        "import importlib.metadata as m,sys\n"
        f"sys.path.insert(0,{str(site_packages)!r})\n"
        f"required={(*REQUIRED_DISTRIBUTIONS[service], RUNTIME_SDK_DISTRIBUTION)!r}\n"
        "missing=[name for name in required if not any("
        "d.metadata.get('Name','').lower().replace('_','-') == name for d in m.distributions())]\n"
        "if missing:\n"
        "    raise SystemExit('missing installed distributions: '+', '.join(missing))\n"
    )
    subprocess.run([str(python_executable), "-I", "-c", distribution_check], check=True)

    (bundle_root / "requirements.lock").write_bytes(lock_path.read_bytes())
    (bundle_root / "source.json").write_bytes(source_path.read_bytes())
    if execution_runtime is not None:
        shutil.copytree(execution_runtime_root, bundle_root / "execution-runtime")
    if service == "navigator":
        shutil.copy2(service_input / "serve-web.py", bundle_root / "serve-web.py")
    entrypoint = bundle_root / "run-service"
    entrypoint.write_text(
        _entrypoint_text(
            service,
            target_profile["pythonExecutable"],
            execution_runtime=execution_runtime,
        ),
        encoding="utf-8",
    )
    entrypoint.chmod(0o755)
    _write_bundle_manifest(
        bundle_root,
        service=service,
        source_commit=source_commit,
        source_repository=source_repository,
        target_profile_id=target_profile_id,
        target_profile=target_profile,
        source_wheel_tags=source_wheel_tags,
        installed_wheel_tags=installed_wheel_tags,
        runtime_dependencies=runtime_dependencies,
        execution_runtime=execution_runtime,
    )
    manifest = validate_bundle(
        bundle_root,
        expected_service=service,
        expected_target_profile=target_profile_id,
    )
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
    target_profile_id: str,
    debian_arch: str,
    python_executable: Path,
    service: str | None = None,
    service_commit_override: str | None = None,
) -> list[Path]:
    """Build one or all self-contained runtime bundles from pinned wheelhouses."""

    selected_services = (service,) if service is not None else SERVICES
    if any(name not in SERVICES for name in selected_services):
        raise ServiceBundleError(f"unsupported service name: {service!r}")
    if service_commit_override is not None and service is None:
        raise ServiceBundleError("--service-commit requires --service")
    if (
        service_commit_override is not None
        and COMMIT_PATTERN.fullmatch(service_commit_override) is None
    ):
        raise ServiceBundleError("--service-commit must be a lowercase 40-character Git SHA")

    wheelhouse = Path(wheelhouse_root)
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise ServiceBundleError(
            f"service wheelhouse is missing: {wheelhouse}; see packaging/service-bundle.md"
        )
    entries = sorted(wheelhouse.iterdir())
    if [entry.name for entry in entries] != sorted(selected_services) or any(
        entry.is_symlink() or not entry.is_dir() for entry in entries
    ):
        raise ServiceBundleError(
            "service wheelhouse root must contain exactly these selected service directories: "
            + ", ".join(selected_services)
        )
    target_profile = _native_python_profile(Path(release_lock_path), target_profile_id)
    _require_profile_host(target_profile)
    _validate_python_input(target_profile, Path(release_lock_path))
    if debian_arch != "amd64":
        raise ServiceBundleError(
            "release-lock.json supports Ubuntu 22.04/24.04 x86_64 (amd64) only; "
            f"refusing service bundle target {debian_arch}"
        )
    host_arch = _debian_arch_for_host()
    if debian_arch != host_arch:
        raise ServiceBundleError(
            f"cross-architecture service bundles are unsupported: requested {debian_arch}, "
            f"build host is {host_arch}"
        )
    python = Path(python_executable)
    version = _python_runtime_version(python)
    if version != target_profile["pythonVersion"]:
        raise ServiceBundleError(
            f"build Python must be CPython {target_profile['pythonVersion']}, got {version}"
        )
    lock = _read_json_object(Path(release_lock_path), "Workspace release-lock.json")
    repositories = lock.get("repositories")
    if not isinstance(repositories, dict):
        raise ServiceBundleError("release-lock.json has no repositories object")

    output = Path(output_root)
    output.mkdir(parents=True, exist_ok=True)
    build_root = Path(tempfile.mkdtemp(prefix="cyrene-wheel-installer-"))
    try:
        builder_env = build_root / "venv"
        python_env = os.environ.copy()
        python_env.pop("PYTHONHOME", None)
        python_env.pop("PYTHONPATH", None)
        python_env["PYTHONNOUSERSITE"] = "1"
        try:
            subprocess.run(
                [str(python), "-m", "venv", "--clear", str(builder_env)],
                check=True,
                capture_output=True,
                text=True,
                env=python_env,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise ServiceBundleError(
                "could not create an isolated environment from the pinned build interpreter"
            ) from error
        builder_python = builder_env / "bin" / "python"
        results: list[Path] = []
        for service_name in selected_services:
            repository = REPOSITORIES[service_name]
            commit = (
                service_commit_override
                if service_name == service and service_commit_override
                else repositories.get(repository)
            )
            if not isinstance(commit, str) or not COMMIT_PATTERN.fullmatch(commit):
                raise ServiceBundleError(
                    f"release-lock.json has no immutable source commit for {repository}"
                )
            results.append(
                _build_one_bundle(
                    service=service_name,
                    wheelhouse=wheelhouse,
                    output_root=output,
                    source_commit=commit,
                    source_repository=repository,
                    target_profile_id=target_profile_id,
                    target_profile=target_profile,
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
        target_profile_id=arguments.target_profile,
        debian_arch=arguments.arch,
        python_executable=arguments.python_executable,
        service=arguments.service,
        service_commit_override=arguments.service_commit,
    )
    for bundle in results:
        manifest = validate_bundle(bundle, release_lock_path=arguments.release_lock)
        print(f"{manifest['service']}: {bundle} ({manifest['version']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and manage Cyrene service release bundles")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="build one or all bundles from an offline wheelhouse")
    build.add_argument("--wheelhouse", type=Path, required=True)
    build.add_argument("--release-lock", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--arch", required=True)
    build.add_argument("--target-profile", required=True)
    build.add_argument("--python-executable", type=Path, required=True)
    build.add_argument(
        "--service", choices=SERVICES, help="build only this independent Product bundle"
    )
    build.add_argument(
        "--service-commit",
        help="override release-lock.json for the selected service with this exact 40-character source SHA",
    )
    build.set_defaults(handler=_build_command)
    arguments = parser.parse_args(argv)
    try:
        return arguments.handler(arguments)
    except (ServiceBundleError, OSError, subprocess.CalledProcessError) as error:
        print(f"service-bundle: ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
