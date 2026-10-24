"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 build_arm64.py                                                       │
│  Module: tooling.deploy.teams_cloud.build_arm64                         │
│  Role: Build from exact remote Git commits and record real ARM64 images. │
│                                                                          │
│  模块职责：从精确远端提交构建 ARM64 镜像并记录真实制品身份。             │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
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
    """Build all plan images from remote snapshots and write a local image lock."""

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
        "baseImages": {},
        "externalImages": {},
        "images": [],
    }
    with tempfile.TemporaryDirectory(prefix="cyrene-teams-cloud-build-") as temporary:
        staging_root = Path(temporary)
        for alias, source in plan["sources"].items():
            lock["sources"][alias] = _archive_remote_source(alias, source, staging_root)
        for image_key, reference in plan.get("externalImages", {}).items():
            resolved = resolve_image(reference)
            _run(["docker", "pull", "--platform", "linux/arm64", resolved["immutableReference"]])
            image_id, image_size = _inspect_local_image(resolved["immutableReference"])
            local_name = (
                f"cyrene-pinned/{image_key.lower()}:{resolved['indexDigest'].split(':', 1)[1][:12]}"
            )
            _run(["docker", "image", "tag", resolved["immutableReference"], local_name])
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
            image_size = inspect.get("Size")
            if not isinstance(image_size, int) or image_size <= 0:
                raise BuildError(f"built image {service} has no measured local image size")
            labels = inspect.get("Config", {}).get("Labels") or {}
            expected_revision = source_commits[build_row["sourceLabelRepo"]]
            if labels.get("org.opencontainers.image.revision") != expected_revision:
                raise BuildError(f"built image {service} lost its pinned source revision label")
            for alias, commit in source_commits.items():
                if labels.get(f"io.cyrene.source.{alias}.revision") != commit:
                    raise BuildError(f"built image {service} lost source provenance for {alias}")
            lock["baseImages"].update(base_rows)
            lock["images"].append(
                {
                    "service": service,
                    "image": image_ref,
                    "imageId": inspect["Id"],
                    "platform": "linux/arm64",
                    "sizeBytes": image_size,
                    "sourceCommits": source_commits,
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
    for row in lock["images"]:
        if not isinstance(row.get("sizeBytes"), int) or row["sizeBytes"] <= 0:
            raise BuildError(f"image size is missing from lock for {row.get('service')}")
        if row.get("registryDigest") is not None and not IMAGE_DIGEST_RE.fullmatch(
            row["registryDigest"]
        ):
            raise BuildError(f"published digest is malformed for {row.get('service')}")
        if lock["state"] == "PUBLISHED" and row.get("registryDigest") is None:
            raise BuildError(f"published lock has no registry digest for {row.get('service')}")
        actual = _image_local_id(row["image"], docker_prefix)
        if actual != row.get("imageId"):
            raise BuildError(f"local image ID no longer matches lock for {row.get('service')}")
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
