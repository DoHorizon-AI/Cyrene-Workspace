"""Focused tests for the Linux Product SDK wheelhouse and bundle contracts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


prepare = _load_module(
    "cyrene_prepare_service_wheelhouse_test",
    WORKSPACE_ROOT / "packaging" / "prepare_service_wheelhouse.py",
)
bundle = _load_module(
    "cyrene_service_bundle_test",
    WORKSPACE_ROOT / "packaging" / "service_bundle.py",
)


def _wheel(path: Path, name: str, version: str) -> tuple[Path, str]:
    normalized = name.replace("-", "_").replace(".", "_")
    wheel_path = path / f"{normalized}-{version}-py3-none-any.whl"
    dist_info = f"{normalized}-{version}.dist-info"
    with zipfile.ZipFile(wheel_path, "w") as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n",
        )
        archive.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\n")
    return wheel_path, hashlib.sha256(wheel_path.read_bytes()).hexdigest()


def _sdk_record(wheel_sha: str) -> dict[str, str]:
    return {
        "component_id": "cyrene-runtime-maintenance-sdk",
        "manifest_digest": "sha256:" + "1" * 64,
        "artifact_digest": "sha256:" + "2" * 64,
        "distribution": "cyrene-runtime-maintenance",
        "version": "0.1.0",
        "wheel_sha256": wheel_sha,
    }


def _requirements(wheels: list[Path]) -> str:
    records = []
    for path in wheels:
        name, version = prepare._wheel_metadata(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        records.append(prepare._format_hashed_requirement(f"{name}=={version}", (digest,)))
    return "\n".join(records) + "\n"


def test_runtime_sdk_input_requires_exact_distribution_version_and_digest(tmp_path: Path) -> None:
    wheel, digest = _wheel(tmp_path, "cyrene-runtime-maintenance", "0.1.0")
    args = Namespace(
        runtime_sdk_wheel=wheel,
        runtime_sdk_version="0.1.0",
        runtime_sdk_sha256=digest,
        runtime_sdk_manifest_digest="sha256:" + "1" * 64,
        runtime_sdk_artifact_digest="sha256:" + "2" * 64,
    )

    artifact, manifest_digest, archive_digest = prepare._load_runtime_sdk_wheel(args)

    assert artifact.name == "cyrene-runtime-maintenance"
    assert artifact.version == "0.1.0"
    assert artifact.sha256 == digest
    assert manifest_digest == args.runtime_sdk_manifest_digest
    assert archive_digest == args.runtime_sdk_artifact_digest

    args.runtime_sdk_sha256 = "0" * 64
    with pytest.raises(prepare.ProducerError, match="differs from the verified wheel digest"):
        prepare._load_runtime_sdk_wheel(args)


def test_runtime_sdk_input_rejects_symlink_and_wrong_distribution(tmp_path: Path) -> None:
    other_wheel, other_digest = _wheel(tmp_path, "another-runtime", "0.1.0")
    args = Namespace(
        runtime_sdk_wheel=other_wheel,
        runtime_sdk_version="0.1.0",
        runtime_sdk_sha256=other_digest,
        runtime_sdk_manifest_digest="sha256:" + "1" * 64,
        runtime_sdk_artifact_digest="sha256:" + "2" * 64,
    )
    with pytest.raises(prepare.ProducerError, match="distribution must be"):
        prepare._load_runtime_sdk_wheel(args)

    link = tmp_path / "sdk-link.whl"
    link.symlink_to(other_wheel)
    args.runtime_sdk_wheel = link
    with pytest.raises(prepare.ProducerError, match="regular non-symlink"):
        prepare._load_runtime_sdk_wheel(args)


def test_single_service_wheelhouse_carries_exact_sdk_dependency_provenance(tmp_path: Path) -> None:
    service_dir = tmp_path / "navigator"
    service_dir.mkdir()
    app_wheel, _ = _wheel(service_dir, "cyrene-navigator", "1.2.3")
    sdk_wheel, sdk_sha = _wheel(service_dir, "cyrene-runtime-maintenance", "0.1.0")
    lock = service_dir / "requirements.lock"
    lock.write_text(_requirements([app_wheel, sdk_wheel]), encoding="utf-8")
    source = {
        "schema_version": 2,
        "service": "navigator",
        "source_repository": "Cyrene-Navigator",
        "source_commit": "a" * 40,
        "requirements_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "runtime_dependencies": [_sdk_record(sdk_sha)],
    }
    (service_dir / "source.json").write_text(json.dumps(source), encoding="utf-8")
    (service_dir / "serve-web.py").write_text("# pinned launcher\n", encoding="utf-8")

    prepare._verify_wheelhouse(
        service_dir,
        prepare.SERVICE_SPECS[0],
        "a" * 40,
    )

    source["runtime_dependencies"][0]["wheel_sha256"] = "0" * 64
    (service_dir / "source.json").write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(prepare.ProducerError, match="exactly pinned runtime SDK wheel"):
        prepare._verify_wheelhouse(service_dir, prepare.SERVICE_SPECS[0], "a" * 40)


def test_service_bundle_build_reads_sdk_wheel_metadata_and_packages_exact_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    service_dir = wheelhouse / "navigator"
    service_dir.mkdir(parents=True)
    app_wheel, _ = _wheel(service_dir, "cyrene-navigator", "1.2.3")
    sdk_wheel, _ = _wheel(service_dir, "cyrene-runtime-maintenance", "0.1.0")
    with zipfile.ZipFile(sdk_wheel, "a") as archive:
        archive.writestr("cyrene_runtime_maintenance/__init__.py", "SDK_MARKER = True\n")
    sdk_sha = hashlib.sha256(sdk_wheel.read_bytes()).hexdigest()
    lock = service_dir / "requirements.lock"
    lock.write_text(_requirements([app_wheel, sdk_wheel]), encoding="utf-8")
    source = {
        "schema_version": 2,
        "service": "navigator",
        "source_repository": "Cyrene-Navigator",
        "source_commit": "a" * 40,
        "requirements_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "runtime_dependencies": [_sdk_record(sdk_sha)],
    }
    (service_dir / "source.json").write_text(json.dumps(source), encoding="utf-8")
    (service_dir / "serve-web.py").write_text(
        'parser.add_argument("--pairing-code-file")\ncontract = "pairingCodeFile"\n',
        encoding="utf-8",
    )
    build_calls: list[list[str]] = []

    def offline_install(arguments: list[str], **kwargs: object) -> None:
        build_calls.append(arguments)
        if "--target" not in arguments:
            return
        target = Path(arguments[arguments.index("--target") + 1])
        target.mkdir(parents=True, exist_ok=True)
        for wheel in service_dir.glob("*.whl"):
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(target)

    monkeypatch.setattr(bundle.subprocess, "run", offline_install)

    result = bundle._build_one_bundle(
        service="navigator",
        wheelhouse=wheelhouse,
        output_root=tmp_path / "releases",
        source_commit="a" * 40,
        source_repository="Cyrene-Navigator",
        debian_arch="amd64",
        python_executable=tmp_path / "python3.12",
        builder_python=tmp_path / "builder-python",
    )

    assert len(build_calls) == 2
    assert (result / "python" / "cyrene_runtime_maintenance" / "__init__.py").read_text(
        encoding="utf-8"
    ) == "SDK_MARKER = True\n"
    manifest = bundle.validate_bundle(result, expected_service="navigator")
    assert manifest["dependencies"]["runtime"] == source["runtime_dependencies"]


@pytest.mark.parametrize(
    "metadata",
    [
        "Metadata-Version: 2.1\nName: cyrene-runtime-maintenance\n\n",
        "Metadata-Version: 2.1\nName: cyrene-runtime-maintenance\nVersion: 0.1.0\nVersion: 0.1.1\n\n",
    ],
)
def test_service_bundle_wheel_metadata_rejects_missing_or_duplicate_fields(
    tmp_path: Path, metadata: str
) -> None:
    wheel = tmp_path / "invalid.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("cyrene_runtime_maintenance-0.1.0.dist-info/METADATA", metadata)

    with pytest.raises(bundle.ServiceBundleError, match="one valid Name and Version"):
        bundle._wheel_metadata(wheel)


def test_inner_service_manifest_binds_sdk_to_lock_and_file_hashes(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    (root / "python").mkdir(parents=True)
    (root / "python" / "cyrene_runtime_maintenance.py").write_text("SDK\n", encoding="utf-8")
    entrypoint = root / "run-service"
    entrypoint.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    entrypoint.chmod(0o755)
    sdk_sha = "a" * 64
    runtime = [_sdk_record(sdk_sha)]
    lock = root / "requirements.lock"
    lock.write_text(
        "cyrene-runtime-maintenance==0.1.0 " + "\\\n" + f"    --hash=sha256:{sdk_sha}\n",
        encoding="utf-8",
    )
    source = {
        "schema_version": 2,
        "service": "navigator",
        "source_repository": "Cyrene-Navigator",
        "source_commit": "b" * 40,
        "requirements_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "runtime_dependencies": runtime,
    }
    (root / "source.json").write_text(json.dumps(source, sort_keys=True), encoding="utf-8")
    manifest = bundle._write_bundle_manifest(
        root,
        service="navigator",
        source_commit="b" * 40,
        source_repository="Cyrene-Navigator",
        debian_arch="amd64",
        python_version="3.12",
        runtime_dependencies=runtime,
    )

    validated = bundle.validate_bundle(root, expected_service="navigator")

    assert validated["artifact_digest"] == manifest["artifact_digest"]
    assert validated["schema_version"] == 2
    assert validated["dependencies"]["runtime"] == runtime

    lock.write_text(
        "cyrene-runtime-maintenance==0.1.0 " + "\\\n" + f"    --hash=sha256:{'0' * 64}\n",
        encoding="utf-8",
    )
    source["requirements_lock_sha256"] = hashlib.sha256(lock.read_bytes()).hexdigest()
    source_path = root / "source.json"
    source_path.write_text(json.dumps(source, sort_keys=True), encoding="utf-8")
    manifest["files"]["requirements.lock"] = hashlib.sha256(lock.read_bytes()).hexdigest()
    manifest["files"]["source.json"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    manifest["dependencies"]["lock_sha256"] = hashlib.sha256(lock.read_bytes()).hexdigest()
    manifest["version"] = ""
    manifest["artifact_digest"] = ""
    digest = bundle._artifact_digest(manifest)
    manifest["version"] = digest
    manifest["artifact_digest"] = digest
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(bundle.ServiceBundleError, match="runtime SDK wheel"):
        bundle.validate_bundle(root, expected_service="navigator")


def test_legacy_v1_bundle_remains_readable_without_sdk_provenance(tmp_path: Path) -> None:
    root = tmp_path / "legacy-v1"
    root.mkdir()
    entrypoint = root / "run-service"
    entrypoint.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    entrypoint.chmod(0o755)
    lock = root / "requirements.lock"
    lock.write_text("cyrene-navigator==1.2.3 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
    source = {
        "schema_version": 1,
        "service": "navigator",
        "source_repository": "Cyrene-Navigator",
        "source_commit": "c" * 40,
        "requirements_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
    }
    (root / "source.json").write_text(json.dumps(source, sort_keys=True), encoding="utf-8")
    files = {
        "requirements.lock": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "run-service": hashlib.sha256(entrypoint.read_bytes()).hexdigest(),
        "source.json": hashlib.sha256((root / "source.json").read_bytes()).hexdigest(),
    }
    manifest = {
        "schema_version": 1,
        "service": "navigator",
        "version": "",
        "entrypoint": "run-service",
        "health": {"path": bundle.HEALTH_PATHS["navigator"]},
        "files": files,
        "source_repository": "Cyrene-Navigator",
        "source_commit": "c" * 40,
        "dependencies": {
            "lock_file": "requirements.lock",
            "lock_sha256": files["requirements.lock"],
        },
        "target": {"debian_arch": "amd64", "python": "3.12"},
        "artifact_digest": "",
    }
    digest = bundle._artifact_digest(manifest)
    manifest["version"] = digest
    manifest["artifact_digest"] = digest
    (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

    validated = bundle.validate_bundle(root, expected_service="navigator")

    assert validated["schema_version"] == 1
    assert "runtime" not in validated["dependencies"]
    with pytest.raises(bundle.ServiceBundleError, match="only schema v2 bundles may be staged"):
        bundle.stage_release(root, install_root=tmp_path / "install")
