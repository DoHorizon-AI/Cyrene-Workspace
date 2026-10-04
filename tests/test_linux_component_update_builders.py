"""Focused tests for the Linux Product SDK wheelhouse and bundle contracts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import zipfile
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

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
UBUNTU_24_PROFILE_ID = "linux-ubuntu-24.04-x86_64-python-3.12"
UBUNTU_24_PROFILE = bundle._native_python_profile(
    WORKSPACE_ROOT / "release-lock.json", UBUNTU_24_PROFILE_ID
)
UBUNTU_22_PROFILE_ID = "linux-ubuntu-22.04-x86_64-python-3.12"
UBUNTU_22_PROFILE = bundle._native_python_profile(
    WORKSPACE_ROOT / "release-lock.json", UBUNTU_22_PROFILE_ID
)


def _wheel(path: Path, name: str, version: str, tag: str = "py3-none-any") -> tuple[Path, str]:
    normalized = name.replace("-", "_").replace(".", "_")
    wheel_path = path / f"{normalized}-{version}-{tag}.whl"
    dist_info = f"{normalized}-{version}.dist-info"
    with zipfile.ZipFile(wheel_path, "w") as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            f"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: {tag}\n",
        )
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


def _execution_runtime_descriptor() -> dict[str, object]:
    return {
        "schema_version": 1,
        "runtime_id": "yield-trainer",
        "source_root": "execution-runtime",
        "project_file": "trainer-runtime/pyproject.toml",
        "lock_file": "trainer-runtime/uv.lock",
        "bootstrap_script": "trainer-runtime/bootstrap.py",
        "probe_script": "trainer-runtime/probe.py",
        "protocol_files": sorted(
            [
                "training/core/src/cy_exec/training/executors/kernel.desc",
                "training/core/src/cy_exec/training/executors/kernel-descriptor.json",
                "training/core/src/cy_exec/training/executors/training_worker.py",
            ]
        ),
        "profile": "CYRENE_YIELD_TRAINER_V1_CUDA128",
        "platform_profile": "CYRENE_PLATFORM_RUNTIME_V1_LOCAL_GPU",
        "python_executable": bundle.PRIVATE_PYTHON_EXECUTABLE,
        "python_version": "3.12.14",
        "uv_executable": bundle.PRIVATE_UV_EXECUTABLE,
        "uv_version": bundle.PRIVATE_UV_VERSION,
        "runtime_home_relative_path": "yield/trainer-runtime",
        "runtime_manifest_file": "runtime.json",
        "runtime_arguments": [
            "--runtime-config",
            "{platform_runtime_config}",
            "--trainer-runtime-config",
            "{trainer_runtime_config}",
        ],
        "api_only_arguments": ["--artifact-root", "{artifact_root}"],
    }


def _write_product_execution_runtime(repo_root: Path) -> dict[str, object]:
    descriptor = _execution_runtime_descriptor()
    declaration = repo_root / "execution-runtime.json"
    declaration.write_text(json.dumps(descriptor, indent=2) + "\n", encoding="utf-8")
    project = repo_root / "trainer-runtime" / "pyproject.toml"
    project.parent.mkdir(parents=True)
    project.write_text(
        '[project]\nname = "cyrene-yield-trainer-runtime"\nrequires-python = ">=3.12,<3.13"\n',
        encoding="utf-8",
    )
    (project.parent / "uv.lock").write_text(
        'version = 1\nrequires-python = ">=3.12,<3.13"\n', encoding="utf-8"
    )
    for filename in ("bootstrap.py", "probe.py"):
        (project.parent / filename).write_text("# locked runtime input\n", encoding="utf-8")
    for relative in descriptor["protocol_files"]:
        path = repo_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# protocol input\n", encoding="utf-8")
    return descriptor


@pytest.mark.parametrize(
    ("profile_id", "profile", "max_glibc", "higher_floor"),
    [
        (UBUNTU_22_PROFILE_ID, UBUNTU_22_PROFILE, "2.35", "manylinux_2_36_x86_64"),
        (UBUNTU_24_PROFILE_ID, UBUNTU_24_PROFILE, "2.39", "manylinux_2_40_x86_64"),
    ],
)
def test_profiles_apply_pep600_glibc_ceilings_and_accept_portable_wheels(
    profile_id: str, profile: dict[str, object], max_glibc: str, higher_floor: str
) -> None:
    lower_floor = "cp312-cp312-manylinux_2_17_x86_64"
    target_floor = f"cp312-cp312-manylinux_{max_glibc.replace('.', '_')}_x86_64"
    pure_python = "py3-none-any"

    assert (
        prepare._native_python_profile(
            json.loads((WORKSPACE_ROOT / "release-lock.json").read_text(encoding="utf-8")),
            profile_id,
        )
        == profile
    )
    prepare._validate_python_input(profile, WORKSPACE_ROOT)
    bundle._validate_python_input(profile, WORKSPACE_ROOT / "release-lock.json")
    assert prepare._wheel_tag_is_compatible(lower_floor, profile)
    assert bundle._wheel_tag_is_compatible(lower_floor, profile)
    assert prepare._wheel_tag_is_compatible(target_floor, profile)
    assert bundle._wheel_tag_is_compatible(target_floor, profile)
    assert prepare._wheel_tag_is_compatible(pure_python, profile)
    assert bundle._wheel_tag_is_compatible(pure_python, profile)
    assert not prepare._wheel_tag_is_compatible(f"cp312-cp312-{higher_floor}", profile)
    assert not bundle._wheel_tag_is_compatible(f"cp312-cp312-{higher_floor}", profile)
    assert not prepare._wheel_tag_is_compatible("cp312-cp312-linux_x86_64", profile)
    assert not bundle._wheel_tag_is_compatible("cp312-cp312-linux_x86_64", profile)


def test_wheelhouse_checks_filename_and_wheel_metadata_tags(tmp_path: Path) -> None:
    valid_22, _ = _wheel(
        tmp_path, "cyrene-native-dependency", "1.0.0", "cp312-cp312-manylinux_2_35_x86_64"
    )
    too_new_22, _ = _wheel(tmp_path, "cyrene-too-new", "1.0.0", "cp312-cp312-manylinux_2_36_x86_64")
    mismatched = tmp_path / "cyrene-mismatched-1.0.0-py3-none-any.whl"
    mismatched.write_bytes(valid_22.read_bytes())

    assert prepare._verify_wheel_tags(valid_22, UBUNTU_22_PROFILE) == [
        "cp312-cp312-manylinux_2_35_x86_64"
    ]
    with pytest.raises(prepare.ProducerError, match="incompatible with target"):
        prepare._verify_wheel_tags(too_new_22, UBUNTU_22_PROFILE)
    with pytest.raises(prepare.ProducerError, match="filename tags differ from WHEEL metadata"):
        prepare._verify_wheel_tags(mismatched, UBUNTU_24_PROFILE)


@pytest.mark.parametrize(
    ("profile", "version", "abi"),
    [
        (UBUNTU_22_PROFILE, "22.04", "glibc-2.35"),
        (UBUNTU_24_PROFILE, "24.04", "glibc-2.39"),
    ],
)
def test_builders_require_the_exact_ubuntu_profile_host(
    monkeypatch: pytest.MonkeyPatch,
    profile: dict[str, object],
    version: str,
    abi: str,
) -> None:
    monkeypatch.setattr(
        bundle.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": version},
    )
    monkeypatch.setattr(bundle.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bundle.platform, "libc_ver", lambda: ("glibc", abi.removeprefix("glibc-")))
    monkeypatch.setattr(
        prepare.platform, "freedesktop_os_release", bundle.platform.freedesktop_os_release
    )
    monkeypatch.setattr(prepare.platform, "machine", bundle.platform.machine)
    monkeypatch.setattr(prepare.platform, "libc_ver", bundle.platform.libc_ver)

    bundle._require_profile_host(profile)
    assert prepare._native_target(profile) == "amd64"


@pytest.mark.parametrize(
    ("profile", "version", "abi"),
    [
        (UBUNTU_22_PROFILE, "24.04", "glibc-2.39"),
        (UBUNTU_24_PROFILE, "22.04", "glibc-2.35"),
    ],
)
def test_builders_reject_a_different_ubuntu_profile_host(
    monkeypatch: pytest.MonkeyPatch,
    profile: dict[str, object],
    version: str,
    abi: str,
) -> None:
    monkeypatch.setattr(
        bundle.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": version},
    )
    monkeypatch.setattr(bundle.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bundle.platform, "libc_ver", lambda: ("glibc", abi.removeprefix("glibc-")))

    with pytest.raises(bundle.ServiceBundleError, match="does not match the build host"):
        bundle._require_profile_host(profile)


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


@pytest.mark.parametrize(
    ("target_profile_id", "target_profile"),
    [
        (UBUNTU_22_PROFILE_ID, UBUNTU_22_PROFILE),
        (UBUNTU_24_PROFILE_ID, UBUNTU_24_PROFILE),
    ],
)
def test_product_execution_runtime_copies_only_declared_hashed_sources(
    tmp_path: Path,
    target_profile_id: str,
    target_profile: dict[str, object],
) -> None:
    product_repo = tmp_path / "product"
    product_repo.mkdir()
    descriptor = _write_product_execution_runtime(product_repo)
    (product_repo / "unlisted-secret.txt").write_text("do not bundle\n", encoding="utf-8")
    service_stage = tmp_path / "wheelhouse" / "yield"
    service_stage.mkdir(parents=True)

    copied = prepare._copy_execution_runtime_source(product_repo, service_stage, target_profile)

    assert copied == descriptor
    source_hashes = prepare._execution_runtime_source_hashes(service_stage)
    expected_files = {"execution-runtime/execution-runtime.json"}
    expected_files.update(
        "execution-runtime/" + relative
        for relative in (
            descriptor["project_file"],
            descriptor["lock_file"],
            descriptor["bootstrap_script"],
            descriptor["probe_script"],
            *descriptor["protocol_files"],
        )
    )
    assert set(source_hashes) == expected_files
    assert not (service_stage / "execution-runtime" / "unlisted-secret.txt").exists()
    assert (
        bundle._validate_execution_runtime(
            copied,
            root=service_stage,
            files=bundle._expected_file_map(service_stage),
            profile=target_profile,
        )
        == descriptor
    )


def test_product_execution_runtime_declaration_is_bound_to_copied_bytes(
    tmp_path: Path,
) -> None:
    product_repo = tmp_path / "product"
    product_repo.mkdir()
    descriptor = _write_product_execution_runtime(product_repo)
    service_stage = tmp_path / "wheelhouse" / "yield"
    service_stage.mkdir(parents=True)
    prepare._copy_execution_runtime_source(product_repo, service_stage, UBUNTU_24_PROFILE)
    declaration = service_stage / "execution-runtime" / "execution-runtime.json"
    altered = dict(descriptor, runtime_id="changed-runtime")
    declaration.write_text(json.dumps(altered), encoding="utf-8")

    with pytest.raises(bundle.ServiceBundleError, match="differs from the manifest descriptor"):
        bundle._validate_execution_runtime(
            descriptor,
            root=service_stage,
            files=bundle._expected_file_map(service_stage),
            profile=UBUNTU_24_PROFILE,
        )


def test_legacy_product_launchers_keep_their_existing_runtime_arguments() -> None:
    launchers = {
        service: bundle._entrypoint_text(service, bundle.PRIVATE_PYTHON_EXECUTABLE)
        for service in bundle.SERVICES
    }

    assert all("--runtime-config" not in launcher for launcher in launchers.values())
    assert all("--trainer-runtime-config" not in launcher for launcher in launchers.values())
    assert all("service-prepare" not in launcher for launcher in launchers.values())
    assert '"$BUNDLE_DIR/serve-web.py"' in launchers["navigator"]
    assert '--pairing-code-file "$PAIRING_CODE_FILE"' in launchers["navigator"]
    assert '--state-directory "${CYRENE_DATA_DIR:-/var/lib/cyrene}/yield"' in launchers["yield"]
    assert (
        f"{bundle.PRIVATE_PYTHON_EXECUTABLE} -s -m cyrene_reactor_product.cli control"
        in launchers["reactor"]
    )
    assert "cyrene_exchange_product.cli" in launchers["exchange"]
    assert "cyrene_catalyst.cli serve" in launchers["catalyst"]


@pytest.mark.parametrize("active_target", [None, "releases/" + "c" * 64])
def test_prepare_execution_runtime_is_versioned_idempotent_and_never_activates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_target: str | None,
) -> None:
    service_uid = os.geteuid()
    service_gid = os.getegid()
    monkeypatch.setattr(
        bundle.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(pw_uid=service_uid),
    )
    monkeypatch.setattr(
        bundle.grp,
        "getgrnam",
        lambda _name: SimpleNamespace(gr_gid=service_gid),
    )
    release_lock = WORKSPACE_ROOT / "release-lock.json"
    version = "b" * 64
    install_root = tmp_path / "install"
    service_root = install_root / "services" / "yield"
    releases = service_root / "releases"
    releases.mkdir(parents=True)
    active = service_root / "active"
    if active_target is not None:
        active.symlink_to(active_target)
    release = releases / version
    release.mkdir()
    product_repo = tmp_path / "product"
    product_repo.mkdir()
    descriptor = _write_product_execution_runtime(product_repo)
    prepare._copy_execution_runtime_source(product_repo, release, UBUNTU_24_PROFILE)
    manifest = {
        "version": version,
        "target": {"profile_id": UBUNTU_24_PROFILE_ID},
        "execution_runtime": descriptor,
    }
    monkeypatch.setattr(
        bundle, "_select_runtime_release", lambda *args, **kwargs: (release, manifest)
    )
    python_executable = tmp_path / "private-python"
    uv_executable = tmp_path / "private-uv"
    monkeypatch.setattr(
        bundle,
        "_verify_execution_runtime_tools",
        lambda *args, **kwargs: (python_executable, uv_executable),
    )
    data_root = tmp_path / "data"
    data_root.mkdir(mode=0o700)
    data_root.chmod(0o700)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def bootstrap(command: list[str], **options: object) -> SimpleNamespace:
        calls.append((command, options))
        runtime_home = Path(options["env"]["CYRENE_TRAINER_RUNTIME_HOME"])
        runtime_manifest = runtime_home / "runtime.json"
        runtime_manifest.write_text(
            json.dumps({"status": "READY", "profile": descriptor["profile"]}),
            encoding="utf-8",
        )
        runtime_manifest.chmod(0o600)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = bundle.prepare_execution_runtime(
        "yield",
        version=version,
        install_root=install_root,
        data_root=data_root,
        release_lock_path=release_lock,
        runtime_lock_path=tmp_path / "python-runtime.lock.json",
        runner=bootstrap,
        service_uid=service_uid,
        service_gid=service_gid,
    )

    assert result["status"] == "prepared"
    runtime_home = Path(result["runtimeHome"])
    assert runtime_home == data_root / "yield" / "trainer-runtime" / version
    assert not runtime_home.is_relative_to(releases)
    assert result["runtimeManifest"] == str(runtime_home / "runtime.json")
    command, options = calls[0]
    assert command[0] == str(python_executable)
    assert "--python-executable" in command
    assert descriptor["python_executable"] in command
    assert "--uv-executable" in command
    assert str(uv_executable) in command
    assert options["cwd"] == str(release / "execution-runtime")
    if os.geteuid() == 0:
        assert options["user"] == service_uid
        assert options["group"] == service_gid

    second_result = bundle.prepare_execution_runtime(
        "yield",
        version=version,
        install_root=install_root,
        data_root=data_root,
        release_lock_path=release_lock,
        runtime_lock_path=tmp_path / "python-runtime.lock.json",
        runner=lambda *_args, **_kwargs: pytest.fail("prepared runtime must not sync again"),
        service_uid=service_uid,
        service_gid=service_gid,
    )
    assert second_result["status"] == "already_prepared"
    if active_target is None:
        assert not active.exists()
    else:
        assert os.readlink(active) == active_target


def test_prepare_execution_runtime_refuses_partial_runtime_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service_uid = os.geteuid()
    service_gid = os.getegid()
    monkeypatch.setattr(
        bundle.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(pw_uid=service_uid),
    )
    monkeypatch.setattr(
        bundle.grp,
        "getgrnam",
        lambda _name: SimpleNamespace(gr_gid=service_gid),
    )
    version = "d" * 64
    release = tmp_path / "release"
    release.mkdir()
    product_repo = tmp_path / "product"
    product_repo.mkdir()
    descriptor = _write_product_execution_runtime(product_repo)
    prepare._copy_execution_runtime_source(product_repo, release, UBUNTU_24_PROFILE)
    manifest = {
        "version": version,
        "target": {"profile_id": UBUNTU_24_PROFILE_ID},
        "execution_runtime": descriptor,
    }
    monkeypatch.setattr(
        bundle, "_select_runtime_release", lambda *args, **kwargs: (release, manifest)
    )
    monkeypatch.setattr(
        bundle,
        "_verify_execution_runtime_tools",
        lambda *args, **kwargs: (tmp_path / "python", tmp_path / "uv"),
    )
    data_root = tmp_path / "data"
    data_root.mkdir(mode=0o700)
    partial_home = data_root / "yield" / "trainer-runtime" / version
    partial_home.mkdir(parents=True, mode=0o700)
    for path in (data_root / "yield", data_root / "yield" / "trainer-runtime", partial_home):
        path.chmod(0o700)

    with pytest.raises(bundle.ServiceBundleError, match="runtime home is incomplete"):
        bundle.prepare_execution_runtime(
            "yield",
            version=version,
            install_root=tmp_path / "install",
            data_root=data_root,
            release_lock_path=WORKSPACE_ROOT / "release-lock.json",
            runtime_lock_path=tmp_path / "python-runtime.lock.json",
            runner=lambda *_args, **_kwargs: pytest.fail("partial state must fail closed"),
            service_uid=service_uid,
            service_gid=service_gid,
        )


def test_package_bundle_bootstrap_stages_exact_bytes_without_changing_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts_root = tmp_path / "service-artifacts"
    install_root = tmp_path / "install" / "services"
    monkeypatch.setenv("CYRENE_SERVICE_ARTIFACTS_ROOT", str(artifacts_root))
    service_roots: dict[str, Path] = {}
    artifact_bytes: dict[str, bytes] = {}
    active_targets: dict[str, str] = {}
    staged: dict[str, bytes] = {}
    for index, service in enumerate(bundle.SERVICES):
        version = f"bundle-{index}"
        artifact = artifacts_root / service / version
        artifact.mkdir(parents=True)
        payload = f"signed payload for {service}\n".encode()
        (artifact / "payload.bin").write_bytes(payload)
        artifact_bytes[service] = payload
        service_root = install_root / service
        service_root.mkdir(parents=True)
        service_roots[service] = service_root
        if index > 0:
            target = f"releases/old-{index}"
            (service_root / "active").symlink_to(target)
            active_targets[service] = target

    monkeypatch.setattr(bundle, "_ensure_secure_directory", lambda path, **_kwargs: path)
    monkeypatch.setattr(bundle, "_assert_secure_tree", lambda _path: None)
    monkeypatch.setattr(bundle, "_service_root", lambda service, _root=None: service_roots[service])
    monkeypatch.setattr(
        bundle,
        "validate_bundle",
        lambda path, expected_service=None, **_kwargs: {
            "service": expected_service,
            "version": Path(path).name,
        },
    )

    def stage_exact(bundle_path: Path, **_kwargs: object) -> Path:
        service = Path(bundle_path).parent.name
        payload = (Path(bundle_path) / "payload.bin").read_bytes()
        staged[service] = payload
        release = service_roots[service] / "releases" / Path(bundle_path).name
        release.mkdir(parents=True)
        (release / "payload.bin").write_bytes(payload)
        return release

    monkeypatch.setattr(bundle, "stage_release", stage_exact)
    monkeypatch.setattr(bundle, "resolve_active_release", lambda _service: None)
    monkeypatch.setattr(
        bundle,
        "activate_release",
        lambda *_args, **_kwargs: pytest.fail("stage-only package bootstrap must not activate"),
    )

    assert bundle.bootstrap_package_bundles(activate_missing=False) == len(bundle.SERVICES)
    assert staged == artifact_bytes
    assert not (service_roots["navigator"] / "active").exists()
    assert not (service_roots["navigator"] / "active").is_symlink()
    for service, target in active_targets.items():
        assert os.readlink(service_roots[service] / "active") == target


def test_single_service_wheelhouse_carries_exact_sdk_dependency_provenance(tmp_path: Path) -> None:
    service_dir = tmp_path / "navigator"
    service_dir.mkdir()
    product_repo = tmp_path / "product-source"
    product_repo.mkdir()
    descriptor = _write_product_execution_runtime(product_repo)
    copied_descriptor = prepare._copy_execution_runtime_source(
        product_repo, service_dir, UBUNTU_24_PROFILE
    )
    assert copied_descriptor == descriptor
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
        "target_profile": UBUNTU_24_PROFILE_ID,
        "wheel_tags": dict(
            sorted(
                {
                    app_wheel.name: ["py3-none-any"],
                    sdk_wheel.name: ["py3-none-any"],
                }.items()
            )
        ),
        "execution_runtime": descriptor,
        "execution_runtime_source_sha256": prepare._execution_runtime_source_hashes(service_dir),
    }
    (service_dir / "source.json").write_text(json.dumps(source), encoding="utf-8")
    (service_dir / "serve-web.py").write_text("# pinned launcher\n", encoding="utf-8")

    prepare._verify_wheelhouse(
        service_dir,
        prepare.SERVICE_SPECS[0],
        "a" * 40,
        target_profile_id=UBUNTU_24_PROFILE_ID,
        target_profile=UBUNTU_24_PROFILE,
    )

    runtime_input = service_dir / "execution-runtime" / "trainer-runtime" / "bootstrap.py"
    runtime_input.write_text("# changed after source provenance was written\n", encoding="utf-8")
    with pytest.raises(prepare.ProducerError, match="pinned source, lock, or wheel tags"):
        prepare._verify_wheelhouse(
            service_dir,
            prepare.SERVICE_SPECS[0],
            "a" * 40,
            target_profile_id=UBUNTU_24_PROFILE_ID,
            target_profile=UBUNTU_24_PROFILE,
        )
    runtime_input.write_text("# locked runtime input\n", encoding="utf-8")

    source["runtime_dependencies"][0]["wheel_sha256"] = "0" * 64
    (service_dir / "source.json").write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(prepare.ProducerError, match="exactly pinned runtime SDK wheel"):
        prepare._verify_wheelhouse(
            service_dir,
            prepare.SERVICE_SPECS[0],
            "a" * 40,
            target_profile_id=UBUNTU_24_PROFILE_ID,
            target_profile=UBUNTU_24_PROFILE,
        )


def test_service_bundle_build_carries_signed_product_execution_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    service_dir = wheelhouse / "yield"
    service_dir.mkdir(parents=True)
    product_repo = tmp_path / "product-source"
    product_repo.mkdir()
    descriptor = _write_product_execution_runtime(product_repo)
    prepare._copy_execution_runtime_source(product_repo, service_dir, UBUNTU_24_PROFILE)
    app_wheel, _ = _wheel(service_dir, "cyrene-yield", "1.2.3")
    sdk_wheel, _ = _wheel(service_dir, "cyrene-runtime-maintenance", "0.1.0")
    with zipfile.ZipFile(sdk_wheel, "a") as archive:
        archive.writestr("cyrene_runtime_maintenance/__init__.py", "SDK_MARKER = True\n")
    sdk_sha = hashlib.sha256(sdk_wheel.read_bytes()).hexdigest()
    lock = service_dir / "requirements.lock"
    lock.write_text(_requirements([app_wheel, sdk_wheel]), encoding="utf-8")
    source = {
        "schema_version": 2,
        "service": "yield",
        "source_repository": "Cyrene-Yield",
        "source_commit": "a" * 40,
        "requirements_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "runtime_dependencies": [_sdk_record(sdk_sha)],
        "target_profile": UBUNTU_24_PROFILE_ID,
        "wheel_tags": dict(
            sorted(
                {
                    app_wheel.name: ["py3-none-any"],
                    sdk_wheel.name: ["py3-none-any"],
                }.items()
            )
        ),
        "execution_runtime": descriptor,
        "execution_runtime_source_sha256": prepare._execution_runtime_source_hashes(service_dir),
    }
    (service_dir / "source.json").write_text(json.dumps(source), encoding="utf-8")
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
    monkeypatch.setattr(
        bundle.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "VERSION_ID": "24.04"},
    )
    monkeypatch.setattr(bundle.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bundle.platform, "libc_ver", lambda: ("glibc", "2.39"))

    result = bundle._build_one_bundle(
        service="yield",
        wheelhouse=wheelhouse,
        output_root=tmp_path / "releases",
        source_commit="a" * 40,
        source_repository="Cyrene-Yield",
        target_profile_id=UBUNTU_24_PROFILE_ID,
        target_profile=UBUNTU_24_PROFILE,
        python_executable=tmp_path / "python3.12",
        builder_python=tmp_path / "builder-python",
    )

    assert len(build_calls) == 2
    assert (result / "python" / "cyrene_runtime_maintenance" / "__init__.py").read_text(
        encoding="utf-8"
    ) == "SDK_MARKER = True\n"
    manifest = bundle.validate_bundle(result, expected_service="yield")
    assert manifest["dependencies"]["runtime"] == source["runtime_dependencies"]
    assert manifest["execution_runtime"] == descriptor
    assert source["execution_runtime_source_sha256"] == {
        file_name: digest
        for file_name, digest in manifest["files"].items()
        if file_name.startswith("execution-runtime/")
    }
    launcher = (result / "run-service").read_text(encoding="utf-8")
    assert bundle.PRIVATE_PYTHON_EXECUTABLE in launcher
    assert '--runtime-config "$PLATFORM_RUNTIME_CONFIG"' in launcher
    assert '--trainer-runtime-config "$TRAINER_RUNTIME_CONFIG"' in launcher
    assert '--artifact-root "$ARTIFACT_ROOT"' in launcher
    assert "exec python3 " not in launcher

    runtime_input = result / "execution-runtime" / "trainer-runtime" / "bootstrap.py"
    runtime_input.write_text("# tampered\n", encoding="utf-8")
    with pytest.raises(bundle.ServiceBundleError, match="payload file list or SHA-256"):
        bundle.validate_bundle(result, expected_service="yield")


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
    dist_info = root / "python" / "cyrene_runtime_maintenance-0.1.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "WHEEL").write_text(
        "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        encoding="utf-8",
    )
    (root / "python" / "cyrene_runtime_maintenance.py").write_text("SDK\n", encoding="utf-8")
    entrypoint = root / "run-service"
    entrypoint.write_text(
        bundle._entrypoint_text("navigator", bundle.PRIVATE_PYTHON_EXECUTABLE),
        encoding="utf-8",
    )
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
        "target_profile": UBUNTU_24_PROFILE_ID,
        "wheel_tags": {"cyrene-runtime-maintenance-0.1.0-py3-none-any.whl": ["py3-none-any"]},
    }
    (root / "source.json").write_text(json.dumps(source, sort_keys=True), encoding="utf-8")
    manifest = bundle._write_bundle_manifest(
        root,
        service="navigator",
        source_commit="b" * 40,
        source_repository="Cyrene-Navigator",
        target_profile_id=UBUNTU_24_PROFILE_ID,
        target_profile=UBUNTU_24_PROFILE,
        source_wheel_tags=source["wheel_tags"],
        installed_wheel_tags=["py3-none-any"],
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
