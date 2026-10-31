"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 test_build_arm64.py                                                  │
│  Module: tooling.deploy.teams_cloud.tests.test_build_arm64              │
│  Role: Verify source and image-reference lock requirements.              │
│                                                                          │
│  模块职责：验证源码及镜像引用锁定要求。                                  │
└──────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
build = __import__("build_arm64")


def test_computer_proxy_compose_uses_the_image_lock_generated_key(tmp_path: Path) -> None:
    """Keep the proxy service reference aligned with build_arm64's canonical lock variable."""

    output = tmp_path / "images.env"
    build._write_compose_env(
        output,
        {
            "images": [
                {
                    "service": "computer-runtime-proxy",
                    "image": "cyrene/computer-runtime-proxy:test",
                }
            ],
            "externalImages": {},
        },
    )
    assert "CYRENE_IMAGE_COMPUTER_RUNTIME_PROXY=cyrene/computer-runtime-proxy:test" in (
        output.read_text(encoding="utf-8")
    )

    deployment_root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load(
        (deployment_root / "compose.computer-runtime.yaml").read_text(encoding="utf-8")
    )
    image = compose["services"]["computer-runtime-proxy"]["image"]
    assert image.startswith("${CYRENE_IMAGE_COMPUTER_RUNTIME_PROXY:")


def test_authority_rust_builder_uses_index_for_build_platform_only() -> None:
    """Cross-build Authority with the host child while keeping runtime inputs ARM64-pinned."""

    arm_digest = "a" * 64
    index_digest = "b" * 64
    resolved = {
        "reference": "rust:1.96.1-slim-bookworm@sha256:" + index_digest,
        "immutableReference": "rust:1.96.1-slim-bookworm@sha256:" + arm_digest,
        "indexReference": "rust:1.96.1-slim-bookworm@sha256:" + index_digest,
        "indexDigest": "sha256:" + index_digest,
        "arm64ManifestDigest": "sha256:" + arm_digest,
        "platform": "linux/arm64",
    }

    authority_rust, authority_key, authority_lock = build._base_image_build_input(
        "workspace-authority", "RUST_BASE_IMAGE", resolved, "linux/amd64"
    )
    assert authority_rust == resolved["indexReference"]
    assert authority_key == "workspace-authority.RUST_BASE_IMAGE"
    assert authority_lock["buildInputReference"] == resolved["indexReference"]
    assert authority_lock["buildInputPlatform"] == "linux/amd64"
    assert authority_lock["immutableReference"] == resolved["immutableReference"]

    authority_runtime, runtime_key, runtime_lock = build._base_image_build_input(
        "workspace-authority", "RUNTIME_BASE_IMAGE", resolved, "linux/amd64"
    )
    other_rust, other_key, other_lock = build._base_image_build_input(
        "computer-runtime", "RUST_BASE_IMAGE", resolved, "linux/amd64"
    )
    assert authority_runtime == resolved["immutableReference"]
    assert runtime_key == "RUNTIME_BASE_IMAGE"
    assert runtime_lock["buildInputPlatform"] == "linux/arm64"
    assert other_rust == resolved["immutableReference"]
    assert other_key == "RUST_BASE_IMAGE"
    assert other_lock["buildInputPlatform"] == "linux/arm64"


def _plan() -> dict[str, Any]:
    """Build the smallest valid plan with immutable ARM64 image indexes."""

    commit = "a" * 40
    digest = "b" * 64
    return {
        "schemaVersion": "cyrene-teams-cloud-build-v1",
        "taskId": "cloud-task-20261023",
        "targetPlatform": "linux/arm64",
        "sources": {
            "workspace": {
                "remote": "https://github.com/DoHorizon-AI/Cyrene-Workspace.git",
                "commit": commit,
                "checkoutPath": "Cyrene-Workspace",
            }
        },
        "images": [
            {
                "service": "runtime",
                "dockerfile": "Cyrene-Workspace/Dockerfile",
                "context": ".",
                "sourceRepos": ["workspace"],
                "sourceLabelRepo": "workspace",
                "buildArgs": {"PYTHON_BASE_IMAGE": f"python:3.12-slim@sha256:{digest}"},
            }
        ],
        "externalImages": {"POSTGRES": f"postgres:16-bookworm@sha256:{digest}"},
    }


def test_computer_runtime_build_plan_copies_the_plugin_proto_tree() -> None:
    """Keep the generated DirectPluginRuntime proto available to the pinned Cargo build."""

    deployment_root = Path(__file__).resolve().parents[1]
    plan = json.loads((deployment_root / "config" / "build-plan.template.json").read_text())
    image = next(row for row in plan["images"] if row["service"] == "computer-runtime")
    authority = next(row for row in plan["images"] if row["service"] == "workspace-authority")
    dockerfile = (deployment_root / "Dockerfile.computer-runtime").read_text()

    assert "plugins" in image["sourceRepos"]
    runtime_base = image["buildArgs"]["RUNTIME_BASE_IMAGE"]
    assert runtime_base == authority["buildArgs"]["RUNTIME_BASE_IMAGE"]
    runtime_digest = runtime_base.rsplit("@sha256:", 1)[1]
    assert len(runtime_digest) == 64
    assert all(character in "0123456789abcdef" for character in runtime_digest)
    assert "COPY Cyrene-Plugins-Official/contracts/proto ./contracts/proto" in dockerfile


def test_dsh_upstream_build_cache_isolated_from_navigator_app_sources() -> None:
    """Keep the pinned DSH SDK build cache independent from Navigator application changes.

    确保 Navigator 应用源码变化不会重新触发固定 DSH SDK 的耗时构建。
    """

    deployment_root = Path(__file__).resolve().parents[1]
    dockerfile = (deployment_root / "Dockerfile.dsh-executor").read_text(encoding="utf-8")
    plan = json.loads(
        (deployment_root / "config" / "build-plan.template.json").read_text(encoding="utf-8")
    )
    dsh_image = next(row for row in plan["images"] if row["service"] == "dsh-executor")
    dsh_commit = next(
        line.partition("=")[2]
        for line in dockerfile.splitlines()
        if line.startswith("ARG DSH_COMMIT=")
    )

    upstream_stage = dockerfile.index("FROM ${NODE_BASE_IMAGE} AS dsh-upstream")
    app_stage = dockerfile.index("FROM dsh-upstream AS build")
    upstream_lock_copy = dockerfile.index(
        "COPY Cyrene-Services/Cyrene-Navigator/harness/upstream.lock.json"
    )
    prepare_script_copy = dockerfile.index(
        "COPY Cyrene-Services/Cyrene-Navigator/scripts/prepare-harness.mjs"
    )
    upstream_build = dockerfile.index(
        "node /app/scripts/prepare-harness.mjs --root=/opt/dsh --install --build --link"
    )
    app_copies = [
        dockerfile.index("COPY Cyrene-Services/Cyrene-Navigator/harness/ ./harness/"),
        dockerfile.index("COPY Cyrene-Services/Cyrene-Navigator/scripts/ ./scripts/"),
        dockerfile.index("COPY Cyrene-Services/Cyrene-Navigator/src/ ./src/"),
    ]
    dependency_link_copy = dockerfile.index(
        "COPY --from=dsh-upstream /app/harness/node_modules/ ./harness/node_modules/"
    )
    harness_compile = dockerfile.index(
        "node /opt/dsh/node_modules/typescript/bin/tsc -p /app/harness/tsconfig.json --pretty false"
    )

    assert upstream_stage < upstream_lock_copy < upstream_build
    assert upstream_stage < prepare_script_copy < upstream_build
    assert upstream_build < app_stage < min(app_copies)
    assert max(app_copies) < dependency_link_copy < harness_compile
    assert dsh_image["buildArgs"]["DSH_COMMIT"] == dsh_commit
    assert dsh_image["sourceLabelRepo"] == "navigator"
    assert set(dsh_image["sourceRepos"]) == {"workspace", "navigator", "dsh"}
    assert plan["sources"]["dsh"]["commit"] == dsh_commit


def test_codebuddy_release_dockerfile_needs_no_private_antigravity_binary() -> None:
    """Keep the automatic DSH release build on the pinned CodeBuddy ACP path only."""

    deployment_root = Path(__file__).resolve().parents[1]
    dockerfile = (deployment_root / "Dockerfile.dsh-executor-codebuddy").read_text(encoding="utf-8")
    plan = json.loads(
        (deployment_root / "config" / "build-plan.template.json").read_text(encoding="utf-8")
    )
    dsh_image = next(row for row in plan["images"] if row["service"] == "dsh-executor")

    assert "@tencent-ai/codebuddy-code@2.163.0" in dockerfile
    assert 'CYRENE_HARNESS_MODEL_SOURCE=""' in dockerfile
    assert "CYRENE_HARNESS_MODEL=glm-5.3-flash" in dockerfile
    assert "antigravity" not in dockerfile.lower()
    assert "ASSET_ANTIGRAVITY_CLI_SHA256" not in dockerfile
    assert "runtime-artifacts/" not in dockerfile
    assert dsh_image["buildArgs"]["CODEBUDDY_PACKAGE"] == "@tencent-ai/codebuddy-code@2.163.0"


def test_cloud_management_pins_the_versioned_gcloud_archive_and_digest() -> None:
    """Keep the GCloud archive URL, version, and verified ARM64 digest aligned."""

    deployment_root = Path(__file__).resolve().parents[1]
    dockerfile = (deployment_root / "Dockerfile.cloud-management").read_text(encoding="utf-8")
    plan = json.loads(
        (deployment_root / "config" / "build-plan.template.json").read_text(encoding="utf-8")
    )
    cloud_image = next(row for row in plan["images"] if row["service"] == "cloud-management")
    build_args = cloud_image["buildArgs"]
    docker_args = {
        line.removeprefix("ARG ").partition("=")[0]: line.partition("=")[2]
        for line in dockerfile.splitlines()
        if line.startswith("ARG GCLOUD_CLI_")
    }

    assert build_args["GCLOUD_CLI_VERSION"] == "590.0.1"
    assert build_args["GCLOUD_CLI_ARM64_SHA256"] == (
        "fd760d8b1c2c4c484d7b659964994b07aaefddc6ac4350e681940477dbdbb22c"
    )
    assert build_args["GCLOUD_CLI_VERSION"] == docker_args["GCLOUD_CLI_VERSION"]
    assert build_args["GCLOUD_CLI_ARM64_SHA256"] == docker_args["GCLOUD_CLI_ARM64_SHA256"]
    assert "google-cloud-cli-${GCLOUD_CLI_VERSION}-linux-arm.tar.gz" in dockerfile
    assert "downloads/google-cloud-cli-linux-arm.tar.gz" not in dockerfile


def test_build_plan_requires_digest_pinned_base_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accept immutable image references and reject floating version tags."""

    plan_path = tmp_path / "build-plan.json"
    plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
    monkeypatch.setattr(build, "_private_artifact", lambda _path: True)
    assert build.validate_plan(plan_path)["targetPlatform"] == "linux/arm64"

    plan = _plan()
    plan["images"][0]["buildArgs"]["PYTHON_BASE_IMAGE"] = "python:3.12-slim"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(build.BuildError, match="pin a registry digest"):
        build.validate_plan(plan_path)


@pytest.mark.parametrize("digest", ["b" * 63, "g" * 64, "b" * 65])
def test_build_plan_rejects_base_image_digests_unless_exactly_64_hex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, digest: str
) -> None:
    """Reject truncated, non-hex, and oversized fixed-image SHA-256 digests."""

    plan = _plan()
    plan["images"][0]["buildArgs"]["PYTHON_BASE_IMAGE"] = f"python:3.12-slim@sha256:{digest}"
    plan_path = tmp_path / "build-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(build, "_private_artifact", lambda _path: True)
    with pytest.raises(build.BuildError, match="pin a registry digest"):
        build.validate_plan(plan_path)


def test_build_plan_rejects_tag_only_external_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Require the runtime services' Postgres and Caddy images to be immutable too."""

    plan = _plan()
    plan["externalImages"]["POSTGRES"] = "postgres:16-bookworm"
    plan_path = tmp_path / "build-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(build, "_private_artifact", lambda _path: True)
    with pytest.raises(build.BuildError, match="pin a registry digest"):
        build.validate_plan(plan_path)


def test_build_plan_pins_private_binary_assets_to_one_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Require an explicit artifact digest and an image reference for the CLI asset."""

    source = tmp_path / "private" / "agy"
    source.parent.mkdir()
    source.write_bytes(b"approved arm64 cli")
    plan = _plan()
    asset_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    plan["binaryArtifacts"] = [
        {
            "id": "antigravity-cli",
            "version": "1.4.7",
            "sourcePath": str(source),
            "sha256": asset_digest,
            "stagingPath": "runtime-artifacts/antigravity/agy",
        }
    ]
    plan["images"][0]["binaryArtifacts"] = ["antigravity-cli"]
    plan_path = tmp_path / "build-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    monkeypatch.setattr(build, "_private_artifact", lambda _path: True)
    assert build.validate_plan(plan_path)["binaryArtifacts"][0]["id"] == "antigravity-cli"

    plan["images"][0]["binaryArtifacts"] = ["missing-cli"]
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(build.BuildError, match="unknown or duplicated binary artifact"):
        build.validate_plan(plan_path)


def test_binary_artifact_copy_checks_digest_and_preserves_staging_path(tmp_path: Path) -> None:
    """Reject mutated CLI bytes and stage a verified private input under the build context."""

    source = tmp_path / "agy"
    source.write_bytes(b"approved arm64 cli")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    asset = {
        "id": "antigravity-cli",
        "version": "1.4.7",
        "sourcePath": str(source),
        "sha256": digest,
        "stagingPath": "runtime-artifacts/antigravity/agy",
    }
    staging = tmp_path / "staging"
    record = build._copy_binary_artifact(asset, staging)
    staged = staging / "runtime-artifacts/antigravity/agy"
    assert staged.read_bytes() == source.read_bytes()
    assert record["sha256"] == digest
    assert staged.stat().st_mode & 0o777 == 0o555

    asset["sha256"] = "0" * 64
    with pytest.raises(build.BuildError, match="failed its pinned SHA-256"):
        build._copy_binary_artifact(asset, tmp_path / "rejected")


def _oci_archive(
    *,
    duplicate_layer: bool = False,
    omit_layer_blob: bool = False,
    layer_digest_override: str | None = None,
    layer_size_delta: int = 0,
    include_index: bool = True,
    nested_index_depth: int = 0,
) -> tuple[bytes, str, int, str]:
    """Create a small content-addressed OCI archive for image-size probes."""

    uncompressed_layer = b"cyrene synthetic layer tar payload"
    compressed_layer = gzip.compress(uncompressed_layer, mtime=0)
    actual_layer_digest = "sha256:" + hashlib.sha256(compressed_layer).hexdigest()
    layer_digest = layer_digest_override or actual_layer_digest
    layer_size = len(compressed_layer) + layer_size_delta
    descriptor = {
        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
        "digest": layer_digest,
        "size": layer_size,
    }
    layer_rows = [descriptor, descriptor] if duplicate_layer else [descriptor]
    diff_ids = ["sha256:" + hashlib.sha256(uncompressed_layer).hexdigest()]
    if duplicate_layer:
        diff_ids.append(diff_ids[0])
    config_bytes = json.dumps(
        {
            "architecture": "arm64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": diff_ids},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    manifest_bytes = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": config_digest,
                "size": len(config_bytes),
            },
            "layers": layer_rows,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    index_bytes = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": manifest_digest,
                    "size": len(manifest_bytes),
                    "platform": {"os": "linux", "architecture": "arm64"},
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    nested_index_blobs: dict[str, bytes] = {}
    for _ in range(nested_index_depth):
        nested_digest = "sha256:" + hashlib.sha256(index_bytes).hexdigest()
        nested_index_blobs["blobs/sha256/" + nested_digest.split(":", 1)[1]] = index_bytes
        index_bytes = json.dumps(
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.index.v1+json",
                        "digest": nested_digest,
                        "size": len(
                            nested_index_blobs["blobs/sha256/" + nested_digest.split(":", 1)[1]]
                        ),
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    files = {
        "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
        "index.json": index_bytes,
        "blobs/sha256/" + manifest_digest.split(":", 1)[1]: manifest_bytes,
        "blobs/sha256/" + config_digest.split(":", 1)[1]: config_bytes,
        "blobs/sha256/" + layer_digest.split(":", 1)[1]: compressed_layer,
        **nested_index_blobs,
    }
    if omit_layer_blob:
        files.pop("blobs/sha256/" + layer_digest.split(":", 1)[1])
    if not include_index:
        files.pop("index.json")

    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w:") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    return archive_bytes.getvalue(), config_digest, len(compressed_layer), manifest_digest


class _FakeDockerSaveProcess:
    """Provide one mocked `docker image save` tar stream to the parser."""

    def __init__(self, archive_bytes: bytes) -> None:
        self.stdout = io.BytesIO(archive_bytes)
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        self._returncode = 0
        return 0

    def terminate(self) -> None:
        self._returncode = -15

    def kill(self) -> None:
        self._returncode = -9


def test_compressed_oci_layer_size_deduplicates_and_verifies_blobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Count descriptor bytes once per digest after matching the exact image ID."""

    archive_bytes, image_id, compressed_size, _manifest_id = _oci_archive(duplicate_layer=True)
    monkeypatch.setattr(
        build.subprocess,
        "Popen",
        lambda *_args, **_kwargs: _FakeDockerSaveProcess(archive_bytes),
    )
    assert build._compressed_oci_layer_bytes("example/image:tag", image_id) == compressed_size


def test_compressed_oci_layer_size_accepts_engine_manifest_image_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Match an image ID whether Docker exposes the OCI manifest or config digest."""

    archive_bytes, _config_id, compressed_size, manifest_id = _oci_archive()
    monkeypatch.setattr(
        build.subprocess,
        "Popen",
        lambda *_args, **_kwargs: _FakeDockerSaveProcess(archive_bytes),
    )
    assert build._compressed_oci_layer_bytes("example/image:tag", manifest_id) == compressed_size


def test_compressed_oci_layer_size_follows_bounded_nested_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Follow multiple content-addressed OCI indexes before measuring the ARM64 leaf."""

    archive_bytes, image_id, compressed_size, _manifest_id = _oci_archive(nested_index_depth=2)
    monkeypatch.setattr(
        build.subprocess,
        "Popen",
        lambda *_args, **_kwargs: _FakeDockerSaveProcess(archive_bytes),
    )
    assert build._compressed_oci_layer_bytes("example/image:tag", image_id) == compressed_size


def test_compressed_oci_layer_size_rejects_excessive_index_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop following attacker-controlled OCI index chains beyond the fixed bound."""

    archive_bytes, image_id, _compressed_size, _manifest_id = _oci_archive(
        nested_index_depth=build.MAX_OCI_INDEX_DEPTH + 1
    )
    monkeypatch.setattr(
        build.subprocess,
        "Popen",
        lambda *_args, **_kwargs: _FakeDockerSaveProcess(archive_bytes),
    )
    with pytest.raises(build.BuildError, match="nested OCI index is invalid"):
        build._compressed_oci_layer_bytes("example/image:tag", image_id)


@pytest.mark.parametrize(
    ("archive_options", "expected_id", "error"),
    [
        ({"omit_layer_blob": True}, None, "missing a declared"),
        ({"layer_size_delta": 1}, None, "missing a declared"),
        ({"layer_digest_override": "sha256:" + "f" * 64}, None, "failed its content digest"),
        ({}, "sha256:" + "e" * 64, "exactly one manifest"),
        ({"include_index": False}, None, "metadata is missing or oversized"),
    ],
)
def test_compressed_oci_layer_size_rejects_incomplete_or_mismatched_archives(
    monkeypatch: pytest.MonkeyPatch,
    archive_options: dict[str, Any],
    expected_id: str | None,
    error: str,
) -> None:
    """Fail closed on missing blobs, false digests, wrong IDs, and non-OCI saves."""

    archive_bytes, image_id, _compressed_size, _manifest_id = _oci_archive(**archive_options)
    monkeypatch.setattr(
        build.subprocess,
        "Popen",
        lambda *_args, **_kwargs: _FakeDockerSaveProcess(archive_bytes),
    )
    with pytest.raises(build.BuildError, match=error):
        build._compressed_oci_layer_bytes("example/image:tag", expected_id or image_id)


def test_image_size_probe_uses_unpacked_rootfs_and_removes_unstarted_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use `SizeRootFs` plus compressed layers without running the image entrypoint."""

    image_id = "sha256:" + "a" * 64
    calls: list[list[str]] = []

    def fake_run(argv: list[str], *, cwd: Path | None = None, capture: bool = False) -> str:
        calls.append(argv)
        if "create" in argv:
            return "b" * 64
        if "inspect" in argv:
            probe_name = argv[-1]
            create = next(command for command in calls if "create" in command)
            label = create[create.index("--label") + 1]
            label_value = label.split("=", 1)[1]
            name = create[create.index("--name") + 1]
            assert probe_name == "b" * 64
            assert label_value == name
            return json.dumps(
                [
                    {
                        "Image": image_id,
                        "State": {"Status": "created", "Running": False},
                        "SizeRootFs": 200,
                        "Config": {"Labels": {"io.cyrene.teams-cloud.size-probe": name}},
                    }
                ]
            )
        if "rm" in argv:
            return "b" * 64
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(build, "_run", fake_run)
    monkeypatch.setattr(build, "_inspect_local_image", lambda *_args: (image_id, 180))
    monkeypatch.setattr(build, "_compressed_oci_layer_bytes", lambda *_args: 50)

    measured = build._measure_local_image_store_bytes("example/image:tag", image_id)
    assert measured == {
        "sizeBytes": 250,
        "unpackedRootFsBytes": 200,
        "compressedLayerBytes": 50,
        "materializedImageBytes": 180,
    }
    assert any(
        "--network" in command and command[command.index("--network") + 1] == "none"
        for command in calls
    )
    assert not any("start" in command for command in calls)
    assert calls[-1][-2:] == ["rm", "b" * 64]


def test_image_size_probe_removes_its_container_on_health_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean up the probe container even when Docker reports it as started."""

    image_id = "sha256:" + "a" * 64
    calls: list[list[str]] = []

    def fake_run(argv: list[str], *, cwd: Path | None = None, capture: bool = False) -> str:
        calls.append(argv)
        if "create" in argv:
            return "c" * 64
        if "inspect" in argv:
            create = next(command for command in calls if "create" in command)
            name = create[create.index("--name") + 1]
            return json.dumps(
                [
                    {
                        "Image": image_id,
                        "State": {"Status": "running", "Running": True},
                        "SizeRootFs": 200,
                        "Config": {"Labels": {"io.cyrene.teams-cloud.size-probe": name}},
                    }
                ]
            )
        if "rm" in argv:
            return "c" * 64
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(build, "_run", fake_run)
    with pytest.raises(build.BuildError, match="started unexpectedly"):
        build._measure_local_image_store_bytes("example/image:tag", image_id)
    assert calls[-1][-2:] == ["rm", "c" * 64]
