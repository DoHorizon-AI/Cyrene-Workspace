#!/usr/bin/env python3
"""Build pinned Linux service wheelhouses for the Workspace DEB package.

This producer reads the Workspace release lock and each pinned service's
``uv.lock``, then writes flat, hash-verified Python 3.12 wheelhouses.
该脚本按 Workspace 发布锁和服务提交生成可离线安装的 Linux wheelhouse。
"""

from __future__ import annotations

import argparse
import email.parser
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
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
DISTRIBUTION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
PIN_PATTERN = re.compile(r"(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)==(?P<version>[^\s;]+)\Z")
DIRECT_GIT_PATTERN = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)\s*@\s*(?P<url>git\+https?://\S+)\Z"
)


@dataclass(frozen=True)
class ServiceSpec:
    """Release inputs for one independently versioned service."""

    service: str
    repository: str
    lock_relative_path: str
    application_distributions: tuple[str, ...]


@dataclass(frozen=True)
class GitSource:
    """A locked Git source normalized for exact checkout and matching."""

    remote: str
    commit: str
    subdirectory: str

    @property
    def identity(self) -> tuple[str, str, str]:
        return (_normalize_remote(self.remote), self.commit, self.subdirectory)


@dataclass(frozen=True)
class WheelArtifact:
    """A built wheel together with the package identity verified in METADATA."""

    path: Path
    name: str
    version: str
    sha256: str
    provenance: str


RUNTIME_SDK_COMPONENT_ID = "cyrene-runtime-maintenance-sdk"
RUNTIME_SDK_DISTRIBUTION = "cyrene-runtime-maintenance"


SERVICE_SPECS = (
    ServiceSpec("navigator", "Cyrene-Navigator", "uv.lock", ("cyrene-navigator",)),
    ServiceSpec("yield", "Cyrene-Yield", "uv.lock", ("cyrene-yield",)),
    ServiceSpec(
        "reactor",
        "Cyrene-Reactor",
        "product/uv.lock",
        ("cyrene-reactor-product",),
    ),
    ServiceSpec(
        "exchange",
        "Cyrene-Exchange",
        "product/uv.lock",
        ("cyrene-exchange", "cyrene-exchange-product"),
    ),
    ServiceSpec("catalyst", "Cyrene-Catalyst", "uv.lock", ("cyrene-catalyst",)),
)


class ProducerError(RuntimeError):
    """Raised when an input cannot be proven to match its pinned source."""


def _normalize_distribution(value: str) -> str:
    """Apply the normalization used for wheel and package distribution names."""

    return re.sub(r"[-_.]+", "-", value).lower()


def _normalize_remote(value: str) -> str:
    """Normalize a public Git remote without changing its repository identity."""

    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http", "ssh"} or not parsed.netloc:
        raise ProducerError(f"unsupported Git source remote: {value!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ProducerError("Git source remotes must not embed credentials")
    path = parsed.path.rstrip("/")
    path = path.removesuffix(".git")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}".rstrip("/")


def _parse_yaml_scalar(value: str) -> str:
    """Read the quoted scalar values used by repositories.yaml repository rows."""

    raw = value.strip()
    if raw.startswith('"'):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ProducerError(f"invalid quoted repositories.yaml scalar: {raw}") from error
        if not isinstance(decoded, str):
            raise ProducerError(f"expected a string in repositories.yaml, got {raw}")
        return decoded
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    return raw


def _load_repository_remotes(path: Path) -> dict[str, tuple[str, str]]:
    """Read canonical path and remote fields for entries in repositories.yaml."""

    entries: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if raw_line.startswith("  - name:"):
            if current is not None:
                entries[current["name"]] = current
            current = {"name": _parse_yaml_scalar(raw_line.split(":", 1)[1])}
            continue
        if current is None:
            continue
        match = re.match(r"^    (path|canonical_remote):\s*(.+?)\s*$", raw_line)
        if match:
            current[match.group(1)] = _parse_yaml_scalar(match.group(2))
    if current is not None:
        entries[current["name"]] = current

    result: dict[str, tuple[str, str]] = {}
    for spec in SERVICE_SPECS:
        entry = entries.get(spec.repository)
        if entry is None or not entry.get("path") or not entry.get("canonical_remote"):
            raise ProducerError(
                f"repositories.yaml is missing path/canonical_remote for {spec.repository}"
            )
        result[spec.repository] = (entry["path"], entry["canonical_remote"])
    return result


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    """Read a JSON object from disk and report contextual parse failures."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProducerError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise ProducerError(f"{label} must be a JSON object: {path}")
    return value


def _run(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Run a build command and include captured output when it fails."""

    completed = subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        rendered = " ".join(arguments)
        raise ProducerError(f"command failed ({completed.returncode}): {rendered}\n{detail}")
    return completed.stdout


def _checkout_exact(
    remote: str,
    commit: str,
    checkout_root: Path,
    checkout_cache: dict[tuple[str, str], Path],
) -> Path:
    """Fetch and detach one exact commit, refusing branch or tag substitution."""

    if not COMMIT_PATTERN.fullmatch(commit):
        raise ProducerError(f"locked Git source is not a full lowercase SHA: {commit!r}")
    normalized_remote = _normalize_remote(remote)
    key = (normalized_remote, commit)
    cached = checkout_cache.get(key)
    if cached is not None:
        return cached

    checkout = (
        checkout_root / f"{hashlib.sha256(normalized_remote.encode()).hexdigest()[:12]}-{commit}"
    )
    checkout.mkdir(parents=True)
    _run(["git", "init", "--quiet", str(checkout)])
    _run(["git", "-C", str(checkout), "remote", "add", "origin", remote])
    _run(["git", "-C", str(checkout), "fetch", "--depth=1", "--no-tags", "origin", commit])
    _run(["git", "-C", str(checkout), "checkout", "--quiet", "--detach", commit])
    actual = _run(["git", "-C", str(checkout), "rev-parse", "HEAD"]).strip()
    if actual != commit:
        raise ProducerError(f"Git checkout differs from lock: expected {commit}, got {actual}")
    if _run(["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=all"]).strip():
        raise ProducerError(f"fresh exact-SHA checkout is not clean: {checkout}")
    checkout_cache[key] = checkout
    return checkout


def _parse_git_source(value: str, *, requirement_url: bool = False) -> GitSource:
    """Parse uv.lock and uv export Git forms into one immutable source identity."""

    raw = value
    if requirement_url:
        if not raw.startswith("git+"):
            raise ProducerError(f"expected a Git requirement URL, got {raw!r}")
        raw = raw[4:]
    parts = urlsplit(raw)
    path = parts.path
    query = parse_qs(parts.query, keep_blank_values=True)
    subdirectory = ""

    if requirement_url:
        if "@" not in path:
            raise ProducerError(f"Git requirement does not pin a commit: {value!r}")
        repository_path, commit = path.rsplit("@", 1)
        if parts.fragment:
            fragment = parse_qs(parts.fragment, keep_blank_values=True)
            if fragment.get("subdirectory"):
                subdirectory = unquote(fragment["subdirectory"][0])
        if query.get("subdirectory"):
            subdirectory = unquote(query["subdirectory"][0])
    else:
        repository_path = path
        fragment_commit = parts.fragment
        query_commit = query.get("rev", [""])[0]
        commit = query_commit or fragment_commit
        if query_commit and fragment_commit and query_commit != fragment_commit:
            raise ProducerError(f"uv.lock Git revision and fragment differ: {value!r}")
        if query.get("subdirectory"):
            subdirectory = unquote(query["subdirectory"][0])

    if not COMMIT_PATTERN.fullmatch(commit):
        raise ProducerError(f"Git source is not pinned to a full lowercase SHA: {value!r}")
    remote = urlunsplit((parts.scheme, parts.netloc, repository_path, "", ""))
    _normalize_remote(remote)
    if subdirectory:
        source_path = PurePosixPath(subdirectory)
        if (
            source_path.is_absolute()
            or "\\" in subdirectory
            or any(part in {"", ".", ".."} for part in source_path.parts)
        ):
            raise ProducerError(f"unsafe Git subdirectory in lock: {subdirectory!r}")
        subdirectory = source_path.as_posix()
    return GitSource(remote=remote, commit=commit, subdirectory=subdirectory)


def _logical_requirements(export_text: str) -> list[str]:
    """Join uv's backslash-continued requirement and hash lines."""

    logical: list[str] = []
    pending = ""
    for raw_line in export_text.splitlines():
        line = raw_line.strip()
        if not line or (not pending and line.startswith("#")):
            continue
        continuation = line.endswith("\\")
        if continuation:
            line = line[:-1].rstrip()
        pending = f"{pending} {line}".strip()
        if not continuation:
            logical.append(pending)
            pending = ""
    if pending:
        raise ProducerError("uv export returned an unterminated continuation line")
    return logical


def _split_hashes(logical_line: str) -> tuple[str, tuple[str, ...]]:
    """Split one exported requirement into its requirement expression and hashes."""

    hashes = tuple(re.findall(r"--hash=sha256:([0-9a-f]{64})", logical_line))
    residue = re.sub(r"\s+--hash=sha256:[0-9a-f]{64}", "", logical_line).strip()
    if "--hash=" in residue:
        raise ProducerError(f"uv export emitted an unsupported hash option: {logical_line}")
    return residue, hashes


def _wheel_metadata(path: Path) -> tuple[str, str]:
    """Read and validate the single distribution identity stored in a wheel."""

    try:
        with zipfile.ZipFile(path) as archive:
            metadata_paths = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA") and name.count("/") == 1
            ]
            if len(metadata_paths) != 1:
                raise ProducerError(f"wheel must contain one dist-info/METADATA: {path}")
            metadata = email.parser.Parser().parsestr(
                archive.read(metadata_paths[0]).decode("utf-8", errors="replace")
            )
    except (OSError, zipfile.BadZipFile) as error:
        raise ProducerError(f"cannot inspect wheel metadata {path}: {error}") from error
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not DISTRIBUTION_PATTERN.fullmatch(name) or not version:
        raise ProducerError(f"wheel metadata is missing a valid Name or Version: {path}")
    return name, version


def _build_wheel(
    *,
    uv_executable: str,
    python_executable: str,
    project_root: Path,
    expected_name: str,
    expected_version: str,
    output_root: Path,
    provenance: str,
    env: dict[str, str],
) -> WheelArtifact:
    """Build one wheel and verify its package name and version against uv.lock."""

    if not (project_root / "pyproject.toml").is_file():
        raise ProducerError(f"locked wheel source has no pyproject.toml: {project_root}")
    output_root.mkdir(parents=True, exist_ok=False)
    _run(
        [
            uv_executable,
            "build",
            "--wheel",
            "--no-create-gitignore",
            "--python",
            python_executable,
            "--out-dir",
            str(output_root),
            str(project_root),
        ],
        env=env,
    )
    wheels = sorted(output_root.glob("*.whl"))
    if len(wheels) != 1:
        raise ProducerError(f"expected one wheel from {project_root}, found {len(wheels)}")
    wheel = wheels[0]
    actual_name, actual_version = _wheel_metadata(wheel)
    if _normalize_distribution(actual_name) != _normalize_distribution(expected_name):
        raise ProducerError(
            f"wheel name differs from uv.lock: expected {expected_name}, got {actual_name}"
        )
    if actual_version != expected_version:
        raise ProducerError(
            f"wheel version differs from uv.lock for {expected_name}: "
            f"expected {expected_version}, got {actual_version}"
        )
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    return WheelArtifact(wheel, actual_name, actual_version, digest, provenance)


def _lock_packages(lock_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate and return the package records from an uv lockfile."""

    packages = lock_data.get("package")
    if lock_data.get("version") != 1 or not isinstance(packages, list):
        raise ProducerError("unsupported or malformed uv.lock")
    if not all(isinstance(package, dict) for package in packages):
        raise ProducerError("uv.lock package entries must be objects")
    return packages


def _local_packages(
    packages: list[dict[str, Any]],
    *,
    lock_directory: Path,
    checkout_root: Path,
    expected_distributions: tuple[str, ...],
) -> list[tuple[dict[str, Any], Path]]:
    """Resolve every locked editable/path project and enforce the app wheel set."""

    local: list[tuple[dict[str, Any], Path]] = []
    for package in packages:
        source = package.get("source", {})
        relative = source.get("editable") or source.get("directory")
        if not relative:
            continue
        project_root = (lock_directory / relative).resolve()
        try:
            project_root.relative_to(checkout_root.resolve())
        except ValueError as error:
            raise ProducerError(
                f"uv.lock local source escapes its exact service checkout: {relative!r}"
            ) from error
        local.append((package, project_root))

    actual_names = {_normalize_distribution(package["name"]) for package, _ in local}
    expected_names = {_normalize_distribution(name) for name in expected_distributions}
    if actual_names != expected_names:
        raise ProducerError(
            "locked local wheel set differs from the service contract: "
            f"expected {sorted(expected_names)}, got {sorted(actual_names)}"
        )
    return local


def _git_package_for_requirement(
    name: str,
    source: GitSource,
    packages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Match a uv-export Git requirement to its exact uv.lock package record."""

    matches: list[dict[str, Any]] = []
    for package in packages:
        locked_source = package.get("source", {}).get("git")
        if not locked_source or _normalize_distribution(
            package.get("name", "")
        ) != _normalize_distribution(name):
            continue
        if _parse_git_source(locked_source).identity == source.identity:
            matches.append(package)
    if len(matches) != 1:
        raise ProducerError(
            f"Git requirement does not match one exact uv.lock entry: {name} {source.remote}@{source.commit}"
        )
    return matches[0]


def _format_hashed_requirement(requirement: str, hashes: tuple[str, ...]) -> str:
    """Format a pip-compatible exact pin and its allowed wheel hashes."""

    if not hashes:
        raise ProducerError(f"requirements.lock pin has no SHA-256 hashes: {requirement}")
    unique_hashes = tuple(dict.fromkeys(hashes))
    lines = [f"{requirement} \\"]
    for index, digest in enumerate(unique_hashes):
        if not SHA256_PATTERN.fullmatch(digest):
            raise ProducerError(f"invalid wheel SHA-256: {digest}")
        suffix = " \\" if index < len(unique_hashes) - 1 else ""
        lines.append(f"    --hash=sha256:{digest}{suffix}")
    return "\n".join(lines)


def _requirements_text(
    export_text: str,
    *,
    packages: list[dict[str, Any]],
    local_artifacts: list[WheelArtifact],
    git_artifacts: dict[tuple[str, str, str], WheelArtifact],
    git_provenance: dict[str, str],
    service_repository: str,
    service_commit: str,
    uv_lock_digest: str,
) -> str:
    """Convert frozen uv export output into a fully hash-pinned pip lock."""

    output: list[str] = []
    included_names: set[str] = set()
    for logical in _logical_requirements(export_text):
        expression, hashes = _split_hashes(logical)
        requirement_expression, separator, marker = expression.partition(";")
        requirement_expression = requirement_expression.strip()
        marker_suffix = f" ;{marker.strip()}" if separator else ""

        direct_match = DIRECT_GIT_PATTERN.fullmatch(requirement_expression)
        if direct_match is not None:
            name = direct_match.group("name")
            source = _parse_git_source(direct_match.group("url"), requirement_url=True)
            package = _git_package_for_requirement(name, source, packages)
            artifact = git_artifacts.get(source.identity)
            if artifact is None:
                raise ProducerError(f"locked Git wheel was not built: {name} @ {source.remote}")
            if _normalize_distribution(artifact.name) != _normalize_distribution(name):
                raise ProducerError(
                    f"Git wheel name differs from uv export: {name}, got {artifact.name}"
                )
            if artifact.version != package.get("version"):
                raise ProducerError(
                    f"Git wheel version differs from uv.lock for {name}: "
                    f"expected {package.get('version')}, got {artifact.version}"
                )
            pin = f"{name}=={package['version']}{marker_suffix}"
            output.append(_format_hashed_requirement(pin, (artifact.sha256,)))
            included_names.add(_normalize_distribution(name))
            git_provenance[_normalize_distribution(name)] = (
                f"{name}=={package['version']} source={source.remote}@{source.commit}"
                f"#subdirectory={source.subdirectory or '.'} wheel_sha256={artifact.sha256}"
            )
            continue

        pin_match = PIN_PATTERN.fullmatch(requirement_expression)
        if pin_match is None:
            raise ProducerError(
                "uv export contains a non-immutable runtime requirement; "
                f"refusing direct, editable, or range source: {logical}"
            )
        if not hashes:
            raise ProducerError(f"uv export package has no locked hashes: {logical}")
        pin = f"{pin_match.group('name')}=={pin_match.group('version')}{marker_suffix}"
        output.append(_format_hashed_requirement(pin, hashes))
        included_names.add(_normalize_distribution(pin_match.group("name")))

    for artifact in sorted(local_artifacts, key=lambda item: _normalize_distribution(item.name)):
        normalized_name = _normalize_distribution(artifact.name)
        if normalized_name in included_names:
            raise ProducerError(
                f"local wheel is already present in the frozen runtime export: {artifact.name}"
            )
        output.append(
            _format_hashed_requirement(f"{artifact.name}=={artifact.version}", (artifact.sha256,))
        )
        included_names.add(normalized_name)
        git_provenance[normalized_name] = (
            f"{artifact.name}=={artifact.version} source={artifact.provenance} "
            f"wheel_sha256={artifact.sha256}"
        )

    comments = [
        "# Generated from the frozen uv.lock for the exact service source commit.",
        f"# service_repository={service_repository}",
        f"# service_commit={service_commit}",
        f"# uv_lock_sha256={uv_lock_digest}",
    ]
    comments.extend(f"# {line}" for _, line in sorted(git_provenance.items()))
    return "\n".join((*comments, *output)) + "\n"


def _requirement_hashes(path: Path) -> dict[tuple[str, str], set[str]]:
    """Read the generated pip lock for final wheelhouse verification."""

    requirements: dict[tuple[str, str], set[str]] = {}
    current: tuple[str, str] | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash="):
            if current is None:
                raise ProducerError(f"orphan hash in requirements.lock: {line}")
            digest = line.removeprefix("--hash=sha256:").removesuffix("\\").strip()
            if not SHA256_PATTERN.fullmatch(digest):
                raise ProducerError(f"invalid hash in requirements.lock: {line}")
            requirements.setdefault(current, set()).add(digest)
            continue
        match = PIN_PATTERN.fullmatch(line.rstrip("\\").split(";", 1)[0].strip())
        if match is None:
            raise ProducerError(f"requirements.lock contains an invalid pin: {line}")
        current = (
            _normalize_distribution(match.group("name")),
            match.group("version"),
        )
        requirements.setdefault(current, set())
    for identity, hashes in requirements.items():
        if not hashes:
            raise ProducerError(f"requirements.lock pin has no hashes: {identity}")
    return requirements


def _verify_wheelhouse(service_dir: Path, spec: ServiceSpec, source_commit: str) -> None:
    """Verify all flat wheels match the generated exact pins and artifact hashes."""

    lock = service_dir / "requirements.lock"
    allowed = _requirement_hashes(lock)
    source = _read_json_object(service_dir / "source.json", f"{spec.service} source.json")
    runtime_dependencies = source.get("runtime_dependencies")
    expected_source = {
        "schema_version": 2,
        "service": spec.service,
        "source_repository": spec.repository,
        "source_commit": source_commit,
        "requirements_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "runtime_dependencies": runtime_dependencies,
    }
    if set(source) != set(expected_source) or source != expected_source:
        raise ProducerError(f"{spec.service}/source.json differs from its pinned source or lock")
    if not isinstance(runtime_dependencies, list) or len(runtime_dependencies) != 1:
        raise ProducerError(f"{spec.service}/source.json must pin exactly one runtime SDK dependency")
    sdk = runtime_dependencies[0]
    expected_sdk_fields = {
        "component_id",
        "manifest_digest",
        "artifact_digest",
        "distribution",
        "version",
        "wheel_sha256",
    }
    if (
        not isinstance(sdk, dict)
        or set(sdk) != expected_sdk_fields
        or sdk.get("component_id") != RUNTIME_SDK_COMPONENT_ID
        or sdk.get("distribution") != RUNTIME_SDK_DISTRIBUTION
        or not isinstance(sdk.get("manifest_digest"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", sdk["manifest_digest"]) is None
        or not isinstance(sdk.get("artifact_digest"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", sdk["artifact_digest"]) is None
        or not isinstance(sdk.get("version"), str)
        or not isinstance(sdk.get("wheel_sha256"), str)
        or SHA256_PATTERN.fullmatch(sdk["wheel_sha256"]) is None
    ):
        raise ProducerError(f"{spec.service}/source.json has invalid runtime SDK provenance")
    expected_entries = {"requirements.lock", "source.json"}
    if spec.service == "navigator":
        expected_entries.add("serve-web.py")
    wheel_paths: list[Path] = []
    for child in service_dir.iterdir():
        if child.is_symlink() or not child.is_file():
            raise ProducerError(f"wheelhouse entries must be flat regular files: {child}")
        if child.name.endswith(".whl"):
            wheel_paths.append(child)
        elif child.name not in expected_entries:
            raise ProducerError(f"unexpected {spec.service} wheelhouse entry: {child.name}")

    required_names = {
        _normalize_distribution(name)
        for name in (*spec.application_distributions, RUNTIME_SDK_DISTRIBUTION)
    }
    locked_names = {name for name, _ in allowed}
    if not required_names.issubset(locked_names):
        raise ProducerError(f"{spec.service}/requirements.lock omits an application distribution")
    seen_distributions: set[str] = set()
    for wheel in sorted(wheel_paths):
        name, version = _wheel_metadata(wheel)
        identity = (_normalize_distribution(name), version)
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if identity not in allowed or digest not in allowed[identity]:
            raise ProducerError(f"wheel is not pinned by requirements.lock: {wheel.name}")
        seen_distributions.add(identity[0])
    missing = [
        name
        for name in spec.application_distributions
        if _normalize_distribution(name) not in seen_distributions
    ]
    if missing:
        raise ProducerError(
            f"{spec.service} wheelhouse is missing application wheel(s): {', '.join(missing)}"
        )
    sdk_identity = (_normalize_distribution(RUNTIME_SDK_DISTRIBUTION), sdk["version"])
    sdk_matches = []
    for wheel in wheel_paths:
        name, version = _wheel_metadata(wheel)
        if (
            (_normalize_distribution(name), version) == sdk_identity
            and hashlib.sha256(wheel.read_bytes()).hexdigest() == sdk["wheel_sha256"]
        ):
            sdk_matches.append(wheel)
    if sdk_identity not in allowed or sdk["wheel_sha256"] not in allowed[sdk_identity] or len(sdk_matches) != 1:
        raise ProducerError(f"{spec.service} wheelhouse is missing the exactly pinned runtime SDK wheel")
    if not wheel_paths:
        raise ProducerError(f"{spec.service} wheelhouse contains no wheels")


def _native_target() -> str:
    """Require the native Linux Python 3.12 target used by service_bundle.py."""

    if not sys.platform.startswith("linux"):
        raise ProducerError("wheelhouse preparation must run on Linux")
    if sys.version_info[:2] != (3, 12):
        raise ProducerError(
            f"wheelhouse preparation requires Python 3.12, got {sys.version_info.major}.{sys.version_info.minor}"
        )
    machine = platform.machine().lower()
    if machine != "x86_64":
        raise ProducerError(
            f"release-lock.json supports Ubuntu 24.04 x86_64 only, got Linux/{machine}"
        )
    return "amd64"


def _load_runtime_sdk_wheel(args: argparse.Namespace) -> tuple[WheelArtifact, str, str]:
    """Verify the independently attested SDK wheel passed by the release workflow."""

    values = (
        args.runtime_sdk_wheel,
        args.runtime_sdk_version,
        args.runtime_sdk_sha256,
        args.runtime_sdk_manifest_digest,
        args.runtime_sdk_artifact_digest,
    )
    if any(value is None for value in values):
        raise ProducerError(
            "Product wheelhouses require --runtime-sdk-wheel, --runtime-sdk-version, "
            "--runtime-sdk-sha256, --runtime-sdk-manifest-digest, and "
            "--runtime-sdk-artifact-digest from the verified SDK release"
        )
    path = Path(args.runtime_sdk_wheel).expanduser().absolute()
    try:
        info = path.lstat()
    except OSError as error:
        raise ProducerError(f"cannot inspect runtime SDK wheel {path}: {error}") from error
    if not stat.S_ISREG(info.st_mode):
        raise ProducerError("runtime SDK wheel must be a regular non-symlink file")
    if not SHA256_PATTERN.fullmatch(args.runtime_sdk_sha256):
        raise ProducerError("--runtime-sdk-sha256 must be 64 lowercase hexadecimal characters")
    for flag, value in (
        ("--runtime-sdk-manifest-digest", args.runtime_sdk_manifest_digest),
        ("--runtime-sdk-artifact-digest", args.runtime_sdk_artifact_digest),
    ):
        if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
            raise ProducerError(f"{flag} must be sha256 followed by 64 lowercase hexadecimal characters")
    if not args.runtime_sdk_version or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}", args.runtime_sdk_version):
        raise ProducerError("--runtime-sdk-version must be a concrete version")
    name, version = _wheel_metadata(path)
    if _normalize_distribution(name) != _normalize_distribution(RUNTIME_SDK_DISTRIBUTION):
        raise ProducerError(
            f"runtime SDK wheel distribution must be {RUNTIME_SDK_DISTRIBUTION}, got {name}"
        )
    if version != args.runtime_sdk_version:
        raise ProducerError(
            f"runtime SDK wheel version differs from its verified manifest: "
            f"expected {args.runtime_sdk_version}, got {version}"
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != args.runtime_sdk_sha256:
        raise ProducerError("runtime SDK wheel SHA-256 differs from the verified wheel digest")
    return (
        WheelArtifact(path, name, version, digest, RUNTIME_SDK_COMPONENT_ID),
        args.runtime_sdk_manifest_digest,
        args.runtime_sdk_artifact_digest,
    )


def _write_service(
    *,
    spec: ServiceSpec,
    repository_metadata: dict[str, tuple[str, str]],
    release_lock: dict[str, Any],
    checkout_root: Path,
    checkout_cache: dict[tuple[str, str], Path],
    staging_root: Path,
    uv_executable: str,
    python_executable: str,
    command_env: dict[str, str],
    service_commit_override: str | None = None,
    runtime_sdk: WheelArtifact,
    runtime_sdk_manifest_digest: str,
    runtime_sdk_artifact_digest: str,
) -> Path:
    """Build one complete service wheelhouse from pinned service and Git sources."""

    relative_repo_path, source_remote = repository_metadata[spec.repository]
    if Path(relative_repo_path).is_absolute():
        raise ProducerError(
            f"repositories.yaml repository path must be relative: {relative_repo_path}"
        )
    try:
        service_commit = (
            service_commit_override
            if service_commit_override is not None
            else release_lock["repositories"][spec.repository]
        )
    except (KeyError, TypeError) as error:
        raise ProducerError(f"release-lock.json is missing {spec.repository}") from error
    if not isinstance(service_commit, str) or not COMMIT_PATTERN.fullmatch(service_commit):
        raise ProducerError(f"release-lock.json has an invalid SHA for {spec.repository}")

    repo_root = _checkout_exact(source_remote, service_commit, checkout_root, checkout_cache)
    # repositories.yaml remains topology authority; builds always use the detached checkout above.
    lock_path = repo_root / spec.lock_relative_path
    if lock_path.is_symlink() or not lock_path.is_file():
        raise ProducerError(f"pinned service revision has no expected uv lockfile: {lock_path}")
    try:
        lock_data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ProducerError(f"cannot parse pinned uv.lock {lock_path}: {error}") from error
    packages = _lock_packages(lock_data)
    lock_directory = lock_path.parent
    local_packages = _local_packages(
        packages,
        lock_directory=lock_directory,
        checkout_root=repo_root,
        expected_distributions=spec.application_distributions,
    )

    service_stage = staging_root / spec.service
    service_stage.mkdir(parents=True, exist_ok=False)
    source_wheels = staging_root / ".built-wheels" / spec.service
    source_wheels.mkdir(parents=True, exist_ok=False)
    shutil.copy2(runtime_sdk.path, source_wheels / runtime_sdk.path.name)
    local_artifacts: list[WheelArtifact] = []

    for index, (package, project_root) in enumerate(local_packages):
        name = str(package.get("name", ""))
        version = str(package.get("version", ""))
        relative = package.get("source", {}).get("editable") or package.get("source", {}).get(
            "directory"
        )
        provenance = f"{spec.repository}@{service_commit}#{relative}"
        artifact = _build_wheel(
            uv_executable=uv_executable,
            python_executable=python_executable,
            project_root=project_root,
            expected_name=name,
            expected_version=version,
            output_root=staging_root / ".build" / spec.service / f"local-{index}",
            provenance=provenance,
            env=command_env,
        )
        local_artifacts.append(artifact)
        shutil.copy2(artifact.path, source_wheels / artifact.path.name)

    project_root = (
        repo_root / spec.lock_relative_path.rsplit("/", 1)[0]
        if "/" in spec.lock_relative_path
        else repo_root
    )
    uv_export_path = staging_root / ".exports" / f"{spec.service}.requirements.txt"
    uv_export_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            uv_executable,
            "export",
            "--locked",
            "--no-dev",
            "--no-default-groups",
            "--no-emit-project",
            "--no-emit-local",
            "--no-annotate",
            "--no-header",
            "--format",
            "requirements.txt",
            "--python",
            python_executable,
            "--output-file",
            str(uv_export_path),
            "--directory",
            str(project_root),
        ],
        env=command_env,
    )
    export_text = uv_export_path.read_text(encoding="utf-8")

    git_requirements: list[tuple[str, GitSource, dict[str, Any]]] = []
    for logical in _logical_requirements(export_text):
        expression, _ = _split_hashes(logical)
        requirement_expression = expression.partition(";")[0].strip()
        direct_match = DIRECT_GIT_PATTERN.fullmatch(requirement_expression)
        if direct_match is None:
            continue
        name = direct_match.group("name")
        source = _parse_git_source(direct_match.group("url"), requirement_url=True)
        package = _git_package_for_requirement(name, source, packages)
        git_requirements.append((name, source, package))

    built_git: dict[tuple[str, str, str], WheelArtifact] = {}
    git_provenance: dict[str, str] = {}
    for index, (name, source, package) in enumerate(git_requirements):
        if source.identity in built_git:
            continue
        git_checkout = _checkout_exact(source.remote, source.commit, checkout_root, checkout_cache)
        package_root = (
            (git_checkout / source.subdirectory).resolve() if source.subdirectory else git_checkout
        )
        try:
            package_root.relative_to(git_checkout.resolve())
        except ValueError as error:
            raise ProducerError(
                f"Git package path escapes its exact checkout: {source.subdirectory}"
            ) from error
        artifact = _build_wheel(
            uv_executable=uv_executable,
            python_executable=python_executable,
            project_root=package_root,
            expected_name=name,
            expected_version=str(package.get("version", "")),
            output_root=staging_root / ".build" / spec.service / f"git-{index}",
            provenance=(
                f"{source.remote}@{source.commit}#subdirectory={source.subdirectory or '.'}"
            ),
            env=command_env,
        )
        if any(
            _normalize_distribution(existing.name) == _normalize_distribution(artifact.name)
            and existing.version != artifact.version
            for existing in built_git.values()
        ):
            raise ProducerError(
                f"one service lock selects conflicting Git versions of {artifact.name}"
            )
        built_git[source.identity] = artifact
        shutil.copy2(artifact.path, source_wheels / artifact.path.name)

    if any(
        _normalize_distribution(item.name) == _normalize_distribution(runtime_sdk.name)
        for item in local_artifacts
    ) or any(
        _normalize_distribution(item.name) == _normalize_distribution(runtime_sdk.name)
        for item in built_git.values()
    ):
        raise ProducerError("service lock already contains the separately pinned runtime SDK")
    local_artifacts.append(
        WheelArtifact(
            runtime_sdk.path,
            runtime_sdk.name,
            runtime_sdk.version,
            runtime_sdk.sha256,
            f"{RUNTIME_SDK_COMPONENT_ID}@{runtime_sdk_manifest_digest}"
            f" artifact={runtime_sdk_artifact_digest}",
        )
    )

    uv_lock_digest = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    requirements_path = service_stage / "requirements.lock"
    requirements_path.write_text(
        _requirements_text(
            export_text,
            packages=packages,
            local_artifacts=local_artifacts,
            git_artifacts=built_git,
            git_provenance=git_provenance,
            service_repository=spec.repository,
            service_commit=service_commit,
            uv_lock_digest=uv_lock_digest,
        ),
        encoding="utf-8",
    )

    source_record = {
        "schema_version": 2,
        "service": spec.service,
        "source_repository": spec.repository,
        "source_commit": service_commit,
        "requirements_lock_sha256": hashlib.sha256(requirements_path.read_bytes()).hexdigest(),
        "runtime_dependencies": [
            {
                "component_id": RUNTIME_SDK_COMPONENT_ID,
                "manifest_digest": runtime_sdk_manifest_digest,
                "artifact_digest": runtime_sdk_artifact_digest,
                "distribution": runtime_sdk.name,
                "version": runtime_sdk.version,
                "wheel_sha256": runtime_sdk.sha256,
            }
        ],
    }
    (service_stage / "source.json").write_text(
        json.dumps(source_record, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )

    if spec.service == "navigator":
        launcher = repo_root / "scripts" / "serve-web.py"
        if launcher.is_symlink() or not launcher.is_file():
            raise ProducerError(
                f"pinned Navigator source has no safe scripts/serve-web.py: {launcher}"
            )
        launcher_text = launcher.read_text(encoding="utf-8")
        if (
            '"--pairing-code-file"' not in launcher_text
            or '"pairingCodeFile"' not in launcher_text
            or '"pairingCode": pairing_code' in launcher_text
        ):
            raise ProducerError(
                "pinned Navigator serve-web.py lacks the pairing-code file handoff required by the consumer"
            )
        shutil.copy2(launcher, service_stage / "serve-web.py")

    # The frozen export is already a complete transitive closure; do not follow wheel METADATA
    # direct Git references during preparation. 中文：锁文件已展开完整依赖闭包，避免回源解析。
    _run(
        [
            python_executable,
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--no-deps",
            "--require-hashes",
            "--only-binary=:all:",
            "--find-links",
            str(source_wheels),
            "--dest",
            str(service_stage),
            "--requirement",
            str(requirements_path),
        ],
        env=command_env,
    )
    _verify_wheelhouse(service_stage, spec, service_commit)
    print(
        f"Prepared {spec.service}: source={service_commit}, "
        f"wheels={len(list(service_stage.glob('*.whl')))}, target=linux/{_native_target()}/python3.12"
    )
    return service_stage


def _publish_service_directories(
    output_root: Path,
    staged_services: list[tuple[str, Path]],
    transaction_root: Path,
) -> None:
    """Replace generated service directories together and restore old inputs on failure."""

    output_root.mkdir(parents=True, exist_ok=True)
    if output_root.is_symlink() or not output_root.is_dir():
        raise ProducerError(f"output root must be a real directory: {output_root}")
    for service, _ in staged_services:
        destination = output_root / service
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            raise ProducerError(f"service output path must be a directory: {destination}")

    backup_root = transaction_root / "backups"
    backup_root.mkdir()
    moved_old: dict[str, Path] = {}
    installed: set[str] = set()
    try:
        for service, staged in staged_services:
            destination = output_root / service
            if destination.exists():
                backup = backup_root / service
                os.replace(destination, backup)
                moved_old[service] = backup
            os.replace(staged, destination)
            installed.add(service)
    except OSError as error:
        for service, _ in reversed(staged_services):
            destination = output_root / service
            if service in installed and destination.exists():
                shutil.rmtree(destination)
            backup = moved_old.get(service)
            if backup is not None and backup.exists():
                os.replace(backup, destination)
        raise ProducerError(f"could not publish all service wheelhouses: {error}") from error


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the public producer command line."""

    workspace_default = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description=(
            "Build one or all exact-SHA Linux/Python 3.12 Product service "
            "wheelhouses from release-lock.json and frozen uv.lock files."
        )
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=workspace_default,
        help=f"Cyrene-Workspace root (default: {workspace_default})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="destination root for the selected service directory or all five service directories",
    )
    parser.add_argument(
        "--service",
        choices=[spec.service for spec in SERVICE_SPECS],
        help="prepare one independently published Product instead of all five",
    )
    parser.add_argument(
        "--service-commit",
        help="override release-lock.json for the selected service with this exact 40-character source SHA",
    )
    parser.add_argument("--runtime-sdk-wheel", type=Path, help="wheel extracted from the verified cyrene-runtime-maintenance-sdk bundle")
    parser.add_argument("--runtime-sdk-version", help="version declared by the verified SDK release manifest")
    parser.add_argument("--runtime-sdk-sha256", help="raw 64-character SHA-256 of the SDK wheel")
    parser.add_argument("--runtime-sdk-manifest-digest", help="sha256:<64hex> SDK public manifest digest")
    parser.add_argument("--runtime-sdk-artifact-digest", help="sha256:<64hex> SDK bundle archive digest")
    parser.add_argument(
        "--uv",
        default=shutil.which("uv") or "uv",
        help="uv executable used for frozen export and wheel builds",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Build and publish all service wheelhouses or return a clear failure."""

    try:
        args = _parse_args(argv)
        if args.service_commit is not None:
            if args.service is None:
                raise ProducerError("--service-commit requires --service")
            if not COMMIT_PATTERN.fullmatch(args.service_commit):
                raise ProducerError("--service-commit must be a lowercase 40-character Git SHA")
        architecture = _native_target()
        workspace_root = args.workspace_root.resolve()
        output_root = args.output.expanduser().absolute()
        if not (workspace_root / "repositories.yaml").is_file():
            raise ProducerError(f"Workspace repositories.yaml is missing under {workspace_root}")
        release_lock_path = workspace_root / "release-lock.json"
        release_lock = _read_json_object(release_lock_path, "release-lock.json")
        if release_lock.get("schemaVersion") != 1 or not isinstance(
            release_lock.get("repositories"), dict
        ):
            raise ProducerError(
                "release-lock.json has an unsupported schema or missing repositories map"
            )
        supported_environment = release_lock.get("supportedEnvironment")
        python_lock = release_lock.get("python")
        supported_os = (
            supported_environment.get("os") if isinstance(supported_environment, dict) else None
        )
        if supported_os != "Ubuntu 24.04 x86_64":
            raise ProducerError(
                "wheelhouse target must match release-lock.json supportedEnvironment.os "
                f"(expected 'Ubuntu 24.04 x86_64', got {supported_os!r})"
            )
        python_runtime = python_lock.get("runtime") if isinstance(python_lock, dict) else None
        if python_runtime != "3.12":
            raise ProducerError(
                "wheelhouse target must match release-lock.json Python runtime 3.12"
            )
        repository_metadata = _load_repository_remotes(workspace_root / "repositories.yaml")
        selected_specs = tuple(
            spec for spec in SERVICE_SPECS if args.service is None or spec.service == args.service
        )
        runtime_sdk, runtime_sdk_manifest_digest, runtime_sdk_artifact_digest = (
            _load_runtime_sdk_wheel(args)
        )
        uv_executable = shutil.which(args.uv)
        if uv_executable is None:
            raise ProducerError(f"uv executable was not found: {args.uv}")
        python_executable = str(Path(sys.executable).resolve())
        output_root.parent.mkdir(parents=True, exist_ok=True)
        if output_root.is_symlink() or (output_root.exists() and not output_root.is_dir()):
            raise ProducerError(f"output root must be a real directory: {output_root}")
        if output_root.exists():
            existing_names = sorted(child.name for child in output_root.iterdir())
            expected_names = sorted(spec.service for spec in selected_specs)
            if existing_names and existing_names != expected_names:
                raise ProducerError(
                    "output root must be empty or contain exactly these selected service directories: "
                    + ", ".join(expected_names)
                )
            for spec in selected_specs:
                destination = output_root / spec.service
                if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
                    raise ProducerError(f"service output path must be a directory: {destination}")

        command_env = os.environ.copy()
        command_env["UV_PYTHON_DOWNLOADS"] = "never"
        command_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        with tempfile.TemporaryDirectory(
            prefix="cyrene-service-wheelhouse-", dir=output_root.parent
        ) as temporary:
            transaction_root = Path(temporary)
            checkout_root = transaction_root / "checkouts"
            checkout_root.mkdir()
            staging_root = transaction_root / "staged"
            staging_root.mkdir()
            checkout_cache: dict[tuple[str, str], Path] = {}
            staged_services: list[tuple[str, Path]] = []
            for spec in selected_specs:
                staged = _write_service(
                    spec=spec,
                    repository_metadata=repository_metadata,
                    release_lock=release_lock,
                    checkout_root=checkout_root,
                    checkout_cache=checkout_cache,
                    staging_root=staging_root,
                    uv_executable=uv_executable,
                    python_executable=python_executable,
                    command_env=command_env,
                    service_commit_override=(
                        args.service_commit if spec.service == args.service else None
                    ),
                    runtime_sdk=runtime_sdk,
                    runtime_sdk_manifest_digest=runtime_sdk_manifest_digest,
                    runtime_sdk_artifact_digest=runtime_sdk_artifact_digest,
                )
                staged_services.append((spec.service, staged))
            _publish_service_directories(output_root, staged_services, transaction_root)
        service_names = ", ".join(spec.service for spec in selected_specs)
        noun = "wheelhouse" if len(selected_specs) == 1 else "wheelhouses"
        print(
            f"Published {noun} for {service_names} to {output_root} "
            f"(linux/{architecture}, Python 3.12)"
        )
        return 0
    except (OSError, ProducerError, KeyError, TypeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
