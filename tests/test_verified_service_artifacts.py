"""Focused tests for verifying and staging immutable Product release bytes."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import tarfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]


def _module() -> ModuleType:
    path = WORKSPACE_ROOT / "packaging" / "verified_service_artifacts.py"
    spec = importlib.util.spec_from_file_location("cyrene_verified_service_artifacts_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_product_release_metadata_requires_immutable_assets_and_exact_bytes() -> None:
    module = _module()
    release_id = "stable-" + "a" * 40
    metadata = {
        "tag_name": release_id,
        "immutable": True,
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": "bundle.tar.gz",
                "digest": "sha256:" + "b" * 64,
                "size": 17,
                "state": "uploaded",
            }
        ],
    }

    module._verify_release_assets(metadata, release_id, "stable", [("bundle.tar.gz", "b" * 64, 17)])
    for field, value in (("immutable", False), ("draft", True)):
        changed = dict(metadata)
        changed[field] = value
        with pytest.raises(module.VerifiedServiceArtifactError):
            module._verify_release_assets(
                changed, release_id, "stable", [("bundle.tar.gz", "b" * 64, 17)]
            )

    changed = json.loads(json.dumps(metadata))
    changed["assets"][0]["digest"] = "sha256:" + "c" * 64
    with pytest.raises(module.VerifiedServiceArtifactError, match="does not match local bytes"):
        module._verify_release_assets(
            changed, release_id, "stable", [("bundle.tar.gz", "b" * 64, 17)]
        )


@pytest.mark.parametrize(
    ("target_profile", "target_fields", "expected_archive", "expected_manifest"),
    [
        (
            "linux-ubuntu-22.04-x86_64-python-3.12",
            {
                "os": "linux",
                "osVersion": "22.04",
                "distribution": "ubuntu",
                "distributionVersion": "22.04",
                "architecture": "x86_64",
                "abi": "glibc-2.35",
                "runtime": "python:3.12",
            },
            "cyrene-catalyst-linux-ubuntu-22.04-x86_64-python-3.12.tar.gz",
            "cyrene-catalyst-linux-ubuntu-22-04-x86-64-glibc-2-35-python-3-12.manifest.json",
        ),
        (
            "linux-ubuntu-24.04-x86_64-python-3.12",
            {
                "os": "linux",
                "osVersion": "24.04",
                "distribution": "ubuntu",
                "distributionVersion": "24.04",
                "architecture": "x86_64",
                "abi": "glibc-2.39",
                "runtime": "python:3.12",
            },
            "cyrene-catalyst-linux-ubuntu-24.04-x86_64-python-3.12.tar.gz",
            "cyrene-catalyst-linux-ubuntu-24-04-x86-64-glibc-2-39-python-3-12.manifest.json",
        ),
    ],
)
def test_product_asset_names_follow_catalog_target_profile(
    target_profile: str,
    target_fields: dict[str, str],
    expected_archive: str,
    expected_manifest: str,
) -> None:
    module = _module()

    assert module._canonical_product_asset_names(
        "cyrene-catalyst", target_profile, target_fields
    ) == (
        expected_archive,
        expected_manifest,
    )

    changed_target = dict(target_fields, abi="glibc-2.40")
    with pytest.raises(
        module.VerifiedServiceArtifactError, match="canonical Product naming profile"
    ):
        module._canonical_product_asset_names("cyrene-catalyst", target_profile, changed_target)


def _native_outer_manifest(target_profile: str, schema_version: object = 1) -> dict[str, object]:
    module = _module()
    return {
        "schemaVersion": schema_version,
        "releaseId": "preview-" + "6f999daacf8a2afd76033bfc5d0d7bcf68202de1",
        "componentId": "cyrene-catalyst",
        "version": "27b4303983bb4abc9abb924806d4c91bcfd431aae91eeb3fc1dd162682e9aa25",
        "channel": "preview",
        "target": module.PRODUCT_TARGETS[target_profile].copy(),
        "source": {
            "repository": "https://github.com/DoHorizon-AI/Cyrene-Catalyst",
            "ref": "refs/heads/develop",
            "commit": "6f999daacf8a2afd76033bfc5d0d7bcf68202de1",
        },
    }


@pytest.mark.parametrize(
    "target_profile",
    [
        "linux-ubuntu-22.04-x86_64-python-3.12",
        "linux-ubuntu-24.04-x86_64-python-3.12",
    ],
)
def test_outer_manifest_accepts_signed_native_v1_profile_identity(target_profile: str) -> None:
    module = _module()
    manifest = _native_outer_manifest(target_profile)

    module._verify_outer_manifest_identity(
        manifest,
        component_id="cyrene-catalyst",
        release_id="preview-" + "6f999daacf8a2afd76033bfc5d0d7bcf68202de1",
        channel="preview",
        target_fields=module.PRODUCT_TARGETS[target_profile],
        source_repository="https://github.com/DoHorizon-AI/Cyrene-Catalyst",
        source_ref="refs/heads/develop",
        source_commit="6f999daacf8a2afd76033bfc5d0d7bcf68202de1",
    )


@pytest.mark.parametrize("schema_version", [1, 2])
def test_outer_manifest_accepts_only_supported_integer_schema_versions(
    schema_version: int,
) -> None:
    module = _module()
    target_profile = "linux-ubuntu-24.04-x86_64-python-3.12"
    manifest = _native_outer_manifest(target_profile, schema_version)

    module._verify_outer_manifest_identity(
        manifest,
        component_id="cyrene-catalyst",
        release_id="preview-" + "6f999daacf8a2afd76033bfc5d0d7bcf68202de1",
        channel="preview",
        target_fields=module.PRODUCT_TARGETS[target_profile],
        source_repository="https://github.com/DoHorizon-AI/Cyrene-Catalyst",
        source_ref="refs/heads/develop",
        source_commit="6f999daacf8a2afd76033bfc5d0d7bcf68202de1",
    )


@pytest.mark.parametrize("schema_version", [3, "1", True, None])
def test_outer_manifest_rejects_unknown_or_non_integer_schema_versions(
    schema_version: object,
) -> None:
    module = _module()
    target_profile = "linux-ubuntu-24.04-x86_64-python-3.12"
    manifest = _native_outer_manifest(target_profile, schema_version)

    with pytest.raises(module.VerifiedServiceArtifactError, match="identity or target differs"):
        module._verify_outer_manifest_identity(
            manifest,
            component_id="cyrene-catalyst",
            release_id="preview-" + "6f999daacf8a2afd76033bfc5d0d7bcf68202de1",
            channel="preview",
            target_fields=module.PRODUCT_TARGETS[target_profile],
            source_repository="https://github.com/DoHorizon-AI/Cyrene-Catalyst",
            source_ref="refs/heads/develop",
            source_commit="6f999daacf8a2afd76033bfc5d0d7bcf68202de1",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("os", "windows"),
        ("osVersion", "20.04"),
        ("distribution", "debian"),
        ("distributionVersion", "22.04"),
        ("architecture", "aarch64"),
        ("abi", "glibc-2.31"),
        ("runtime", "python:3.11"),
    ],
)
def test_outer_manifest_rejects_target_tuple_changes(field: str, value: str) -> None:
    module = _module()
    target_profile = "linux-ubuntu-24.04-x86_64-python-3.12"
    manifest = _native_outer_manifest(target_profile)
    manifest["target"][field] = value  # type: ignore[index]

    with pytest.raises(module.VerifiedServiceArtifactError, match="identity or target differs"):
        module._verify_outer_manifest_identity(
            manifest,
            component_id="cyrene-catalyst",
            release_id="preview-" + "6f999daacf8a2afd76033bfc5d0d7bcf68202de1",
            channel="preview",
            target_fields=module.PRODUCT_TARGETS[target_profile],
            source_repository="https://github.com/DoHorizon-AI/Cyrene-Catalyst",
            source_ref="refs/heads/develop",
            source_commit="6f999daacf8a2afd76033bfc5d0d7bcf68202de1",
        )


@pytest.mark.parametrize(
    ("identity_field", "expected", "error_match"),
    [
        ("componentId", "cyrene-exchange", "identity or target differs"),
        ("releaseId", "preview-" + "0" * 40, "identity or target differs"),
        ("channel", "stable", "identity or target differs"),
        ("repository", "https://github.com/example/other", "source differs"),
        ("ref", "refs/heads/main", "source differs"),
        ("commit", "0" * 40, "source differs"),
    ],
)
def test_outer_manifest_rejects_component_and_source_identity_changes(
    identity_field: str, expected: str, error_match: str
) -> None:
    module = _module()
    target_profile = "linux-ubuntu-24.04-x86_64-python-3.12"
    manifest = _native_outer_manifest(target_profile)
    if identity_field in {"repository", "ref", "commit"}:
        manifest["source"][identity_field] = expected  # type: ignore[index]
    else:
        manifest[identity_field] = expected

    with pytest.raises(module.VerifiedServiceArtifactError, match=error_match):
        module._verify_outer_manifest_identity(
            manifest,
            component_id="cyrene-catalyst",
            release_id="preview-" + "6f999daacf8a2afd76033bfc5d0d7bcf68202de1",
            channel="preview",
            target_fields=module.PRODUCT_TARGETS[target_profile],
            source_repository="https://github.com/DoHorizon-AI/Cyrene-Catalyst",
            source_ref="refs/heads/develop",
            source_commit="6f999daacf8a2afd76033bfc5d0d7bcf68202de1",
        )


def test_outer_manifest_digest_must_match_index_and_manifest() -> None:
    module = _module()
    manifest = _native_outer_manifest("linux-ubuntu-24.04-x86_64-python-3.12")
    manifest["manifestDigest"] = module._manifest_digest(manifest)
    manifest_record = {"manifestDigest": manifest["manifestDigest"]}

    module._verify_outer_manifest_digest(manifest_record, manifest)

    manifest_record["manifestDigest"] = "sha256:" + "0" * 64
    with pytest.raises(module.VerifiedServiceArtifactError, match="manifest digest is invalid"):
        module._verify_outer_manifest_digest(manifest_record, manifest)
    manifest_record["manifestDigest"] = manifest["manifestDigest"]
    manifest["manifestDigest"] = "sha256:" + "0" * 64
    with pytest.raises(module.VerifiedServiceArtifactError, match="manifest digest is invalid"):
        module._verify_outer_manifest_digest(manifest_record, manifest)


def _write_staging_inputs(
    module: ModuleType, root: Path, target_profile: str
) -> tuple[Path, Path, dict[str, dict[str, object]], dict[str, str]]:
    """Build small immutable-shaped inputs for a complete five-service stage."""
    input_root = root / "inputs"
    input_root.mkdir()
    target_fields = module.PRODUCT_TARGETS[target_profile]
    commit_by_service = {
        service: str(index) * 40 for index, service in enumerate(module.SERVICES, start=1)
    }
    catalog_components = []
    publishers = []
    records: dict[str, dict[str, object]] = {}
    release_assets: dict[str, dict[str, object]] = {}

    for component_id, service in sorted(module.SERVICE_COMPONENTS.items()):
        repository_name = module.SERVICE_REPOSITORIES[service]
        repository = f"DoHorizon-AI/{repository_name}"
        workflow = f"{repository}/.github/workflows/component-release.yml"
        release_assets[repository] = {}
        catalog_components.append(
            {
                "componentId": component_id,
                "publisher": repository,
                "kind": "python-bundle",
                "pythonBundleService": service,
                "targets": [
                    {
                        "targetId": target_profile,
                        "artifactKind": "python-bundle",
                        "support": "supported",
                    }
                ],
            }
        )
        publishers.append({"repository": repository, "workflow": workflow})

        source_commit = commit_by_service[service]
        release_id = f"preview-{source_commit}"
        archive_name, manifest_name = module._canonical_product_asset_names(
            component_id, target_profile, target_fields
        )
        archive_path = input_root / archive_name
        inner_manifest_bytes = b"{}"
        with tarfile.open(archive_path, "w:gz") as archive:
            member = tarfile.TarInfo("manifest.json")
            member.mode = 0o644
            member.size = len(inner_manifest_bytes)
            archive.addfile(member, io.BytesIO(inner_manifest_bytes))
        artifact_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        artifact_size = archive_path.stat().st_size
        inner_manifest_sha = hashlib.sha256(inner_manifest_bytes).hexdigest()
        attestation_name = archive_name + ".attestation.jsonl"
        attestation_path = input_root / attestation_name
        attestation_path.write_bytes(b"attestation fixture")
        attestation_sha = hashlib.sha256(attestation_path.read_bytes()).hexdigest()
        artifact_uri = (
            f"https://github.com/{repository}/releases/download/{release_id}/{archive_name}"
        )
        outer_manifest: dict[str, object] = {
            "schemaVersion": 1,
            "releaseId": release_id,
            "componentId": component_id,
            "version": "1.0.0",
            "channel": "preview",
            "target": target_fields,
            "artifact": {
                "kind": "python-bundle",
                "format": "tar.gz",
                "sha256": f"sha256:{artifact_sha}",
                "sizeBytes": artifact_size,
                "uri": artifact_uri,
                "files": {"manifest.json": f"sha256:{inner_manifest_sha}"},
            },
            "dependencies": [],
            "restart": {},
            "source": {
                "repository": f"https://github.com/{repository}",
                "ref": "refs/heads/develop",
                "commit": source_commit,
            },
            "provenance": {
                "attestation": {
                    "kind": "github-artifact-attestation",
                    "repository": repository,
                    "workflow": workflow,
                    "predicateType": "https://slsa.dev/provenance/v1",
                }
            },
        }
        outer_manifest["manifestDigest"] = module._manifest_digest(outer_manifest)
        manifest_bytes = json.dumps(outer_manifest, separators=(",", ":")).encode()
        (input_root / manifest_name).write_bytes(manifest_bytes)
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        records[component_id] = {
            "componentId": component_id,
            "repository": repository,
            "releaseId": release_id,
            "source": {"ref": "refs/heads/develop", "commit": source_commit},
            "artifact": {
                "path": archive_name,
                "sha256": artifact_sha,
                "sizeBytes": artifact_size,
                "kind": "python-bundle",
                "format": "tar.gz",
            },
            "manifest": {
                "path": manifest_name,
                "sha256": manifest_sha,
                "manifestDigest": outer_manifest["manifestDigest"],
            },
            "attestation": {
                "path": attestation_name,
                "sha256": attestation_sha,
                "repository": repository,
                "workflow": workflow,
                "predicateType": "https://slsa.dev/provenance/v1",
                "subjectName": archive_name,
                "sourceRef": "refs/heads/develop",
                "sourceCommit": source_commit,
            },
        }
        release_assets[repository][release_id] = [
            (archive_name, artifact_sha, artifact_size),
            (manifest_name, manifest_sha, len(manifest_bytes)),
            (attestation_name, attestation_sha, attestation_path.stat().st_size),
        ]

    catalog_path = root / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "components": catalog_components,
                "publishers": publishers,
                "targets": [
                    {
                        "id": target_profile,
                        "hostSupport": "supported",
                        "target": target_fields,
                    }
                ],
                "channels": {"preview": {"sourceRefs": ["refs/heads/develop"]}},
            }
        ),
        encoding="utf-8",
    )
    index = {"schemaVersion": 1, "targetProfile": target_profile, "services": records}
    (input_root / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return input_root, catalog_path, release_assets, commit_by_service


@pytest.mark.parametrize(
    "target_profile",
    [
        "linux-ubuntu-22.04-x86_64-python-3.12",
        "linux-ubuntu-24.04-x86_64-python-3.12",
    ],
)
def test_verify_and_stage_separates_extraction_scratch_from_final_service_trees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target_profile: str
) -> None:
    module = _module()
    input_root, catalog_path, release_assets, commit_by_service = _write_staging_inputs(
        module, tmp_path, target_profile
    )
    lock_path = tmp_path / "release-lock.json"
    lock_path.write_text("{}", encoding="utf-8")
    output_root = tmp_path / "stage"
    events: list[str] = []

    def read_release(repository: str, release_id: str) -> dict[str, object]:
        events.append(f"release:{repository}")
        assets = [
            {"name": name, "digest": f"sha256:{digest}", "size": size, "state": "uploaded"}
            for name, digest, size in release_assets[repository][release_id]
        ]
        return {
            "tag_name": release_id,
            "immutable": True,
            "draft": False,
            "prerelease": True,
            "assets": assets,
        }

    def validate_bundle(
        bundle_root: Path,
        *,
        expected_service: str,
        expected_target_profile: str,
        release_lock_path: Path,
    ) -> dict[str, object]:
        events.append(f"bundle:{expected_service}")
        assert expected_target_profile == target_profile
        assert release_lock_path == lock_path
        assert (bundle_root / "manifest.json").read_bytes() == b"{}"
        return {
            "schema_version": 2,
            "service": expected_service,
            "source_commit": commit_by_service[expected_service],
            "version": "fixture-1.0.0",
        }

    def verify_attestation(*args: object, **kwargs: object) -> None:
        events.append(f"attestation:{kwargs['repository']}")

    monkeypatch.setattr(
        module,
        "_load_profile",
        lambda _path, _profile: {
            "repositories": {
                module.SERVICE_REPOSITORIES[service]: commit
                for service, commit in commit_by_service.items()
            }
        },
    )
    monkeypatch.setattr(
        module,
        "_load_service_bundle_module",
        lambda: SimpleNamespace(validate_bundle=validate_bundle),
    )

    result = module.verify_and_stage(
        input_root=input_root,
        target_profile=target_profile,
        output_root=output_root,
        release_lock_path=lock_path,
        catalog_path=catalog_path,
        release_reader=read_release,
        attestation_verifier=verify_attestation,
    )

    assert set(result["services"]) == set(module.SERVICE_COMPONENTS)
    assert len([event for event in events if event.startswith("release:")]) == 5
    assert len([event for event in events if event.startswith("attestation:")]) == 5
    assert len([event for event in events if event.startswith("bundle:")]) == 5
    assert not (output_root / ".verified-extracted").exists()
    for service in module.SERVICES:
        staged_bundle = output_root / "service-artifacts" / service / "fixture-1.0.0"
        assert (staged_bundle / "manifest.json").read_bytes() == b"{}"


def test_verified_asset_input_rejects_symlinks_and_path_traversal(tmp_path: Path) -> None:
    module = _module()
    root = tmp_path / "input"
    root.mkdir()
    target = tmp_path / "outside"
    target.write_text("bytes", encoding="utf-8")
    (root / "bundle.tar.gz").symlink_to(target)

    with pytest.raises(module.VerifiedServiceArtifactError, match="missing or unsafe"):
        module._regular_input(root, "bundle.tar.gz", "artifact")
    with pytest.raises(module.VerifiedServiceArtifactError, match="normalized relative path"):
        module._regular_input(root, "../outside", "artifact")


def test_product_archive_extraction_accepts_exact_files_and_rejects_links(tmp_path: Path) -> None:
    module = _module()
    archive_path = tmp_path / "bundle.tar.gz"
    content = b"verified payload"
    manifest_bytes = b"{}\n"
    with tarfile.open(archive_path, "w:gz") as archive:
        for name, data, mode in (
            ("run-service", content, 0o755),
            ("manifest.json", manifest_bytes, 0o644),
        ):
            member = tarfile.TarInfo(name)
            member.mode = mode
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))

    extracted = module._extract_bundle_archive(
        archive_path,
        tmp_path / "extracted",
        {
            "run-service": "sha256:" + hashlib.sha256(content).hexdigest(),
            "manifest.json": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
        },
    )
    assert (extracted / "run-service").read_bytes() == content

    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo("run-service")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"
        archive.addfile(member)
    with pytest.raises(module.VerifiedServiceArtifactError, match="link or special"):
        module._extract_bundle_archive(archive_path, tmp_path / "unsafe", {})


def test_attestation_verifier_binds_exact_archive_subject(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module()
    artifact = tmp_path / "bundle.tar.gz"
    bundle = tmp_path / "bundle.tar.gz.attestation.jsonl"
    artifact.write_bytes(b"artifact")
    bundle.write_bytes(b"detached bundle")
    subject_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    monkeypatch.setattr(module.shutil, "which", lambda executable: "/usr/bin/gh")
    seen: list[str] = []

    def verified_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.extend(command)
        statement = {
            "predicateType": "https://slsa.dev/provenance/v1",
            "subject": [{"name": artifact.name, "digest": {"sha256": subject_sha}}],
        }
        return subprocess.CompletedProcess(
            command, 0, json.dumps([{"verificationResult": {"statement": statement}}]), ""
        )

    module._verify_attestation(
        artifact,
        bundle,
        repository="DoHorizon-AI/Cyrene-Navigator",
        workflow=".github/workflows/component-release.yml",
        source_ref="refs/heads/main",
        source_commit="a" * 40,
        subject_name=artifact.name,
        sha256=subject_sha,
        runner=verified_runner,
    )
    assert "attestation" in seen
    assert "verify" in seen
    assert "--source-digest" in seen
    assert "--signer-workflow" in seen
    assert "accepted:true" not in seen

    def wrong_subject_runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        statement = {
            "predicateType": "https://slsa.dev/provenance/v1",
            "subject": [{"name": artifact.name, "digest": {"sha256": "0" * 64}}],
        }
        return subprocess.CompletedProcess(
            command, 0, json.dumps([{"verificationResult": {"statement": statement}}]), ""
        )

    with pytest.raises(
        module.VerifiedServiceArtifactError, match="does not bind the exact Product archive"
    ):
        module._verify_attestation(
            artifact,
            bundle,
            repository="DoHorizon-AI/Cyrene-Navigator",
            workflow=".github/workflows/component-release.yml",
            source_ref="refs/heads/main",
            source_commit="a" * 40,
            subject_name=artifact.name,
            sha256=subject_sha,
            runner=wrong_subject_runner,
        )
