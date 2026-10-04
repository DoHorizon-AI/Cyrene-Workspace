#!/usr/bin/env python3
"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 python_runtime.py                                                │
│  Module: packaging.python_runtime                                    │
│  Role: Verify and stage Cyrene's locked private CPython runtime.      │
│                                                                      │
│  模块职责：校验并准备 Cyrene 固定的私有 CPython 运行时                   │
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
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_LOCK = Path(__file__).resolve().with_name("python-runtime.lock.json")
DEFAULT_RELEASE_LOCK = Path(__file__).resolve().parents[1] / "release-lock.json"
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_RUNTIME_UNPACKED_BYTES = 1024 * 1024 * 1024
ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "api.github.com",
        "files.pythonhosted.org",
        "github.com",
        "objects.githubusercontent.com",
        "raw.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "uploads.github.com",
    }
)


class PythonRuntimeError(RuntimeError):
    """A fail-closed lock, provenance, archive, or staging error."""


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    """Read one UTF-8 JSON object and translate parse errors for CLI output."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PythonRuntimeError(f"cannot read {label} at {path}: {error}") from error
    if not isinstance(value, dict):
        raise PythonRuntimeError(f"{label} must contain a JSON object: {path}")
    return value


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PythonRuntimeError(f"{label} must be a JSON object")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PythonRuntimeError(f"{label} must be a non-empty string")
    return value


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or HEX_SHA256.fullmatch(value) is None:
        raise PythonRuntimeError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _safe_relative_path(value: Any, label: str) -> Path:
    raw = _require_string(value, label)
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise PythonRuntimeError(f"{label} must be a normalized relative path")
    return Path(*path.parts)


def _normalized_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_requirements_lock(path: Path) -> dict[str, tuple[str, str]]:
    """Read the deliberately narrow, one-hash-per-wheel pip lock format."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise PythonRuntimeError(f"cannot read runtime requirements lock {path}: {error}") from error

    pins: dict[str, tuple[str, str]] = {}
    index = 0
    while index < len(lines):
        requirement = lines[index].strip()
        index += 1
        if not requirement or requirement.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9][A-Za-z0-9_.+-]*) \\", requirement)
        if match is None or index >= len(lines):
            raise PythonRuntimeError(
                f"runtime requirements lock must use exact pins followed by one SHA-256: {requirement}"
            )
        hash_match = re.fullmatch(r"\s*--hash=sha256:([0-9a-f]{64})", lines[index])
        if hash_match is None:
            raise PythonRuntimeError(f"runtime requirements lock has an invalid hash for {match.group(1)}")
        index += 1
        name = _normalized_distribution(match.group(1))
        if name in pins:
            raise PythonRuntimeError(f"runtime requirements lock repeats distribution {name}")
        pins[name] = (match.group(2), hash_match.group(1))
    return pins


def validate_lock(lock: dict[str, Any], lock_path: Path) -> tuple[dict[str, Any], Path]:
    """Validate the complete runtime, uv mapping, and offline wheel lock."""

    if lock.get("schemaVersion") != 1:
        raise PythonRuntimeError("python runtime lock has an unsupported schemaVersion")
    python = _require_object(lock.get("python"), "python")
    if python.get("implementation") != "CPython" or python.get("version") != "3.12.14":
        raise PythonRuntimeError("the private Python pin must remain CPython 3.12.14")
    if python.get("distribution") != "astral-sh/python-build-standalone":
        raise PythonRuntimeError("the private Python provider must remain Astral python-build-standalone")
    if python.get("target") != "x86_64-unknown-linux-gnu":
        raise PythonRuntimeError("the private Python target must remain x86_64-unknown-linux-gnu")

    install_root = _require_string(python.get("installRoot"), "python.installRoot")
    executable = _require_string(python.get("executable"), "python.executable")
    if install_root != "/opt/cyrene/python/3.12.14":
        raise PythonRuntimeError("python.installRoot must be /opt/cyrene/python/3.12.14")
    if executable != f"{install_root}/bin/python3.12":
        raise PythonRuntimeError("python.executable must point into the locked installRoot")
    if python.get("archiveRoot") != "python" or python.get("archiveStripComponents") != 1:
        raise PythonRuntimeError("the Python archive must have one leading python/ directory")
    if python.get("bundledPipVersion") != "26.2.1":
        raise PythonRuntimeError("the bundled pip version must be pinned with the Python archive")

    archive = _require_object(python.get("archive"), "python.archive")
    archive_name = _require_string(archive.get("name"), "python.archive.name")
    archive_url = _require_string(archive.get("url"), "python.archive.url")
    archive_sha = _require_sha256(archive.get("sha256"), "python.archive.sha256")
    if not isinstance(archive.get("size"), int) or isinstance(archive.get("size"), bool):
        raise PythonRuntimeError("python.archive.size must be an integer")
    release = _require_object(archive.get("release"), "python.archive.release")
    if (
        release.get("repository") != "astral-sh/python-build-standalone"
        or release.get("tag") != "20260929"
        or release.get("commit") != "b498734a5791d0e6786695a226fd398a41c6f7f6"
        or release.get("immutable") is not True
        or release.get("apiUrl")
        != "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/20260929"
    ):
        raise PythonRuntimeError("Python archive provenance must match the immutable Astral release")
    parsed_archive_url = urllib.parse.urlsplit(archive_url)
    if (
        parsed_archive_url.scheme != "https"
        or parsed_archive_url.hostname != "github.com"
        or parsed_archive_url.path.rsplit("/", 1)[-1] != urllib.parse.quote(archive_name, safe="")
    ):
        raise PythonRuntimeError("python.archive.url must directly identify its locked GitHub release asset")
    if release.get("assetApiUrl") != "https://api.github.com/repos/astral-sh/python-build-standalone/releases/assets/598637702":
        raise PythonRuntimeError("Python archive asset API identity differs from the official immutable asset")
    if release.get("assetId") != 598637702:
        raise PythonRuntimeError("Python archive asset ID differs from the official immutable asset")

    resolver = _require_object(lock.get("buildResolver"), "buildResolver")
    if resolver.get("tool") != "uv" or resolver.get("version") != "0.12.21":
        raise PythonRuntimeError("the pinned Python resolver and trainer tool must remain uv 0.12.21")
    if (
        resolver.get("installedPath") != "/opt/cyrene/uv/0.12.21/uv"
        or resolver.get("usage")
        != {
            "build": "resolve-frozen-python-distribution-mapping",
            "runtime": "explicit-trainer-environment-prepare",
            "automaticRuntimeBootstrap": False,
        }
    ):
        raise PythonRuntimeError("the locked uv runtime path and explicit-use policy are invalid")
    uv_archive = _require_object(resolver.get("binaryArchive"), "buildResolver.binaryArchive")
    if uv_archive.get("name") != "uv-x86_64-unknown-linux-gnu.tar.gz":
        raise PythonRuntimeError("the build resolver must use the pinned uv GNU x86_64 archive")
    uv_executable_sha = _require_sha256(
        uv_archive.get("executableSha256"), "buildResolver.binaryArchive.executableSha256"
    )
    if uv_executable_sha != "e8a4e7b4fd6283892fccfc4f335fb16cdf01064bb135172e4e2e73b478eb2076":
        raise PythonRuntimeError("the uv executable digest differs from its verified official archive")
    uv_release = _require_object(uv_archive.get("release"), "buildResolver.binaryArchive.release")
    if (
        uv_release.get("repository") != "astral-sh/uv"
        or uv_release.get("tag") != "0.12.21"
        or uv_release.get("commit") != "7af826859382eb191e47467540850caa8f493e5b"
        or uv_release.get("immutable") is not True
    ):
        raise PythonRuntimeError("uv resolver provenance must match the immutable uv 0.12.21 release")
    if uv_release.get("assetId") != 599238020:
        raise PythonRuntimeError("uv asset ID differs from the official immutable asset")
    if (
        uv_release.get("apiUrl")
        != "https://api.github.com/repos/astral-sh/uv/releases/tags/0.12.21"
        or uv_release.get("assetApiUrl")
        != "https://api.github.com/repos/astral-sh/uv/releases/assets/599238020"
        or uv_archive.get("url")
        != "https://github.com/astral-sh/uv/releases/download/0.12.21/uv-x86_64-unknown-linux-gnu.tar.gz"
        or uv_archive.get("sha256")
        != "23f02075b652bb1df64178cfae41b5caf160822e720e2663568f3f5d63bc52c0"
        or uv_archive.get("size") != 19782662
    ):
        raise PythonRuntimeError("uv binary archive metadata differs from its official immutable asset")
    mapping = _require_object(resolver.get("distributionMetadata"), "buildResolver.distributionMetadata")
    mapping_url = _require_string(mapping.get("url"), "buildResolver.distributionMetadata.url")
    mapping_sha = _require_sha256(mapping.get("sha256"), "buildResolver.distributionMetadata.sha256")
    if (
        mapping_url
        != "https://raw.githubusercontent.com/astral-sh/uv/7af826859382eb191e47467540850caa8f493e5b/crates/uv-python/download-metadata.json"
        or mapping_sha != "6167f194053b58a461b6b440bbfff121f0971bd6757fce95cfec90b19ee080df"
        or mapping.get("mappingKey") != "cpython-3.12.14-linux-x86_64-gnu"
    ):
        raise PythonRuntimeError("uv Python download metadata must be pinned to the immutable 0.12.21 source")
    selected_mapping = _require_object(mapping.get("mapping"), "buildResolver.distributionMetadata.mapping")
    if (
        selected_mapping.get("url") != archive_url
        or selected_mapping.get("sha256") != archive_sha
        or selected_mapping.get("build") != "20260929"
        or selected_mapping.get("major") != 3
        or selected_mapping.get("minor") != 12
        or selected_mapping.get("patch") != 14
        or selected_mapping.get("os") != "linux"
        or selected_mapping.get("libc") != "gnu"
        or selected_mapping.get("arch") != {"family": "x86_64", "variant": None}
    ):
        raise PythonRuntimeError("uv's pinned distribution mapping does not match the archive pin")

    dependencies = _require_object(lock.get("runtimeDependencies"), "runtimeDependencies")
    system_packages = dependencies.get("systemPackages")
    if system_packages != ["ca-certificates", "libcrypt1", "libgcc-s1"]:
        raise PythonRuntimeError(
            "runtimeDependencies.systemPackages must list the private runtime's pinned Ubuntu prerequisites"
        )
    requirements_relative = _safe_relative_path(
        dependencies.get("requirementsPath"), "runtimeDependencies.requirementsPath"
    )
    requirements_path = lock_path.resolve().parent / requirements_relative
    requirements_hash = _require_sha256(
        dependencies.get("requirementsSha256"), "runtimeDependencies.requirementsSha256"
    )
    actual_requirements_hash = _sha256_file(requirements_path)
    if actual_requirements_hash != requirements_hash:
        raise PythonRuntimeError("runtime requirements lock SHA-256 does not match python-runtime.lock.json")
    wheels = dependencies.get("wheels")
    if not isinstance(wheels, list) or not wheels:
        raise PythonRuntimeError("runtimeDependencies.wheels must be a non-empty list")
    pins = _parse_requirements_lock(requirements_path)
    expected_names: set[str] = set()
    for index, raw_wheel in enumerate(wheels):
        wheel = _require_object(raw_wheel, f"runtimeDependencies.wheels[{index}]")
        name = _normalized_distribution(_require_string(wheel.get("name"), "wheel.name"))
        version = _require_string(wheel.get("version"), f"runtimeDependencies.wheels[{index}].version")
        filename = _require_string(wheel.get("filename"), f"runtimeDependencies.wheels[{index}].filename")
        url = _require_string(wheel.get("url"), f"runtimeDependencies.wheels[{index}].url")
        digest = _require_sha256(wheel.get("sha256"), f"runtimeDependencies.wheels[{index}].sha256")
        if not isinstance(wheel.get("size"), int) or isinstance(wheel.get("size"), bool):
            raise PythonRuntimeError(f"runtimeDependencies.wheels[{index}].size must be an integer")
        parsed_url = urllib.parse.urlsplit(url)
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != "files.pythonhosted.org"
            or parsed_url.path.rsplit("/", 1)[-1] != filename
        ):
            raise PythonRuntimeError(f"runtime wheel URL must identify the locked PyPI wheel: {filename}")
        if name in expected_names:
            raise PythonRuntimeError(f"runtime dependency wheel is repeated: {name}")
        expected_names.add(name)
        if pins.get(name) != (version, digest):
            raise PythonRuntimeError(f"runtime wheel pin differs from requirements lock: {name}=={version}")
    if set(pins) != expected_names:
        raise PythonRuntimeError("requirements lock and runtime wheel list contain different distributions")

    payload = _require_object(lock.get("payload"), "payload")
    for field in (
        "runtimeArchivePath",
        "requirementsPath",
        "lockPath",
        "wheelDirectory",
        "verificationRecordPath",
    ):
        _safe_relative_path(payload.get(field), f"payload.{field}")
    return python, requirements_path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise PythonRuntimeError(f"cannot read file for SHA-256 at {path}: {error}") from error
    return digest.hexdigest()


def _read_limited_url(url: str, *, limit: int = MAX_METADATA_BYTES) -> bytes:
    """Fetch one pinned HTTPS source and reject redirects outside official hosts."""

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
        raise PythonRuntimeError(f"refusing non-official download URL: {url}")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json, application/json, application/octet-stream",
            "User-Agent": "CyrenePrivatePythonBootstrap/1",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            final_host = urllib.parse.urlsplit(response.geturl()).hostname
            if final_host not in ALLOWED_DOWNLOAD_HOSTS:
                raise PythonRuntimeError(f"official download redirected to an untrusted host: {final_host}")
            content = response.read(limit + 1)
    except (urllib.error.URLError, TimeoutError) as error:
        raise PythonRuntimeError(f"download failed for {url}: {error}") from error
    if len(content) > limit:
        raise PythonRuntimeError(f"download exceeds the {limit}-byte limit: {url}")
    return content


def _read_release_metadata(url: str, *, repository: str, tag: str, commit: str) -> dict[str, Any]:
    content = _read_limited_url(url)
    try:
        release = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PythonRuntimeError(f"invalid GitHub release metadata from {url}: {error}") from error
    release = _require_object(release, f"GitHub release metadata {repository}@{tag}")
    if (
        release.get("tag_name") != tag
        or release.get("target_commitish") != commit
        or release.get("immutable") is not True
    ):
        raise PythonRuntimeError(f"GitHub release {repository}@{tag} is not the pinned immutable release")
    return release


def _find_asset(release: dict[str, Any], *, asset_id: int, name: str, url: str, sha256: str, size: int) -> None:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise PythonRuntimeError("GitHub release metadata has no assets list")
    for raw_asset in assets:
        if not isinstance(raw_asset, dict) or raw_asset.get("id") != asset_id:
            continue
        if (
            raw_asset.get("name") != name
            or raw_asset.get("browser_download_url") != url
            or raw_asset.get("digest") != f"sha256:{sha256}"
            or raw_asset.get("size") != size
        ):
            raise PythonRuntimeError(f"GitHub asset {asset_id} metadata differs from the runtime lock")
        return
    raise PythonRuntimeError(f"GitHub release does not contain the locked asset {name}")


def _verified_download(url: str, destination: Path, *, sha256: str, size: int) -> Path:
    """Download a fixed artifact through an atomic cache path, then verify bytes."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size == size and _sha256_file(destination) == sha256:
        return destination
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".partial", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": "CyrenePrivatePythonBootstrap/1",
            },
        )
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(temporary_fd, "wb") as output:
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    final_host = urllib.parse.urlsplit(response.geturl()).hostname
                    if final_host not in ALLOWED_DOWNLOAD_HOSTS:
                        raise PythonRuntimeError(
                            f"official artifact redirected to an untrusted host: {final_host}"
                        )
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        digest.update(chunk)
                        total += len(chunk)
                        if total > size:
                            raise PythonRuntimeError(f"download exceeds its locked size: {url}")
            except (urllib.error.URLError, TimeoutError) as error:
                raise PythonRuntimeError(f"download failed for {url}: {error}") from error
            output.flush()
            os.fsync(output.fileno())
        if total != size:
            raise PythonRuntimeError(f"download size mismatch for {url}: expected {size}, received {total}")
        if digest.hexdigest() != sha256:
            raise PythonRuntimeError(f"download SHA-256 mismatch for {url}")
        os.replace(temporary, destination)
        return destination
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _verified_local_or_download(
    source: Path | None, url: str, destination: Path, *, sha256: str, size: int
) -> Path:
    if source is None:
        return _verified_download(url, destination, sha256=sha256, size=size)
    if source.is_symlink() or not source.is_file():
        raise PythonRuntimeError(f"locked artifact input is not a regular non-symlink file: {source}")
    if source.stat().st_size != size or _sha256_file(source) != sha256:
        raise PythonRuntimeError(f"locked artifact input has a size or SHA-256 mismatch: {source}")
    if source.resolve() == destination.resolve():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return destination


def _resolve_uv(
    resolver: dict[str, Any], work_dir: Path, explicit_executable: Path | None
) -> Path:
    """Use the pinned uv binary to resolve the local frozen distribution map."""

    asset = _require_object(resolver.get("binaryArchive"), "buildResolver.binaryArchive")
    expected_sha256 = _require_sha256(
        asset.get("executableSha256"), "buildResolver.binaryArchive.executableSha256"
    )
    if explicit_executable is not None:
        uv_executable = explicit_executable
    else:
        archive_path = _verified_download(
            asset["url"],
            work_dir / "downloads" / asset["name"],
            sha256=asset["sha256"],
            size=asset["size"],
        )
        _verify_uv_release(resolver)
        uv_dir = work_dir / "uv"
        cached_executable = uv_dir / asset["executable"]
        if (
            cached_executable.is_file()
            and _sha256_file(cached_executable) == expected_sha256
            and _uv_version(cached_executable) == resolver["version"]
        ):
            uv_executable = cached_executable
        else:
            uv_executable = _extract_uv_binary(archive_path, asset, uv_dir)
    if not uv_executable.is_file() or not os.access(uv_executable, os.X_OK):
        raise PythonRuntimeError(f"uv executable is missing or not executable: {uv_executable}")
    actual_version = _uv_version(uv_executable)
    if _sha256_file(uv_executable) != expected_sha256:
        raise PythonRuntimeError("uv executable SHA-256 differs from the verified official binary")
    if actual_version != resolver.get("version"):
        raise PythonRuntimeError(
            f"build resolver must be uv {resolver.get('version')}; found uv {actual_version} at {uv_executable}"
        )
    return uv_executable.resolve()


def _uv_version(executable: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = re.match(r"^uv ([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)", result.stdout.strip())
    return match.group(1) if match else None


def _verify_uv_release(resolver: dict[str, Any]) -> None:
    asset = _require_object(resolver.get("binaryArchive"), "buildResolver.binaryArchive")
    release = _require_object(asset.get("release"), "buildResolver.binaryArchive.release")
    metadata = _read_release_metadata(
        release["apiUrl"],
        repository=release["repository"],
        tag=release["tag"],
        commit=release["commit"],
    )
    _find_asset(
        metadata,
        asset_id=asset["release"]["assetId"],
        name=asset["name"],
        url=asset["url"],
        sha256=asset["sha256"],
        size=asset["size"],
    )


def _extract_uv_binary(archive_path: Path, asset: dict[str, Any], destination: Path) -> Path:
    if destination.exists() or destination.is_symlink():
        raise PythonRuntimeError(f"uv extraction destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    expected_root = _require_string(asset.get("archiveRoot"), "uv archiveRoot")
    expected_executable = _safe_relative_path(asset.get("executable"), "uv executable")
    if expected_executable.parts[0] != expected_root:
        raise PythonRuntimeError("uv executable path does not belong to its archive root")
    expected = Path(*expected_executable.parts)
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            if not members:
                raise PythonRuntimeError("uv resolver archive is empty")
            for member in members:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != expected_root:
                    raise PythonRuntimeError(f"uv resolver archive contains an unsafe path: {member.name}")
                if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    raise PythonRuntimeError(f"uv resolver archive contains a non-regular entry: {member.name}")
            for member in members:
                if member.isdir():
                    (temporary / Path(*PurePosixPath(member.name).parts)).mkdir(
                        parents=True, exist_ok=True
                    )
                else:
                    target = temporary / Path(*PurePosixPath(member.name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise PythonRuntimeError(f"cannot read uv executable archive entry {member.name}")
                    with source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    target.chmod(0o755)
    except BaseException as error:
        shutil.rmtree(temporary, ignore_errors=True)
        if isinstance(error, PythonRuntimeError):
            raise
        if isinstance(error, (OSError, tarfile.TarError)):
            raise PythonRuntimeError(f"cannot unpack pinned uv resolver archive: {error}") from error
        raise
    executable = temporary / expected
    if not executable.is_file():
        shutil.rmtree(temporary, ignore_errors=True)
        raise PythonRuntimeError(f"pinned uv archive lacks {expected}")
    expected_sha256 = _require_sha256(
        asset.get("executableSha256"), "buildResolver.binaryArchive.executableSha256"
    )
    if _sha256_file(executable) != expected_sha256:
        shutil.rmtree(temporary, ignore_errors=True)
        raise PythonRuntimeError("extracted uv executable differs from the verified official binary")
    temporary.rename(destination)
    return destination / expected


def _stage_uv_runtime(executable: Path, resolver: dict[str, Any], stage_root: Path) -> Path:
    """Copy the verified uv executable to its immutable package runtime path."""
    installed_path = _require_string(resolver.get("installedPath"), "buildResolver.installedPath")
    destination = stage_root.joinpath(
        *_safe_relative_path(installed_path.lstrip("/"), "buildResolver.installedPath").parts
    )
    uv_root = destination.parent.parent
    if uv_root.exists() or uv_root.is_symlink():
        raise PythonRuntimeError(f"locked uv runtime staging output already exists: {uv_root}")
    if executable.is_symlink() or not executable.is_file():
        raise PythonRuntimeError(f"verified uv executable is missing or unsafe: {executable}")

    asset = _require_object(resolver.get("binaryArchive"), "buildResolver.binaryArchive")
    expected_digest = _require_sha256(
        asset.get("executableSha256"), "buildResolver.binaryArchive.executableSha256"
    )
    uv_root.mkdir(parents=True, exist_ok=False)
    try:
        destination.parent.mkdir(mode=0o755)
        shutil.copyfile(executable, destination)
        destination.chmod(0o755)
        if _sha256_file(destination) != expected_digest:
            raise PythonRuntimeError("staged uv runtime executable differs from its locked SHA-256")
        if _uv_version(destination) != resolver.get("version"):
            raise PythonRuntimeError("staged uv runtime executable has an unexpected version")
    except (OSError, PythonRuntimeError):
        shutil.rmtree(uv_root, ignore_errors=True)
        raise
    return destination


def _verify_release_assets(lock: dict[str, Any]) -> None:
    python = lock["python"]
    archive = python["archive"]
    pbs_release = archive["release"]
    pbs_metadata = _read_release_metadata(
        pbs_release["apiUrl"],
        repository=pbs_release["repository"],
        tag=pbs_release["tag"],
        commit=pbs_release["commit"],
    )
    _find_asset(
        pbs_metadata,
        asset_id=pbs_release["assetId"],
        name=archive["name"],
        url=archive["url"],
        sha256=archive["sha256"],
        size=archive["size"],
    )
    _verify_uv_release(lock["buildResolver"])


def _load_official_uv_mapping(resolver: dict[str, Any], destination: Path) -> dict[str, Any]:
    source = _require_object(resolver.get("distributionMetadata"), "buildResolver.distributionMetadata")
    content = _read_limited_url(source["url"], limit=source["size"])
    if len(content) != source["size"] or hashlib.sha256(content).hexdigest() != source["sha256"]:
        raise PythonRuntimeError("uv 0.12.21 Python distribution metadata SHA-256 mismatch")
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PythonRuntimeError(f"uv Python distribution metadata is invalid JSON: {error}") from error
    document = _require_object(document, "uv Python distribution metadata")
    key = source["mappingKey"]
    expected_mapping = _require_object(source.get("mapping"), "locked uv Python distribution mapping")
    actual_mapping = document.get(key)
    if actual_mapping != expected_mapping:
        raise PythonRuntimeError("uv 0.12.21 official mapping differs from python-runtime.lock.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({key: expected_mapping}, sort_keys=True) + "\n", encoding="utf-8")
    return {key: expected_mapping}


def _resolve_frozen_mapping(uv_executable: Path, mapping_path: Path, resolver: dict[str, Any]) -> None:
    """Ask uv 0.12.21 to resolve the one-entry local mapping without network access."""

    source = resolver["distributionMetadata"]
    key = source["mappingKey"]
    expected = source["mapping"]
    base = "https://github.com/astral-sh/python-build-standalone/releases/download"
    env = dict(os.environ)
    env.update(
        {
            "UV_PYTHON_INSTALL_MIRROR": base,
            "UV_NO_CONFIG": "1",
            "UV_PYTHON_DOWNLOADS": "manual",
            "UV_CACHE_DIR": str(mapping_path.parent / "uv-cache"),
        }
    )
    command = [
        str(uv_executable),
        "python",
        "list",
        "--all-versions",
        "--only-downloads",
        "--show-urls",
        "--output-format",
        "json",
        "--python-downloads-json-url",
        mapping_path.resolve().as_uri(),
        "3.12.14",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PythonRuntimeError(f"uv could not resolve the frozen Python mapping: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
        raise PythonRuntimeError(f"uv could not resolve the frozen Python mapping: {detail}")
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise PythonRuntimeError("uv returned invalid JSON while resolving its frozen Python mapping") from error
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise PythonRuntimeError("uv did not resolve exactly one pinned CPython distribution")
    resolved = entries[0]
    if (
        resolved.get("key") != key
        or resolved.get("version") != "3.12.14"
        or resolved.get("url") != expected["url"]
        or resolved.get("implementation") != "cpython"
        or resolved.get("arch") != "x86_64"
        or resolved.get("libc") != "gnu"
    ):
        raise PythonRuntimeError("uv's resolved distribution differs from the official pinned mapping")


def _validate_profile(release_lock_path: Path, lock_path: Path, target_profile: str, lock: dict[str, Any]) -> None:
    release_lock = _read_json_object(release_lock_path, "release-lock.json")
    profiles = _require_object(release_lock.get("nativePythonProfiles"), "release-lock.nativePythonProfiles")
    profile = _require_object(profiles.get(target_profile), f"release-lock.nativePythonProfiles.{target_profile}")
    relative_lock = _require_string(profile.get("pythonInput"), f"{target_profile}.pythonInput")
    resolved_lock = (release_lock_path.resolve().parent / relative_lock).resolve()
    if resolved_lock != lock_path.resolve():
        raise PythonRuntimeError(
            f"{target_profile}.pythonInput does not reference the provided Python runtime lock"
        )
    runtime = lock["python"]
    if profile.get("pythonVersion") != runtime["version"] or profile.get("pythonExecutable") != runtime["executable"]:
        raise PythonRuntimeError(f"{target_profile} does not match the private Python runtime pin")
    os_version = profile.get("osVersion")
    expected_abi = {"22.04": "glibc-2.35", "24.04": "glibc-2.39"}.get(os_version)
    if (
        target_profile != f"linux-ubuntu-{os_version}-x86_64-python-3.12"
        or profile.get("os") != "linux"
        or profile.get("distribution") != "ubuntu"
        or profile.get("distributionVersion") != os_version
        or profile.get("architecture") != "x86_64"
        or profile.get("abi") != expected_abi
        or profile.get("runtime") != "python:3.12"
    ):
        raise PythonRuntimeError(f"{target_profile} does not describe a supported Ubuntu Python target")
    try:
        host = platform.freedesktop_os_release()
    except (AttributeError, OSError) as error:
        raise PythonRuntimeError(f"cannot verify the Python build host profile: {error}") from error
    if (
        host.get("ID") != "ubuntu"
        or host.get("VERSION_ID") != os_version
        or platform.machine().lower() not in {"x86_64", "amd64"}
    ):
        raise PythonRuntimeError(
            f"build host does not match {target_profile}: "
            f"{host.get('ID', 'unknown')} {host.get('VERSION_ID', 'unknown')} {platform.machine()}"
        )
    libc_name, libc_version = platform.libc_ver()
    if libc_name != "glibc" or libc_version != profile["abi"].removeprefix("glibc-"):
        raise PythonRuntimeError(
            f"build host glibc does not match {target_profile}: {libc_name} {libc_version}"
        )
    resolver = _require_object(profile.get("wheelResolver"), f"{target_profile}.wheelResolver")
    if resolver.get("tool") != "uv" or resolver.get("version") != lock["buildResolver"]["version"]:
        raise PythonRuntimeError(f"{target_profile} does not use the locked uv resolver")


def _validate_archive_paths(archive_path: Path, *, root_name: str) -> tuple[list[tarfile.TarInfo], int]:
    """Reject absolute, traversing, special, duplicate, or escaping archive members."""

    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            if not members:
                raise PythonRuntimeError("CPython archive is empty")
            seen: set[str] = set()
            symlinks: list[tuple[PurePosixPath, PurePosixPath]] = []
            unpacked_size = 0
            for member in members:
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                    raise PythonRuntimeError(f"CPython archive contains an unsafe path: {member.name}")
                if relative.parts[0] != root_name:
                    raise PythonRuntimeError(
                        f"CPython archive has an unexpected root path: {member.name}"
                    )
                if len(relative.parts) == 1 and not member.isdir():
                    raise PythonRuntimeError("CPython archive root must be a directory")
                normalized_name = relative.as_posix()
                if normalized_name in seen:
                    raise PythonRuntimeError(f"CPython archive repeats a path: {member.name}")
                seen.add(normalized_name)
                if member.isdir():
                    continue
                if member.isfile():
                    if member.size < 0:
                        raise PythonRuntimeError(
                            f"CPython archive has a negative file size: {member.name}"
                        )
                    unpacked_size += member.size
                    continue
                if member.issym():
                    link = PurePosixPath(member.linkname)
                    if link.is_absolute():
                        raise PythonRuntimeError(
                            f"CPython archive has an absolute symlink: {member.name}"
                        )
                    combined = PurePosixPath(os.path.normpath(str(relative.parent / link)))
                    if (
                        combined.is_absolute()
                        or ".." in combined.parts
                        or not combined.parts
                        or combined.parts[0] != root_name
                    ):
                        raise PythonRuntimeError(
                            f"CPython archive symlink escapes its root: {member.name}"
                        )
                    symlinks.append((relative, link))
                    continue
                raise PythonRuntimeError(f"CPython archive contains a special file: {member.name}")
            if unpacked_size > MAX_RUNTIME_UNPACKED_BYTES:
                raise PythonRuntimeError("CPython archive unpacked size exceeds the safety limit")
            symlink_names = {entry.as_posix() for entry, _ in symlinks}
            for member in members:
                parent = PurePosixPath(member.name).parent
                while parent.parts and parent.as_posix() != root_name:
                    if parent.as_posix() in symlink_names:
                        raise PythonRuntimeError(
                            f"CPython archive places a member under a symlink: {member.name}"
                        )
                    parent = parent.parent
            return members, unpacked_size
    except (OSError, tarfile.TarError) as error:
        raise PythonRuntimeError(f"cannot inspect CPython archive: {error}") from error


def _extract_python_archive(archive_path: Path, destination: Path, *, root_name: str) -> None:
    """Extract the verified Python tree while deferring safe symlinks until last."""

    members, _ = _validate_archive_paths(archive_path, root_name=root_name)
    destination.mkdir(parents=True, exist_ok=False)
    symlinks: list[tarfile.TarInfo] = []
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in members:
                path = PurePosixPath(member.name)
                stripped_parts = path.parts[1:]
                if not stripped_parts:
                    continue
                target = destination.joinpath(*stripped_parts)
                if member.issym():
                    symlinks.append(member)
                    continue
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(0o755)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise PythonRuntimeError(f"cannot read CPython archive file {member.name}")
                mode = 0o755 if member.mode & 0o111 else 0o644
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
                with source, os.fdopen(descriptor, "wb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                target.chmod(mode)
        for member in symlinks:
            path = PurePosixPath(member.name)
            target = destination.joinpath(*path.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(member.linkname, target)
        resolved_root = destination.resolve()
        for current, directory_names, _ in os.walk(destination, followlinks=False):
            current_path = Path(current)
            for name in directory_names:
                entry = current_path / name
                if entry.is_symlink() and not entry.resolve(strict=False).is_relative_to(resolved_root):
                    raise PythonRuntimeError(f"CPython archive contains an escaping symlink: {entry}")
            for entry in current_path.iterdir():
                if entry.is_symlink() and not entry.resolve(strict=False).is_relative_to(resolved_root):
                    raise PythonRuntimeError(f"CPython archive contains an escaping symlink: {entry}")
    except (OSError, tarfile.TarError, RuntimeError) as error:
        shutil.rmtree(destination, ignore_errors=True)
        if isinstance(error, PythonRuntimeError):
            raise
        raise PythonRuntimeError(f"cannot safely extract CPython archive: {error}") from error


def _verify_staged_python(executable: Path, lock: dict[str, Any]) -> None:
    expected_versions = {
        _normalized_distribution(item["name"]): item["version"]
        for item in lock["runtimeDependencies"]["wheels"]
    }
    expected_literal = json.dumps(expected_versions, sort_keys=True)
    code = (
        "import importlib.metadata as m, json, jsonschema, os, platform, sys; "
        f"expected = {expected_literal}; "
        "versions = {name: m.version(name) for name in expected}; "
        "print(json.dumps({'version': '.'.join(map(str, sys.version_info[:3])), "
        "'executable': os.path.realpath(sys.executable), 'machine': platform.machine(), "
            "'pip': m.version('pip'), 'jsonschema': jsonschema.__version__, 'dependencies': versions}))"
    )
    try:
        result = subprocess.run(
            [str(executable), "-c", code],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PythonRuntimeError(f"staged private Python could not run: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
        raise PythonRuntimeError(f"private Python dependency verification failed: {detail}")
    try:
        state = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise PythonRuntimeError("private Python returned invalid runtime verification JSON") from error
    expected_path = str(executable.resolve())
    if (
        state.get("version") != lock["python"]["version"]
        or state.get("executable") != expected_path
        or state.get("machine") not in {"x86_64", "AMD64"}
        or state.get("pip") != lock["python"]["bundledPipVersion"]
        or state.get("jsonschema") != expected_versions.get("jsonschema")
    ):
        raise PythonRuntimeError(f"staged private Python does not match the locked runtime: {state}")
    for name, version in expected_versions.items():
        actual = state.get("dependencies", {}).get(name)
        if actual != version:
            raise PythonRuntimeError(f"private Python dependency {name} has {actual}, expected {version}")


def prepare(args: argparse.Namespace) -> dict[str, str]:
    """Verify immutable pins and stage runtime, wheel payload, and offline dependencies."""

    os.umask(0o022)
    lock_path = args.lock.resolve()
    lock = _read_json_object(lock_path, "python runtime lock")
    python, requirements_path = validate_lock(lock, lock_path)
    _validate_profile(args.release_lock, lock_path, args.target_profile, lock)
    stage_root = args.stage_root.resolve()
    work_dir = args.work_dir.resolve()
    if stage_root == Path("/") or work_dir == Path("/"):
        raise PythonRuntimeError("stage-root and work-dir must not be the filesystem root")
    stage_root.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    runtime_root = stage_root.joinpath(*PurePosixPath(python["installRoot"].lstrip("/")).parts)
    payload_root = stage_root.joinpath(*PurePosixPath(lock["payload"]["lockPath"]).parts).parent
    resolver = lock["buildResolver"]
    uv_install_path = PurePosixPath(resolver["installedPath"].lstrip("/"))
    uv_root = stage_root.joinpath(*uv_install_path.parts[:-2])
    for path in (runtime_root, payload_root, uv_root):
        if path.exists() or path.is_symlink():
            raise PythonRuntimeError(f"staging output already exists; refusing to replace it: {path}")
    work_runtime = work_dir / "runtime-root"
    if work_runtime.exists() or work_runtime.is_symlink():
        raise PythonRuntimeError(f"private Python work path already exists: {work_runtime}")

    _verify_release_assets(lock)
    mapping_path = work_dir / "uv-python-downloads.json"
    _load_official_uv_mapping(resolver, mapping_path)
    uv_executable = _resolve_uv(resolver, work_dir, args.uv_executable)
    _resolve_frozen_mapping(uv_executable, mapping_path, resolver)

    archive = python["archive"]
    archive_path = _verified_local_or_download(
        args.archive,
        archive["url"],
        work_dir / "downloads" / archive["name"],
        sha256=archive["sha256"],
        size=archive["size"],
    )
    wheels_dir = work_dir / "downloads" / "wheels"
    wheel_paths: dict[str, Path] = {}
    for wheel in lock["runtimeDependencies"]["wheels"]:
        wheel_paths[wheel["filename"]] = _verified_download(
            wheel["url"],
            wheels_dir / wheel["filename"],
            sha256=wheel["sha256"],
            size=wheel["size"],
        )

    runtime_root.parent.mkdir(parents=True, exist_ok=True)
    _extract_python_archive(archive_path, work_runtime, root_name=python["archiveRoot"])
    site_packages = work_runtime.joinpath(
        *PurePosixPath(lock["runtimeDependencies"]["sitePackagesRelativePath"]).parts
    )
    site_packages.mkdir(parents=True, exist_ok=True)
    installer = work_runtime / "bin" / "python3.12"
    if not installer.is_file():
        raise PythonRuntimeError(f"CPython archive has no expected interpreter: {installer}")
    pip_command = [
        str(installer),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-index",
        "--no-deps",
        "--only-binary=:all:",
        "--require-hashes",
        "--find-links",
        str(wheels_dir),
        "--target",
        str(site_packages),
        "-r",
        str(requirements_path),
    ]
    try:
        result = subprocess.run(
            pip_command,
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ, "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PythonRuntimeError(f"offline private runtime dependency install failed: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
        raise PythonRuntimeError(f"offline private runtime dependency install failed: {detail}")

    _verify_staged_python(installer, lock)
    verification_record = _release_metadata_provenance(lock)

    for current, directory_names, file_names in os.walk(work_runtime, followlinks=False):
        current_path = Path(current)
        current_path.chmod(0o755)
        for directory_name in directory_names:
            child = current_path / directory_name
            if not child.is_symlink():
                child.chmod(0o755)
        for file_name in file_names:
            child = current_path / file_name
            if child.is_symlink():
                continue
            child.chmod(0o755 if child.stat().st_mode & 0o111 else 0o644)
    shutil.copytree(work_runtime, runtime_root, symlinks=True)
    staged_uv_executable = _stage_uv_runtime(uv_executable, resolver, stage_root)

    payload_root.mkdir(parents=True, exist_ok=False)
    payload_assets = lock["payload"]
    runtime_asset_path = stage_root.joinpath(*PurePosixPath(payload_assets["runtimeArchivePath"]).parts)
    runtime_asset_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(archive_path, runtime_asset_path)
    requirements_asset = stage_root.joinpath(*PurePosixPath(payload_assets["requirementsPath"]).parts)
    shutil.copyfile(requirements_path, requirements_asset)
    lock_asset = stage_root.joinpath(*PurePosixPath(payload_assets["lockPath"]).parts)
    shutil.copyfile(lock_path, lock_asset)
    wheel_asset_directory = stage_root.joinpath(*PurePosixPath(payload_assets["wheelDirectory"]).parts)
    wheel_asset_directory.mkdir(parents=True, exist_ok=False)
    for filename, source in wheel_paths.items():
        shutil.copyfile(source, wheel_asset_directory / filename)
    verification_asset = stage_root.joinpath(
        *PurePosixPath(payload_assets["verificationRecordPath"]).parts
    )
    verification_asset.parent.mkdir(parents=True, exist_ok=True)
    verification_asset.write_text(
        json.dumps(verification_record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    return {
        "targetProfile": args.target_profile,
        "stagedPythonExecutable": str(stage_root / python["executable"].lstrip("/")),
        "runtimePythonExecutable": python["executable"],
        "uvExecutable": str(uv_executable),
        "runtimeUvExecutable": resolver["installedPath"],
        "stagedUvExecutable": str(staged_uv_executable),
        "uvExecutableSha256": resolver["binaryArchive"]["executableSha256"],
        "verifiedArchivePath": str(archive_path),
        "archiveSha256": archive["sha256"],
        "requirementsSha256": lock["runtimeDependencies"]["requirementsSha256"],
    }


def _release_metadata_provenance(lock: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic references to the official upstream checks performed."""

    archive = lock["python"]["archive"]
    resolver = lock["buildResolver"]
    uv_archive = resolver["binaryArchive"]
    distribution_metadata = resolver["distributionMetadata"]
    return {
        "schemaVersion": 1,
        "pythonArchive": {
            "url": archive["url"],
            "sha256": archive["sha256"],
            "sizeBytes": archive["size"],
            "repository": archive["release"]["repository"],
            "releaseTag": archive["release"]["tag"],
            "releaseCommit": archive["release"]["commit"],
            "assetId": archive["release"]["assetId"],
        },
        "uvResolver": {
            "version": resolver["version"],
            "installedPath": resolver["installedPath"],
            "usage": resolver["usage"],
            "url": uv_archive["url"],
            "archiveSha256": uv_archive["sha256"],
            "executableSha256": uv_archive["executableSha256"],
            "repository": uv_archive["release"]["repository"],
            "releaseTag": uv_archive["release"]["tag"],
            "releaseCommit": uv_archive["release"]["commit"],
            "assetId": uv_archive["release"]["assetId"],
        },
        "uvDistributionMetadata": {
            "url": distribution_metadata["url"],
            "sha256": distribution_metadata["sha256"],
            "mappingKey": distribution_metadata["mappingKey"],
        },
        "runtimeRequirementsSha256": lock["runtimeDependencies"]["requirementsSha256"],
        "runtimeSystemPackages": lock["runtimeDependencies"]["systemPackages"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python_runtime.py",
        description="Verify and stage the locked Cyrene private CPython runtime.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Validate the runtime lock and dependency hashes.")
    check.add_argument("--lock", type=Path, default=DEFAULT_LOCK)

    stage = subparsers.add_parser("prepare", help="Verify assets and stage the offline private runtime.")
    stage.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    stage.add_argument("--release-lock", type=Path, default=DEFAULT_RELEASE_LOCK)
    stage.add_argument("--target-profile", required=True)
    stage.add_argument("--stage-root", required=True, type=Path)
    stage.add_argument("--work-dir", required=True, type=Path)
    stage.add_argument("--archive", type=Path, help="Use a cached source archive after exact SHA-256 verification.")
    stage.add_argument("--uv-executable", type=Path, help="Use an exact uv 0.12.21 executable.")
    stage.add_argument("--json", action="store_true", help="Write the staging report as one JSON object.")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        lock = _read_json_object(arguments.lock.resolve(), "python runtime lock")
        _, requirements_path = validate_lock(lock, arguments.lock.resolve())
        if arguments.command == "check":
            print(f"Python runtime lock: PASS (CPython {lock['python']['version']}, uv {lock['buildResolver']['version']})")
            print(f"Runtime dependency lock: PASS ({requirements_path})")
            return 0
        report = prepare(arguments)
        if arguments.json:
            print(json.dumps(report, sort_keys=True))
        else:
            print(f"Staged CPython {lock['python']['version']} for {arguments.target_profile}.")
            print(f"Python executable: {report['stagedPythonExecutable']}")
            print(f"Build uv executable: {report['uvExecutable']}")
            print(f"Runtime uv executable: {report['stagedUvExecutable']}")
            print(f"Verified source archive SHA-256: {report['archiveSha256']}")
        return 0
    except PythonRuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
