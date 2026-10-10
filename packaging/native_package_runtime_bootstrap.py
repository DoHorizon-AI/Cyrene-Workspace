"""Verify the first signed Package Runtime admission and derive its source scope.

The module preserves the published Plugin descriptor byte-for-byte. GitHub
attestations are verified out of band, then the actual Package Runtime
installation receipt and current maintenance-source generation bind the
least-privilege source policy.
中文：验证首个签名插件包并按实际安装回执派生最小来源授权。
"""

from __future__ import annotations

import grp
import hashlib
import importlib.util
import io
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path
from typing import Any

PACKAGE_ID = "cyrene.training.llama-factory"
PACKAGE_VERSION = "0.1.0"
PACKAGE_CAPABILITY = "training.llama-factory.v1"
PACKAGE_INTERFACE_VERSION = "1"
PACKAGE_SOURCE_ID = "cyrene-yield"
PACKAGE_BINDING_ID = "yield.llama-factory.primary"
PACKAGE_BOOTSTRAP_PRODUCT_SOURCES = (
    ("cyrene-navigator", "cyrene-navigator.service", "navigator"),
    ("cyrene-yield", "cyrene-yield.service", "yield"),
    ("cyrene-reactor", "cyrene-reactor.service", "reactor"),
    ("cyrene-exchange", "cyrene-exchange.service", "exchange"),
    ("cyrene-catalyst", "cyrene-catalyst.service", "catalyst"),
)
PACKAGE_CHANNEL = "preview"
PACKAGE_SOURCE_REPOSITORY = "DoHorizon-AI/Cyrene-Plugins-Official"
PACKAGE_SOURCE_URL = f"https://github.com/{PACKAGE_SOURCE_REPOSITORY}"
PACKAGE_WORKFLOW = (
    "DoHorizon-AI/Cyrene-Plugins-Official/.github/workflows/training-plugin-package-release.yml"
)
PACKAGE_SOURCE_REF = "refs/heads/develop"
PACKAGE_SOURCE_COMMIT = "7b8315d2f88cc7147cb9f58d48c89d0dabe7bb36"
PACKAGE_TARGET_ID = "linux-x86_64"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GH_BINARY_SHA256 = "141507c337e8b202ad398550c3b73d72f5af92e86f71665214538a81efd4c409"
PACKAGE_POLICY_RELATIVE_PATH = Path("etc/cyrene/runtime-package-sources.json")
PACKAGE_BOOTSTRAP_STAGE_ROOT = Path("/var/lib/cyrene-updates/plugin-package-bootstrap")
PACKAGE_CANDIDATE_CACHE_ROOT = PACKAGE_BOOTSTRAP_STAGE_ROOT / "candidate"
PACKAGE_CANDIDATE_ASSETS = PACKAGE_CANDIDATE_CACHE_ROOT / "assets"
PACKAGE_CANDIDATE_ATTESTATIONS = PACKAGE_CANDIDATE_CACHE_ROOT / "attestations"
PACKAGE_PINNED_GH = PACKAGE_CANDIDATE_CACHE_ROOT / "tools" / "gh"
CYRENE_COMMAND = Path("/usr/bin/cyrene")
PACKAGE_RUNTIME_STATE_ROOT = Path("/var/lib/cyrene/package-runtime")
PACKAGE_RUNTIME_CONTROL_SOCKET = Path("/run/cyrene-package-runtime/control.sock")
PACKAGE_PREPARER_COMMAND = Path("/usr/libexec/cyrene-plugin-python-preparer")
PACKAGE_PREPARER_ARGS = (
    "--uv",
    "/opt/cyrene/uv/0.12.21/uv",
    "--python",
    "/opt/cyrene/python/3.12.14/bin/python3.12",
)
PACKAGE_PREPARER_PYTHON = Path(PACKAGE_PREPARER_ARGS[3])
PACKAGE_PREPARER_UV = Path(PACKAGE_PREPARER_ARGS[1])
WORKLOAD_PREPARER_ROOT = Path("/opt/cyrene/plugin-preparer")
WORKLOAD_PREPARER_RELEASES = WORKLOAD_PREPARER_ROOT / "releases"
WORKLOAD_PREPARER_VERSION = "0.2.0"
WORKLOAD_PREPARER_PYTHON_VERSION = "3.12.14"
WORKLOAD_PREPARER_MODULE = "cyrene_plugin_runtime.dependency_preparer"
WORKLOAD_PREPARER_ENTRYPOINT = "cyrene-plugin-python-preparer"
WORKLOAD_PREPARER_ENTRYPOINT_TARGET = "cyrene_plugin_runtime.dependency_preparer:main"
WORKLOAD_PREPARER_MAX_WHEEL_BYTES = 64 * 1024 * 1024
WORKLOAD_PREPARER_MAX_WHEEL_FILES = 4096
PACKAGE_RUNTIME_COMMAND = Path("/usr/bin/cy-package-runtime")
PACKAGE_POLICY_SCHEMA_VERSION = 1
PACKAGE_RUNTIME_GROUP = "cyrene"
PACKAGE_POLICY_OPERATIONS = (
    "activate",
    "deactivate",
    "recover",
)
PACKAGE_RUNTIME_OPERATIONS = (
    "activate",
    "deactivate",
    "get_installation",
    "recover_binding",
    "runtime_status",
)
RAW_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
TYPED_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
INSTALLATION_ID = re.compile(r"installation-[0-9a-f]{32}\Z")
PACKAGE_VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
MAX_RELEASE_ASSET_BYTES = 128 * 1024 * 1024
MAX_POLICY_BYTES = 1024 * 1024
MAX_BOOTSTRAP_INPUT_BYTES = 64 * 1024
MAX_DESCRIPTOR_BYTES = 1024 * 1024
CHECKSUMS_NAME = "attestation-subjects.sha256"

ASSET_NAMES = {
    "package_release": f"{PACKAGE_ID}-{PACKAGE_VERSION}-package-release.json",
    "descriptor": f"{PACKAGE_ID}-{PACKAGE_VERSION}-package-descriptor.json",
    "archive": f"{PACKAGE_ID}-{PACKAGE_VERSION}-linux-x86_64.zip",
    "dependency_lock": f"{PACKAGE_ID}-{PACKAGE_VERSION}-requirements.lock",
    "preparer_wheel": "cyrene_plugin_runtime-0.2.0-py3-none-any.whl",
}
ATTESTATION_NAMES = {name: f"{name}.attestation.jsonl" for name in ASSET_NAMES.values()}
_SUBJECT_KEYS = ("archive", "descriptor", "dependency_lock", "preparer_wheel")


class PackageRuntimeBootstrapError(ValueError):
    """Raised when immutable package proof or its derived scope is incomplete."""


@dataclass(frozen=True)
class VerifiedPackageCandidate:
    """Package identities established by exact bytes and official attestations."""

    package_id: str
    package_version: str
    capability: str
    interface_version: str
    source_ref: str
    source_commit: str
    release_digest: str
    descriptor_digest: str
    manifest_digest: str
    artifact_digest: str
    archive_digest: str
    dependency_lock_digest: str
    preparer_wheel_digest: str
    attestation_bundle_digests: dict[str, str]
    release_path: Path
    descriptor_path: Path
    archive_path: Path
    dependency_lock_path: Path
    preparer_wheel_path: Path


@dataclass(frozen=True)
class WorkloadPackageCandidate:
    """Exact generic package identity selected by a verified workload plan.

    The legacy fixed-package bootstrap keeps ``VerifiedPackageCandidate`` and its
    stricter Yield-only policy. Workload installs use this independent shape so
    a Catalog component ID can authorize a different dotted PackageId.
    中文：通用工作负载包保留 Catalog 组件 ID 与包运行时 PackageId 的双重身份。
    """

    component_id: str
    package_id: str
    package_version: str
    capability: str
    interface_version: str
    artifact_digest: str
    archive_digest: str
    descriptor_digest: str
    manifest_digest: str
    dependency_lock_digest: str
    descriptor_path: Path
    archive_path: Path


@dataclass(frozen=True)
class ActivityCatalogUpdate:
    """Complete init-catalog projection without credentials or source tokens."""

    source_arguments: tuple[str, ...]
    binding_scopes: dict[str, list[dict[str, Any]]]
    source_identity: dict[str, dict[str, Any]]
    expected_generation: int
    changed: bool


@dataclass(frozen=True)
class PackageBootstrapPlan:
    """A user-confirmable identity for one fixed immutable package candidate."""

    plan_id: str
    plan_digest: str
    catalog_generation: int
    gate_generation: int
    component_artifact_digests: dict[str, str]
    material: dict[str, Any]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PackageRuntimeBootstrapError(f"Package proof repeats JSON field {key!r}")
        result[key] = value
    return result


def _effective_uid() -> int:
    return os.geteuid()


def _require_root() -> None:
    if _effective_uid() != 0:
        raise PermissionError("Package Runtime first admission requires root")


def _runtime_group_id() -> int:
    try:
        return grp.getgrnam(PACKAGE_RUNTIME_GROUP).gr_gid
    except KeyError as error:
        raise PackageRuntimeBootstrapError("Cyrene runtime group is unavailable") from error


def _runtime_user_id() -> int:
    try:
        return pwd.getpwnam(PACKAGE_RUNTIME_GROUP).pw_uid
    except KeyError as error:
        raise PackageRuntimeBootstrapError("Cyrene runtime user is unavailable") from error


def _maintenance_group_id() -> int:
    try:
        return grp.getgrnam("cyrene-runtime-maintenance").gr_gid
    except KeyError as error:
        raise PackageRuntimeBootstrapError("Runtime maintenance group is unavailable") from error


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageRuntimeBootstrapError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise PackageRuntimeBootstrapError(f"{label} must be a JSON object")
    return value


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def build_package_bootstrap_plan(
    candidate: VerifiedPackageCandidate,
    *,
    catalog_generation: int,
    gate_generation: int,
    affected_product_units: list[dict[str, Any]],
) -> PackageBootstrapPlan:
    """Bind package proof, source identities, and the fixed Product cohort into a plan.

    The Product rows are derived from the installed signed cohort and current
    activity catalog by the updater. Their exact membership is part of user
    confirmation, so a changed client set cannot reuse an earlier approval.
    中文：将固定五个 Product 来源与单位纳入确认摘要，避免确认后改变受影响客户端。
    """

    if type(catalog_generation) is not int or catalog_generation < 1:
        raise PackageRuntimeBootstrapError("Package Runtime plan catalog generation is invalid")
    if type(gate_generation) is not int or gate_generation < 1:
        raise PackageRuntimeBootstrapError("Package Runtime plan gate generation is invalid")
    if not isinstance(affected_product_units, list) or len(affected_product_units) != len(
        PACKAGE_BOOTSTRAP_PRODUCT_SOURCES
    ):
        raise PackageRuntimeBootstrapError("Package Runtime Product cohort is incomplete")
    normalized_products: list[dict[str, Any]] = []
    for row, (source_id, unit, service) in zip(
        affected_product_units, PACKAGE_BOOTSTRAP_PRODUCT_SOURCES, strict=True
    ):
        if (
            not isinstance(row, dict)
            or set(row)
            != {"source_id", "component_id", "unit", "service", "uid", "gid", "source_token_sha256"}
            or row.get("source_id") != source_id
            or row.get("component_id") != source_id
            or row.get("unit") != unit
            or row.get("service") != service
            or type(row.get("uid")) is not int
            or row["uid"] < 0
            or type(row.get("gid")) is not int
            or row["gid"] < 0
            or not isinstance(row.get("source_token_sha256"), str)
            or RAW_SHA256.fullmatch(row["source_token_sha256"]) is None
        ):
            raise PackageRuntimeBootstrapError("Package Runtime Product cohort identity is invalid")
        normalized_products.append(dict(row))
    material = {
        "schema_version": 1,
        "target_kind": "PACKAGE_ONLY",
        "package_id": candidate.package_id,
        "package_version": candidate.package_version,
        "capability": candidate.capability,
        "interface_version": candidate.interface_version,
        "channel": PACKAGE_CHANNEL,
        "source_repository": PACKAGE_SOURCE_REPOSITORY,
        "source_ref": candidate.source_ref,
        "source_commit": candidate.source_commit,
        "release_digest": candidate.release_digest,
        "descriptor_digest": candidate.descriptor_digest,
        "manifest_digest": candidate.manifest_digest,
        "artifact_digest": candidate.artifact_digest,
        "archive_digest": candidate.archive_digest,
        "dependency_lock_digest": candidate.dependency_lock_digest,
        "preparer_wheel_digest": candidate.preparer_wheel_digest,
        "attestation_bundle_digests": dict(candidate.attestation_bundle_digests),
        "binding_id": PACKAGE_BINDING_ID,
        "affected_product_units": normalized_products,
        "catalog_generation": catalog_generation,
        "gate_generation": gate_generation,
    }
    plan_digest = "sha256:" + hashlib.sha256(_canonical_json(material)).hexdigest()
    plan_id = "plan-" + plan_digest.removeprefix("sha256:")[:32]
    return PackageBootstrapPlan(
        plan_id=plan_id,
        plan_digest=plan_digest,
        catalog_generation=catalog_generation,
        gate_generation=gate_generation,
        component_artifact_digests={candidate.package_id: candidate.artifact_digest},
        material=material,
    )


def validate_package_bootstrap_confirmation(plan: PackageBootstrapPlan, confirmation: Any) -> None:
    """Require affirmative confirmation of the full locally derived plan identity."""

    if (
        not isinstance(confirmation, dict)
        or set(confirmation) != {"plan_id", "plan_digest", "confirmed"}
        or confirmation.get("plan_id") != plan.plan_id
        or confirmation.get("plan_digest") != plan.plan_digest
        or confirmation.get("confirmed") is not True
    ):
        raise PackageRuntimeBootstrapError(
            "Package Runtime bootstrap requires confirmation bound to the exact candidate"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _require_regular_file(
    path: Path, label: str, *, max_bytes: int = MAX_RELEASE_ASSET_BYTES
) -> os.stat_result:
    if path.is_symlink():
        raise PackageRuntimeBootstrapError(f"{label} must not be a symlink")
    try:
        info = path.lstat()
    except OSError as error:
        raise PackageRuntimeBootstrapError(f"{label} is unavailable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != _effective_uid()
        or stat.S_IMODE(info.st_mode) & 0o022
        or info.st_nlink != 1
        or info.st_size > max_bytes
    ):
        raise PackageRuntimeBootstrapError(f"{label} metadata is unsafe")
    return info


def _require_private_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise PackageRuntimeBootstrapError(f"{label} directory is missing or unsafe")
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != _effective_uid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise PackageRuntimeBootstrapError(f"{label} directory is not owner-controlled")


def _validate_release_metadata(
    metadata: dict[str, Any], asset_hashes: dict[str, str]
) -> dict[str, Any]:
    expected_top_level = {
        "artifact_uri",
        "assets",
        "attestation_policy",
        "channel",
        "package",
        "publication_status",
        "record_type",
        "release_tag",
        "runtime",
        "source",
        "spec_version",
        "target",
    }
    if set(metadata) != expected_top_level:
        raise PackageRuntimeBootstrapError("Package release metadata has an unsupported shape")
    source = metadata.get("source")
    package = metadata.get("package")
    target = metadata.get("target")
    release_tag = f"{PACKAGE_CHANNEL}-{PACKAGE_ID}-{PACKAGE_VERSION}-{PACKAGE_SOURCE_COMMIT}"
    expected_artifact_uri = (
        f"https://github.com/{PACKAGE_SOURCE_REPOSITORY}/releases/download/"
        f"{release_tag}/{ASSET_NAMES['archive']}"
    )
    if not isinstance(source, dict) or set(source) != {
        "commit",
        "input_tree_digest",
        "inputs",
        "ref",
        "repository",
    }:
        raise PackageRuntimeBootstrapError("Package source provenance has an unsupported shape")
    source_inputs = source.get("inputs")
    input_paths = []
    if isinstance(source_inputs, list):
        for entry in source_inputs:
            if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
                raise PackageRuntimeBootstrapError("Package source input inventory is malformed")
            input_path = entry.get("path")
            input_digest = entry.get("sha256")
            if (
                not isinstance(input_path, str)
                or not input_path
                or input_path.startswith("/")
                or ".." in Path(input_path).parts
                or not isinstance(input_digest, str)
                or TYPED_SHA256.fullmatch(input_digest) is None
            ):
                raise PackageRuntimeBootstrapError("Package source input identity is invalid")
            input_paths.append(input_path)
    package_fields = {
        "capability",
        "entrypoint",
        "id",
        "interface_version",
        "version",
    }
    target_fields = {"architecture", "id", "os", "python"}
    if (
        metadata.get("record_type") != "cyrene.plugin.package.release.v1"
        or metadata.get("publication_status") != "PUBLISHED"
        or metadata.get("channel") != PACKAGE_CHANNEL
        or metadata.get("spec_version") != "1"
        or metadata.get("release_tag") != release_tag
        or metadata.get("artifact_uri") != expected_artifact_uri
        or source.get("repository") != PACKAGE_SOURCE_URL
        or source.get("ref") != PACKAGE_SOURCE_REF
        or source.get("commit") != PACKAGE_SOURCE_COMMIT
        or not isinstance(source.get("input_tree_digest"), str)
        or TYPED_SHA256.fullmatch(source["input_tree_digest"]) is None
        or not isinstance(source.get("inputs"), list)
        or not source["inputs"]
        or input_paths != sorted(input_paths)
        or len(set(input_paths)) != len(input_paths)
        or not isinstance(package, dict)
        or set(package) != package_fields
        or package.get("id") != PACKAGE_ID
        or package.get("version") != PACKAGE_VERSION
        or package.get("capability") != PACKAGE_CAPABILITY
        or package.get("interface_version") != PACKAGE_INTERFACE_VERSION
        or package.get("entrypoint") != "llama_factory:LlamaFactoryTrainingPlugin"
        or not isinstance(target, dict)
        or set(target) != target_fields
        or target.get("id") != PACKAGE_TARGET_ID
        or target.get("os") != "linux"
        or target.get("architecture") != "x86_64"
        or target.get("python") != ">=3.11"
    ):
        raise PackageRuntimeBootstrapError(
            "Package release identity differs from the pinned LLF release"
        )

    assets = metadata.get("assets")
    expected_roles = {"package", "descriptor", "dependency_lock", "preparer_wheel"}
    if not isinstance(assets, dict) or set(assets) != expected_roles:
        raise PackageRuntimeBootstrapError("Package release asset roles are incomplete")
    asset_specs = {
        "package": ("archive", {"content_sha256", "format", "name", "sha256"}),
        "descriptor": ("descriptor", {"format", "name", "sha256"}),
        "dependency_lock": ("dependency_lock", {"format", "name", "package_ref", "sha256"}),
        "preparer_wheel": (
            "preparer_wheel",
            {"entrypoint", "format", "name", "package", "sha256", "target", "version"},
        ),
    }
    normalized: dict[str, Any] = {}
    for role, (asset_key, fields) in asset_specs.items():
        value = assets.get(role)
        if not isinstance(value, dict) or set(value) != fields:
            raise PackageRuntimeBootstrapError(f"Package release {role} metadata is malformed")
        name = value.get("name")
        expected_name = ASSET_NAMES[asset_key]
        if name != expected_name or value.get("sha256") != asset_hashes[asset_key]:
            raise PackageRuntimeBootstrapError(f"Package release {role} bytes differ from metadata")
        normalized[asset_key] = value
    package_asset = normalized["archive"]
    wheel = normalized["preparer_wheel"]
    if (
        package_asset.get("format") != "zip"
        or not isinstance(package_asset.get("content_sha256"), str)
        or TYPED_SHA256.fullmatch(package_asset["content_sha256"]) is None
        or normalized["descriptor"].get("format") != "json"
        or normalized["dependency_lock"].get("format") != "requirements.lock"
        or normalized["dependency_lock"].get("package_ref") != "requirements.lock"
        or wheel.get("format") != "wheel"
        or wheel.get("package") != "cyrene-plugin-runtime"
        or wheel.get("target") != "py3-none-any"
        or wheel.get("version") != "0.2.0"
        or wheel.get("entrypoint") != "cyrene-plugin-python-preparer"
    ):
        raise PackageRuntimeBootstrapError(
            "Package runtime assets differ from the LLF release contract"
        )

    runtime = metadata.get("runtime")
    if not isinstance(runtime, dict) or set(runtime) != {
        "launch_executable",
        "preparer_command",
        "preparer_configuration",
        "preparer_package",
        "preparer_wheel_install_command",
        "protocol",
    }:
        raise PackageRuntimeBootstrapError("Package runtime metadata has an unsupported shape")
    preparer = runtime.get("preparer_configuration")
    if (
        runtime.get("launch_executable") != "prepared-runtime"
        or runtime.get("preparer_command") != "cyrene-plugin-python-preparer"
        or runtime.get("preparer_package") != "cyrene-plugin-runtime==0.2.0"
        or runtime.get("protocol") != "cyrene.plugin.runtime.v1.DirectPluginRuntime"
        or not isinstance(preparer, dict)
        or set(preparer)
        != {"arguments", "evidence_protocol", "host_api", "runtime_executable_is_consumed_by"}
        or preparer.get("arguments")
        != ["--uv", "<uv executable>", "--python", "<python >=3.11 executable>"]
        or preparer.get("evidence_protocol") != "cyrene.package-dependency-preparer.v1"
        or preparer.get("host_api") != "cy-package-runtime::CommandDependencyPreparer"
        or preparer.get("runtime_executable_is_consumed_by") != "ProcessPluginServiceSupervisor"
        or runtime.get("preparer_wheel_install_command")
        != [
            "<python >=3.11 executable>",
            "-m",
            "pip",
            "install",
            "--no-deps",
            "<verified preparer wheel path>",
        ]
    ):
        raise PackageRuntimeBootstrapError(
            "Package preparer runtime contract differs from signed metadata"
        )

    attestation_policy = metadata.get("attestation_policy")
    if not isinstance(attestation_policy, dict) or set(attestation_policy) != {
        "provider",
        "source_commit",
        "subject_assets",
        "workflow",
    }:
        raise PackageRuntimeBootstrapError("Package attestation policy has an unsupported shape")
    expected_subjects = [
        {"name": ASSET_NAMES[key], "sha256": asset_hashes[key]} for key in _SUBJECT_KEYS
    ]
    if (
        attestation_policy.get("provider") != "github-artifact-attestation"
        or attestation_policy.get("source_commit") != PACKAGE_SOURCE_COMMIT
        or attestation_policy.get("workflow") != PACKAGE_WORKFLOW
        or attestation_policy.get("subject_assets") != expected_subjects
    ):
        raise PackageRuntimeBootstrapError(
            "Package metadata does not bind the exact attestation subjects"
        )
    return {"package": package, "assets": normalized}


def _verified_manifest_digest(archive_path: Path) -> str:
    """Read the unique top-level Plugin manifest without extracting the archive."""

    try:
        with zipfile.ZipFile(archive_path) as archive:
            matches = [
                info for info in archive.infolist() if info.filename == "plugin.manifest.json"
            ]
            if len(matches) != 1:
                raise PackageRuntimeBootstrapError(
                    "Package archive must contain one top-level Plugin manifest"
                )
            info = matches[0]
            unix_mode = info.external_attr >> 16
            file_type = stat.S_IFMT(unix_mode)
            if info.is_dir() or info.file_size > 1024 * 1024 or file_type not in {0, stat.S_IFREG}:
                raise PackageRuntimeBootstrapError("Package Plugin manifest entry is unsafe")
            manifest_bytes = archive.read(info)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise PackageRuntimeBootstrapError("Package archive is not a valid ZIP file") from error
    manifest = json.loads(manifest_bytes.decode("utf-8"), object_pairs_hook=_unique_object)
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schemaVersion")) is not int
        or manifest["schemaVersion"] != 1
        or manifest.get("id") != PACKAGE_ID
        or manifest.get("version") != PACKAGE_VERSION
        or manifest.get("capabilities") != [PACKAGE_CAPABILITY]
        or not isinstance(manifest.get("runtime"), dict)
        or manifest["runtime"].get("entrypoint") != "llama_factory:LlamaFactoryTrainingPlugin"
    ):
        raise PackageRuntimeBootstrapError("Package archive Plugin manifest identity differs")
    return "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()


def _verify_attestation(
    subject: Path, bundle: Path, *, gh_executable: Path, source_commit: str, label: str
) -> None:
    command = [
        str(gh_executable),
        "attestation",
        "verify",
        str(subject),
        "--bundle",
        str(bundle),
        "--repo",
        PACKAGE_SOURCE_REPOSITORY,
        "--signer-workflow",
        PACKAGE_WORKFLOW,
        "--source-ref",
        PACKAGE_SOURCE_REF,
        "--source-digest",
        source_commit,
        "--cert-oidc-issuer",
        OIDC_ISSUER,
        "--predicate-type",
        PREDICATE_TYPE,
    ]
    with tempfile.TemporaryDirectory(prefix="cyrene-package-gh-config-") as config_directory:
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": config_directory,
            "GH_CONFIG_DIR": config_directory,
            "XDG_CONFIG_HOME": config_directory,
        }
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
            env=environment,
        )
    if result.returncode != 0:
        raise PackageRuntimeBootstrapError(f"Official detached attestation failed for {label}")


def verify_signed_package_candidate(
    assets_directory: Path,
    attestations_directory: Path,
    gh_executable: Path,
) -> VerifiedPackageCandidate:
    """Verify all five immutable release subjects before accepting a package.

    Descriptor reference fields are validated in their published null form;
    attestation evidence remains the separate detached-bundle set.
    """

    assets_directory = Path(assets_directory)
    attestations_directory = Path(attestations_directory)
    gh_executable = Path(gh_executable)
    _require_private_directory(assets_directory, "Package asset")
    _require_private_directory(attestations_directory, "Package attestation")
    if (
        gh_executable.is_symlink()
        or not gh_executable.is_file()
        or not os.access(gh_executable, os.X_OK)
    ):
        raise PackageRuntimeBootstrapError("Pinned GitHub CLI is missing or unsafe")
    gh_info = _require_regular_file(gh_executable, "Pinned GitHub CLI", max_bytes=512 * 1024 * 1024)
    if gh_info.st_mode & 0o111 == 0 or _sha256_file(gh_executable) != f"sha256:{GH_BINARY_SHA256}":
        raise PackageRuntimeBootstrapError("GitHub CLI does not match the pinned binary")

    expected_names = set(ASSET_NAMES.values()) | {CHECKSUMS_NAME}
    if {entry.name for entry in assets_directory.iterdir()} != expected_names:
        raise PackageRuntimeBootstrapError("Package release assets are missing or unexpected")
    if {entry.name for entry in attestations_directory.iterdir()} != set(
        ATTESTATION_NAMES.values()
    ):
        raise PackageRuntimeBootstrapError("Package detached proof set is missing or unexpected")
    asset_paths: dict[str, Path] = {}
    asset_hashes: dict[str, str] = {}
    for key, name in ASSET_NAMES.items():
        path = assets_directory / name
        _require_regular_file(path, f"Package asset {name}")
        asset_paths[key] = path
        asset_hashes[key] = _sha256_file(path)
    _validate_subject_checksum_file(assets_directory / CHECKSUMS_NAME, asset_hashes)
    attestation_paths: dict[str, Path] = {}
    for name in ASSET_NAMES.values():
        path = attestations_directory / ATTESTATION_NAMES[name]
        _require_regular_file(path, f"Package attestation {name}")
        attestation_paths[name] = path

    metadata = _load_json(asset_paths["package_release"], "Package release metadata")
    release_identity = _validate_release_metadata(metadata, asset_hashes)
    release_assets = release_identity["assets"]
    descriptor = _load_json(asset_paths["descriptor"], "Package descriptor")
    if set(descriptor) != {
        "capability",
        "dependencies",
        "implementation",
        "integrity",
        "package",
        "provenance",
        "publication_status",
        "record_type",
        "spec_version",
    }:
        raise PackageRuntimeBootstrapError("Package descriptor has an unsupported shape")
    implementation = descriptor.get("implementation")
    implementation_artifact = (
        implementation.get("artifact") if isinstance(implementation, dict) else None
    )

    integrity = descriptor.get("integrity")
    provenance = descriptor.get("provenance")
    capability = descriptor.get("capability")
    dependencies = descriptor.get("dependencies")
    descriptor_lock = dependencies.get("lock") if isinstance(dependencies, dict) else None
    if (
        descriptor.get("record_type") != "package_descriptor"
        or descriptor.get("publication_status") != "PUBLISHED"
        or descriptor.get("spec_version") != "0.1"
        or descriptor.get("package")
        != {"id": PACKAGE_ID, "manifest_ref": "plugin.manifest.json", "version": PACKAGE_VERSION}
        or capability != {"id": PACKAGE_CAPABILITY, "interface_version": PACKAGE_INTERFACE_VERSION}
        or not isinstance(implementation_artifact, dict)
        or set(implementation_artifact) != {"digest", "format", "status", "uri"}
        or implementation_artifact.get("status") != "PUBLISHED"
        or implementation_artifact.get("format") != "zip"
        or implementation_artifact.get("digest") != release_assets["archive"].get("content_sha256")
        or implementation_artifact.get("uri") != metadata.get("artifact_uri")
        or not isinstance(integrity, dict)
        or set(integrity) != {"archive_digest", "artifact_digest", "signature_ref"}
        or integrity.get("archive_digest") != asset_hashes["archive"]
        or integrity.get("artifact_digest") != release_assets["archive"].get("content_sha256")
        or integrity.get("signature_ref") is not None
        or not isinstance(provenance, dict)
        or set(provenance) != {"attestation_ref", "builder", "source_repository", "source_revision"}
        or provenance.get("attestation_ref") is not None
        or provenance.get("source_repository") != PACKAGE_SOURCE_URL
        or provenance.get("source_revision") != PACKAGE_SOURCE_COMMIT
        or provenance.get("builder") != PACKAGE_WORKFLOW
        or not isinstance(descriptor_lock, dict)
        or set(descriptor_lock) != {"digest", "format", "ref", "status"}
        or descriptor_lock.get("digest") != asset_hashes["dependency_lock"]
        or descriptor_lock.get("format") != "requirements.lock"
        or descriptor_lock.get("ref") != "requirements.lock"
        or descriptor_lock.get("status") != "LOCKED"
    ):
        raise PackageRuntimeBootstrapError(
            "Original package descriptor differs from signed release metadata"
        )

    bundle_digests: dict[str, str] = {}
    for key, name in ASSET_NAMES.items():
        bundle_digest = _sha256_file(attestation_paths[name])
        _verify_attestation(
            asset_paths[key],
            attestation_paths[name],
            gh_executable=gh_executable,
            source_commit=PACKAGE_SOURCE_COMMIT,
            label=name,
        )
        if _sha256_file(attestation_paths[name]) != bundle_digest:
            raise PackageRuntimeBootstrapError("Package attestation bundle changed during verify")
        bundle_digests[name] = bundle_digest

    manifest_digest = _verified_manifest_digest(asset_paths["archive"])
    return VerifiedPackageCandidate(
        package_id=PACKAGE_ID,
        package_version=PACKAGE_VERSION,
        capability=PACKAGE_CAPABILITY,
        interface_version=PACKAGE_INTERFACE_VERSION,
        source_ref=PACKAGE_SOURCE_REF,
        source_commit=PACKAGE_SOURCE_COMMIT,
        release_digest=asset_hashes["package_release"],
        descriptor_digest=asset_hashes["descriptor"],
        manifest_digest=manifest_digest,
        artifact_digest=release_assets["archive"]["content_sha256"],
        archive_digest=asset_hashes["archive"],
        dependency_lock_digest=asset_hashes["dependency_lock"],
        preparer_wheel_digest=asset_hashes["preparer_wheel"],
        attestation_bundle_digests=bundle_digests,
        release_path=asset_paths["package_release"],
        descriptor_path=asset_paths["descriptor"],
        archive_path=asset_paths["archive"],
        dependency_lock_path=asset_paths["dependency_lock"],
        preparer_wheel_path=asset_paths["preparer_wheel"],
    )


def verify_cached_package_candidate() -> VerifiedPackageCandidate:
    """Reverify the exact root-preloaded cache using the pinned GH binary."""

    for directory, mode, label in (
        (PACKAGE_BOOTSTRAP_STAGE_ROOT, 0o700, "Updater Package Runtime cache"),
        (PACKAGE_CANDIDATE_CACHE_ROOT, 0o700, "Package candidate cache"),
        (PACKAGE_CANDIDATE_ASSETS, 0o700, "Package candidate assets"),
        (PACKAGE_CANDIDATE_ATTESTATIONS, 0o700, "Package candidate attestations"),
        (PACKAGE_PINNED_GH.parent, 0o755, "Pinned GitHub CLI tools"),
    ):
        _require_private_directory(directory, label)
        if stat.S_IMODE(directory.lstat().st_mode) != mode:
            raise PackageRuntimeBootstrapError(f"{label} directory mode differs")
    expected_asset_names = set(ASSET_NAMES.values())
    current_asset_names = {entry.name for entry in PACKAGE_CANDIDATE_ASSETS.iterdir()}
    if current_asset_names not in (
        expected_asset_names,
        expected_asset_names | {CHECKSUMS_NAME},
    ):
        raise PackageRuntimeBootstrapError(
            "Package candidate asset cache contains unexpected entries"
        )
    asset_hashes: dict[str, str] = {}
    for key, asset_name in ASSET_NAMES.items():
        asset_path = PACKAGE_CANDIDATE_ASSETS / asset_name
        _require_regular_file(asset_path, f"Package asset {asset_name}")
        asset_hashes[key] = _sha256_file(asset_path)
    checksum_path = PACKAGE_CANDIDATE_ASSETS / CHECKSUMS_NAME
    if CHECKSUMS_NAME not in current_asset_names:
        checksum_content = "".join(
            f"{asset_hashes[key].removeprefix('sha256:')}  {ASSET_NAMES[key]}\n"
            for key in sorted(ASSET_NAMES)
        ).encode("ascii")
        _write_root_file(checksum_path, checksum_content, 0o600)
    else:
        _validate_subject_checksum_file(checksum_path, asset_hashes)
    if {entry.name for entry in PACKAGE_CANDIDATE_CACHE_ROOT.iterdir()} != {
        "assets",
        "attestations",
        "tools",
    } or {entry.name for entry in PACKAGE_PINNED_GH.parent.iterdir()} != {"gh"}:
        raise PackageRuntimeBootstrapError("Package candidate cache contains unexpected entries")
    return verify_signed_package_candidate(
        PACKAGE_CANDIDATE_ASSETS,
        PACKAGE_CANDIDATE_ATTESTATIONS,
        PACKAGE_PINNED_GH,
    )


def candidate_identity(candidate: Any) -> dict[str, Any]:
    """Return stable candidate identities without exposing payload or credentials."""

    fields = (
        "package_id",
        "package_version",
        "capability",
        "interface_version",
        "source_ref",
        "source_commit",
        "release_digest",
        "descriptor_digest",
        "manifest_digest",
        "artifact_digest",
        "archive_digest",
        "dependency_lock_digest",
        "preparer_wheel_digest",
        "attestation_bundle_digests",
        "release_path",
        "descriptor_path",
        "archive_path",
        "dependency_lock_path",
        "preparer_wheel_path",
    )
    if any(not hasattr(candidate, field) for field in fields):
        raise PackageRuntimeBootstrapError("Package Runtime candidate object is incomplete")
    return {field: getattr(candidate, field) for field in fields}


def reverify_package_candidate(candidate: Any, gh_executable: Path) -> VerifiedPackageCandidate:
    """Re-run official proof validation from the candidate's fixed cache layout."""

    identity = candidate_identity(candidate)
    release_path = identity["release_path"]
    if not isinstance(release_path, Path) or release_path.name != ASSET_NAMES["package_release"]:
        raise PackageRuntimeBootstrapError("Package Runtime release cache path is not fixed")
    asset_directory = release_path.parent
    proof_directory = asset_directory.parent / "attestations"
    verified = verify_signed_package_candidate(asset_directory, proof_directory, gh_executable)
    if candidate_identity(verified) != identity:
        raise PackageRuntimeBootstrapError("Package Runtime candidate changed since plan review")
    return verified


def _validate_subject_checksum_file(path: Path, asset_hashes: dict[str, str]) -> None:
    """Check the downloader's inventory as reconciliation only, never as proof."""

    _require_regular_file(path, "Package subject checksum inventory", max_bytes=64 * 1024)
    expected = {ASSET_NAMES[key]: asset_hashes[key].removeprefix("sha256:") for key in ASSET_NAMES}
    actual: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise PackageRuntimeBootstrapError(
            "Package subject checksum inventory is invalid"
        ) from error
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)", line)
        if match is None or match.group(2) in actual:
            raise PackageRuntimeBootstrapError("Package subject checksum inventory is malformed")
        actual[match.group(2)] = match.group(1)
    if actual != expected:
        raise PackageRuntimeBootstrapError(
            "Package subject checksum inventory differs from asset bytes"
        )


def _expected_installation_id(candidate: VerifiedPackageCandidate) -> str:
    material = f"{candidate.package_id}\0{candidate.package_version}\0{candidate.artifact_digest}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"installation-{digest[:32]}"


def validate_installation_record(
    candidate: VerifiedPackageCandidate, record: Any
) -> dict[str, Any]:
    """Validate the actual Package Runtime receipt against the signed candidate."""

    expected_fields = {
        "record_version",
        "installation_id",
        "package_id",
        "package_version",
        "artifact_digest",
        "archive_digest",
        "capabilities",
        "state",
        "verification",
        "dependencies",
        "installed_at_unix_ms",
    }
    if not isinstance(record, dict) or set(record) != expected_fields:
        raise PackageRuntimeBootstrapError(
            "Package Runtime installation receipt has an unsupported shape"
        )
    verification = record.get("verification")
    dependencies = record.get("dependencies")
    if not isinstance(verification, dict) or set(verification) != {
        "verifier",
        "verified_at_unix_ms",
        "artifact_digest",
        "archive_digest",
        "descriptor_digest",
        "manifest_digest",
        "dependency_lock_digest",
    }:
        raise PackageRuntimeBootstrapError("Package Runtime verification receipt is incomplete")
    if not isinstance(dependencies, dict) or set(dependencies) != {
        "preparer",
        "prepared_at_unix_ms",
        "lock_digest",
        "runtime_digest",
        "runtime_executable",
    }:
        raise PackageRuntimeBootstrapError("Package Runtime dependency receipt is incomplete")
    if (
        type(record.get("record_version")) is not int
        or record["record_version"] != 1
        or record.get("installation_id") != _expected_installation_id(candidate)
        or INSTALLATION_ID.fullmatch(str(record.get("installation_id"))) is None
        or record.get("package_id") != candidate.package_id
        or record.get("package_version") != candidate.package_version
        or record.get("artifact_digest") != candidate.artifact_digest
        or record.get("archive_digest") != candidate.archive_digest
        or record.get("capabilities") != [candidate.capability]
        or record.get("state") != "INSTALLED"
        or type(record.get("installed_at_unix_ms")) is not int
        or record["installed_at_unix_ms"] < 1
        or not isinstance(verification.get("verifier"), str)
        or not verification["verifier"]
        or type(verification.get("verified_at_unix_ms")) is not int
        or verification["verified_at_unix_ms"] < 1
        or verification.get("artifact_digest") != candidate.artifact_digest
        or verification.get("archive_digest") != candidate.archive_digest
        or verification.get("descriptor_digest") != candidate.descriptor_digest
        or verification.get("manifest_digest") != candidate.manifest_digest
        or verification.get("dependency_lock_digest") != candidate.dependency_lock_digest
        or not isinstance(dependencies.get("preparer"), str)
        or not dependencies["preparer"]
        or type(dependencies.get("prepared_at_unix_ms")) is not int
        or dependencies["prepared_at_unix_ms"] < 1
        or dependencies.get("lock_digest") != candidate.dependency_lock_digest
        or not isinstance(dependencies.get("runtime_digest"), str)
        or TYPED_SHA256.fullmatch(dependencies["runtime_digest"]) is None
        or not isinstance(dependencies.get("runtime_executable"), str)
        or not Path(dependencies["runtime_executable"]).is_absolute()
    ):
        raise PackageRuntimeBootstrapError(
            "Package Runtime installation receipt differs from signed bytes"
        )
    return record


def build_offline_install_input(
    candidate: VerifiedPackageCandidate,
    *,
    request_id: str,
    descriptor_path: Path,
    archive_path: Path,
    maintenance: Any,
) -> dict[str, Any]:
    """Build the one-shot installer request from an exact active hold."""

    if not isinstance(maintenance, dict) or set(maintenance) != {
        "transaction_id",
        "maintenance_token",
        "target_kind",
        "plan_id",
        "plan_digest",
        "component_artifact_digests",
        "expected_gate_generation",
        "expected_catalog_generation",
    }:
        raise PackageRuntimeBootstrapError("Package Runtime maintenance hold is malformed")
    digest_map = maintenance.get("component_artifact_digests")
    if (
        not isinstance(request_id, str)
        or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}", request_id)
        or not isinstance(descriptor_path, Path)
        or not descriptor_path.is_absolute()
        or not isinstance(archive_path, Path)
        or not archive_path.is_absolute()
        or not isinstance(maintenance.get("transaction_id"), str)
        or not maintenance["transaction_id"]
        or not isinstance(maintenance.get("maintenance_token"), str)
        or not maintenance["maintenance_token"]
        or maintenance.get("target_kind") != "PACKAGE_ONLY"
        or not isinstance(maintenance.get("plan_id"), str)
        or not maintenance["plan_id"]
        or not isinstance(maintenance.get("plan_digest"), str)
        or TYPED_SHA256.fullmatch(maintenance["plan_digest"]) is None
        or not isinstance(digest_map, dict)
        or digest_map.get(candidate.package_id) != candidate.artifact_digest
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or TYPED_SHA256.fullmatch(value) is None
            for key, value in digest_map.items()
        )
        or type(maintenance.get("expected_gate_generation")) is not int
        or maintenance["expected_gate_generation"] < 1
        or type(maintenance.get("expected_catalog_generation")) is not int
        or maintenance["expected_catalog_generation"] < 1
    ):
        raise PackageRuntimeBootstrapError(
            "Package Runtime maintenance hold differs from candidate"
        )
    return {
        "schema_version": 1,
        "request_id": request_id,
        "maintenance": dict(maintenance),
        "candidate": {
            "descriptor_path": str(descriptor_path),
            "archive_path": str(archive_path),
            "component_id": candidate.package_id,
            "package_id": candidate.package_id,
            "package_version": candidate.package_version,
            "artifact_digest": candidate.artifact_digest,
            "archive_digest": candidate.archive_digest,
            "descriptor_digest": candidate.descriptor_digest,
            "manifest_digest": candidate.manifest_digest,
            "dependency_lock_digest": candidate.dependency_lock_digest,
        },
    }


def build_workload_offline_install_input(
    candidate: WorkloadPackageCandidate,
    *,
    request_id: str,
    maintenance: Any,
    descriptor_path: Path,
    archive_path: Path,
) -> dict[str, Any]:
    """Build the Platform request for one Catalog-selected plugin package.

    Unlike ``build_offline_install_input``, the hold digest map is keyed by the
    Catalog component ID and the Platform artifact digest remains distinct from
    the ZIP archive digest. 中文：按 Catalog componentId 绑定 maintenance hold。
    """

    if not isinstance(candidate, WorkloadPackageCandidate):
        raise PackageRuntimeBootstrapError("Workload package candidate is invalid")
    if not isinstance(maintenance, dict) or set(maintenance) != {
        "transaction_id",
        "maintenance_token",
        "target_kind",
        "plan_id",
        "plan_digest",
        "component_artifact_digests",
        "expected_gate_generation",
        "expected_catalog_generation",
    }:
        raise PackageRuntimeBootstrapError("Package Runtime maintenance hold is malformed")
    digest_map = maintenance.get("component_artifact_digests")
    if (
        not isinstance(request_id, str)
        or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}", request_id) is None
        or not isinstance(descriptor_path, Path)
        or not descriptor_path.is_absolute()
        or not isinstance(archive_path, Path)
        or not archive_path.is_absolute()
        or not isinstance(maintenance.get("transaction_id"), str)
        or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}", maintenance["transaction_id"]) is None
        or not isinstance(maintenance.get("maintenance_token"), str)
        or not maintenance["maintenance_token"]
        or maintenance.get("target_kind") != "PACKAGE_ONLY"
        or not isinstance(maintenance.get("plan_id"), str)
        or re.fullmatch(r"plan-[0-9a-f]{32}", maintenance["plan_id"]) is None
        or not isinstance(maintenance.get("plan_digest"), str)
        or TYPED_SHA256.fullmatch(maintenance["plan_digest"]) is None
        or not isinstance(digest_map, dict)
        or digest_map.get(candidate.component_id) != candidate.artifact_digest
        or not digest_map
        or any(
            not isinstance(key, str)
            or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", key) is None
            or not isinstance(value, str)
            or TYPED_SHA256.fullmatch(value) is None
            for key, value in digest_map.items()
        )
        or type(maintenance.get("expected_gate_generation")) is not int
        or maintenance["expected_gate_generation"] < 1
        or type(maintenance.get("expected_catalog_generation")) is not int
        or maintenance["expected_catalog_generation"] < 1
    ):
        raise PackageRuntimeBootstrapError(
            "Package Runtime maintenance hold differs from candidate"
        )
    for value in (
        candidate.artifact_digest,
        candidate.archive_digest,
        candidate.descriptor_digest,
        candidate.manifest_digest,
        candidate.dependency_lock_digest,
    ):
        if not isinstance(value, str) or TYPED_SHA256.fullmatch(value) is None:
            raise PackageRuntimeBootstrapError("Workload package digest is invalid")
    if (
        re.fullmatch(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*){1,7}", candidate.package_id) is None
        or re.fullmatch(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*){1,7}", candidate.capability) is None
        or PACKAGE_VERSION_PATTERN.fullmatch(candidate.package_version) is None
        or not candidate.component_id
        or not descriptor_path.name == "descriptor.json"
        or not archive_path.name == "archive.zip"
    ):
        raise PackageRuntimeBootstrapError("Workload package identity is malformed")
    return {
        "schema_version": 1,
        "request_id": request_id,
        "maintenance": dict(maintenance),
        "candidate": {
            "descriptor_path": str(descriptor_path),
            "archive_path": str(archive_path),
            "component_id": candidate.component_id,
            "package_id": candidate.package_id,
            "package_version": candidate.package_version,
            "artifact_digest": candidate.artifact_digest,
            "archive_digest": candidate.archive_digest,
            "descriptor_digest": candidate.descriptor_digest,
            "manifest_digest": candidate.manifest_digest,
            "dependency_lock_digest": candidate.dependency_lock_digest,
        },
    }


def stage_workload_offline_install_request(
    candidate: WorkloadPackageCandidate,
    *,
    request_id: str,
    maintenance: Any,
    staging_root: Path = PACKAGE_BOOTSTRAP_STAGE_ROOT,
) -> tuple[Path, Path, dict[str, Any]]:
    """Copy signed descriptor/archive bytes into a recoverable root-only request.

    Existing exact request bytes are accepted on retry; mismatched bytes or
    identities are never replaced. 中文：重试仅复用完全相同的私有请求和资产。
    """

    if _effective_uid() != 0 or not staging_root.is_absolute():
        raise PackageRuntimeBootstrapError("Package Runtime bootstrap staging requires root")
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}", request_id) is None:
        raise PackageRuntimeBootstrapError("Package Runtime request ID is unsafe")
    _ensure_root_directory(staging_root, 0o700)
    request_directory = staging_root / request_id
    _ensure_root_directory(request_directory, 0o700, parent=staging_root)
    descriptor_path = request_directory / "descriptor.json"
    archive_path = request_directory / "archive.zip"
    request_path = request_directory / "request.json"
    source_rows = (
        (
            candidate.descriptor_path,
            descriptor_path,
            candidate.descriptor_digest,
            MAX_DESCRIPTOR_BYTES,
        ),
        (candidate.archive_path, archive_path, candidate.archive_digest, MAX_RELEASE_ASSET_BYTES),
    )
    for source, destination, digest, limit in source_rows:
        contents = _safe_source_bytes(source, digest, limit)
        if destination.exists() or destination.is_symlink():
            existing = _safe_source_bytes(destination, digest, limit)
            if existing != contents:
                raise PackageRuntimeBootstrapError("Package Runtime retry asset differs")
        else:
            _write_root_file(destination, contents, 0o600)
    request = build_workload_offline_install_input(
        candidate,
        request_id=request_id,
        maintenance=maintenance,
        descriptor_path=descriptor_path,
        archive_path=archive_path,
    )
    serialized = _canonical_json(request)
    if len(serialized) > MAX_BOOTSTRAP_INPUT_BYTES:
        raise PackageRuntimeBootstrapError("Package Runtime bootstrap request is too large")
    if request_path.exists() or request_path.is_symlink():
        existing = _safe_source_bytes(
            request_path,
            "sha256:" + hashlib.sha256(serialized).hexdigest(),
            MAX_BOOTSTRAP_INPUT_BYTES,
        )
        if existing != serialized:
            raise PackageRuntimeBootstrapError("Package Runtime retry request differs")
    else:
        _write_root_file(request_path, serialized, 0o600)
    return request_directory, request_path, request


def validate_workload_installation_record(
    candidate: WorkloadPackageCandidate,
    record: Any,
) -> dict[str, Any]:
    """Validate the Platform's complete Package Runtime receipt for a workload."""

    expected_fields = {
        "record_version",
        "installation_id",
        "package_id",
        "package_version",
        "artifact_digest",
        "archive_digest",
        "capabilities",
        "state",
        "verification",
        "dependencies",
        "installed_at_unix_ms",
    }
    if not isinstance(record, dict) or set(record) != expected_fields:
        raise PackageRuntimeBootstrapError(
            "Package Runtime workload installation receipt is malformed"
        )
    verification = record.get("verification")
    dependencies = record.get("dependencies")
    if not isinstance(verification, dict) or set(verification) != {
        "verifier",
        "verified_at_unix_ms",
        "artifact_digest",
        "archive_digest",
        "descriptor_digest",
        "manifest_digest",
        "dependency_lock_digest",
    }:
        raise PackageRuntimeBootstrapError(
            "Package Runtime workload verification receipt is incomplete"
        )
    if not isinstance(dependencies, dict) or set(dependencies) != {
        "preparer",
        "prepared_at_unix_ms",
        "lock_digest",
        "runtime_digest",
        "runtime_executable",
    }:
        raise PackageRuntimeBootstrapError(
            "Package Runtime workload dependency receipt is incomplete"
        )
    expected_installation = (
        "installation-"
        + hashlib.sha256(
            f"{candidate.package_id}\0{candidate.package_version}\0{candidate.artifact_digest}".encode()
        ).hexdigest()[:32]
    )
    if (
        type(record.get("record_version")) is not int
        or record["record_version"] != 1
        or record.get("installation_id") != expected_installation
        or INSTALLATION_ID.fullmatch(str(record.get("installation_id"))) is None
        or record.get("package_id") != candidate.package_id
        or record.get("package_version") != candidate.package_version
        or record.get("artifact_digest") != candidate.artifact_digest
        or record.get("archive_digest") != candidate.archive_digest
        or record.get("capabilities") != [candidate.capability]
        or record.get("state") != "INSTALLED"
        or type(record.get("installed_at_unix_ms")) is not int
        or record["installed_at_unix_ms"] < 1
        or verification.get("artifact_digest") != candidate.artifact_digest
        or verification.get("archive_digest") != candidate.archive_digest
        or verification.get("descriptor_digest") != candidate.descriptor_digest
        or verification.get("manifest_digest") != candidate.manifest_digest
        or verification.get("dependency_lock_digest") != candidate.dependency_lock_digest
        or dependencies.get("lock_digest") != candidate.dependency_lock_digest
        or not isinstance(dependencies.get("runtime_executable"), str)
        or not Path(dependencies["runtime_executable"]).is_absolute()
    ):
        raise PackageRuntimeBootstrapError(
            "Package Runtime workload receipt differs from signed bytes"
        )
    return record


def run_workload_offline_install(
    candidate: WorkloadPackageCandidate,
    request: dict[str, Any],
    *,
    command: Path = PACKAGE_RUNTIME_COMMAND,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Install one generic package using Platform's fixed root-only CLI."""

    request_id = request.get("request_id")
    request_directory = PACKAGE_BOOTSTRAP_STAGE_ROOT / str(request_id)
    request_path = request_directory / "request.json"
    request_bytes = _canonical_json(request)
    try:
        stored = _safe_source_bytes(
            request_path,
            "sha256:" + hashlib.sha256(request_bytes).hexdigest(),
            MAX_BOOTSTRAP_INPUT_BYTES,
        )
        if stored != request_bytes:
            raise PackageRuntimeBootstrapError("Package Runtime staged request changed")
        for filename, digest, limit in (
            ("descriptor.json", candidate.descriptor_digest, MAX_DESCRIPTOR_BYTES),
            ("archive.zip", candidate.archive_digest, MAX_RELEASE_ASSET_BYTES),
        ):
            _safe_source_bytes(request_directory / filename, digest, limit)
    except (OSError, PackageRuntimeBootstrapError) as error:
        raise PackageRuntimeBootstrapError(
            "Package Runtime staged workload request is unsafe"
        ) from error
    argv = [
        str(command),
        "--root",
        str(PACKAGE_RUNTIME_STATE_ROOT),
        "--dependency-preparer",
        str(PACKAGE_PREPARER_COMMAND),
    ]
    for argument in PACKAGE_PREPARER_ARGS:
        argv.extend(["--dependency-preparer-arg", argument])
    argv.extend(["--bootstrap-install-offline", "--bootstrap-input-file", str(request_path)])
    try:
        completed = runner(
            argv,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
            env={
                "LANG": "C.UTF-8",
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PackageRuntimeBootstrapError(
            "Package Runtime offline install failed to run"
        ) from error
    lines = completed.stdout.splitlines()
    if completed.returncode != 0 or len(lines) != 1:
        raise PackageRuntimeBootstrapError("Package Runtime offline install did not complete")
    try:
        response = json.loads(lines[0], object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, PackageRuntimeBootstrapError) as error:
        raise PackageRuntimeBootstrapError(
            "Package Runtime install receipt is malformed"
        ) from error
    return validate_workload_offline_install_result(candidate, request, response)


def validate_workload_offline_install_result(
    candidate: WorkloadPackageCandidate,
    request: dict[str, Any],
    response: Any,
) -> dict[str, Any]:
    """Require the exact Platform hold echo and independently validate its record."""

    result_fields = {
        "transaction_id",
        "target_kind",
        "plan_id",
        "plan_digest",
        "component_artifact_digests",
        "component_id",
        "artifact_digest",
        "expected_gate_generation",
        "expected_catalog_generation",
        "gate_generation",
        "catalog_generation",
        "installation",
    }
    result = response.get("result") if isinstance(response, dict) else None
    maintenance = request.get("maintenance")
    if (
        not isinstance(response, dict)
        or set(response) != {"request_id", "ok", "result"}
        or response.get("request_id") != request.get("request_id")
        or response.get("ok") is not True
        or not isinstance(result, dict)
        or set(result) != result_fields
        or not isinstance(maintenance, dict)
        or result.get("transaction_id") != maintenance.get("transaction_id")
        or result.get("target_kind") != "PACKAGE_ONLY"
        or result.get("plan_id") != maintenance.get("plan_id")
        or result.get("plan_digest") != maintenance.get("plan_digest")
        or result.get("component_artifact_digests") != maintenance.get("component_artifact_digests")
        or result.get("component_id") != candidate.component_id
        or result.get("artifact_digest") != candidate.artifact_digest
        or result.get("expected_gate_generation") != maintenance.get("expected_gate_generation")
        or result.get("expected_catalog_generation")
        != maintenance.get("expected_catalog_generation")
        or result.get("gate_generation") != maintenance.get("expected_gate_generation")
        or result.get("catalog_generation") != maintenance.get("expected_catalog_generation")
        or request.get("candidate", {}).get("component_id") != candidate.component_id
        or request.get("candidate", {}).get("package_id") != candidate.package_id
    ):
        raise PackageRuntimeBootstrapError(
            "Package Runtime install receipt differs from its held request"
        )
    return validate_workload_installation_record(candidate, result.get("installation"))


def build_workload_offline_uninstall_input(
    *,
    request_id: str,
    maintenance: Any,
    installation: Any,
) -> dict[str, Any]:
    """Build an exact held uninstall request from an authenticated UDS record."""

    if not isinstance(maintenance, dict) or set(maintenance) != {
        "transaction_id",
        "maintenance_token",
        "target_kind",
        "plan_id",
        "plan_digest",
        "component_artifact_digests",
        "expected_gate_generation",
        "expected_catalog_generation",
    }:
        raise PackageRuntimeBootstrapError("Package Runtime maintenance hold is malformed")
    required = {
        "component_id",
        "installation_id",
        "package_id",
        "package_version",
        "artifact_digest",
        "archive_digest",
        "descriptor_digest",
        "manifest_digest",
        "dependency_lock_digest",
    }
    if not isinstance(installation, dict) or set(installation) != required:
        raise PackageRuntimeBootstrapError("Package Runtime uninstall identity is malformed")
    component_id = installation.get("component_id")
    digest_map = maintenance.get("component_artifact_digests")
    if (
        not isinstance(request_id, str)
        or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}", request_id) is None
        or maintenance.get("target_kind") != "PACKAGE_ONLY"
        or not isinstance(maintenance.get("transaction_id"), str)
        or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}", maintenance["transaction_id"]) is None
        or not isinstance(maintenance.get("maintenance_token"), str)
        or not maintenance["maintenance_token"]
        or not isinstance(maintenance.get("plan_id"), str)
        or re.fullmatch(r"plan-[0-9a-f]{32}", maintenance["plan_id"]) is None
        or not isinstance(maintenance.get("plan_digest"), str)
        or TYPED_SHA256.fullmatch(maintenance["plan_digest"]) is None
        or not isinstance(digest_map, dict)
        or digest_map.get(component_id) != installation.get("artifact_digest")
        or type(maintenance.get("expected_gate_generation")) is not int
        or maintenance["expected_gate_generation"] < 1
        or type(maintenance.get("expected_catalog_generation")) is not int
        or maintenance["expected_catalog_generation"] < 1
        or not isinstance(component_id, str)
        or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", component_id) is None
        or not isinstance(installation.get("installation_id"), str)
        or INSTALLATION_ID.fullmatch(installation["installation_id"]) is None
        or not isinstance(installation.get("package_id"), str)
        or re.fullmatch(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*){1,7}", installation["package_id"])
        is None
        or not isinstance(installation.get("package_version"), str)
        or PACKAGE_VERSION_PATTERN.fullmatch(installation["package_version"]) is None
        or any(
            not isinstance(installation.get(field), str)
            or TYPED_SHA256.fullmatch(installation[field]) is None
            for field in (
                "artifact_digest",
                "archive_digest",
                "descriptor_digest",
                "manifest_digest",
                "dependency_lock_digest",
            )
        )
    ):
        raise PackageRuntimeBootstrapError(
            "Package Runtime uninstall identity differs from held plan"
        )
    return {
        "schema_version": 1,
        "request_id": request_id,
        "maintenance": dict(maintenance),
        "installation": dict(installation),
    }


def stage_workload_offline_uninstall_request(
    *,
    request_id: str,
    maintenance: Any,
    installation: Any,
    staging_root: Path = PACKAGE_BOOTSTRAP_STAGE_ROOT,
) -> tuple[Path, Path, dict[str, Any]]:
    """Write one root-private uninstall request without package payload files."""

    if _effective_uid() != 0 or not staging_root.is_absolute():
        raise PackageRuntimeBootstrapError("Package Runtime bootstrap staging requires root")
    _ensure_root_directory(staging_root, 0o700)
    request_directory = staging_root / request_id
    _ensure_root_directory(request_directory, 0o700, parent=staging_root)
    request = build_workload_offline_uninstall_input(
        request_id=request_id,
        maintenance=maintenance,
        installation=installation,
    )
    content = _canonical_json(request)
    if len(content) > MAX_BOOTSTRAP_INPUT_BYTES:
        raise PackageRuntimeBootstrapError("Package Runtime uninstall request is too large")
    request_path = request_directory / "request.json"
    if request_path.exists() or request_path.is_symlink():
        existing = _safe_source_bytes(
            request_path,
            "sha256:" + hashlib.sha256(content).hexdigest(),
            MAX_BOOTSTRAP_INPUT_BYTES,
        )
        if existing != content:
            raise PackageRuntimeBootstrapError("Package Runtime uninstall retry identity differs")
    else:
        _write_root_file(request_path, content, 0o600)
    return request_directory, request_path, request


def run_workload_offline_uninstall(
    request: dict[str, Any],
    *,
    command: Path = PACKAGE_RUNTIME_COMMAND,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Remove one exact unreferenced installation through Platform's held CLI."""

    request_id = request.get("request_id")
    request_directory = PACKAGE_BOOTSTRAP_STAGE_ROOT / str(request_id)
    request_path = request_directory / "request.json"
    content = _canonical_json(request)
    _safe_source_bytes(
        request_path,
        "sha256:" + hashlib.sha256(content).hexdigest(),
        MAX_BOOTSTRAP_INPUT_BYTES,
    )
    argv = [
        str(command),
        "--root",
        str(PACKAGE_RUNTIME_STATE_ROOT),
        "--dependency-preparer",
        str(PACKAGE_PREPARER_COMMAND),
    ]
    for argument in PACKAGE_PREPARER_ARGS:
        argv.extend(["--dependency-preparer-arg", argument])
    argv.extend(["--bootstrap-uninstall-offline", "--bootstrap-input-file", str(request_path)])
    try:
        completed = runner(
            argv,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
            env={
                "LANG": "C.UTF-8",
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PackageRuntimeBootstrapError(
            "Package Runtime offline uninstall failed to run"
        ) from error
    lines = completed.stdout.splitlines()
    if completed.returncode != 0 or len(lines) != 1:
        raise PackageRuntimeBootstrapError("Package Runtime offline uninstall did not complete")
    try:
        response = json.loads(lines[0], object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, PackageRuntimeBootstrapError) as error:
        raise PackageRuntimeBootstrapError(
            "Package Runtime uninstall receipt is malformed"
        ) from error
    return validate_workload_offline_uninstall_result(request, response)


def validate_workload_offline_uninstall_result(
    request: dict[str, Any], response: Any
) -> dict[str, Any]:
    """Require Platform's secret-free uninstall receipt to echo every held field."""

    result_fields = {
        "transaction_id",
        "target_kind",
        "plan_id",
        "plan_digest",
        "component_artifact_digests",
        "component_id",
        "artifact_digest",
        "expected_gate_generation",
        "expected_catalog_generation",
        "gate_generation",
        "catalog_generation",
        "installation",
        "already_absent",
    }
    result = response.get("result") if isinstance(response, dict) else None
    maintenance = request.get("maintenance")
    installation = request.get("installation")
    if (
        not isinstance(response, dict)
        or set(response) != {"request_id", "ok", "result"}
        or response.get("request_id") != request.get("request_id")
        or response.get("ok") is not True
        or not isinstance(result, dict)
        or set(result) != result_fields
        or not isinstance(maintenance, dict)
        or not isinstance(installation, dict)
        or result.get("transaction_id") != maintenance.get("transaction_id")
        or result.get("target_kind") != "PACKAGE_ONLY"
        or result.get("plan_id") != maintenance.get("plan_id")
        or result.get("plan_digest") != maintenance.get("plan_digest")
        or result.get("component_artifact_digests") != maintenance.get("component_artifact_digests")
        or result.get("component_id") != installation.get("component_id")
        or result.get("artifact_digest") != installation.get("artifact_digest")
        or result.get("expected_gate_generation") != maintenance.get("expected_gate_generation")
        or result.get("expected_catalog_generation")
        != maintenance.get("expected_catalog_generation")
        or result.get("gate_generation") != maintenance.get("expected_gate_generation")
        or result.get("catalog_generation") != maintenance.get("expected_catalog_generation")
        or result.get("installation") != installation
        or type(result.get("already_absent")) is not bool
    ):
        raise PackageRuntimeBootstrapError(
            "Package Runtime uninstall receipt differs from held identity"
        )
    return result


def _safe_source_bytes(path: Path, expected_digest: str, limit: int) -> bytes:
    """Read a verified release file through a no-follow, single-link descriptor."""

    try:
        before = path.lstat()
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
    except OSError as error:
        raise PackageRuntimeBootstrapError(
            "Verified Package Runtime asset is unavailable"
        ) from error
    try:
        with os.fdopen(descriptor, "rb") as stream:
            current = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or current.st_size > limit
                or current.st_dev != before.st_dev
                or current.st_ino != before.st_ino
                or before.st_mode != current.st_mode
            ):
                raise PackageRuntimeBootstrapError("Verified Package Runtime asset is unsafe")
            content = stream.read(limit + 1)
    except OSError as error:
        raise PackageRuntimeBootstrapError(
            "Verified Package Runtime asset cannot be read"
        ) from error
    if len(content) > limit or "sha256:" + hashlib.sha256(content).hexdigest() != expected_digest:
        raise PackageRuntimeBootstrapError(
            "Verified Package Runtime asset changed after attestation"
        )
    return content


def _require_root_directory_chain(path: Path, label: str) -> None:
    """Require an absolute root-owned path with no writable or linked directory."""

    if not path.is_absolute():
        raise PackageRuntimeBootstrapError(f"{label} path must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as error:
            raise PackageRuntimeBootstrapError(f"{label} path is unavailable") from error
        mode = stat.S_IMODE(info.st_mode)
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or (mode & 0o022 and not mode & stat.S_ISVTX)
        ):
            raise PackageRuntimeBootstrapError(f"{label} path is unsafe")


def _require_root_file(path: Path, label: str, *, executable: bool = False) -> os.stat_result:
    """Require a single-link root-owned regular file with a stable mode."""

    try:
        info = path.lstat()
    except OSError as error:
        raise PackageRuntimeBootstrapError(f"{label} is unavailable") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o022
        or not info.st_mode & 0o400
        or (executable and not info.st_mode & 0o100)
    ):
        raise PackageRuntimeBootstrapError(f"{label} metadata is unsafe")
    return info


def _require_preparer_identity(value: Any) -> dict[str, Any]:
    """Validate the selected Plugin wheel identity before using its staged bytes."""

    expected = {
        "schemaVersion",
        "planId",
        "planDigest",
        "componentId",
        "componentArtifactDigest",
        "manifestDigest",
        "manifestAssetDigest",
        "packageId",
        "packageVersion",
        "releaseId",
        "releaseTag",
        "sourceCommit",
        "publisherIdentity",
        "attestationRef",
        "wheel",
    }
    if not isinstance(value, dict) or set(value) != expected or value.get("schemaVersion") != 1:
        raise PackageRuntimeBootstrapError("First-Core preparer wheel identity is malformed")
    for field in ("planDigest", "componentArtifactDigest", "manifestDigest", "manifestAssetDigest"):
        if TYPED_SHA256.fullmatch(str(value.get(field, ""))) is None:
            raise PackageRuntimeBootstrapError("First-Core preparer plan digest is malformed")
    if (
        re.fullmatch(r"plan-[0-9a-f]{32}", str(value.get("planId", ""))) is None
        or value.get("planId") != "plan-" + value["planDigest"].split(":", 1)[1][:32]
        or not isinstance(value.get("componentId"), str)
        or re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,127}", value["componentId"]) is None
        or not isinstance(value.get("packageId"), str)
        or not isinstance(value.get("packageVersion"), str)
        or value.get("releaseTag") != value.get("releaseId")
        or not isinstance(value.get("releaseId"), str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", value["releaseId"]) is None
        or re.fullmatch(r"[0-9a-f]{40}", str(value.get("sourceCommit", ""))) is None
        or not isinstance(value.get("publisherIdentity"), dict)
        or not isinstance(value.get("attestationRef"), dict)
        or value["attestationRef"].get("sourceCommit") != value.get("sourceCommit")
    ):
        raise PackageRuntimeBootstrapError("First-Core preparer wheel source differs from its plan")
    wheel = value.get("wheel")
    publisher = value.get("publisherIdentity")
    repository = publisher.get("repository") if isinstance(publisher, dict) else None
    if (
        not isinstance(repository, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
        or not isinstance(wheel, dict)
        or set(wheel) != {"name", "path", "uri", "sha256", "sizeBytes", "attestationBundleDigest"}
        or not isinstance(wheel.get("name"), str)
        or re.fullmatch(r"[A-Za-z0-9_.+-]{1,200}\.whl", wheel["name"]) is None
        or not isinstance(wheel.get("path"), str)
        or not Path(wheel["path"]).is_absolute()
        or Path(wheel["path"]).name != wheel["name"]
        or not isinstance(wheel.get("uri"), str)
        or wheel.get("uri")
        != f"https://github.com/{repository}/releases/download/{value.get('releaseId')}/{wheel.get('name')}"
        or not isinstance(wheel.get("sizeBytes"), int)
        or isinstance(wheel.get("sizeBytes"), bool)
        or wheel["sizeBytes"] < 1
        or wheel["sizeBytes"] > WORKLOAD_PREPARER_MAX_WHEEL_BYTES
        or TYPED_SHA256.fullmatch(str(wheel.get("sha256", ""))) is None
        or TYPED_SHA256.fullmatch(str(wheel.get("attestationBundleDigest", ""))) is None
    ):
        raise PackageRuntimeBootstrapError("First-Core preparer wheel asset is malformed")
    return value


def _inspect_workload_preparer_wheel(contents: bytes) -> None:
    """Confirm the attested wheel declares the Package Runtime preparer CLI."""

    try:
        with zipfile.ZipFile(io.BytesIO(contents)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if (
                not infos
                or len(infos) > WORKLOAD_PREPARER_MAX_WHEEL_FILES
                or len(names) != len(set(names))
                or sum(info.file_size for info in infos) > WORKLOAD_PREPARER_MAX_WHEEL_BYTES
                or any(
                    not name
                    or name.startswith("/")
                    or "\\" in name
                    or ".." in Path(name).parts
                    or stat.S_ISLNK(info.external_attr >> 16)
                    for name, info in zip(names, infos, strict=True)
                )
            ):
                raise PackageRuntimeBootstrapError("Preparer wheel archive layout is unsafe")
            metadata_paths = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metadata_paths) != 1:
                raise PackageRuntimeBootstrapError(
                    "Preparer wheel has no unique distribution metadata"
                )
            metadata_path = metadata_paths[0]
            dist_info = metadata_path.rsplit("/", 1)[0]
            metadata = BytesParser().parsebytes(archive.read(metadata_path))
            entry_points = archive.read(f"{dist_info}/entry_points.txt").decode("utf-8")
            wheel_metadata = archive.read(f"{dist_info}/WHEEL").decode("utf-8")
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile, KeyError) as error:
        if isinstance(error, PackageRuntimeBootstrapError):
            raise
        raise PackageRuntimeBootstrapError("Preparer wheel metadata cannot be read") from error

    normalized_name = str(metadata.get("Name", "")).lower().replace("_", "-")
    if (
        normalized_name != "cyrene-plugin-runtime"
        or metadata.get("Version") != WORKLOAD_PREPARER_VERSION
    ):
        raise PackageRuntimeBootstrapError("Preparer wheel distribution identity is unsupported")
    in_console_scripts = False
    matching_entrypoints = []
    for line in entry_points.splitlines():
        item = line.strip()
        if item.startswith("[") and item.endswith("]"):
            in_console_scripts = item == "[console_scripts]"
        elif in_console_scripts and "=" in item:
            name, target = (part.strip() for part in item.split("=", 1))
            if name == WORKLOAD_PREPARER_ENTRYPOINT:
                matching_entrypoints.append(target)
    tags = [
        line.removeprefix("Tag: ").strip()
        for line in wheel_metadata.splitlines()
        if line.startswith("Tag: ")
    ]
    if matching_entrypoints != [WORKLOAD_PREPARER_ENTRYPOINT_TARGET] or "py3-none-any" not in tags:
        raise PackageRuntimeBootstrapError("Preparer wheel entrypoint or ABI is unsupported")


def _run_preparer_bootstrap(
    command: list[str], runner: Any | None, *, label: str
) -> subprocess.CompletedProcess[str]:
    """Run one fixed preparer bootstrap command without user or network config."""

    environment = {
        "HOME": "/root",
        "PATH": "/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        result = (runner or subprocess.run)(
            command,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PackageRuntimeBootstrapError(f"{label} could not run") from error
    if result.returncode != 0:
        raise PackageRuntimeBootstrapError(f"{label} failed")
    return result


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_workload_preparer_receipt(release_path: Path) -> dict[str, Any]:
    receipt_path = release_path / "preparer-install.json"
    _require_root_file(receipt_path, "Package Runtime preparer receipt")
    try:
        value = json.loads(receipt_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageRuntimeBootstrapError("Package Runtime preparer receipt is invalid") from error
    if not isinstance(value, dict):
        raise PackageRuntimeBootstrapError("Package Runtime preparer receipt is malformed")
    return value


def _publish_workload_preparer_command(
    script_bytes: bytes,
    digest_hex: str,
    *,
    owner_uid: int = 0,
    owner_gid: int = 0,
) -> None:
    """Publish the fixed CLI atomically and reconcile only its exact pending link."""

    final_path = PACKAGE_PREPARER_COMMAND
    pending_path = final_path.parent / f".{final_path.name}.{digest_hex}.pending"

    def inspect(path: Path, *, allowed_links: set[int]) -> os.stat_result:
        try:
            info = path.lstat()
        except OSError as error:
            raise PackageRuntimeBootstrapError(
                "Package Runtime preparer command is unavailable"
            ) from error
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner_uid
            or info.st_gid != owner_gid
            or stat.S_IMODE(info.st_mode) != 0o555
            or info.st_nlink not in allowed_links
        ):
            raise PackageRuntimeBootstrapError(
                "Package Runtime preparer command metadata is unsafe"
            )
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                    or opened.st_nlink not in allowed_links
                    or opened.st_size > 1024 * 1024
                    or opened.st_mode != info.st_mode
                ):
                    raise PackageRuntimeBootstrapError(
                        "Package Runtime preparer command changed while opening"
                    )
                content = stream.read(1024 * 1024 + 1)
        except OSError as error:
            raise PackageRuntimeBootstrapError(
                "Package Runtime preparer command cannot be read"
            ) from error
        if content != script_bytes:
            raise PackageRuntimeBootstrapError(
                "Package Runtime preparer command differs from its release"
            )
        return info

    final_exists = final_path.exists() or final_path.is_symlink()
    pending_exists = pending_path.exists() or pending_path.is_symlink()
    pending_info: os.stat_result | None = None
    if pending_exists:
        pending_info = inspect(pending_path, allowed_links={1, 2})

    if final_exists:
        final_info = inspect(final_path, allowed_links={1, 2})
        if final_info.st_nlink == 2:
            if (
                pending_info is None
                or pending_info.st_nlink != 2
                or (pending_info.st_dev, pending_info.st_ino)
                != (final_info.st_dev, final_info.st_ino)
            ):
                raise PackageRuntimeBootstrapError("Preparer command has an unaccounted hard link")
            pending_path.unlink()
            _fsync_directory(final_path.parent)
            final_info = inspect(final_path, allowed_links={1})
        elif pending_info is not None:
            if pending_info.st_nlink != 1:
                raise PackageRuntimeBootstrapError("Preparer command has an unrelated pending file")
            pending_path.unlink()
            _fsync_directory(final_path.parent)
            final_info = inspect(final_path, allowed_links={1})
        else:
            inspect(final_path, allowed_links={1})
        return

    if pending_info is None:
        _write_root_file(pending_path, script_bytes, 0o555)
        pending_info = inspect(pending_path, allowed_links={1})
    try:
        os.link(pending_path, final_path, follow_symlinks=False)
    except FileExistsError:
        final_info = inspect(final_path, allowed_links={1, 2})
        if final_info.st_nlink == 1:
            if pending_info.st_nlink != 1:
                raise PackageRuntimeBootstrapError("Preparer command raced with another release")
            pending_path.unlink()
            _fsync_directory(final_path.parent)
            inspect(final_path, allowed_links={1})
            return
        if final_info.st_nlink == 2:
            if pending_info.st_nlink != 2 or (pending_info.st_dev, pending_info.st_ino) != (
                final_info.st_dev,
                final_info.st_ino,
            ):
                raise PackageRuntimeBootstrapError("Preparer command raced with another release")
        else:
            raise PackageRuntimeBootstrapError("Preparer command has an unexpected link count")
    final_info = inspect(final_path, allowed_links={2})
    pending_info = inspect(pending_path, allowed_links={2})
    if (pending_info.st_dev, pending_info.st_ino) != (final_info.st_dev, final_info.st_ino):
        raise PackageRuntimeBootstrapError("Preparer command pending link differs from its release")
    pending_path.unlink()
    _fsync_directory(final_path.parent)
    inspect(final_path, allowed_links={1})


def install_workload_preparer(staged_identity: Any, *, runner: Any | None = None) -> dict[str, str]:
    """Install the exact staged preparer wheel before starting Package Runtime.

    The caller supplies a Plugin asset already bound to the checked workload
    plan and verified through the Plugin release contract. This function
    rechecks the wheel bytes, installs no dependencies or network packages,
    and publishes only the generated console entrypoint at the fixed unit path.
    """

    _require_root()
    identity = _require_preparer_identity(staged_identity)
    wheel = identity["wheel"]
    wheel_path = Path(wheel["path"])
    _require_root_directory_chain(wheel_path.parent, "staged preparer wheel parent")
    _require_root_file(wheel_path, "staged preparer wheel")
    contents = _safe_source_bytes(
        wheel_path,
        wheel["sha256"],
        WORKLOAD_PREPARER_MAX_WHEEL_BYTES,
    )
    if len(contents) != wheel["sizeBytes"]:
        raise PackageRuntimeBootstrapError("Staged preparer wheel size differs from its release")
    _inspect_workload_preparer_wheel(contents)

    _require_root_directory_chain(PACKAGE_PREPARER_PYTHON.parent, "locked CPython path")
    _require_root_file(PACKAGE_PREPARER_PYTHON, "locked CPython", executable=True)
    python_version = _run_preparer_bootstrap(
        [
            str(PACKAGE_PREPARER_PYTHON),
            "-B",
            "-I",
            "-c",
            "import sys; print('.'.join(map(str, sys.version_info[:3])))",
        ],
        runner,
        label="Locked CPython version probe",
    )
    if python_version.stdout.strip() != WORKLOAD_PREPARER_PYTHON_VERSION:
        raise PackageRuntimeBootstrapError("Locked CPython version differs from the Plugin SDK")
    _require_root_directory_chain(PACKAGE_PREPARER_UV.parent, "locked uv path")
    _require_root_file(PACKAGE_PREPARER_UV, "locked uv", executable=True)
    _require_root_directory_chain(WORKLOAD_PREPARER_ROOT.parent, "preparer runtime parent")
    _ensure_root_directory(WORKLOAD_PREPARER_ROOT, 0o755, parent=WORKLOAD_PREPARER_ROOT.parent)
    _ensure_root_directory(WORKLOAD_PREPARER_RELEASES, 0o755, parent=WORKLOAD_PREPARER_ROOT)
    _require_root_directory_chain(PACKAGE_PREPARER_COMMAND.parent, "preparer command parent")

    digest_hex = wheel["sha256"].removeprefix("sha256:")
    release_path = WORKLOAD_PREPARER_RELEASES / digest_hex
    venv_path = release_path / "venv"
    venv_python = venv_path / "bin" / "python"
    runtime_script = release_path / WORKLOAD_PREPARER_ENTRYPOINT
    if release_path.exists() or release_path.is_symlink():
        if release_path.is_symlink() or not release_path.is_dir():
            raise PackageRuntimeBootstrapError("Preparer release directory is unsafe")
        receipt = _read_workload_preparer_receipt(release_path)
        if (
            receipt.get("schemaVersion") != 1
            or receipt.get("wheelDigest") != wheel["sha256"]
            or receipt.get("wheelSizeBytes") != wheel["sizeBytes"]
            or receipt.get("entrypoint") != WORKLOAD_PREPARER_ENTRYPOINT
            or receipt.get("entrypointTarget") != WORKLOAD_PREPARER_ENTRYPOINT_TARGET
            or receipt.get("pythonPath") != str(venv_python)
            or receipt.get("commandPath") != str(PACKAGE_PREPARER_COMMAND)
        ):
            raise PackageRuntimeBootstrapError("Existing preparer release differs from this wheel")
        _require_root_directory_chain(venv_path / "bin", "preparer venv path")
        _require_root_file(venv_python, "preparer venv Python", executable=True)
        _require_root_file(runtime_script, "preparer release command", executable=True)
        script_bytes = _safe_source_bytes(
            runtime_script, "sha256:" + str(receipt.get("entrypointSha256", "")), 1024 * 1024
        )
        command_sha = "sha256:" + hashlib.sha256(script_bytes).hexdigest()
        if command_sha != "sha256:" + str(receipt.get("entrypointSha256", "")):
            raise PackageRuntimeBootstrapError("Preparer release command changed after install")
    else:
        if PACKAGE_PREPARER_COMMAND.exists() or PACKAGE_PREPARER_COMMAND.is_symlink():
            raise PackageRuntimeBootstrapError(
                "An unmanaged Package Runtime preparer already exists"
            )
        temporary_path = Path(tempfile.mkdtemp(prefix=".pp-", dir=WORKLOAD_PREPARER_ROOT))
        try:
            os.chmod(temporary_path, 0o755, follow_symlinks=False)
            os.chown(temporary_path, 0, 0, follow_symlinks=False)
            staged_wheel = temporary_path / wheel["name"]
            _write_root_file(staged_wheel, contents, 0o444)
            temporary_venv = temporary_path / "venv"
            _run_preparer_bootstrap(
                [
                    str(PACKAGE_PREPARER_PYTHON),
                    "-I",
                    "-m",
                    "venv",
                    "--copies",
                    str(temporary_venv),
                ],
                runner,
                label="Locked CPython venv creation",
            )
            temporary_python = temporary_venv / "bin" / "python"
            _require_root_directory_chain(temporary_venv / "bin", "preparer venv path")
            _require_root_file(temporary_python, "preparer venv Python", executable=True)
            _run_preparer_bootstrap(
                [
                    str(temporary_python),
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
                    str(staged_wheel),
                ],
                runner,
                label="Offline preparer wheel installation",
            )
            _run_preparer_bootstrap(
                [
                    str(temporary_python),
                    "-I",
                    "-m",
                    WORKLOAD_PREPARER_MODULE,
                    "--help",
                ],
                runner,
                label="Preparer package import probe",
            )
            generated_script = temporary_venv / "bin" / WORKLOAD_PREPARER_ENTRYPOINT
            _require_root_file(generated_script, "generated preparer entrypoint", executable=True)
            script_bytes = _safe_source_bytes(
                generated_script,
                "sha256:" + hashlib.sha256(generated_script.read_bytes()).hexdigest(),
                1024 * 1024,
            )
            first_line, separator, remainder = script_bytes.partition(b"\n")
            expected_shebang = b"#!" + os.fsencode(temporary_python)
            if not separator or first_line != expected_shebang:
                raise PackageRuntimeBootstrapError("Generated preparer has an unexpected shebang")
            final_shebang = b"#!" + os.fsencode(venv_python)
            if len(final_shebang) > 125:
                raise PackageRuntimeBootstrapError("Preparer Python path exceeds the shebang limit")
            script_bytes = final_shebang + b"\n" + remainder
            command_digest = "sha256:" + hashlib.sha256(script_bytes).hexdigest()
            release_receipt = {
                "schemaVersion": 1,
                "wheelDigest": wheel["sha256"],
                "wheelSizeBytes": wheel["sizeBytes"],
                "entrypoint": WORKLOAD_PREPARER_ENTRYPOINT,
                "entrypointTarget": WORKLOAD_PREPARER_ENTRYPOINT_TARGET,
                "entrypointSha256": command_digest.removeprefix("sha256:"),
                "pythonPath": str(venv_python),
                "commandPath": str(PACKAGE_PREPARER_COMMAND),
            }
            _write_root_file(
                temporary_path / WORKLOAD_PREPARER_ENTRYPOINT,
                script_bytes,
                0o555,
            )
            _write_root_file(
                temporary_path / "preparer-install.json",
                (json.dumps(release_receipt, sort_keys=True, separators=(",", ":")) + "\n").encode(
                    "utf-8"
                ),
                0o444,
            )
            os.replace(temporary_path, release_path)
            _fsync_directory(WORKLOAD_PREPARER_RELEASES)
        except Exception:
            if temporary_path.exists() and not temporary_path.is_symlink():
                shutil.rmtree(temporary_path)
            raise
        receipt = release_receipt
        runtime_script = release_path / WORKLOAD_PREPARER_ENTRYPOINT
        script_bytes = _safe_source_bytes(
            runtime_script,
            "sha256:" + receipt["entrypointSha256"],
            1024 * 1024,
        )
        command_sha = "sha256:" + receipt["entrypointSha256"]

    _publish_workload_preparer_command(script_bytes, digest_hex)

    _require_root_directory_chain(venv_path / "bin", "preparer venv path")
    _require_root_file(venv_python, "preparer venv Python", executable=True)
    _require_root_file(
        PACKAGE_PREPARER_COMMAND, "Package Runtime preparer command", executable=True
    )
    _run_preparer_bootstrap(
        [str(PACKAGE_PREPARER_COMMAND), "--help"],
        runner,
        label="Installed preparer CLI import probe",
    )
    return {
        "wheelDigest": wheel["sha256"],
        "releasePath": str(release_path),
        "pythonPath": str(venv_python),
        "commandPath": str(PACKAGE_PREPARER_COMMAND),
        "commandDigest": command_sha,
    }


def _ensure_root_directory(
    path: Path,
    mode: int,
    *,
    parent: Path | None = None,
    group_id: int = 0,
) -> None:
    """Create or verify one root-owned, non-symlink staging directory."""

    if _effective_uid() != 0:
        raise PackageRuntimeBootstrapError("Package Runtime bootstrap staging requires root")
    parent_path = parent or path.parent
    try:
        parent_info = parent_path.lstat()
        if (
            parent_path.is_symlink()
            or not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != 0
            or stat.S_IMODE(parent_info.st_mode) & 0o022
        ):
            raise PackageRuntimeBootstrapError("Package Runtime staging parent is unsafe")
        path.mkdir(mode=mode)
        os.chown(path, 0, group_id, follow_symlinks=False)
        os.chmod(path, mode, follow_symlinks=False)
        info = path.lstat()
    except FileExistsError:
        try:
            info = path.lstat()
        except OSError as error:
            raise PackageRuntimeBootstrapError("Package Runtime staging path is unsafe") from error
    except OSError as error:
        raise PackageRuntimeBootstrapError(
            "Package Runtime staging directory cannot be created"
        ) from error
    if (
        path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != group_id
        or stat.S_IMODE(info.st_mode) != mode
    ):
        raise PackageRuntimeBootstrapError("Package Runtime staging directory is unsafe")


def _write_root_file(path: Path, content: bytes, mode: int, *, group_id: int = 0) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            mode,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, mode, follow_symlinks=False)
        os.chown(path, 0, group_id, follow_symlinks=False)
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise PackageRuntimeBootstrapError(
            "Package Runtime staging file cannot be written"
        ) from error


def stage_offline_install_request(
    candidate: VerifiedPackageCandidate,
    *,
    request_id: str,
    maintenance: Any,
    staging_root: Path = PACKAGE_BOOTSTRAP_STAGE_ROOT,
) -> tuple[Path, Path]:
    """Copy exact attested inputs and write the root-private one-shot request."""

    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}", request_id) is None:
        raise PackageRuntimeBootstrapError("Package Runtime request ID is unsafe")
    if _effective_uid() != 0:
        raise PackageRuntimeBootstrapError("Package Runtime bootstrap staging requires root")
    if not staging_root.is_absolute():
        raise PackageRuntimeBootstrapError("Package Runtime staging root must be absolute")
    _ensure_root_directory(staging_root, 0o700)
    request_directory = staging_root / request_id
    _ensure_root_directory(request_directory, 0o700, parent=staging_root)
    evidence_root = request_directory / "evidence"
    evidence_assets = evidence_root / "assets"
    evidence_attestations = evidence_root / "attestations"
    _ensure_root_directory(evidence_root, 0o700, parent=request_directory)
    _ensure_root_directory(evidence_assets, 0o700, parent=evidence_root)
    _ensure_root_directory(evidence_attestations, 0o700, parent=evidence_root)
    source_asset_paths = {
        "package_release": candidate.release_path,
        "descriptor": candidate.descriptor_path,
        "archive": candidate.archive_path,
        "dependency_lock": candidate.dependency_lock_path,
        "preparer_wheel": candidate.preparer_wheel_path,
    }
    source_asset_digests = {
        "package_release": candidate.release_digest,
        "descriptor": candidate.descriptor_digest,
        "archive": candidate.archive_digest,
        "dependency_lock": candidate.dependency_lock_digest,
        "preparer_wheel": candidate.preparer_wheel_digest,
    }
    for key, asset_name in ASSET_NAMES.items():
        contents = _safe_source_bytes(
            source_asset_paths[key], source_asset_digests[key], MAX_RELEASE_ASSET_BYTES
        )
        _write_root_file(evidence_assets / asset_name, contents, 0o600)
    checksum_content = "".join(
        f"{source_asset_digests[key].removeprefix('sha256:')}  {ASSET_NAMES[key]}\n"
        for key in sorted(ASSET_NAMES)
    ).encode("ascii")
    _write_root_file(evidence_assets / CHECKSUMS_NAME, checksum_content, 0o600)
    source_attestations = candidate.release_path.parent.parent / "attestations"
    if set(candidate.attestation_bundle_digests) != set(ASSET_NAMES.values()):
        raise PackageRuntimeBootstrapError("Package Runtime attestation evidence set is incomplete")
    for asset_name in ASSET_NAMES.values():
        bundle_name = ATTESTATION_NAMES[asset_name]
        bundle_contents = _safe_source_bytes(
            source_attestations / bundle_name,
            candidate.attestation_bundle_digests[asset_name],
            MAX_RELEASE_ASSET_BYTES,
        )
        _write_root_file(evidence_attestations / bundle_name, bundle_contents, 0o600)
    descriptor_path = request_directory / "descriptor.json"
    archive_path = request_directory / "archive.zip"
    descriptor_bytes = _safe_source_bytes(
        candidate.descriptor_path, candidate.descriptor_digest, MAX_DESCRIPTOR_BYTES
    )
    archive_bytes = _safe_source_bytes(
        candidate.archive_path, candidate.archive_digest, MAX_RELEASE_ASSET_BYTES
    )
    _write_root_file(descriptor_path, descriptor_bytes, 0o600)
    _write_root_file(archive_path, archive_bytes, 0o600)
    request = build_offline_install_input(
        candidate,
        request_id=request_id,
        descriptor_path=descriptor_path,
        archive_path=archive_path,
        maintenance=maintenance,
    )
    request_bytes = json.dumps(
        request, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    if len(request_bytes) > MAX_BOOTSTRAP_INPUT_BYTES:
        raise PackageRuntimeBootstrapError("Package Runtime bootstrap request is too large")
    request_path = request_directory / "request.json"
    _write_root_file(request_path, request_bytes, 0o600)
    return request_directory, request_path


def stage_catalog_commit_inputs(
    request_directory: Path,
    *,
    request_id: str,
    maintenance: Any,
    catalog_update: ActivityCatalogUpdate,
) -> tuple[Path, Path]:
    """Persist the exact hold proof and complete source-scope map for init-catalog."""

    if (
        not request_directory.is_absolute()
        or request_directory.parent != PACKAGE_BOOTSTRAP_STAGE_ROOT
        or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}", request_directory.name) is None
    ):
        raise PackageRuntimeBootstrapError("Package Runtime catalog stage path is not canonical")
    _ensure_root_directory(PACKAGE_BOOTSTRAP_STAGE_ROOT, 0o700)
    _ensure_root_directory(
        request_directory,
        0o700,
        parent=PACKAGE_BOOTSTRAP_STAGE_ROOT,
    )
    if not isinstance(maintenance, dict) or set(maintenance) != {
        "transaction_id",
        "maintenance_token",
        "target_kind",
        "plan_id",
        "plan_digest",
        "component_artifact_digests",
        "expected_gate_generation",
        "expected_catalog_generation",
    }:
        raise PackageRuntimeBootstrapError("Package Runtime maintenance hold is malformed")
    if (
        not isinstance(request_id, str)
        or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}", request_id) is None
        or maintenance.get("target_kind") != "PACKAGE_ONLY"
        or not isinstance(maintenance.get("transaction_id"), str)
        or not maintenance["transaction_id"]
        or not isinstance(maintenance.get("maintenance_token"), str)
        or not maintenance["maintenance_token"]
        or not isinstance(maintenance.get("plan_id"), str)
        or re.fullmatch(r"plan-[0-9a-f]{32}", maintenance["plan_id"]) is None
        or not isinstance(maintenance.get("plan_digest"), str)
        or TYPED_SHA256.fullmatch(maintenance["plan_digest"]) is None
        or not isinstance(maintenance.get("component_artifact_digests"), dict)
        or not maintenance["component_artifact_digests"]
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or TYPED_SHA256.fullmatch(value) is None
            for key, value in maintenance["component_artifact_digests"].items()
        )
        or type(maintenance.get("expected_gate_generation")) is not int
        or maintenance["expected_gate_generation"] < 1
        or type(maintenance.get("expected_catalog_generation")) is not int
        or maintenance["expected_catalog_generation"] < 1
    ):
        raise PackageRuntimeBootstrapError("Package Runtime maintenance hold is invalid")
    proof = {
        "request_id": request_id,
        "maintenance_token": maintenance["maintenance_token"],
        "plan_id": maintenance["plan_id"],
        "plan_digest": maintenance["plan_digest"],
        "component_artifact_digests": maintenance["component_artifact_digests"],
    }
    proof_path = request_directory / "maintenance-proof.json"
    scopes_path = request_directory / "binding-scopes.json"
    _write_root_file(proof_path, _canonical_json(proof), 0o600)
    _write_root_file(scopes_path, _canonical_json(catalog_update.binding_scopes), 0o600)
    return proof_path, scopes_path


def run_activity_catalog_update(
    catalog_update: ActivityCatalogUpdate,
    *,
    proof_path: Path,
    scopes_path: Path,
    command: Path = CYRENE_COMMAND,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Commit all retained Product sources and exact scopes under the same hold."""

    if not proof_path.is_absolute() or not scopes_path.is_absolute():
        raise PackageRuntimeBootstrapError("Package Runtime catalog inputs must be absolute")
    if (
        proof_path.parent != scopes_path.parent
        or proof_path.parent.parent != PACKAGE_BOOTSTRAP_STAGE_ROOT
        or proof_path.name != "maintenance-proof.json"
        or scopes_path.name != "binding-scopes.json"
    ):
        raise PackageRuntimeBootstrapError("Package Runtime catalog input paths are not canonical")
    _validate_private_stage_json(proof_path)
    if _validate_private_stage_json(scopes_path) != catalog_update.binding_scopes:
        raise PackageRuntimeBootstrapError("Package Runtime staged binding scopes changed")
    argv = [
        str(command),
        "component-run",
        "cyrene-runtime-maintenance",
        "--",
        "init-catalog",
        "--catalog",
        "/var/lib/cyrene/runtime/activity-sources.json",
        "--token-dir",
        "/etc/cyrene/runtime-activity-source-tokens",
        "--catalog-gid",
        str(_maintenance_group_id()),
        "--binding-scopes-json",
        str(scopes_path),
        "--maintenance-proof-file",
        str(proof_path),
        *catalog_update.source_arguments,
    ]
    try:
        completed = runner(
            argv,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env={
                "LANG": "C.UTF-8",
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PackageRuntimeBootstrapError(
            "Runtime activity catalog update failed to run"
        ) from error
    lines = completed.stdout.splitlines()
    if completed.returncode != 0 or len(lines) != 1:
        raise PackageRuntimeBootstrapError("Runtime activity catalog update did not complete")
    try:
        result = json.loads(lines[0], object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, PackageRuntimeBootstrapError) as error:
        raise PackageRuntimeBootstrapError(
            "Runtime activity catalog result is malformed"
        ) from error
    expected_ids = [
        argument.split("=", 1)[0]
        for index, argument in enumerate(catalog_update.source_arguments)
        if index % 2 == 1
    ]
    if (
        not isinstance(result, dict)
        or set(result) != {"schema_version", "generation", "sources"}
        or type(result.get("schema_version")) is not int
        or result["schema_version"] != 1
        or type(result.get("generation")) is not int
        or result["generation"] != catalog_update.expected_generation
        or not isinstance(result.get("sources"), list)
    ):
        raise PackageRuntimeBootstrapError("Runtime activity catalog result identity differs")
    rows = result["sources"]
    if (
        len(rows) != len(expected_ids)
        or any(
            not isinstance(row, dict)
            or set(row) != {"source_id", "token_file", "binding_scope_count"}
            or not isinstance(row.get("source_id"), str)
            or row.get("token_file")
            != f"/etc/cyrene/runtime-activity-source-tokens/{row.get('source_id')}.token"
            or type(row.get("binding_scope_count")) is not int
            for row in rows
        )
        or [row["source_id"] for row in rows] != expected_ids
        or any(
            row["binding_scope_count"] != len(catalog_update.binding_scopes[row["source_id"]])
            for row in rows
        )
    ):
        raise PackageRuntimeBootstrapError("Runtime activity catalog source result differs")
    return result


def _validate_private_stage_json(path: Path) -> Any:
    """Read one root-only stage file without following links or accepting aliases."""

    try:
        before = path.lstat()
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        with os.fdopen(descriptor, "rb") as stream:
            current = os.fstat(stream.fileno())
            if (
                path.is_symlink()
                or not stat.S_ISREG(current.st_mode)
                or current.st_uid != 0
                or current.st_gid != 0
                or stat.S_IMODE(current.st_mode) != 0o600
                or current.st_nlink != 1
                or current.st_dev != before.st_dev
                or current.st_ino != before.st_ino
                or current.st_size > MAX_BOOTSTRAP_INPUT_BYTES
            ):
                raise PackageRuntimeBootstrapError("Package Runtime private stage file is unsafe")
            content = stream.read(MAX_BOOTSTRAP_INPUT_BYTES + 1)
    except OSError as error:
        raise PackageRuntimeBootstrapError(
            "Package Runtime private stage file is unavailable"
        ) from error
    if len(content) > MAX_BOOTSTRAP_INPUT_BYTES:
        raise PackageRuntimeBootstrapError("Package Runtime private stage file is too large")
    try:
        return json.loads(content, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, PackageRuntimeBootstrapError) as error:
        raise PackageRuntimeBootstrapError(
            "Package Runtime private stage JSON is malformed"
        ) from error


def validate_activity_catalog_readback(
    catalog: Any,
    catalog_update: ActivityCatalogUpdate,
    command_result: Any,
) -> dict[str, Any]:
    """Require disk readback to preserve source identities and exact scopes."""

    if (
        not isinstance(command_result, dict)
        or command_result.get("generation") != catalog_update.expected_generation
        or type(command_result.get("generation")) is not int
    ):
        raise PackageRuntimeBootstrapError("Runtime activity catalog generation changed")
    generation, _selected = _activity_catalog_identity(catalog)
    if generation != catalog_update.expected_generation:
        raise PackageRuntimeBootstrapError("Runtime activity catalog readback is stale")
    actual_sources = catalog["sources"]
    if [source["source_id"] for source in actual_sources] != sorted(catalog_update.source_identity):
        raise PackageRuntimeBootstrapError("Runtime activity catalog source set changed")
    for source in actual_sources:
        source_id = source["source_id"]
        if {
            key: source.get(key) for key in ("uid", "gid", "source_token_sha256")
        } != catalog_update.source_identity[source_id] or source.get(
            "binding_scopes", []
        ) != catalog_update.binding_scopes[source_id]:
            raise PackageRuntimeBootstrapError(
                "Runtime activity catalog source identity or scope changed"
            )
    return catalog


def run_offline_install(
    candidate: VerifiedPackageCandidate,
    request: dict[str, Any],
    *,
    command: Path = CYRENE_COMMAND,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Run the Platform one-shot installer and validate its real response."""

    request_id = request.get("request_id")
    request_directory = PACKAGE_BOOTSTRAP_STAGE_ROOT / str(request_id)
    request_path = request_directory / "request.json"
    candidate_input = request.get("candidate")
    if (
        not isinstance(candidate_input, dict)
        or candidate_input.get("descriptor_path") != str(request_directory / "descriptor.json")
        or candidate_input.get("archive_path") != str(request_directory / "archive.zip")
    ):
        raise PackageRuntimeBootstrapError("Package Runtime request paths are not canonical")
    try:
        directory_info = request_directory.lstat()
        request_info = request_path.lstat()
        if (
            request_directory.is_symlink()
            or not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != 0
            or directory_info.st_gid != 0
            or stat.S_IMODE(directory_info.st_mode) != 0o700
            or request_path.is_symlink()
            or not stat.S_ISREG(request_info.st_mode)
            or request_info.st_uid != 0
            or request_info.st_gid != 0
            or stat.S_IMODE(request_info.st_mode) != 0o600
            or request_info.st_nlink != 1
        ):
            raise PackageRuntimeBootstrapError("Package Runtime request file metadata is unsafe")
        request_bytes = _safe_source_bytes(
            request_path,
            "sha256:" + hashlib.sha256(_canonical_json(request)).hexdigest(),
            MAX_BOOTSTRAP_INPUT_BYTES,
        )
        stored_request = json.loads(request_bytes, object_pairs_hook=_unique_object)
    except (OSError, json.JSONDecodeError, PackageRuntimeBootstrapError) as error:
        raise PackageRuntimeBootstrapError("Package Runtime staged request is unsafe") from error
    if stored_request != request:
        raise PackageRuntimeBootstrapError("Package Runtime staged request changed")
    for staged_path, expected_digest, limit in (
        (
            request_directory / "descriptor.json",
            candidate.descriptor_digest,
            MAX_DESCRIPTOR_BYTES,
        ),
        (request_directory / "archive.zip", candidate.archive_digest, MAX_RELEASE_ASSET_BYTES),
    ):
        try:
            info = staged_path.lstat()
            if (
                staged_path.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0
                or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise PackageRuntimeBootstrapError("Package Runtime staged asset is unsafe")
            _safe_source_bytes(staged_path, expected_digest, limit)
        except OSError as error:
            raise PackageRuntimeBootstrapError(
                "Package Runtime staged asset is unavailable"
            ) from error
    argv = [
        str(command),
        "component-run",
        "cy-package-runtime",
        "--",
        "--root",
        str(PACKAGE_RUNTIME_STATE_ROOT),
        "--dependency-preparer",
        str(PACKAGE_PREPARER_COMMAND),
    ]
    for argument in PACKAGE_PREPARER_ARGS:
        argv.extend(["--dependency-preparer-arg", argument])
    argv.extend(["--bootstrap-install-offline", "--bootstrap-input-file", str(request_path)])
    try:
        completed = runner(
            argv,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
            env={
                "LANG": "C.UTF-8",
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PackageRuntimeBootstrapError(
            "Package Runtime one-shot installer failed to run"
        ) from error
    lines = completed.stdout.splitlines()
    if completed.returncode != 0 or len(lines) != 1:
        raise PackageRuntimeBootstrapError("Package Runtime one-shot installer did not complete")
    try:
        response = json.loads(lines[0], object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, PackageRuntimeBootstrapError) as error:
        raise PackageRuntimeBootstrapError(
            "Package Runtime installer response is malformed"
        ) from error
    return validate_offline_install_result(candidate, request, response)


def validate_offline_install_result(
    candidate: VerifiedPackageCandidate,
    request: dict[str, Any],
    response: Any,
) -> dict[str, Any]:
    """Accept only the CLI's exact successful hold echo and real receipt."""

    if not isinstance(response, dict) or set(response) != {"request_id", "ok", "result"}:
        raise PackageRuntimeBootstrapError("Package Runtime install response is malformed")
    result = response.get("result")
    expected_maintenance = request.get("maintenance")
    request_id = request.get("request_id")
    candidate_request = request.get("candidate")
    if (
        response.get("request_id") != request_id
        or response.get("ok") is not True
        or not isinstance(result, dict)
        or set(result) != {"maintenance", "catalog_generation", "gate_generation", "installation"}
        or not isinstance(expected_maintenance, dict)
        or not isinstance(candidate_request, dict)
    ):
        raise PackageRuntimeBootstrapError("Package Runtime install response is not successful")
    hold_echo = result.get("maintenance")
    if not isinstance(result.get("catalog_generation"), int) or isinstance(
        result["catalog_generation"], bool
    ):
        raise PackageRuntimeBootstrapError("Package Runtime catalog generation is malformed")
    if not isinstance(result.get("gate_generation"), int) or isinstance(
        result["gate_generation"], bool
    ):
        raise PackageRuntimeBootstrapError("Package Runtime gate generation is malformed")
    if not isinstance(hold_echo, dict) or set(hold_echo) != {
        "transaction_id",
        "target_kind",
        "plan_id",
        "plan_digest",
        "component_artifact_digests",
        "component_id",
        "artifact_digest",
        "expected_gate_generation",
        "expected_catalog_generation",
    }:
        raise PackageRuntimeBootstrapError("Package Runtime install hold echo is malformed")
    if (
        hold_echo.get("transaction_id") != expected_maintenance.get("transaction_id")
        or hold_echo.get("target_kind") != "PACKAGE_ONLY"
        or hold_echo.get("plan_id") != expected_maintenance.get("plan_id")
        or hold_echo.get("plan_digest") != expected_maintenance.get("plan_digest")
        or hold_echo.get("component_artifact_digests")
        != expected_maintenance.get("component_artifact_digests")
        or hold_echo.get("component_id") != candidate.package_id
        or hold_echo.get("artifact_digest") != candidate.artifact_digest
        or hold_echo.get("expected_gate_generation")
        != expected_maintenance.get("expected_gate_generation")
        or hold_echo.get("expected_catalog_generation")
        != expected_maintenance.get("expected_catalog_generation")
        or result.get("gate_generation") != expected_maintenance.get("expected_gate_generation")
        or result.get("catalog_generation")
        != expected_maintenance.get("expected_catalog_generation")
        or candidate_request.get("component_id") != candidate.package_id
        or candidate_request.get("artifact_digest") != candidate.artifact_digest
    ):
        raise PackageRuntimeBootstrapError("Package Runtime install hold echo differs from request")
    return validate_installation_record(candidate, result.get("installation"))


def build_binding_scope_input(
    candidate: VerifiedPackageCandidate, installation_record: Any
) -> dict[str, list[dict[str, Any]]]:
    """Derive the selected Yield binding scope from a verified install receipt."""

    record = validate_installation_record(candidate, installation_record)
    scope = {
        "binding_id": PACKAGE_BINDING_ID,
        "package_id": candidate.package_id,
        "installation_ids": [record["installation_id"]],
        "operations": list(PACKAGE_POLICY_OPERATIONS),
    }
    return {PACKAGE_SOURCE_ID: [scope]}


def build_activity_catalog_update(
    candidate: VerifiedPackageCandidate,
    installation_record: Any,
    activity_catalog: Any,
) -> ActivityCatalogUpdate:
    """Preserve every current source scope and add this exact installed binding."""

    record = validate_installation_record(candidate, installation_record)
    generation, selected = _activity_catalog_identity(activity_catalog)
    wanted = build_binding_scope_input(candidate, record)[PACKAGE_SOURCE_ID][0]
    new_scopes: dict[str, list[dict[str, Any]]] = {}
    source_identity: dict[str, dict[str, Any]] = {}
    source_arguments: list[str] = []
    changed = False
    for source in activity_catalog["sources"]:
        source_id = source["source_id"]
        scopes = [dict(scope) for scope in source.get("binding_scopes", [])]
        if source_id == PACKAGE_SOURCE_ID:
            existing = next(
                (scope for scope in scopes if scope["binding_id"] == PACKAGE_BINDING_ID), None
            )
            if existing is None:
                scopes.append(wanted)
                changed = True
            elif existing != wanted:
                raise PackageRuntimeBootstrapError(
                    "Yield binding is already assigned to a different package installation"
                )
            scopes.sort(key=lambda scope: scope["binding_id"])
        new_scopes[source_id] = scopes
        source_identity[source_id] = {
            "uid": source["uid"],
            "gid": source["gid"],
            "source_token_sha256": source["source_token_sha256"],
        }
        source_arguments.extend(["--source", f"{source_id}={source['uid']}:{source['gid']}"])
    if selected.get("source_id") != PACKAGE_SOURCE_ID:
        raise PackageRuntimeBootstrapError("Yield activity source is unavailable")
    return ActivityCatalogUpdate(
        source_arguments=tuple(source_arguments),
        binding_scopes=new_scopes,
        source_identity=source_identity,
        expected_generation=generation + 1 if changed else generation,
        changed=changed,
    )


def _daemon_bindings(scopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate catalog mutation operations to daemon control operations."""

    bindings = []
    for scope in scopes:
        if scope["operations"] != list(PACKAGE_POLICY_OPERATIONS):
            raise PackageRuntimeBootstrapError("Yield mutation scope differs from the owner plan")
        bindings.append(
            {
                "binding_id": scope["binding_id"],
                "package_id": scope["package_id"],
                "installation_ids": list(scope["installation_ids"]),
                "operations": list(PACKAGE_RUNTIME_OPERATIONS),
            }
        )
    return bindings


def _activity_catalog_identity(
    catalog: Any, *, requested_source_id: str = PACKAGE_SOURCE_ID
) -> tuple[int, dict[str, Any]]:
    if not isinstance(catalog, dict) or set(catalog) != {"schema_version", "generation", "sources"}:
        raise PackageRuntimeBootstrapError("Runtime activity catalog has an unsupported shape")
    generation = catalog.get("generation")
    sources = catalog.get("sources")
    if (
        type(catalog.get("schema_version")) is not int
        or catalog["schema_version"] != 1
        or type(generation) is not int
        or generation < 1
        or not isinstance(sources, list)
    ):
        raise PackageRuntimeBootstrapError("Runtime activity catalog identity is invalid")
    selected: dict[str, Any] | None = None
    ids: list[str] = []
    for source in sources:
        if not isinstance(source, dict) or set(source) not in (
            {"source_id", "uid", "gid", "source_token_sha256"},
            {"source_id", "uid", "gid", "source_token_sha256", "binding_scopes"},
        ):
            raise PackageRuntimeBootstrapError("Runtime activity source schema is invalid")
        source_id = source.get("source_id")
        if not isinstance(source_id, str):
            raise PackageRuntimeBootstrapError("Runtime activity source ID is invalid")
        ids.append(source_id)
        _validate_catalog_binding_scopes(source)
        if source_id == requested_source_id:
            selected = source
    if ids != sorted(ids) or len(set(ids)) != len(ids) or selected is None:
        raise PackageRuntimeBootstrapError("Requested activity source is missing or ambiguous")
    if (
        type(selected.get("uid")) is not int
        or selected["uid"] < 1
        or type(selected.get("gid")) is not int
        or not isinstance(selected.get("source_token_sha256"), str)
        or RAW_SHA256.fullmatch(selected["source_token_sha256"]) is None
    ):
        raise PackageRuntimeBootstrapError("Activity source trust fields are invalid")
    return generation, selected


def _validate_catalog_binding_scopes(source: dict[str, Any]) -> None:
    """Validate optional mutation scopes; absent or empty scopes grant nothing."""

    if "binding_scopes" not in source:
        return
    scopes = source["binding_scopes"]
    if not isinstance(scopes, list):
        raise PackageRuntimeBootstrapError("Runtime activity binding scopes are invalid")
    binding_ids: list[str] = []
    for scope in scopes:
        if not isinstance(scope, dict) or set(scope) != {
            "binding_id",
            "package_id",
            "installation_ids",
            "operations",
        }:
            raise PackageRuntimeBootstrapError("Runtime activity binding scope is malformed")
        binding_id = scope["binding_id"]
        package_id = scope["package_id"]
        installation_ids = scope["installation_ids"]
        operations = scope["operations"]
        if (
            not isinstance(binding_id, str)
            or re.fullmatch(r"[a-z][a-z0-9._-]{0,159}", binding_id) is None
            or not isinstance(package_id, str)
            or re.fullmatch(r"[a-z][a-z0-9._-]{0,159}", package_id) is None
            or not isinstance(installation_ids, list)
            or not installation_ids
            or any(
                not isinstance(installation_id, str)
                or INSTALLATION_ID.fullmatch(installation_id) is None
                for installation_id in installation_ids
            )
            or installation_ids != sorted(set(installation_ids))
            or not isinstance(operations, list)
            or any(
                not isinstance(operation, str) or operation not in PACKAGE_POLICY_OPERATIONS
                for operation in operations
            )
            or operations != sorted(set(operations))
        ):
            raise PackageRuntimeBootstrapError("Runtime activity binding scope is invalid")
        binding_ids.append(binding_id)
    if binding_ids != sorted(set(binding_ids)):
        raise PackageRuntimeBootstrapError("Runtime activity binding IDs are ambiguous")


def build_runtime_source_policy(
    candidate: VerifiedPackageCandidate,
    installation_record: Any,
    activity_catalog: Any,
) -> dict[str, Any]:
    """Bind one verified installation to Yield's current catalog generation."""

    record = validate_installation_record(candidate, installation_record)
    generation, source = _activity_catalog_identity(activity_catalog)
    expected_gid = _runtime_group_id()
    if source["gid"] != expected_gid:
        raise PackageRuntimeBootstrapError(
            "Yield activity group differs from the fixed Cyrene group"
        )
    scopes = build_binding_scope_input(candidate, record)[PACKAGE_SOURCE_ID]
    if source.get("binding_scopes") != scopes:
        raise PackageRuntimeBootstrapError(
            "Yield activity catalog scope differs from the install receipt"
        )
    return {
        "schema_version": PACKAGE_POLICY_SCHEMA_VERSION,
        "generation": generation,
        "sources": [
            {
                "source_id": PACKAGE_SOURCE_ID,
                "uid": source["uid"],
                "gid": source["gid"],
                "source_token_sha256": source["source_token_sha256"],
                "bindings": _daemon_bindings(scopes),
            }
        ],
    }


def validate_runtime_source_policy(
    policy: Any,
    candidate: VerifiedPackageCandidate,
    installation_record: Any,
    activity_catalog: Any,
) -> dict[str, Any]:
    """Read-only check that the installed policy still matches current trust inputs."""

    expected = build_runtime_source_policy(candidate, installation_record, activity_catalog)
    if policy != expected:
        raise PackageRuntimeBootstrapError(
            "Installed Package Runtime source policy is stale or changed"
        )
    return expected


def _read_policy_file(path: Path, *, group_id: int, owner_id: int) -> bytes:
    try:
        path_info = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as error:
        raise PackageRuntimeBootstrapError(
            "Package Runtime source policy is missing or unsafe"
        ) from error
    try:
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                path.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != owner_id
                or info.st_gid != group_id
                or stat.S_IMODE(info.st_mode) != 0o440
                or info.st_nlink != 1
                or info.st_size > MAX_POLICY_BYTES
                or info.st_dev != path_info.st_dev
                or info.st_ino != path_info.st_ino
            ):
                raise PackageRuntimeBootstrapError(
                    "Package Runtime source policy metadata is unsafe"
                )
            content = stream.read(MAX_POLICY_BYTES + 1)
    except OSError as error:
        raise PackageRuntimeBootstrapError("Package Runtime source policy is unreadable") from error
    if len(content) > MAX_POLICY_BYTES:
        raise PackageRuntimeBootstrapError("Package Runtime source policy exceeds size limits")
    return content


def _validate_policy_parent(path: Path, owner_id: int) -> None:
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise PackageRuntimeBootstrapError("Package Runtime policy directory is missing or unsafe")
    info = parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != owner_id
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise PackageRuntimeBootstrapError(
            "Package Runtime policy directory is not root-controlled"
        )


def _validate_policy_shape(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict) or set(policy) != {"schema_version", "generation", "sources"}:
        raise PackageRuntimeBootstrapError("Package Runtime source policy schema is invalid")
    if (
        type(policy.get("schema_version")) is not int
        or policy["schema_version"] != PACKAGE_POLICY_SCHEMA_VERSION
        or type(policy.get("generation")) is not int
        or policy["generation"] < 1
        or not isinstance(policy.get("sources"), list)
        or len(policy["sources"]) != 1
    ):
        raise PackageRuntimeBootstrapError("Package Runtime source policy identity is invalid")
    source = policy["sources"][0]
    if not isinstance(source, dict) or set(source) != {
        "source_id",
        "uid",
        "gid",
        "source_token_sha256",
        "bindings",
    }:
        raise PackageRuntimeBootstrapError("Package Runtime source policy principal is invalid")
    if (
        source.get("source_id") != PACKAGE_SOURCE_ID
        or type(source.get("uid")) is not int
        or source["uid"] < 1
        or type(source.get("gid")) is not int
        or source["gid"] < 1
        or not isinstance(source.get("source_token_sha256"), str)
        or RAW_SHA256.fullmatch(source["source_token_sha256"]) is None
        or not isinstance(source.get("bindings"), list)
        or len(source["bindings"]) != 1
    ):
        raise PackageRuntimeBootstrapError(
            "Package Runtime source policy principal fields are invalid"
        )
    binding = source["bindings"][0]
    if not isinstance(binding, dict) or set(binding) != {
        "binding_id",
        "package_id",
        "installation_ids",
        "operations",
    }:
        raise PackageRuntimeBootstrapError("Package Runtime binding scope is invalid")
    if (
        binding.get("binding_id") != PACKAGE_BINDING_ID
        or binding.get("package_id") != PACKAGE_ID
        or not isinstance(binding.get("installation_ids"), list)
        or len(binding["installation_ids"]) != 1
        or INSTALLATION_ID.fullmatch(str(binding["installation_ids"][0])) is None
        or binding.get("operations") != list(PACKAGE_RUNTIME_OPERATIONS)
    ):
        raise PackageRuntimeBootstrapError(
            "Package Runtime binding scope differs from the fixed owner plan"
        )
    return policy


def probe_runtime_authority(
    activity_catalog: Any,
    *,
    expected_catalog_generation: int,
    source_id: str = PACKAGE_SOURCE_ID,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
    socket_path: Path = PACKAGE_RUNTIME_CONTROL_SOCKET,
    token_directory: Path = Path("/etc/cyrene/runtime-activity-source-tokens"),
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Prove the live daemon sees this source generation and exact capability.

    中文：通过真实 Unix socket 和已配置来源令牌确认 daemon generation 与能力。
    """

    if (
        not isinstance(source_id, str)
        or re.fullmatch(r"[a-z0-9._-]{1,160}", source_id) is None
        or (expected_uid is None) != (expected_gid is None)
        or (expected_uid is not None and (type(expected_uid) is not int or expected_uid <= 0))
        or (expected_gid is not None and (type(expected_gid) is not int or expected_gid <= 0))
    ):
        raise PackageRuntimeBootstrapError("Package Runtime authority source identity is invalid")
    generation, source = _activity_catalog_identity(activity_catalog, requested_source_id=source_id)
    if generation != expected_catalog_generation:
        raise PackageRuntimeBootstrapError("Package Runtime catalog generation is stale")
    if not socket_path.is_absolute() or timeout <= 0 or timeout > 30:
        raise PackageRuntimeBootstrapError("Package Runtime control endpoint is invalid")
    runtime_uid = _runtime_user_id()
    runtime_gid = _runtime_group_id()
    if expected_uid is None:
        expected_uid = runtime_uid
        expected_gid = runtime_gid
        if source["uid"] != expected_uid or source["gid"] != expected_gid:
            raise PackageRuntimeBootstrapError(
                "Package Runtime probe source does not match the fixed cyrene service identity"
            )
    elif source["uid"] != expected_uid or source["gid"] != expected_gid:
        raise PackageRuntimeBootstrapError("Package Runtime probe source principal differs")
    token_path = token_directory / f"{source_id}.token"
    token = _read_source_token(token_path, source["source_token_sha256"])
    try:
        parent_info = socket_path.parent.lstat()
        socket_info = socket_path.lstat()
    except OSError as error:
        raise PackageRuntimeBootstrapError(
            "Package Runtime control socket is unavailable"
        ) from error
    if (
        socket_path.parent.is_symlink()
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != runtime_uid
        or parent_info.st_gid != runtime_gid
        or stat.S_IMODE(parent_info.st_mode) != 0o750
        or socket_path.is_symlink()
        or not stat.S_ISSOCK(socket_info.st_mode)
        or socket_info.st_uid != runtime_uid
        or socket_info.st_gid != runtime_gid
        or stat.S_IMODE(socket_info.st_mode) != 0o660
    ):
        raise PackageRuntimeBootstrapError("Package Runtime control socket metadata is unsafe")
    request_id = f"cyrene-package-runtime-health-{os.getpid()}-{os.urandom(8).hex()}"
    request = {
        "request_id": request_id,
        "operation": "authority",
        "auth": {"source_id": source_id, "source_token": token},
        "catalog_generation": expected_catalog_generation,
    }
    encoded = _canonical_json(request) + b"\n"
    if len(encoded) > 65536:
        raise PackageRuntimeBootstrapError("Package Runtime authority request is too large")
    response_bytes = _run_authority_probe_as_source(
        encoded, socket_path=socket_path, uid=source["uid"], gid=source["gid"], timeout=timeout
    )
    if not response_bytes.endswith(b"\n") or response_bytes.count(b"\n") != 1:
        raise PackageRuntimeBootstrapError("Package Runtime authority response framing is invalid")
    try:
        response = json.loads(response_bytes[:-1], object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, PackageRuntimeBootstrapError) as error:
        raise PackageRuntimeBootstrapError(
            "Package Runtime authority response is malformed"
        ) from error
    if (
        not isinstance(response, dict)
        or set(response) != {"request_id", "ok", "result"}
        or response.get("request_id") != request_id
        or response.get("ok") is not True
        or not isinstance(response.get("result"), dict)
    ):
        raise PackageRuntimeBootstrapError("Package Runtime authority response is not successful")
    result = response["result"]
    if (
        set(result) != {"authority", "protocol_version", "catalog_generation", "capabilities"}
        or result.get("authority") != "platform_package_runtime"
        or result.get("protocol_version") != "cy-package-runtime.control.v1"
        or type(result.get("catalog_generation")) is not int
        or result["catalog_generation"] != expected_catalog_generation
        or result.get("capabilities") != ["cy-package-runtime.binding-operation-admission.v1"]
    ):
        raise PackageRuntimeBootstrapError("Package Runtime authority identity is unknown")
    return {
        "authority": result["authority"],
        "protocol_version": result["protocol_version"],
        "catalog_generation": result["catalog_generation"],
        "capabilities": list(result["capabilities"]),
    }


def ensure_yield_product_package_environment(
    candidate: VerifiedPackageCandidate,
    installation_record: Any,
    activity_catalog: Any,
    runtime_source_policy: Any,
    *,
    previous_catalog_generation: int,
) -> Any:
    """Load the independent Product environment writer beside this helper.

    The updater calls this only after catalog and policy readback while its
    existing maintenance hold remains active. The writer synchronizes shared
    source generation and Yield's separate admitted-binding configuration.
    """

    module_path = Path(__file__).with_name("native_product_package_environment.py")
    spec = importlib.util.spec_from_file_location(
        "_cyrene_native_product_package_environment", module_path
    )
    if spec is None or spec.loader is None:
        raise PackageRuntimeBootstrapError("Product Package Runtime environment writer is missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module.ensure_yield_package_environment(
            sys.modules[__name__],
            candidate,
            installation_record,
            activity_catalog,
            runtime_source_policy,
            previous_catalog_generation=previous_catalog_generation,
        )
    except Exception as error:
        raise PackageRuntimeBootstrapError(
            "Yield Package Runtime environment could not be projected safely"
        ) from error


def _read_source_token(path: Path, expected_digest: str) -> str:
    """Read the source token into memory without accepting path or metadata aliases."""

    if RAW_SHA256.fullmatch(expected_digest) is None:
        raise PackageRuntimeBootstrapError("Package Runtime source token identity is invalid")
    descriptor: int | None = None
    try:
        before = path.lstat()
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        info = os.fstat(descriptor)
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o400
            or info.st_nlink != 1
            or info.st_dev != before.st_dev
            or info.st_ino != before.st_ino
            or info.st_size < 1
            or info.st_size > 4096
        ):
            raise PackageRuntimeBootstrapError("Package Runtime source token metadata is unsafe")
        content = os.read(descriptor, 4097)
    except OSError as error:
        raise PackageRuntimeBootstrapError("Package Runtime source token is unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(content) > 4096:
        raise PackageRuntimeBootstrapError("Package Runtime source token digest differs")
    try:
        token = content.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise PackageRuntimeBootstrapError("Package Runtime source token is malformed") from error
    if not token or any(character.isspace() or ord(character) < 0x21 for character in token):
        raise PackageRuntimeBootstrapError("Package Runtime source token is malformed")
    if hashlib.sha256(token.encode("utf-8")).hexdigest() != expected_digest:
        raise PackageRuntimeBootstrapError("Package Runtime source token digest differs")
    return token


def _run_authority_probe_as_source(
    request: bytes, *, socket_path: Path, uid: int, gid: int, timeout: float
) -> bytes:
    """Connect from the catalog-bound service identity without putting its token in argv."""

    probe_program = """import socket, sys
path = sys.argv[1]
request = sys.stdin.buffer.readline(65537)
if not request.endswith(b'\\n') or len(request) > 65536 or sys.stdin.buffer.read(1):
    raise SystemExit(2)
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
    connection.settimeout(float(sys.argv[2]))
    connection.connect(path)
    connection.sendall(request)
    connection.shutdown(socket.SHUT_WR)
    response = bytearray()
    while True:
        chunk = connection.recv(65537 - len(response))
        if not chunk:
            break
        response.extend(chunk)
        if len(response) > 65536:
            raise SystemExit(3)
sys.stdout.buffer.write(response)
"""
    if _effective_uid() == 0:
        if uid < 1 or gid < 1:
            raise PackageRuntimeBootstrapError("Package Runtime source identity cannot be assumed")

        def drop_privileges() -> None:
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)

        preexec = drop_privileges
    elif _effective_uid() == uid and os.getegid() == gid:
        preexec = None
    else:
        raise PackageRuntimeBootstrapError(
            "Package Runtime authority probe cannot use the source service identity"
        )
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", probe_program, str(socket_path), str(timeout)],
            input=request,
            capture_output=True,
            timeout=timeout + 2,
            check=False,
            env={"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"},
            preexec_fn=preexec,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PackageRuntimeBootstrapError("Package Runtime authority probe failed") from error
    if completed.returncode != 0 or len(completed.stdout) > 65536:
        raise PackageRuntimeBootstrapError("Package Runtime authority probe failed")
    return completed.stdout


def write_runtime_source_policy(policy: dict[str, Any], *, root: Path = Path("/")) -> str:
    """Install the source policy atomically as root:cyrene mode 0440.

    Repeating the same exact policy is idempotent; a different existing file is
    preserved and rejected. The caller owns the maintenance hold and update lock.
    """

    _require_root()
    owner_id = _effective_uid()
    group_id = _runtime_group_id()
    _validate_policy_shape(policy)
    policy_path = Path(root) / PACKAGE_POLICY_RELATIVE_PATH
    _validate_policy_parent(policy_path, owner_id)
    content = _canonical_json(policy)
    if len(content) > MAX_POLICY_BYTES:
        raise PackageRuntimeBootstrapError("Package Runtime source policy exceeds size limits")
    if policy_path.exists() or policy_path.is_symlink():
        existing = _read_policy_file(policy_path, group_id=group_id, owner_id=owner_id)
        if existing != content:
            raise PackageRuntimeBootstrapError("A different Package Runtime policy already exists")
        return "sha256:" + hashlib.sha256(existing).hexdigest()

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".runtime-package-sources-", dir=policy_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o440)
        os.fchown(descriptor, owner_id, group_id)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, policy_path, follow_symlinks=False)
        except FileExistsError:
            existing = _read_policy_file(policy_path, group_id=group_id, owner_id=owner_id)
            if existing != content:
                raise PackageRuntimeBootstrapError(
                    "A different Package Runtime policy won the atomic install race"
                )
        temporary_path.unlink()
        directory_fd = os.open(policy_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    readback = _read_policy_file(policy_path, group_id=group_id, owner_id=owner_id)
    if readback != content:
        raise PackageRuntimeBootstrapError("Package Runtime source policy readback differs")
    return "sha256:" + hashlib.sha256(readback).hexdigest()


def read_runtime_source_policy(*, root: Path = Path("/")) -> dict[str, Any]:
    """Read the fixed source policy without following links or accepting metadata drift."""

    _require_root()
    group_id = _runtime_group_id()
    path = Path(root) / PACKAGE_POLICY_RELATIVE_PATH
    owner_id = _effective_uid()
    _validate_policy_parent(path, owner_id)
    content = _read_policy_file(path, group_id=group_id, owner_id=owner_id)
    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageRuntimeBootstrapError(
            "Package Runtime source policy is invalid JSON"
        ) from error
    return _validate_policy_shape(value)
