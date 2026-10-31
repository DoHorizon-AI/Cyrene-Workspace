"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 test_release_reconcile.py                                       │
│  Module: tooling.deploy.teams_cloud.tests.test_release_reconcile    │
│  Role: Verify timer installation and safe release continuation.     │
│                                                                     │
│  模块职责：用隔离文件路径验证协调器安装与安全续跑。                  │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_bundle
import release_deploy as deployment

FIXTURE = Path(__file__).parent / "fixtures/simulated-release-manifest-v1.json"
UNIT_NAMES = (
    "cyrene-teams-release-reconcile.service",
    "cyrene-teams-release-reconcile.timer",
)


def _manifest(*, run_id: int, attempt: int = 1) -> dict[str, Any]:
    """Return a validator-approved manifest with a distinct publisher attempt."""

    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    publisher = value["publisherRun"]
    publisher["runId"] = run_id
    publisher["runAttempt"] = attempt
    release_id = release_bundle._release_id(
        value["sourceRun"]["headSha"], value["sourceFingerprintSha256"], publisher
    )
    value["releaseId"] = release_id
    value["releaseTag"] = release_id
    for image in value["images"]:
        source = value["sources"][release_bundle.IMAGE_LABEL_SOURCE[image["service"]]]
        image["image"] = f"cyrene/{image['service']}:{release_id}-{source['commit'][:12]}"
    release_bundle.validate_release_manifest(value)
    return value


def _policy(root: Path) -> deployment.DeploymentPolicy:
    """Create a non-host policy record for functions under test."""

    root.mkdir(parents=True, exist_ok=True)
    return deployment.DeploymentPolicy(
        path=root / "policy.json",
        sha256="1" * 64,
        selection=root / "selection.json",
        journals=root / "journals",
        root=root,
        probe_policy=root / "probe-policy.json",
        probe_policy_sha256="2" * 64,
        targets=("github-issues", "exchange"),
        max_memory_mib=2048,
        max_cpus=1.5,
        required_free_gib=0.1,
        minimum_available_mib=512,
        drain_seconds=300,
        health_seconds=120,
        window_wait_seconds=0,
        ingress_exposure="host-fronted-staging",
    )


def _candidate(root: Path, manifest: dict[str, Any]) -> tuple[Path, Path]:
    """Persist only the pinned manifest and binding required by reconciliation."""

    directory = root / manifest["releaseId"]
    directory.mkdir(parents=True)
    manifest_path = directory / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    binding_path = directory / "deployment-binding.json"
    binding_path.write_text(
        json.dumps(
            {
                "releaseId": manifest["releaseId"],
                "manifestSha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return directory, manifest_path


class _ReconcileBackend:
    """Read-only fake for journals and uncertain image-import read-back."""

    def __init__(self, states: dict[str, dict[str, Any] | None] | None = None) -> None:
        self.states = states or {}
        self.status_calls: list[str] = []
        self.import_checks: list[str] = []
        self.import_result = True

    def status(self, step: Path) -> dict[str, Any] | None:
        self.status_calls.append(step.name)
        return copy.deepcopy(self.states.get(step.name))

    def import_resolved(self, step: Path, image: dict[str, Any]) -> bool:
        self.import_checks.append(image["service"])
        return self.import_result


def test_reconcile_empty_queue_is_read_only_no_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty private release directory does not issue Docker or deployment calls."""

    policy = _policy(tmp_path / ".artifacts" / "releases")
    backend = _ReconcileBackend()
    monkeypatch.setattr(deployment, "CanonicalBackend", lambda _policy: backend)
    monkeypatch.setattr(
        deployment,
        "deploy_release",
        lambda *_args, **_kwargs: pytest.fail("empty queue must not deploy"),
    )

    result = deployment.reconcile_pending_release(policy)

    assert result["state"] == "NO_CHANGE"
    assert backend.status_calls == []
    assert backend.import_checks == []


def test_runtime_observation_module_is_in_the_release_tools_closure() -> None:
    """Every runtime-local import used by status and reconcile is shipped."""

    assert "release_observation.py" in release_bundle.REQUIRED_DEPLOYMENT_TOOL_FILES
    source_root = Path(__file__).resolve().parents[1]
    imported_helpers = {
        "build_arm64.py",
        "deployment_probe.py",
        "host_fronted_canary.py",
        "preflight.py",
        "release_bundle.py",
        "release_observation.py",
        "runtime_update.py",
        "stage_transition.py",
    }
    assert imported_helpers <= release_bundle.REQUIRED_DEPLOYMENT_TOOL_FILES
    config_files = {
        "config/" + path.name for path in (source_root / "config").iterdir() if path.is_file()
    }
    required_config_files = {
        path
        for path in (
            release_bundle.REQUIRED_DEPLOYMENT_TOOL_FILES
            | release_bundle.AUTOMATION_DEPLOYMENT_TOOL_FILES
            | release_bundle.OFFICE_GRAPH_DEPLOYMENT_TOOL_FILES
            | release_bundle.ANTIGRAVITY_ACP_DEPLOYMENT_TOOL_FILES
        )
        if path.startswith("config/")
    }
    assert config_files <= required_config_files


def test_systemd_reconciler_units_call_the_bounded_release_cli() -> None:
    """The installed timer invokes only the pinned policy and private receipt paths."""

    source_root = Path(__file__).resolve().parents[1]
    service = (source_root / UNIT_NAMES[0]).read_text(encoding="utf-8")
    timer = (source_root / UNIT_NAMES[1]).read_text(encoding="utf-8")

    assert (
        "ExecStart=/usr/bin/python3 /usr/local/lib/cyrene-teams-deployment/current/"
        "release_deploy.py reconcile --policy /etc/cyrene/teams-cloud-deployment.json "
        "--receipt /var/lib/cyrene-connectors-runtime/.artifacts/releases/"
        "reconcile-receipt.json"
    ) in service
    assert "TimeoutStartSec=55min" in service
    assert "OnBootSec=2min" in timer
    assert "OnUnitInactiveSec=5min" in timer
    assert "Unit=cyrene-teams-release-reconcile.service" in timer


def test_reconcile_blocks_unknown_native_operation_without_replaying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A journal outside the explicit safe terminal set stops before any continuation."""

    policy = _policy(tmp_path / ".artifacts" / "releases")
    manifest = _manifest(run_id=700)
    directory, _manifest_path = _candidate(policy.root, manifest)
    backend = _ReconcileBackend(
        {
            "navigator-persistence": {
                "state": "APPLYING",
                "operationId": "operation-in-progress",
                "planSha256": "3" * 64,
            }
        }
    )
    monkeypatch.setattr(deployment, "CanonicalBackend", lambda _policy: backend)
    monkeypatch.setattr(
        deployment,
        "deployment_status",
        lambda *_args, **_kwargs: pytest.fail("unresolved journal must stop first"),
    )
    monkeypatch.setattr(
        deployment,
        "deploy_release",
        lambda *_args, **_kwargs: pytest.fail("unknown operation must never replay"),
    )

    result = deployment.reconcile_pending_release(policy)

    assert result["releaseId"] == directory.name
    assert result["state"] == "RECOVERY_REQUIRED"
    assert result["errorCode"] == "PRIOR_NATIVE_OPERATION_UNRESOLVED"
    assert backend.status_calls == ["navigator-persistence"]
    assert backend.import_checks == []


def test_reconcile_import_intent_uses_readback_and_never_reloads_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolved Docker load is only inspected; failure blocks all later updates."""

    policy = _policy(tmp_path / ".artifacts" / "releases")
    manifest = _manifest(run_id=701)
    directory, _manifest_path = _candidate(policy.root, manifest)
    step = directory / "steps" / "navigator-persistence"
    step.mkdir(parents=True)
    (step / "image-import-intent.json").write_text("{}", encoding="utf-8")
    backend = _ReconcileBackend()
    backend.import_result = False
    monkeypatch.setattr(deployment, "CanonicalBackend", lambda _policy: backend)
    monkeypatch.setattr(
        deployment,
        "deployment_status",
        lambda *_args, **_kwargs: pytest.fail("unresolved import must stop first"),
    )
    monkeypatch.setattr(
        deployment,
        "deploy_release",
        lambda *_args, **_kwargs: pytest.fail("unresolved import must never replay"),
    )

    result = deployment.reconcile_pending_release(policy)

    assert result["state"] == "RECOVERY_REQUIRED"
    assert result["errorCode"] == "PRIOR_IMAGE_IMPORT_UNRESOLVED"
    assert backend.import_checks == ["navigator-persistence"]


def test_reconcile_resumes_only_newest_safely_resumable_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safe continuation uses the newest publisher identity and a release-bound URL."""

    policy = _policy(tmp_path / ".artifacts" / "releases")
    older, _ = _candidate(policy.root, _manifest(run_id=702))
    newest, newest_manifest_path = _candidate(policy.root, _manifest(run_id=703))
    backend = _ReconcileBackend()
    deployment_status_calls: list[str] = []
    deploy_calls: list[tuple[Any, ...]] = []

    def fake_status(_policy: Any, release_id: str) -> dict[str, Any]:
        deployment_status_calls.append(release_id)
        return {"releaseId": release_id, "state": "SAFE_TO_RESUME", "mayResumeSafely": True}

    def fake_deploy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        deploy_calls.append((*args, kwargs))
        return {"releaseId": newest.name, "state": "APPLIED"}

    monkeypatch.setattr(deployment, "CanonicalBackend", lambda _policy: backend)
    monkeypatch.setattr(deployment, "deployment_status", fake_status)
    monkeypatch.setattr(deployment, "deploy_release", fake_deploy)

    result = deployment.reconcile_pending_release(policy)

    assert result == {"releaseId": newest.name, "state": "APPLIED"}
    assert older.name != newest.name
    assert deployment_status_calls == [newest.name]
    assert len(deploy_calls) == 1
    args, kwargs = deploy_calls[0][:-1], deploy_calls[0][-1]
    assert args[0] is policy
    assert args[1] == newest / "release-manifest.json"
    assert args[2] == hashlib.sha256(args[1].read_bytes()).hexdigest()
    assert args[3] == (
        "https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/download/" + newest.name
    )
    assert kwargs == {"execute": True}
    assert json.loads(newest_manifest_path.read_text(encoding="utf-8"))["releaseId"] == newest.name


def test_install_reconciler_uses_only_simulated_host_paths_and_is_repeatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installer replaces only mocked units/pointer and safely repeats the same version."""

    policy = _policy(tmp_path / ".artifacts" / "releases")
    manifest = _manifest(run_id=704)
    base = policy.root / manifest["releaseId"]
    base.mkdir()
    manifest_path = base / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    expected_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    tools_root = Path(__file__).resolve().parents[1]
    for name in UNIT_NAMES:
        (base / name).write_bytes((tools_root / name).read_bytes())

    host_directory = tmp_path / "simulated-host" / "lib" / "cyrene-teams-deployment"
    systemd_directory = tmp_path / "simulated-host" / "etc" / "systemd" / "system"
    systemd_directory.mkdir(parents=True)
    previous_path_factory = deployment.Path
    path_class = type(previous_path_factory("."))
    pointer_path = host_directory / "current"

    class _SimulatedRootOwnedPath(path_class):
        @staticmethod
        def _root_owned(value: os.stat_result) -> os.stat_result:
            fields = list(value)
            fields[4] = 0
            return os.stat_result(fields)

        def lstat(self) -> os.stat_result:
            value = super().lstat()
            if str(self) == str(pointer_path):
                return self._root_owned(value)
            return value

        def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
            value = super().stat(follow_symlinks=follow_symlinks)
            if self.parent == systemd_directory and self.name in UNIT_NAMES:
                return self._root_owned(value)
            return value

    def simulated_path(value: Any = ".") -> Path:
        if str(value) == "/usr/local/lib/cyrene-teams-deployment":
            return _SimulatedRootOwnedPath(host_directory)
        if str(value) == "/etc/systemd/system":
            return _SimulatedRootOwnedPath(systemd_directory)
        return _SimulatedRootOwnedPath(value)

    monkeypatch.setattr(deployment, "Path", simulated_path)
    monkeypatch.setattr(deployment, "__file__", str(base / "release_deploy.py"))

    def ensure_test_directory(path: Path, *, create: bool) -> None:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        assert path.is_dir()

    monkeypatch.setattr(deployment, "_directory", ensure_test_directory)
    commands: list[list[str]] = []

    def fake_systemctl(
        command: list[str], *, capture_output: bool, timeout: int, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        assert capture_output is True
        assert timeout in {15, 30}
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout="active\n" if "show" in command else ""
        )

    monkeypatch.setattr(deployment.subprocess, "run", fake_systemctl)

    first = deployment.install_reconciler(policy, manifest["releaseId"], expected_digest)
    second = deployment.install_reconciler(policy, manifest["releaseId"], expected_digest)

    assert first["state"] == second["state"] == "INSTALLED"
    assert first["applicationContainersModified"] is False
    pointer = pointer_path
    assert pointer.is_symlink() and pointer.resolve() == base.resolve()
    assert all(
        (systemd_directory / name).read_bytes() == (base / name).read_bytes() for name in UNIT_NAMES
    )
    assert [command[1] for command in commands] == [
        "daemon-reload",
        "enable",
        "show",
        "daemon-reload",
        "enable",
        "show",
    ]
    assert not any("docker" in command for command in commands)


def test_install_reconciler_cli_dispatches_and_writes_private_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The systemd command line reaches the installer and persists only its receipt."""

    policy = _policy(tmp_path / ".artifacts" / "releases")
    release_id = _manifest(run_id=705)["releaseId"]
    digest = "a" * 64
    receipt = policy.root / "reconcile-receipt.json"
    calls: list[tuple[Any, ...]] = []

    monkeypatch.setattr(deployment, "load_policy", lambda _path: policy)
    monkeypatch.setattr(
        deployment,
        "install_reconciler",
        lambda *args: (
            calls.append(args)
            or {
                "schemaVersion": deployment.RECEIPT_SCHEMA,
                "releaseId": release_id,
                "manifestSha256": digest,
                "state": "INSTALLED",
            }
        ),
    )

    exit_code = deployment.main(
        [
            "install-reconciler",
            "--policy",
            str(policy.path),
            "--receipt",
            str(receipt),
            "--release-id",
            release_id,
            "--expected-manifest-sha256",
            digest,
        ]
    )

    assert exit_code == 0
    assert calls == [(policy, release_id, digest)]
    assert json.loads(receipt.read_text(encoding="utf-8"))["state"] == "INSTALLED"
    assert json.loads(capsys.readouterr().out)["state"] == "INSTALLED"


def test_default_probe_policy_path_satisfies_the_canonical_private_artifact_gate() -> None:
    """The shipped deployment template must be consumable by the real probe loader."""

    import preflight

    base = Path(__file__).resolve().parents[1]
    policy = json.loads((base / "config/deployment-policy.template.json").read_text())
    assert preflight._private_artifact(Path(policy["probePolicyPath"]))
