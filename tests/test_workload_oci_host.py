"""Focused tests for the fixed, root-managed Echo OCI host helper."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "packaging" / "workload_oci_host.py"
MODULE_SPEC = importlib.util.spec_from_file_location("cyrene_workload_oci_host_test", MODULE_PATH)
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
oci_host = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = oci_host
MODULE_SPEC.loader.exec_module(oci_host)


def _sha(label: str) -> str:
    """Return one stable typed SHA-256 fixture digest."""

    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def _result(
    arguments: Sequence[str], *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Build one command result consumed by the injected runtime runner."""

    return subprocess.CompletedProcess(list(arguments), returncode, stdout, stderr)


def _raw_spec() -> dict[str, Any]:
    """Create a production-shaped spec with an opaque Plugin connection reference."""

    return {
        "schemaVersion": 1,
        "componentId": "cyrene-echo",
        "targetId": oci_host.ECHO_TARGET_ID,
        "target": dict(oci_host.ECHO_TARGET),
        "manifestDigest": _sha("echo-manifest"),
        "imageRepository": oci_host.OFFICIAL_ECHO_REPOSITORY,
        "imageDigest": _sha("echo-image"),
        "platform": dict(oci_host.ECHO_PLATFORM),
        "containerName": "cyrene-echo-" + "a" * 12,
        "hostUid": os.getuid() or 1001,
        "hostGid": os.getgid() or 1001,
        "hostPort": 8094,
        "activitySourceId": "cyrene-echo",
        "activityCatalogGeneration": 13,
        "activityTokenDigest": hashlib.sha256(b"source-token-fixture").hexdigest(),
        "activityTokenPath": str(oci_host.DEFAULT_SOURCE_TOKEN_PATH),
        "maintenanceSocketPath": str(oci_host.DEFAULT_MAINTENANCE_SOCKET_PATH),
        "dataDirectory": str(oci_host.DEFAULT_DATA_DIRECTORY),
        "artifactDirectory": str(oci_host.DEFAULT_ARTIFACT_DIRECTORY),
        "environment": {
            "CYRENE_EVALUATION_RUNNER_CONNECTION_REF": "opaque://plugin-supervisor/conn/secret-ref",
            "CYRENE_DATA_TOOLS_TOKEN": "opaque-product-token",
        },
    }


class FakeDocker:
    """Model only the Docker operations owned by this helper."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.container: dict[str, Any] | None = None
        self.container_id = "b" * 64
        self.spec: oci_host.OciHostSpec | None = None
        self.token_path: Path | None = None
        self.environment_file_mode: int | None = None
        self.environment_file_owner: tuple[int, int] | None = None

    def __call__(self, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        self.calls.append(command)
        if command[:1] == ["pull"]:
            return _result(command)
        if command[:2] == ["image", "inspect"]:
            image = {
                "Id": _sha("image-config"),
                "RepoDigests": [self.spec.image_reference],
                "Os": "linux",
                "Architecture": "amd64",
                "Config": {"Env": []},
            }
            return _result(command, stdout=json.dumps(image))
        if command[:3] == ["container", "ls", "--all"]:
            return _result(command, stdout=f"{self.container_id}\n" if self.container else "")
        if command[:2] == ["container", "inspect"]:
            return _result(command, stdout=json.dumps(self.container))
        if command[:1] == ["run"]:
            environment_file = Path(command[command.index("--env-file") + 1])
            info = environment_file.stat()
            self.environment_file_mode = stat.S_IMODE(info.st_mode)
            self.environment_file_owner = (info.st_uid, info.st_gid)
            self.container = _container_record(self.spec, self.container_id, self.token_path)
            return _result(command, stdout=f"{self.container_id}\n")
        if command[:1] == ["start"]:
            self.container["State"]["Status"] = "running"
            return _result(command)
        if command[:1] == ["stop"]:
            self.container["State"]["Status"] = "exited"
            return _result(command)
        if command[:1] == ["rm"]:
            self.container = None
            return _result(command)
        raise AssertionError(f"unexpected Docker operation: {command[:3]}")


def _container_record(
    spec: oci_host.OciHostSpec, container_id: str, token_path: Path
) -> dict[str, Any]:
    """Return the protected subset Docker inspect exposes for one test container."""

    mounts = [
        {
            "Source": str(spec.data_directory),
            "Destination": "/data/echo",
            "RW": True,
        },
        {
            "Source": str(spec.artifact_directory),
            "Destination": "/data/artifacts",
            "RW": True,
        },
        {
            "Source": str(spec.maintenance_socket_path),
            "Destination": str(oci_host.CONTAINER_MAINTENANCE_SOCKET_PATH),
            "RW": False,
        },
        {
            "Source": str(token_path),
            "Destination": str(oci_host.CONTAINER_TOKEN_PATH),
            "RW": False,
        },
    ]
    environment = [
        f"{key}={value}" for key, value in sorted(oci_host._container_environment(spec).items())
    ]
    return {
        "Id": container_id,
        "Name": f"/{spec.container_name}",
        "Config": {
            "Image": spec.image_reference,
            "User": f"{spec.host_uid}:{spec.host_gid}",
            "Labels": oci_host._container_labels(spec),
            "Env": environment,
        },
        "HostConfig": {
            "NetworkMode": "host",
            "UsernsMode": "host",
            "Privileged": False,
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "AutoRemove": False,
            "PortBindings": {},
            "RestartPolicy": {"Name": "unless-stopped"},
        },
        "State": {"Status": "running"},
        "Mounts": mounts,
    }


@pytest.fixture
def test_host_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], Path]:
    """Redirect fixed host paths into isolated test storage."""

    owner_uid = os.getuid()
    owner_gid = os.getgid()
    monkeypatch.setattr(oci_host, "ROOT_OWNER_UID", owner_uid)
    monkeypatch.setattr(oci_host, "ROOT_OWNER_GID", owner_gid)
    monkeypatch.setattr(oci_host, "DEFAULT_DATA_DIRECTORY", tmp_path / "var/lib/cyrene/echo")
    monkeypatch.setattr(
        oci_host,
        "DEFAULT_ARTIFACT_DIRECTORY",
        tmp_path / "var/lib/cyrene/echo/artifacts",
    )
    monkeypatch.setattr(
        oci_host,
        "DEFAULT_SOURCE_TOKEN_PATH",
        tmp_path / "etc/cyrene/runtime-activity-source-tokens/cyrene-echo.token",
    )
    monkeypatch.setattr(
        oci_host,
        "DEFAULT_MAINTENANCE_SOCKET_PATH",
        tmp_path / "run/cyrene/runtime-maintenance.sock",
    )
    monkeypatch.setattr(
        oci_host,
        "DEFAULT_TOKEN_STAGING_ROOT",
        tmp_path / "var/lib/cyrene/runtime-activity-source-tokens/oci",
    )
    monkeypatch.setattr(
        oci_host,
        "DEFAULT_PRIVATE_RUNTIME_ROOT",
        tmp_path / "run/cyrene/workload-oci",
    )
    payload = _raw_spec()
    payload["activityTokenPath"] = str(oci_host.DEFAULT_SOURCE_TOKEN_PATH)
    payload["maintenanceSocketPath"] = str(oci_host.DEFAULT_MAINTENANCE_SOCKET_PATH)
    payload["dataDirectory"] = str(oci_host.DEFAULT_DATA_DIRECTORY)
    payload["artifactDirectory"] = str(oci_host.DEFAULT_ARTIFACT_DIRECTORY)
    return payload, tmp_path


def _make_fake_runtime(
    payload: Mapping[str, Any], monkeypatch: pytest.MonkeyPatch
) -> tuple[oci_host.OciHostSpec, FakeDocker]:
    """Construct a fake daemon tied to a validated exact-image spec."""

    spec = oci_host.OciHostSpec.from_mapping(payload)
    docker = FakeDocker()
    docker.spec = spec
    docker.token_path = oci_host._staged_token_path(spec)
    monkeypatch.setattr(oci_host, "_require_root", lambda: None)
    monkeypatch.setattr(oci_host, "_require_maintenance_socket", lambda _path: None)
    monkeypatch.setattr(oci_host, "_read_source_token", lambda _path: b"source-token-fixture")
    monkeypatch.setattr(oci_host, "_ensure_product_directories", lambda _spec: None)
    return spec, docker


def test_spec_binds_exact_echo_u24_official_image_and_opaque_ref(
    test_host_paths: tuple[dict[str, Any], Path],
) -> None:
    """Accept only the signed Product identity while keeping opaque refs out of receipts."""

    payload, _ = test_host_paths
    spec = oci_host.OciHostSpec.from_mapping(payload)
    public = oci_host.OciContainerObservation(
        component_id=spec.component_id,
        target_id=spec.target_id,
        manifest_digest=spec.manifest_digest,
        image_digest=spec.image_digest,
        image_reference=spec.image_reference,
        container_name=spec.container_name,
        container_id=None,
        state="created",
        host_uid=spec.host_uid,
        host_gid=spec.host_gid,
        activity_source_id=spec.activity_source_id,
        activity_catalog_generation=spec.activity_catalog_generation,
        data_directory=str(spec.data_directory),
        artifact_directory=str(spec.artifact_directory),
    ).to_receipt_fields()
    assert spec.image_reference == f"{oci_host.OFFICIAL_ECHO_REPOSITORY}@{spec.image_digest}"
    assert "CYRENE_EVALUATION_RUNNER_CONNECTION_REF" not in json.dumps(public)
    assert "opaque://plugin-supervisor" not in repr(spec)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("imageRepository", "attacker.invalid/echo"),
        ("imageDigest", "latest"),
        ("targetId", "windows-10.0-x86_64-docker-linux"),
        ("platform", {"os": "linux", "architecture": "arm64"}),
        ("hostUid", True),
        ("hostPort", 65536),
        ("activityCatalogGeneration", 0),
        ("containerName", "echo;touch /tmp/owned"),
        ("environment", {"ECHO_HOST": "0.0.0.0"}),
    ],
)
def test_spec_rejects_mutable_identity_and_unsafe_overrides(
    test_host_paths: tuple[dict[str, Any], Path], field: str, value: Any
) -> None:
    """Reject tag refs, target substitution, arbitrary env, and unsafe host fields."""

    payload, _ = test_host_paths
    payload[field] = value
    with pytest.raises(oci_host.WorkloadOciHostError):
        oci_host.OciHostSpec.from_mapping(payload)


def test_pull_verifies_digest_and_linux_amd64_platform(
    test_host_paths: tuple[dict[str, Any], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pull uses the exact repository digest and checks daemon platform metadata."""

    spec, docker = _make_fake_runtime(test_host_paths[0], monkeypatch)
    result = oci_host.pull_and_verify_image(spec, runner=docker)
    assert result["imageDigest"] == spec.image_digest
    assert result["platform"] == "linux/amd64"
    assert docker.calls[0] == ["pull", spec.image_reference]


def test_pull_fails_closed_when_daemon_reports_another_digest(
    test_host_paths: tuple[dict[str, Any], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful pull is insufficient if RepoDigests omits the requested digest."""

    spec, docker = _make_fake_runtime(test_host_paths[0], monkeypatch)
    original = docker.__call__

    def mismatch(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if list(arguments)[:2] == ["image", "inspect"]:
            bad = {
                "Id": _sha("image-config"),
                "RepoDigests": [f"{spec.image_repository}@{_sha('wrong-image')}"],
                "Os": "linux",
                "Architecture": "amd64",
            }
            return _result(arguments, stdout=json.dumps(bad))
        return original(arguments)

    with pytest.raises(oci_host.WorkloadOciHostError, match="exact digest"):
        oci_host.pull_and_verify_image(spec, runner=mismatch)


def test_start_uses_loopback_nonprivileged_readonly_container_and_private_env(
    test_host_paths: tuple[dict[str, Any], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Create the named container with strict mounts, host UID/GID, and no public port."""

    payload, root = test_host_paths
    spec, docker = _make_fake_runtime(payload, monkeypatch)
    observation = oci_host.start_container(spec, runner=docker)
    assert observation.state == "running"
    run_args = next(call for call in docker.calls if call[:1] == ["run"])
    assert run_args[run_args.index("--user") + 1] == f"{spec.host_uid}:{spec.host_gid}"
    assert run_args[run_args.index("--network") + 1] == "host"
    assert run_args[run_args.index("--userns") + 1] == "host"
    assert run_args[run_args.index("--cap-drop") + 1] == "ALL"
    assert "--privileged" not in run_args
    assert "--read-only" in run_args
    assert "--publish" not in run_args and "-p" not in run_args
    assert docker.environment_file_mode == 0o600
    assert docker.environment_file_owner == (oci_host.ROOT_OWNER_UID, oci_host.ROOT_OWNER_GID)
    assert not list(root.rglob("product.env"))
    staged_token = oci_host._staged_token_path(spec)
    token_stat = staged_token.stat()
    assert stat.S_IMODE(token_stat.st_mode) == 0o400
    assert (token_stat.st_uid, token_stat.st_gid) == (spec.host_uid, spec.host_gid)
    assert b"opaque-product-token" not in staged_token.read_bytes()


def test_start_rejects_existing_same_name_container_with_different_mounts(
    test_host_paths: tuple[dict[str, Any], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never restart an object whose exact source or data mounts drifted."""

    spec, docker = _make_fake_runtime(test_host_paths[0], monkeypatch)
    oci_host._stage_activity_token(spec)
    docker.container = _container_record(spec, docker.container_id, docker.token_path)
    docker.container["Mounts"].pop()
    with pytest.raises(oci_host.WorkloadOciHostError, match="mount set"):
        oci_host.start_container(spec, runner=docker)


def test_stop_remove_preserves_data_and_never_removes_docker_volumes(
    test_host_paths: tuple[dict[str, Any], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop and remove the exact container object while leaving host data untouched."""

    spec, docker = _make_fake_runtime(test_host_paths[0], monkeypatch)
    oci_host._stage_activity_token(spec)
    docker.container = _container_record(spec, docker.container_id, docker.token_path)
    observation = oci_host.remove_container(spec, runner=docker)
    assert observation.state == "not-installed"
    assert docker.container is None
    assert any(call[:1] == ["stop"] for call in docker.calls)
    remove_call = next(call for call in docker.calls if call[:1] == ["rm"])
    assert "-v" not in remove_call and "--volumes" not in remove_call
    assert not any(call[:1] == ["volume"] for call in docker.calls)


def test_remove_can_delete_only_staged_token_without_recursive_cleanup(
    test_host_paths: tuple[dict[str, Any], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credential cleanup unlinks the helper-owned token and retains Product data."""

    payload, _ = test_host_paths
    spec, docker = _make_fake_runtime(payload, monkeypatch)
    oci_host._stage_activity_token(spec)
    data = spec.data_directory
    artifact = spec.artifact_directory
    data.mkdir(parents=True, exist_ok=True)
    (data / "customer-training.jsonl").write_text("retained\n", encoding="utf-8")
    artifact.mkdir(parents=True, exist_ok=True)
    observation = oci_host.remove_container(spec, runner=docker, remove_runtime_credentials=True)
    assert observation.state == "not-installed"
    assert not oci_host._staged_token_path(spec).exists()
    assert not oci_host._staged_token_path(spec).parent.exists()
    assert (data / "customer-training.jsonl").read_text(encoding="utf-8") == "retained\n"
    assert artifact.is_dir()


def test_remove_can_recover_when_staged_token_digest_is_stale(
    test_host_paths: tuple[dict[str, Any], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale ActivitySource token blocks readiness but cannot block safe uninstall."""

    payload, _ = test_host_paths
    spec, docker = _make_fake_runtime(payload, monkeypatch)
    token_path = oci_host._stage_activity_token(spec)
    docker.container = _container_record(spec, docker.container_id, token_path)
    token_path.chmod(0o600)
    token_path.write_bytes(b"rotated-token")
    token_path.chmod(0o400)

    with pytest.raises(oci_host.WorkloadOciHostError, match="differs from the catalog"):
        oci_host.container_status(spec, runner=docker)

    result = oci_host.remove_container(spec, runner=docker, remove_runtime_credentials=True)
    assert result.state == "not-installed"
    assert docker.container is None
    assert not token_path.exists()


def test_runtime_socket_mount_is_exact_and_readonly(
    test_host_paths: tuple[dict[str, Any], Path],
) -> None:
    """The Docker request mounts only the required Unix socket, never its directory."""

    spec = oci_host.OciHostSpec.from_mapping(test_host_paths[0])
    args = oci_host._build_run_arguments(
        spec,
        Path("/var/lib/cyrene/runtime-activity-source-tokens/oci/test/activity-token"),
        Path("/run/cyrene/workload-oci/test/product.env"),
    )
    mount_args = [args[index + 1] for index, item in enumerate(args[:-1]) if item == "--mount"]
    exact_socket_mount = (
        f"type=bind,source={spec.maintenance_socket_path},"
        f"target={oci_host.CONTAINER_MAINTENANCE_SOCKET_PATH},readonly"
    )
    assert exact_socket_mount in mount_args
    assert not any("source=/run/cyrene," in item for item in mount_args)
    assert all("docker.sock" not in item for item in mount_args)


def test_source_token_must_be_root_owned_private_regular_file(
    test_host_paths: tuple[dict[str, Any], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject source token links and group/world-readable host credentials."""

    payload, root = test_host_paths
    monkeypatch.setattr(oci_host, "ROOT_OWNER_UID", os.getuid())
    token_path = Path(payload["activityTokenPath"])
    token_path.parent.mkdir(parents=True)
    token_path.write_bytes(b"source-token")
    token_path.chmod(0o400)
    assert oci_host._read_source_token(token_path) == b"source-token"

    token_path.chmod(0o444)
    with pytest.raises(oci_host.WorkloadOciHostError, match="permissions"):
        oci_host._read_source_token(token_path)

    token_path.unlink()
    link = root / "source-token-link"
    link.symlink_to(root / "missing-token")
    with pytest.raises(oci_host.WorkloadOciHostError, match="unavailable"):
        oci_host._read_source_token(link)


def test_staged_activity_token_must_match_signed_catalog_digest(
    test_host_paths: tuple[dict[str, Any], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a private host token whose bytes no longer match Catalog identity."""

    payload, _ = test_host_paths
    payload["activityTokenDigest"] = hashlib.sha256(b"different-token").hexdigest()
    spec, _ = _make_fake_runtime(payload, monkeypatch)
    with pytest.raises(oci_host.WorkloadOciHostError, match="catalog digest"):
        oci_host._stage_activity_token(spec)
