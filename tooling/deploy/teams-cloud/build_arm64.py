"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 build_arm64.py                                                       │
│  Module: tooling.deploy.teams_cloud.build_arm64                         │
│  Role: Build exact remote commits and record full ARM64 image footprints.│
│                                                                          │
│  模块职责：从精确远端提交构建 ARM64 镜像并记录完整本地磁盘占用。       │
└──────────────────────────────────────────────────────────────────────────┘
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
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

REMOTE_ALLOWLIST = {
    "https://github.com/DoHorizon-AI/Cyrene-Workspace.git",
    "https://github.com/DoHorizon-AI/Cyrene-Navigator.git",
    "https://github.com/DoHorizon-AI/Cyrene-Exchange.git",
    "https://github.com/DoHorizon-AI/Cyrene-Platform.git",
    "https://github.com/DoHorizon-AI/Cyrene-Plugins-Official.git",
    "https://github.com/deepseek-ai/deepseek-harness.git",
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SECRET_ARG_RE = re.compile(r"(secret|token|password|private.?key|credential)", re.IGNORECASE)
MAX_IMAGE_SAVE_ARCHIVE_BYTES = 32 * 1024**3
MAX_BINARY_ARTIFACT_BYTES = 512 * 1024**2
MAX_IMAGE_SAVE_MEMBERS = 8192
MAX_OCI_JSON_BYTES = 4 * 1024**2
MAX_OCI_MANIFESTS = 64
MAX_OCI_INDEX_DEPTH = 4
MAX_OCI_LAYERS = 512


class BuildError(ValueError):
    """A source, build, architecture, or artifact proof failed closed."""


def _private_artifact(path: Path) -> bool:
    """Keep plans, image locks, and generated environment files under umbrella artifacts."""

    resolved = path.resolve()
    return ".artifacts" in resolved.parts and not any(
        (parent / ".git").exists() for parent in (resolved, *resolved.parents)
    )


def _run(argv: list[str], *, cwd: Path | None = None, capture: bool = False) -> str:
    """Run an explicit argv without a shell and return only successful stdout."""

    result = subprocess.run(
        argv,
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() if capture else ""
        raise BuildError(
            f"command failed ({result.returncode}): {Path(argv[0]).name} {detail[:400]}"
        )
    return result.stdout or "" if capture else ""


def _safe_relative(value: Any, name: str, *, allow_dot: bool = False) -> PurePosixPath:
    """Accept only repository-relative POSIX paths with no traversal."""

    if not isinstance(value, str) or not value:
        raise BuildError(f"{name} must be a non-empty relative path")
    path = PurePosixPath(value)
    if value == "." and allow_dot:
        return path
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise BuildError(f"{name} must stay inside the staged source tree")
    return path


def _file_sha256(path: Path) -> str:
    """Hash one regular build input without loading it into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_plan(path: Path) -> dict[str, Any]:
    """Validate exact source refs and build definitions before using Docker."""

    if not _private_artifact(path):
        raise BuildError("build plans must be stored under the umbrella .artifacts directory")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildError("build plan is not readable JSON") from error
    if not isinstance(plan, dict) or plan.get("schemaVersion") != "cyrene-teams-cloud-build-v1":
        raise BuildError("unsupported build plan schema")
    if plan.get("targetPlatform") != "linux/arm64":
        raise BuildError("only linux/arm64 is accepted for this deployment target")
    task_id = plan.get("taskId")
    if not isinstance(task_id, str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9._-]{2,80}", task_id
    ):
        raise BuildError("taskId must be a stable non-placeholder identifier")
    sources = plan.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise BuildError("plan must pin at least one source repository")
    for alias, source in sources.items():
        if not isinstance(alias, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", alias):
            raise BuildError("source aliases must be lowercase identifiers")
        if not isinstance(source, dict):
            raise BuildError(f"source {alias} must be an object")
        remote, commit = source.get("remote"), source.get("commit")
        if remote not in REMOTE_ALLOWLIST:
            raise BuildError(f"source {alias} remote is outside the Cyrene allowlist")
        if not isinstance(commit, str) or not SHA_RE.fullmatch(commit):
            raise BuildError(f"source {alias} is missing an exact 40-character commit SHA")
        _safe_relative(source.get("checkoutPath"), f"source {alias} checkoutPath")

    binary_artifacts = plan.get("binaryArtifacts", [])
    if not isinstance(binary_artifacts, list):
        raise BuildError("binaryArtifacts must be an array")
    asset_ids: set[str] = set()
    staging_paths: set[str] = set()
    for asset in binary_artifacts:
        if not isinstance(asset, dict) or set(asset) != {
            "id",
            "version",
            "sourcePath",
            "sha256",
            "stagingPath",
        }:
            raise BuildError("binary artifact entries must match the closed asset schema")
        asset_id = asset.get("id")
        if not isinstance(asset_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", asset_id):
            raise BuildError("binary artifact id must be a stable lowercase identifier")
        if asset_id in asset_ids:
            raise BuildError(f"duplicate binary artifact id: {asset_id}")
        version = asset.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9][A-Za-z0-9.+_-]{0,63}", version):
            raise BuildError(f"binary artifact {asset_id} requires an explicit version")
        digest = asset.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise BuildError(f"binary artifact {asset_id} requires a SHA-256 digest")
        source_path = asset.get("sourcePath")
        if not isinstance(source_path, str) or not Path(source_path).is_absolute():
            raise BuildError(f"binary artifact {asset_id} sourcePath must be absolute")
        source_file = Path(source_path)
        if (
            source_file.is_symlink()
            or not source_file.is_file()
            or not _private_artifact(source_file)
        ):
            raise BuildError(
                f"binary artifact {asset_id} must be a regular file under the private umbrella .artifacts directory"
            )
        relative = _safe_relative(
            asset.get("stagingPath"), f"binary artifact {asset_id} stagingPath"
        )
        if relative.as_posix() in staging_paths:
            raise BuildError(f"binary artifacts collide at {relative.as_posix()}")
        staging_paths.add(relative.as_posix())
        asset_ids.add(asset_id)

    images = plan.get("images")
    if not isinstance(images, list) or not images:
        raise BuildError("plan must define at least one buildable ARM64 image")
    service_names: set[str] = set()
    for image in images:
        if not isinstance(image, dict):
            raise BuildError("every image build must be an object")
        service, aliases = image.get("service"), image.get("sourceRepos")
        if not isinstance(service, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", service):
            raise BuildError("image service must be a stable lowercase name")
        if service in service_names:
            raise BuildError(f"duplicate image service: {service}")
        service_names.add(service)
        if (
            not isinstance(aliases, list)
            or not aliases
            or any(alias not in sources for alias in aliases)
        ):
            raise BuildError(f"image {service} must reference pinned source repositories")
        _safe_relative(image.get("dockerfile"), f"image {service} dockerfile")
        _safe_relative(image.get("context", "."), f"image {service} context", allow_dot=True)
        args = image.get("buildArgs", {})
        if not isinstance(args, dict):
            raise BuildError(f"image {service} buildArgs must be an object")
        for key, value in args.items():
            if not isinstance(key, str) or SECRET_ARG_RE.search(key):
                raise BuildError(f"image {service} contains a forbidden secret-like build argument")
            if not isinstance(value, str) or not value:
                raise BuildError(f"image {service} build argument {key} must be explicit")
            if key.endswith(("_IMAGE", "_BASE_IMAGE")) and not re.fullmatch(
                r".+@sha256:[0-9a-f]{64}", value
            ):
                raise BuildError(f"image {service} base image {key} must pin a registry digest")
        image_assets = image.get("binaryArtifacts", [])
        if (
            not isinstance(image_assets, list)
            or any(asset_id not in asset_ids for asset_id in image_assets)
            or len(set(image_assets)) != len(image_assets)
        ):
            raise BuildError(f"image {service} references an unknown or duplicated binary artifact")
        _safe_relative(image.get("sourceLabelRepo"), f"image {service} sourceLabelRepo")
        if image["sourceLabelRepo"] not in aliases:
            raise BuildError(
                f"image {service} source label must identify one of its pinned sources"
            )
        if "dsh" in aliases and args.get("DSH_COMMIT") != sources["dsh"].get("commit"):
            raise BuildError(
                f"image {service} DSH commit argument must match the exact pinned DSH source"
            )
    external_images = plan.get("externalImages", {})
    if not isinstance(external_images, dict):
        raise BuildError("externalImages must be an object")
    for name, reference in external_images.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise BuildError("external image keys must be environment identifiers")
        if not isinstance(reference, str) or not re.fullmatch(r".+@sha256:[0-9a-f]{64}", reference):
            raise BuildError(f"external image {name} must pin a registry digest")
    return plan


def _copy_binary_artifact(asset: dict[str, Any], staging_root: Path) -> dict[str, Any]:
    """Verify and stage one owner-supplied binary under a safe context path."""

    source = Path(asset["sourcePath"])
    relative = _safe_relative(asset["stagingPath"], f"binary artifact {asset['id']} stagingPath")
    destination = staging_root / relative
    if destination.exists() or destination.is_symlink():
        raise BuildError(f"binary artifact staging path collides: {relative.as_posix()}")
    size = source.stat().st_size
    if size <= 0 or size > MAX_BINARY_ARTIFACT_BYTES:
        raise BuildError(f"binary artifact {asset['id']} has an invalid size")
    actual = _file_sha256(source)
    if actual != asset["sha256"]:
        raise BuildError(f"binary artifact {asset['id']} failed its pinned SHA-256 check")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    os.chmod(destination, 0o555)
    copied_digest = _file_sha256(destination)
    if copied_digest != actual:
        raise BuildError(f"staged binary artifact {asset['id']} changed during copy")
    return {
        "id": asset["id"],
        "version": asset["version"],
        "sha256": actual,
        "sizeBytes": size,
        "stagingPath": relative.as_posix(),
    }


def _image_platform(raw: bytes) -> tuple[str, str]:
    """Select the platform child digest from a multi-architecture OCI index."""

    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildError("registry returned an invalid OCI manifest") from error
    manifests = document.get("manifests") if isinstance(document, dict) else None
    if not isinstance(manifests, list):
        raise BuildError("base/runtime image must publish a multi-architecture manifest list")
    candidates = [
        row
        for row in manifests
        if isinstance(row, dict)
        and row.get("platform", {}).get("os") == "linux"
        and row.get("platform", {}).get("architecture") in {"arm64", "aarch64"}
    ]
    if len(candidates) != 1 or not isinstance(candidates[0].get("digest"), str):
        raise BuildError("image index does not contain exactly one linux/arm64 manifest")
    digest = candidates[0]["digest"]
    if not IMAGE_DIGEST_RE.fullmatch(digest):
        raise BuildError("registry returned a malformed linux/arm64 manifest digest")
    return digest, "linux/arm64"


def resolve_image(reference: str) -> dict[str, str]:
    """Resolve a concrete version tag to a content digest and ARM64 child."""

    raw = _run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", reference], capture=True
    ).encode()
    arm64_digest, target_platform = _image_platform(raw)
    summary = _run(["docker", "buildx", "imagetools", "inspect", reference], capture=True)
    digest_lines = [
        line.partition(":")[2].strip()
        for line in summary.splitlines()
        if line.startswith("Digest:")
    ]
    root_digest = digest_lines[0] if len(digest_lines) == 1 else ""
    if not IMAGE_DIGEST_RE.fullmatch(root_digest):
        raise BuildError("registry did not return a content-addressed image index digest")
    if "@" in reference:
        repository = reference.rsplit("@", 1)[0]
    else:
        repository = reference.rpartition(":")[0]
    return {
        "reference": reference,
        "immutableReference": repository + "@" + arm64_digest,
        "indexReference": repository + "@" + root_digest,
        "indexDigest": root_digest,
        "arm64ManifestDigest": arm64_digest,
        "platform": target_platform,
    }


def _archive_remote_source(
    alias: str, source: dict[str, Any], staging_root: Path
) -> dict[str, str]:
    """Fetch one exact commit in a temporary bare repo and archive only its tree."""

    checkout = staging_root / _safe_relative(source["checkoutPath"], f"source {alias} checkoutPath")
    if checkout.exists() or checkout.is_symlink():
        raise BuildError(f"source checkout path collides: {checkout}")
    checkout.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"cyrene-{alias}-git-") as temporary:
        bare_repo = Path(temporary) / "source.git"
        _run(["git", "init", "--bare", str(bare_repo)])
        _run(["git", "-C", str(bare_repo), "remote", "add", "origin", source["remote"]])
        _run(
            [
                "git",
                "-C",
                str(bare_repo),
                "fetch",
                "--no-tags",
                "--depth=1",
                "origin",
                source["commit"],
            ]
        )
        commit = _run(
            ["git", "-C", str(bare_repo), "rev-parse", "FETCH_HEAD^{commit}"], capture=True
        ).strip()
        if commit != source["commit"]:
            raise BuildError(f"remote returned a different commit for {alias}")
        tree = _run(
            ["git", "-C", str(bare_repo), "rev-parse", "FETCH_HEAD^{tree}"], capture=True
        ).strip()
        archive = subprocess.Popen(
            ["git", "-C", str(bare_repo), "archive", "--format=tar", "FETCH_HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert archive.stdout is not None
        try:
            with tarfile.open(fileobj=archive.stdout, mode="r|") as tar:
                tar.extractall(checkout, filter="data")
        finally:
            archive.stdout.close()
        stderr = archive.stderr.read().decode("utf-8", errors="replace") if archive.stderr else ""
        if archive.wait() != 0:
            raise BuildError(f"could not archive exact source {alias}: {stderr[:300]}")
    return {"remote": source["remote"], "commit": commit, "tree": tree}


def _workspace_is_exact_and_clean(plan: dict[str, Any], workspace_root: Path) -> None:
    """Require build scripts and Dockerfiles to come from the pinned clean commit."""

    selected = plan["sources"].get("workspace")
    if not selected:
        raise BuildError("plan must pin the Workspace source containing this build recipe")
    current = _run(["git", "-C", str(workspace_root), "rev-parse", "HEAD"], capture=True).strip()
    if current != selected["commit"]:
        raise BuildError("current Workspace HEAD does not equal the pinned source SHA")
    state = _run(["git", "-C", str(workspace_root), "status", "--porcelain"], capture=True).strip()
    if state:
        raise BuildError("Workspace checkout is dirty; commit intended changes before building")


def build(plan_path: Path, output: Path, *, allow_emulated: bool) -> dict[str, Any]:
    """Build pinned images and lock their post-unpack local Docker store footprints.

    `sizeBytes` is the larger of Docker's materialized image size and
    `SizeRootFs + compressed OCI layers`; it never records the pre-unpack layer-only
    size returned immediately after Buildx import.

    中文：从固定远端提交构建镜像，并记录解包后的本地 Docker 存储估算。
    `sizeBytes` 取解包镜像大小与“完整根文件系统 + 压缩层”中的较大值。
    """

    plan = validate_plan(plan_path)
    workspace_root = Path(__file__).resolve().parents[3]
    _workspace_is_exact_and_clean(plan, workspace_root)
    if not _private_artifact(output):
        raise BuildError(
            "build lock/evidence output must stay under the umbrella .artifacts directory"
        )
    if platform.system() != "Linux" or platform.machine().lower() not in {
        "aarch64",
        "arm64",
        "x86_64",
        "amd64",
    }:
        raise BuildError("build host must be Linux x86_64 or ARM64 with Docker Buildx")
    _run(["docker", "buildx", "version"], capture=True)
    builder_info = _run(["docker", "buildx", "inspect"], capture=True)
    if "linux/arm64" not in builder_info and not allow_emulated:
        raise BuildError(
            "active Buildx builder lacks linux/arm64; select native ARM64 or explicit emulation"
        )

    output.mkdir(parents=True, exist_ok=True)
    lock: dict[str, Any] = {
        "schemaVersion": "cyrene-teams-cloud-image-lock-v1",
        "taskId": plan["taskId"],
        "targetPlatform": "linux/arm64",
        "state": "LOCAL_ONLY",
        "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "planSha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "sources": {},
        "binaryArtifacts": [],
        "baseImages": {},
        "externalImages": {},
        "images": [],
    }
    with tempfile.TemporaryDirectory(prefix="cyrene-teams-cloud-build-") as temporary:
        staging_root = Path(temporary)
        for alias, source in plan["sources"].items():
            lock["sources"][alias] = _archive_remote_source(alias, source, staging_root)
        artifact_by_id: dict[str, dict[str, Any]] = {}
        for asset in plan.get("binaryArtifacts", []):
            verified = _copy_binary_artifact(asset, staging_root)
            lock["binaryArtifacts"].append(verified)
            artifact_by_id[verified["id"]] = verified
        for image_key, reference in plan.get("externalImages", {}).items():
            resolved = resolve_image(reference)
            _run(["docker", "pull", "--platform", "linux/arm64", resolved["immutableReference"]])
            local_name = (
                f"cyrene-pinned/{image_key.lower()}:{resolved['indexDigest'].split(':', 1)[1][:12]}"
            )
            _run(["docker", "image", "tag", resolved["immutableReference"], local_name])
            image_id, _initial_size = _inspect_local_image(local_name)
            image_size = _measure_local_image_store_bytes(local_name, image_id)["sizeBytes"]
            resolved["localImage"] = local_name
            resolved["imageId"] = image_id
            resolved["sizeBytes"] = image_size
            lock["externalImages"][image_key] = resolved
        for build_row in plan["images"]:
            service = build_row["service"]
            dockerfile = staging_root / _safe_relative(
                build_row["dockerfile"], f"{service} dockerfile"
            )
            context = staging_root / _safe_relative(
                build_row.get("context", "."), f"{service} context"
            )
            # A dot build context is the staged source root, never the local checkout.
            if build_row.get("context", ".") == ".":
                context = staging_root
            if not dockerfile.is_file() or not context.is_dir():
                raise BuildError(
                    f"build inputs are missing in selected source snapshot for {service}"
                )
            resolved_args: dict[str, str] = {}
            base_rows: dict[str, Any] = {}
            for key, value in build_row.get("buildArgs", {}).items():
                resolved = resolve_image(value) if key.endswith(("_IMAGE", "_BASE_IMAGE")) else None
                resolved_args[key] = resolved["immutableReference"] if resolved else value
                if resolved:
                    base_rows[key] = resolved
            image_asset_ids = build_row.get("binaryArtifacts", [])
            image_asset_digests: dict[str, str] = {}
            for asset_id in image_asset_ids:
                asset = artifact_by_id[asset_id]
                build_arg = "ASSET_" + re.sub(r"[^A-Z0-9]", "_", asset_id.upper()) + "_SHA256"
                if build_arg in resolved_args:
                    raise BuildError(
                        f"image {service} duplicates generated build argument {build_arg}"
                    )
                resolved_args[build_arg] = asset["sha256"]
                image_asset_digests[asset_id] = asset["sha256"]
            tag_sha = lock["sources"][build_row["sourceLabelRepo"]]["commit"][:12]
            image_ref = f"cyrene/{service}:{plan['taskId']}-{tag_sha}"
            argv = [
                "docker",
                "buildx",
                "build",
                "--platform",
                "linux/arm64",
                "--load",
                "--tag",
                image_ref,
            ]
            argv.extend(["--file", str(dockerfile)])
            for key, value in sorted(resolved_args.items()):
                argv.extend(["--build-arg", f"{key}={value}"])
            source_commits = {
                alias: lock["sources"][alias]["commit"] for alias in build_row["sourceRepos"]
            }
            argv.extend(["--label", "org.opencontainers.image.version=" + plan["taskId"]])
            argv.extend(
                [
                    "--label",
                    "org.opencontainers.image.revision="
                    + source_commits[build_row["sourceLabelRepo"]],
                ]
            )
            argv.extend(
                ["--build-arg", "SOURCE_REVISION=" + source_commits[build_row["sourceLabelRepo"]]]
            )
            for alias, commit in sorted(source_commits.items()):
                argv.extend(["--label", f"io.cyrene.source.{alias}.revision={commit}"])
            for asset_id, digest in sorted(image_asset_digests.items()):
                argv.extend(["--label", f"io.cyrene.binary-asset.{asset_id}.sha256={digest}"])
                argv.extend(
                    [
                        "--label",
                        f"io.cyrene.binary-asset.{asset_id}.version="
                        + artifact_by_id[asset_id]["version"],
                    ]
                )
            argv.append(str(context))
            _run(argv)
            inspect = json.loads(_run(["docker", "image", "inspect", image_ref], capture=True))[0]
            platform_value = f"{inspect.get('Os')}/{inspect.get('Architecture')}"
            if platform_value not in {"linux/arm64", "linux/aarch64"}:
                raise BuildError(
                    f"built image {service} reports {platform_value}, expected linux/arm64"
                )
            if not isinstance(inspect.get("Id"), str) or not IMAGE_DIGEST_RE.fullmatch(
                inspect["Id"]
            ):
                raise BuildError(f"built image {service} has no content-addressed local image ID")
            image_size = _measure_local_image_store_bytes(image_ref, inspect["Id"])["sizeBytes"]
            labels = inspect.get("Config", {}).get("Labels") or {}
            expected_revision = source_commits[build_row["sourceLabelRepo"]]
            if labels.get("org.opencontainers.image.revision") != expected_revision:
                raise BuildError(f"built image {service} lost its pinned source revision label")
            for alias, commit in source_commits.items():
                if labels.get(f"io.cyrene.source.{alias}.revision") != commit:
                    raise BuildError(f"built image {service} lost source provenance for {alias}")
            for asset_id, digest in image_asset_digests.items():
                if labels.get(f"io.cyrene.binary-asset.{asset_id}.sha256") != digest:
                    raise BuildError(
                        f"built image {service} lost provenance for binary asset {asset_id}"
                    )
                if (
                    labels.get(f"io.cyrene.binary-asset.{asset_id}.version")
                    != artifact_by_id[asset_id]["version"]
                ):
                    raise BuildError(
                        f"built image {service} lost version for binary asset {asset_id}"
                    )
            lock["baseImages"].update(base_rows)
            lock["images"].append(
                {
                    "service": service,
                    "image": image_ref,
                    "imageId": inspect["Id"],
                    "platform": "linux/arm64",
                    "sizeBytes": image_size,
                    "sourceCommits": source_commits,
                    "binaryArtifacts": image_asset_ids,
                    "binaryArtifactDigests": image_asset_digests,
                    "registryDigest": None,
                }
            )
    lock_path = output / "image-lock.json"
    _atomic_json(lock_path, lock)
    _write_compose_env(output / "images.env", lock)
    return lock


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    """Write JSON without leaving a partial provenance record."""

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _write_compose_env(path: Path, lock: dict[str, Any]) -> None:
    """Render only image refs; never put credentials or private config in this file."""

    rows = ["# Generated from image-lock.json; contains no credentials."]
    for row in lock["images"]:
        key = "CYRENE_IMAGE_" + re.sub(r"[^A-Z0-9]", "_", row["service"].upper())
        rows.append(f"{key}={row['image']}")
    for key, row in sorted(lock.get("externalImages", {}).items()):
        rows.append(f"CYRENE_{key}_IMAGE={row['localImage']}")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(rows) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _inspect_local_image(reference: str, docker_prefix: list[str] | None = None) -> tuple[str, int]:
    """Read a local image ID and enforce the selected ARM64 platform."""

    inspect = json.loads(
        _run([*(docker_prefix or ["docker"]), "image", "inspect", reference], capture=True)
    )[0]
    platform_value = f"{inspect.get('Os')}/{inspect.get('Architecture')}"
    if platform_value not in {"linux/arm64", "linux/aarch64"}:
        raise BuildError(f"image {reference} reports {platform_value}, expected linux/arm64")
    return inspect.get("Id", ""), int(inspect.get("Size", 0))


def _read_oci_json_member(
    archive: tarfile.TarFile, members: dict[str, tarfile.TarInfo], name: str
) -> tuple[dict[str, Any], bytes]:
    """Read one bounded regular JSON member from a Docker OCI image archive."""

    member = members.get(name)
    if member is None or not member.isfile() or member.size > MAX_OCI_JSON_BYTES:
        raise BuildError(f"Docker image archive metadata is missing or oversized: {name}")
    stream = archive.extractfile(member)
    if stream is None:
        raise BuildError(f"Docker image archive metadata is unreadable: {name}")
    raw = stream.read(MAX_OCI_JSON_BYTES + 1)
    if len(raw) != member.size or len(raw) > MAX_OCI_JSON_BYTES:
        raise BuildError(f"Docker image archive metadata is truncated: {name}")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildError(f"Docker image archive metadata is invalid JSON: {name}") from error
    if not isinstance(document, dict):
        raise BuildError(f"Docker image archive metadata is not an object: {name}")
    return document, raw


def _verify_archive_blob(
    archive: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    digest: str,
    expected_size: int,
) -> None:
    """Verify one content-addressed OCI blob without extracting it to disk."""

    if not IMAGE_DIGEST_RE.fullmatch(digest) or expected_size <= 0:
        raise BuildError("Docker image archive contains a malformed layer descriptor")
    member = members.get("blobs/sha256/" + digest.partition(":")[2])
    if member is None or not member.isfile() or member.size != expected_size:
        raise BuildError("Docker image archive is missing a declared content-addressed blob")
    stream = archive.extractfile(member)
    if stream is None:
        raise BuildError("Docker image archive blob is unreadable")
    hasher = hashlib.sha256()
    bytes_read = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        bytes_read += len(chunk)
        if bytes_read > expected_size:
            raise BuildError("Docker image archive blob exceeds its declared size")
        hasher.update(chunk)
    if bytes_read != expected_size or "sha256:" + hasher.hexdigest() != digest:
        raise BuildError("Docker image archive blob failed its content digest check")


def _compressed_oci_layer_bytes(
    reference: str,
    expected_image_id: str,
    docker_prefix: list[str] | None = None,
) -> int:
    """Validate and total unique compressed OCI layers from `docker image save`.

    中文：核验镜像归档中的内容摘要，并统计唯一压缩层的实际字节数。
    """

    prefix = docker_prefix or ["docker"]
    command = [*prefix, "image", "save", reference]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise BuildError("could not start Docker image archive export") from error
    if process.stdout is None:
        process.kill()
        process.wait()
        raise BuildError("Docker image archive export has no output stream")

    try:
        with tempfile.TemporaryFile(prefix="cyrene-image-save-") as saved_archive:
            archive_bytes = 0
            while True:
                chunk = process.stdout.read(1024 * 1024)
                if not chunk:
                    break
                archive_bytes += len(chunk)
                if archive_bytes > MAX_IMAGE_SAVE_ARCHIVE_BYTES:
                    process.terminate()
                    raise BuildError("Docker image archive exceeds its bounded size")
                saved_archive.write(chunk)
            process.stdout.close()
            exit_code = process.wait()
            if exit_code != 0:
                raise BuildError(f"Docker image archive export failed ({exit_code})")
            if archive_bytes == 0:
                raise BuildError("Docker image archive export was empty")
            saved_archive.seek(0)
            with tarfile.open(fileobj=saved_archive, mode="r:") as archive:
                try:
                    archive_members = archive.getmembers()
                except (tarfile.TarError, OSError) as error:
                    raise BuildError("Docker image archive directory is invalid") from error
                if len(archive_members) > MAX_IMAGE_SAVE_MEMBERS:
                    raise BuildError("Docker image archive contains too many entries")
                members: dict[str, tarfile.TarInfo] = {}
                for member in archive_members:
                    member_path = PurePosixPath(member.name)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise BuildError("Docker image archive contains an unsafe member path")
                    if member.name in members:
                        raise BuildError("Docker image archive contains duplicate member paths")
                    members[member.name] = member

                layout, _layout_bytes = _read_oci_json_member(archive, members, "oci-layout")
                if layout.get("imageLayoutVersion") != "1.0.0":
                    raise BuildError("Docker image archive uses an unsupported OCI layout version")
                index, _index_bytes = _read_oci_json_member(archive, members, "index.json")
                if index.get("schemaVersion") != 2:
                    raise BuildError("Docker image archive OCI index is invalid")
                matching_manifests: list[tuple[dict[str, Any], dict[str, Any]]] = []
                seen_descriptors: set[str] = set()
                descriptor_count = 0

                def visit_descriptor(descriptor: Any, depth: int) -> None:
                    """Follow bounded OCI indexes to one platform-matched image manifest."""

                    nonlocal descriptor_count
                    if not isinstance(descriptor, dict):
                        raise BuildError("Docker image archive OCI index descriptor is invalid")
                    descriptor_count += 1
                    if descriptor_count > MAX_OCI_MANIFESTS:
                        raise BuildError("Docker image archive OCI index has too many descriptors")
                    digest, descriptor_size = descriptor.get("digest"), descriptor.get("size")
                    if (
                        not isinstance(digest, str)
                        or not IMAGE_DIGEST_RE.fullmatch(digest)
                        or not isinstance(descriptor_size, int)
                        or isinstance(descriptor_size, bool)
                        or descriptor_size <= 0
                        or descriptor_size > MAX_OCI_JSON_BYTES
                    ):
                        raise BuildError("Docker image archive OCI descriptor is invalid")

                    platform_info = descriptor.get("platform")
                    if platform_info is not None:
                        if not isinstance(platform_info, dict):
                            raise BuildError("Docker image archive descriptor platform is invalid")
                        os_name, architecture = (
                            platform_info.get("os"),
                            platform_info.get("architecture"),
                        )
                        if not isinstance(os_name, str) or not isinstance(architecture, str):
                            return
                        if os_name == "unknown" or architecture == "unknown":
                            return
                        if os_name != "linux" or architecture not in {"arm64", "aarch64"}:
                            return
                    if digest in seen_descriptors:
                        return
                    seen_descriptors.add(digest)

                    document, document_bytes = _read_oci_json_member(
                        archive,
                        members,
                        "blobs/sha256/" + digest.partition(":")[2],
                    )
                    if (
                        len(document_bytes) != descriptor_size
                        or "sha256:" + hashlib.sha256(document_bytes).hexdigest() != digest
                    ):
                        raise BuildError(
                            "Docker image archive OCI descriptor failed its digest check"
                        )

                    nested_descriptors = document.get("manifests")
                    if nested_descriptors is not None:
                        if (
                            depth >= MAX_OCI_INDEX_DEPTH
                            or document.get("schemaVersion") != 2
                            or not isinstance(nested_descriptors, list)
                            or not nested_descriptors
                            or len(nested_descriptors) > MAX_OCI_MANIFESTS
                        ):
                            raise BuildError("Docker image archive nested OCI index is invalid")
                        for nested in nested_descriptors:
                            visit_descriptor(nested, depth + 1)
                        return

                    manifest = document
                    if document.get("schemaVersion") != 2 or not isinstance(
                        document.get("layers"), list
                    ):
                        raise BuildError("Docker image archive descriptor is not an image manifest")
                    config_descriptor = manifest.get("config")
                    if not isinstance(config_descriptor, dict):
                        raise BuildError("Docker image archive manifest has no config descriptor")
                    config_digest = config_descriptor.get("digest")
                    config_size = config_descriptor.get("size")
                    if (
                        not isinstance(config_digest, str)
                        or not IMAGE_DIGEST_RE.fullmatch(config_digest)
                        or not isinstance(config_size, int)
                        or isinstance(config_size, bool)
                        or config_size <= 0
                        or config_size > MAX_OCI_JSON_BYTES
                    ):
                        raise BuildError("Docker image archive config descriptor is invalid")
                    if digest != expected_image_id and config_digest != expected_image_id:
                        return
                    config, config_bytes = _read_oci_json_member(
                        archive,
                        members,
                        "blobs/sha256/" + config_digest.partition(":")[2],
                    )
                    if (
                        len(config_bytes) != config_size
                        or "sha256:" + hashlib.sha256(config_bytes).hexdigest() != config_digest
                    ):
                        raise BuildError("Docker image archive config failed its digest check")
                    if config.get("os") != "linux" or config.get("architecture") not in {
                        "arm64",
                        "aarch64",
                    }:
                        raise BuildError("Docker image archive config is not linux/arm64")
                    matching_manifests.append((manifest, config))

                descriptors = index.get("manifests")
                if (
                    not isinstance(descriptors, list)
                    or not descriptors
                    or len(descriptors) > MAX_OCI_MANIFESTS
                ):
                    raise BuildError("Docker image archive OCI index is invalid")
                for descriptor in descriptors:
                    visit_descriptor(descriptor, 0)

                if len(matching_manifests) != 1:
                    raise BuildError(
                        "Docker image archive does not contain exactly one manifest for the locked image ID"
                    )
                manifest, config = matching_manifests[0]
                layers = manifest.get("layers")
                if not isinstance(layers, list) or len(layers) > MAX_OCI_LAYERS:
                    raise BuildError("Docker image archive layer list is invalid")
                rootfs = config.get("rootfs")
                diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
                if (
                    not isinstance(diff_ids, list)
                    or len(diff_ids) != len(layers)
                    or any(
                        not isinstance(diff_id, str) or not IMAGE_DIGEST_RE.fullmatch(diff_id)
                        for diff_id in diff_ids
                    )
                ):
                    raise BuildError("Docker image archive config does not match its layer count")
                layer_sizes: dict[str, int] = {}
                accepted_layer_prefixes = (
                    "application/vnd.oci.image.layer.",
                    "application/vnd.oci.image.layer.nondistributable.",
                    "application/vnd.docker.image.rootfs.diff.",
                )
                for layer in layers:
                    if not isinstance(layer, dict):
                        raise BuildError("Docker image archive layer descriptor is invalid")
                    digest, size, media_type = (
                        layer.get("digest"),
                        layer.get("size"),
                        layer.get("mediaType"),
                    )
                    if (
                        not isinstance(digest, str)
                        or not IMAGE_DIGEST_RE.fullmatch(digest)
                        or not isinstance(size, int)
                        or isinstance(size, bool)
                        or size <= 0
                        or size > MAX_IMAGE_SAVE_ARCHIVE_BYTES
                        or not isinstance(media_type, str)
                        or not media_type.startswith(accepted_layer_prefixes)
                    ):
                        raise BuildError("Docker image archive layer descriptor is malformed")
                    previous = layer_sizes.setdefault(digest, size)
                    if previous != size:
                        raise BuildError(
                            "Docker image archive repeats a layer digest with new size"
                        )

                if not layer_sizes:
                    raise BuildError("Docker image archive contains no content layers")
                for digest, size in layer_sizes.items():
                    _verify_archive_blob(archive, members, digest, size)
                return sum(layer_sizes.values())
    except (OSError, tarfile.TarError) as error:
        raise BuildError("Docker image archive could not be measured safely") from error
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _measure_local_image_store_bytes(
    reference: str,
    expected_image_id: str,
    docker_prefix: list[str] | None = None,
) -> dict[str, int]:
    """Measure a conservative full ARM64 image footprint without starting its entrypoint.

    A labelled, no-network container is created but never started so Docker can report
    `SizeRootFs`. The final estimate also includes compressed OCI blobs and is removed
    in `finally` by its unique container ID.

    中文：创建不联网且不启动的临时容器，让 Docker 展开镜像并读取完整根文件系统大小；
    再加上压缩 OCI 层，并在 `finally` 中按本次容器 ID 清理。
    """

    prefix = docker_prefix or ["docker"]
    probe_name = "cyrene-size-probe-" + uuid.uuid4().hex
    probe_created = False
    probe_reference = probe_name
    try:
        container_id = _run(
            [
                *prefix,
                "container",
                "create",
                "--platform",
                "linux/arm64",
                "--network",
                "none",
                "--name",
                probe_name,
                "--label",
                "io.cyrene.teams-cloud.size-probe=" + probe_name,
                reference,
            ],
            capture=True,
        ).strip()
        probe_created = True
        if not container_id or not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            raise BuildError("Docker did not return a valid image-size probe container ID")
        probe_reference = container_id
        container_info = json.loads(
            _run(
                [*prefix, "container", "inspect", "--size", container_id],
                capture=True,
            )
        )
        if not isinstance(container_info, list) or len(container_info) != 1:
            raise BuildError("Docker image-size probe returned an invalid container record")
        info = container_info[0]
        if info.get("Image") != expected_image_id:
            raise BuildError("Docker image-size probe points at a different local image ID")
        probe_config = info.get("Config")
        probe_labels = probe_config.get("Labels") if isinstance(probe_config, dict) else None
        if (
            not isinstance(probe_labels, dict)
            or probe_labels.get("io.cyrene.teams-cloud.size-probe") != probe_name
        ):
            raise BuildError(
                "Docker image-size probe container is missing its unique ownership label"
            )
        state = info.get("State")
        if not isinstance(state, dict) or state.get("Status") != "created" or state.get("Running"):
            raise BuildError("Docker image-size probe container was started unexpectedly")
        rootfs_size = info.get("SizeRootFs")
        if not isinstance(rootfs_size, int) or isinstance(rootfs_size, bool) or rootfs_size <= 0:
            raise BuildError("Docker did not report an unpacked root filesystem size")
        local_id, materialized_image_size = _inspect_local_image(reference, prefix)
        if local_id != expected_image_id:
            raise BuildError("local image changed while measuring its root filesystem")
        compressed_layer_bytes = _compressed_oci_layer_bytes(reference, expected_image_id, prefix)
        return {
            "sizeBytes": max(materialized_image_size, rootfs_size + compressed_layer_bytes),
            "unpackedRootFsBytes": rootfs_size,
            "compressedLayerBytes": compressed_layer_bytes,
            "materializedImageBytes": materialized_image_size,
        }
    finally:
        if probe_created:
            _run([*prefix, "container", "rm", probe_reference], capture=True)


def _image_local_id(reference: str, docker_prefix: list[str] | None = None) -> str:
    """Read a local image ID without pulling or retagging the image."""

    return _inspect_local_image(reference, docker_prefix)[0]


def check_local_lock(lock_path: Path, docker_prefix: list[str] | None = None) -> None:
    """Refuse deployment when any local tag differs from its recorded image ID."""

    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildError("image lock is not readable JSON") from error
    if (
        lock.get("schemaVersion") != "cyrene-teams-cloud-image-lock-v1"
        or lock.get("targetPlatform") != "linux/arm64"
        or lock.get("state") not in {"LOCAL_ONLY", "PUBLISHED"}
    ):
        raise BuildError("unsupported image lock or target platform")
    if not lock.get("images"):
        raise BuildError("image lock contains no built images")
    artifact_rows = lock.get("binaryArtifacts", [])
    if not isinstance(artifact_rows, list):
        raise BuildError("image lock binaryArtifacts must be an array")
    artifact_by_id: dict[str, dict[str, Any]] = {}
    for artifact in artifact_rows:
        if (
            not isinstance(artifact, dict)
            or not isinstance(artifact.get("id"), str)
            or not isinstance(artifact.get("version"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("sha256", "")))
            or not isinstance(artifact.get("sizeBytes"), int)
            or artifact["sizeBytes"] <= 0
            or not isinstance(artifact.get("stagingPath"), str)
        ):
            raise BuildError("image lock contains a malformed binary artifact record")
        if artifact["id"] in artifact_by_id:
            raise BuildError(f"image lock repeats binary artifact {artifact['id']}")
        _safe_relative(artifact["stagingPath"], f"binary artifact {artifact['id']} stagingPath")
        artifact_by_id[artifact["id"]] = artifact
    for row in lock["images"]:
        image_artifacts = row.get("binaryArtifacts", [])
        image_digests = row.get("binaryArtifactDigests", {})
        if (
            not isinstance(image_artifacts, list)
            or len(set(image_artifacts)) != len(image_artifacts)
            or not isinstance(image_digests, dict)
            or set(image_digests) != set(image_artifacts)
        ):
            raise BuildError(
                f"image binary artifact references are malformed for {row.get('service')}"
            )
        for artifact_id in image_artifacts:
            artifact = artifact_by_id.get(artifact_id)
            if artifact is None or image_digests.get(artifact_id) != artifact["sha256"]:
                raise BuildError(
                    f"image binary artifact digest does not match lock for {row.get('service')}"
                )
        if not isinstance(row.get("sizeBytes"), int) or row["sizeBytes"] <= 0:
            raise BuildError(f"image size is missing from lock for {row.get('service')}")
        if row.get("registryDigest") is not None and not IMAGE_DIGEST_RE.fullmatch(
            row["registryDigest"]
        ):
            raise BuildError(f"published digest is malformed for {row.get('service')}")
        if lock["state"] == "PUBLISHED" and row.get("registryDigest") is None:
            raise BuildError(f"published lock has no registry digest for {row.get('service')}")
        inspected = json.loads(
            _run([*(docker_prefix or ["docker"]), "image", "inspect", row["image"]], capture=True)
        )[0]
        actual = inspected.get("Id")
        if actual != row.get("imageId"):
            raise BuildError(f"local image ID no longer matches lock for {row.get('service')}")
        labels = inspected.get("Config", {}).get("Labels") or {}
        for artifact_id in image_artifacts:
            artifact = artifact_by_id[artifact_id]
            if (
                labels.get(f"io.cyrene.binary-asset.{artifact_id}.sha256") != artifact["sha256"]
                or labels.get(f"io.cyrene.binary-asset.{artifact_id}.version")
                != artifact["version"]
            ):
                raise BuildError(
                    f"local image labels do not match binary asset {artifact_id} for {row.get('service')}"
                )
    for key, row in lock.get("externalImages", {}).items():
        if not IMAGE_DIGEST_RE.fullmatch(row.get("indexDigest", "")):
            raise BuildError(f"external image {key} has no immutable registry digest")
        actual = _image_local_id(row.get("localImage", ""), docker_prefix)
        if actual != row.get("imageId"):
            raise BuildError(f"external image {key} ID no longer matches lock")


def main() -> int:
    """Build, inspect, or verify an ARM64 image lock."""

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build", help="Build pinned sources as linux/arm64 images")
    build_parser.add_argument("--plan", type=Path, required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.add_argument("--allow-emulated", action="store_true")
    verify_parser = commands.add_parser(
        "verify-local", help="Check local image tags against the image-ID lock"
    )
    verify_parser.add_argument("--lock", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == "build":
            result = build(
                arguments.plan.resolve(),
                arguments.output.resolve(),
                allow_emulated=arguments.allow_emulated,
            )
            print(
                json.dumps(
                    {
                        "state": result["state"],
                        "targetPlatform": result["targetPlatform"],
                        "images": len(result["images"]),
                    }
                )
            )
        else:
            check_local_lock(arguments.lock.resolve())
            print("local ARM64 image IDs match the recorded lock")
    except (OSError, BuildError, subprocess.SubprocessError, tarfile.TarError) as error:
        print(f"ARM64 build blocked: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
