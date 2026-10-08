"""Trust-boundary tests for the Workspace Runtime Maintenance SDK helper."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path) -> Any:
    """Load the packaging helper without importing neighboring scripts."""

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


environment = _load_module(
    "workload_sdk_environment_test", ROOT / "packaging/workload_sdk_environment.py"
)


def _sha(value: str) -> str:
    """Return the canonical SHA-256 text used by workload rows."""

    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class FakeCommandRunner:
    """Model subprocess boundaries without claiming a production SDK install."""

    def __init__(self, private_python: Path, *, fail_pip: bool = False) -> None:
        self.private_python = private_python
        self.fail_pip = fail_pip
        self.commands: list[list[str]] = []

    def __call__(
        self, argv: list[str] | tuple[str, ...], environment_vars: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        self.commands.append(command)
        if command[0] == str(self.private_python) and "venv" in command:
            venv_path = Path(command[-1])
            python_path = venv_path / "bin" / "python"
            python_path.parent.mkdir(parents=True, exist_ok=True)
            venv_path.chmod(0o755)
            python_path.parent.chmod(0o755)
            (venv_path / "pyvenv.cfg").write_text("home = /private/python\n", encoding="utf-8")
            (venv_path / "pyvenv.cfg").chmod(0o644)
            python_path.write_bytes(b"test-only executable placeholder")
            python_path.chmod(0o755)
            return subprocess.CompletedProcess(command, 0, "", "")
        if "pip" in command:
            assert "--no-deps" in command
            assert "--no-index" in command
            assert environment_vars["PIP_NO_INDEX"] == "1"
            assert environment_vars["PIP_CONFIG_FILE"]
            if self.fail_pip:
                raise subprocess.CalledProcessError(1, command, stderr="test interruption")
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[0] == str(self.private_python):
            private_identity = {
                "implementation": "cpython",
                "version": "3.12.14",
                "executable": str(self.private_python),
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(private_identity), "")
        output: dict[str, Any] = {
            "version": "0.1.0",
            "methods": {
                "from_source_secret": True,
                "authority": True,
                "runtime_status": True,
            },
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(output), "")


@pytest.fixture
def sdk_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Build a root-identity test stage and redirect fixed paths into tmp_path."""

    monkeypatch.setattr(environment.os, "geteuid", lambda: 0)
    # The test process is not root; production ownership is exercised separately below.
    monkeypatch.setattr(environment, "_require_root_owned", lambda _info, _label: None)
    operator_root = tmp_path / "operator"
    monkeypatch.setattr(environment, "WORKLOAD_OPERATOR_ROOT", operator_root)
    private_python = tmp_path / "private-python" / "bin" / "python3.12"
    private_python.parent.mkdir(parents=True)
    private_python.parent.parent.chmod(0o755)
    private_python.parent.chmod(0o755)
    private_python.write_bytes(b"test-only private Python executable")
    private_python.chmod(0o755)
    monkeypatch.setattr(environment, "WORKLOAD_OPERATOR_PYTHON", private_python)

    stage_root = tmp_path / "staging"
    bundle_path = stage_root / "bundle"
    bundle_path.mkdir(parents=True)
    stage_root.chmod(0o755)
    bundle_path.chmod(0o755)
    archive_path = stage_root / "runtime-maintenance-sdk.tar.gz"
    archive_path.write_bytes(b"verified test archive")
    archive_path.chmod(0o644)
    (bundle_path / "sdk-release.json").write_text(
        json.dumps(
            {
                "distribution": environment.WORKLOAD_SDK_DISTRIBUTION,
                "version": "0.1.0",
                "wheel": environment.WORKLOAD_SDK_WHEEL_NAME,
            }
        ),
        encoding="utf-8",
    )
    wheel_path = bundle_path / environment.WORKLOAD_SDK_WHEEL_NAME
    wheel_path.write_bytes(b"test-only wheel bytes")
    wheel_path.chmod(0o644)
    component = {
        "componentId": environment.WORKLOAD_SDK_COMPONENT_ID,
        "artifactKind": environment.WORKLOAD_SDK_ARTIFACT_KIND,
        "targetId": environment.WORKLOAD_SDK_TARGET_ID,
        "version": "0.1.0",
        "releaseId": "v0.1.0",
        "digest": _sha("official archive identity"),
        "manifestDigest": _sha("sdk release manifest"),
        "manifestAssetDigest": _sha("sdk manifest asset"),
        "manifestUri": "https://example.invalid/releases/v0.1.0/sdk-release.json",
        "publisherIdentity": {
            "id": "official-cyrene-workspace-sdk",
            "repository": "DoHorizon-AI/Cyrene-Workspace",
            "workflow": "DoHorizon-AI/Cyrene-Workspace/.github/workflows/sdk.yml",
            "tagFormat": "component-version",
        },
        "indexIdentity": {
            "publisherIdentity": {
                "id": "official-cyrene-workspace-sdk",
                "repository": "DoHorizon-AI/Cyrene-Workspace",
                "workflow": "DoHorizon-AI/Cyrene-Workspace/.github/workflows/sdk.yml",
                "tagFormat": "component-version",
            },
            "repository": "DoHorizon-AI/Cyrene-Workspace",
            "assetName": "component-release-index-v1.json",
            "assetUri": "https://example.invalid/releases/v0.1.0/component-release-index-v1.json",
            "assetDigest": _sha("release index asset"),
            "indexDigest": _sha("release index content"),
            "channel": "stable",
            "releaseTag": "v0.1.0",
        },
        "attestationRef": {
            "repository": "DoHorizon-AI/Cyrene-Workspace",
            "workflow": "DoHorizon-AI/Cyrene-Workspace/.github/workflows/sdk.yml",
            "sourceCommit": "a" * 40,
            "subjectName": "sdk-release.json",
            "subjectDigest": _sha("sdk manifest asset"),
        },
        "verification": {"identityAttested": True},
    }
    staged_identity = {
        "archivePath": str(archive_path),
        "bundlePath": str(bundle_path),
        "wheelPath": str(wheel_path),
        "wheelDigest": _sha(wheel_path.read_bytes().decode("utf-8")),
        "planId": "plan-test-42",
        "planDigest": _sha("plan material"),
    }
    return {
        "component": component,
        "staged": staged_identity,
        "wheel": wheel_path,
        "operatorRoot": operator_root,
        "privatePython": private_python,
    }


def _prepare(values: dict[str, Any], runner: FakeCommandRunner) -> dict[str, Any]:
    """Call the public helper with the verified test handoff."""

    return cast(
        dict[str, Any],
        environment.prepare_workload_sdk_environment(
            values["component"], values["staged"], runner=runner
        ),
    )


def _prepare_alternate_release(values: dict[str, Any], runner: FakeCommandRunner) -> dict[str, Any]:
    """Prepare another immutable SDK release while reusing the verified test wheel."""

    component = json.loads(json.dumps(values["component"]))
    component["digest"] = _sha("alternate official archive identity")
    component["manifestDigest"] = _sha("alternate SDK manifest")
    component["manifestAssetDigest"] = _sha("alternate SDK manifest asset")
    component["indexIdentity"]["assetDigest"] = _sha("alternate release index asset")
    component["indexIdentity"]["indexDigest"] = _sha("alternate release index content")
    component["attestationRef"]["subjectDigest"] = component["manifestAssetDigest"]
    staged_identity = json.loads(json.dumps(values["staged"]))
    staged_identity["planId"] = "plan-test-alternate"
    staged_identity["planDigest"] = _sha("alternate plan material")
    return cast(
        dict[str, Any],
        environment.prepare_workload_sdk_environment(component, staged_identity, runner=runner),
    )


def test_rejects_a_wheel_sha_mismatch_before_running_commands(
    sdk_stage: dict[str, Any],
) -> None:
    """The helper independently hashes the exact staged wheel before install."""

    runner = FakeCommandRunner(sdk_stage["privatePython"])
    sdk_stage["wheel"].write_bytes(b"replacement wheel")

    with pytest.raises(environment.WorkloadSdkEnvironmentError, match="SHA-256"):
        _prepare(sdk_stage, runner)

    assert runner.commands == []
    assert not (sdk_stage["operatorRoot"] / "current").is_symlink()


def test_rejects_selected_sdk_without_explicit_attestation_result(
    sdk_stage: dict[str, Any],
) -> None:
    """The install receipt can only attest a caller-proven selected release row."""

    sdk_stage["component"].pop("verification")
    runner = FakeCommandRunner(sdk_stage["privatePython"])

    with pytest.raises(environment.WorkloadSdkEnvironmentError, match="verification"):
        _prepare(sdk_stage, runner)

    assert runner.commands == []
    assert not (sdk_stage["operatorRoot"] / "current").is_symlink()


def test_rejects_wheel_symlink_pollution(sdk_stage: dict[str, Any], tmp_path: Path) -> None:
    """A matching file reached through a symlink is not an accepted stage input."""

    wheel = sdk_stage["wheel"]
    external_wheel = tmp_path / "external-wheel.whl"
    external_wheel.write_bytes(wheel.read_bytes())
    wheel.unlink()
    wheel.symlink_to(external_wheel)
    runner = FakeCommandRunner(sdk_stage["privatePython"])

    with pytest.raises(environment.WorkloadSdkEnvironmentError, match="symbolic link"):
        _prepare(sdk_stage, runner)

    assert runner.commands == []


def test_repeated_preparation_reuses_the_same_versioned_environment(
    sdk_stage: dict[str, Any],
) -> None:
    """An exact retry does not recreate the venv or reinstall the wheel."""

    runner = FakeCommandRunner(sdk_stage["privatePython"])
    first = _prepare(sdk_stage, runner)
    second = _prepare(sdk_stage, runner)

    assert first == second
    assert sum("venv" in command for command in runner.commands) == 1
    assert sum("pip" in command for command in runner.commands) == 1
    assert (sdk_stage["operatorRoot"] / "current").readlink() == Path("releases") / Path(
        first["releasePath"]
    ).name


def test_interrupted_install_does_not_change_current_pointer(sdk_stage: dict[str, Any]) -> None:
    """A failed pip step leaves the existing active release selected."""

    releases = sdk_stage["operatorRoot"] / "releases"
    old_release = releases / f"0.0.9--{'a' * 64}"
    old_release.mkdir(parents=True)
    old_release.chmod(0o755)
    releases.chmod(0o755)
    sdk_stage["operatorRoot"].chmod(0o755)
    current = sdk_stage["operatorRoot"] / "current"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.symlink_to(Path("releases") / old_release.name)
    runner = FakeCommandRunner(sdk_stage["privatePython"], fail_pip=True)

    with pytest.raises(environment.WorkloadSdkEnvironmentError, match="command failed"):
        _prepare(sdk_stage, runner)

    assert current.readlink() == Path("releases") / old_release.name
    assert sorted(path.name for path in releases.iterdir()) == [old_release.name]


def test_failed_current_switch_restores_the_previous_version_pointer(
    sdk_stage: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-replace activation error restores the previous atomic pointer."""

    releases = sdk_stage["operatorRoot"] / "releases"
    old_release = releases / f"0.0.9--{'a' * 64}"
    old_release.mkdir(parents=True)
    old_release.chmod(0o755)
    releases.chmod(0o755)
    sdk_stage["operatorRoot"].chmod(0o755)
    current = sdk_stage["operatorRoot"] / "current"
    current.parent.mkdir(parents=True, exist_ok=True)
    old_target = Path("releases") / old_release.name
    current.symlink_to(old_target)
    replace = environment._replace_current_link
    switched = False

    def replace_then_fail_once(source: Path, destination: Path) -> None:
        nonlocal switched
        replace(source, destination)
        if not switched:
            switched = True
            raise OSError("simulated directory sync failure after pointer replacement")

    monkeypatch.setattr(environment, "_replace_current_link", replace_then_fail_once)
    runner = FakeCommandRunner(sdk_stage["privatePython"])

    with pytest.raises(environment.WorkloadSdkEnvironmentError, match="previous version restored"):
        _prepare(sdk_stage, runner)

    assert current.readlink() == old_target


def test_returns_exact_source_identity_receipt(sdk_stage: dict[str, Any]) -> None:
    """The updater receipt binds the source plan, manifest, archive, and wheel."""

    result = _prepare(sdk_stage, FakeCommandRunner(sdk_stage["privatePython"]))

    assert result["sourceIdentity"] == {
        "archivePath": sdk_stage["staged"]["archivePath"],
        "bundlePath": sdk_stage["staged"]["bundlePath"],
        "wheelPath": sdk_stage["staged"]["wheelPath"],
        "wheelDigest": sdk_stage["staged"]["wheelDigest"],
        "artifactDigest": sdk_stage["component"]["digest"],
        "manifestDigest": sdk_stage["component"]["manifestDigest"],
        "manifestAssetDigest": sdk_stage["component"]["manifestAssetDigest"],
        "releaseId": sdk_stage["component"]["releaseId"],
        "releaseTag": "v0.1.0",
        "indexIdentity": sdk_stage["component"]["indexIdentity"],
        "publisherIdentity": sdk_stage["component"]["publisherIdentity"],
        "attestationRef": sdk_stage["component"]["attestationRef"],
        "planId": sdk_stage["staged"]["planId"],
        "planDigest": sdk_stage["staged"]["planDigest"],
    }
    assert result["schemaVersion"] == 1
    assert result["digest"] == sdk_stage["component"]["digest"]
    assert result["manifestAssetDigest"] == sdk_stage["component"]["manifestAssetDigest"]
    assert result["wheelDigest"] == sdk_stage["staged"]["wheelDigest"]
    assert result["pythonPath"].endswith("/venv/bin/python")
    receipt = Path(result["releasePath"]) / environment.INSTALL_RECEIPT_NAME
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        "schemaVersion": 1,
        "componentId": environment.WORKLOAD_SDK_COMPONENT_ID,
        "artifactKind": environment.WORKLOAD_SDK_ARTIFACT_KIND,
        "version": "0.1.0",
        "targetId": environment.WORKLOAD_SDK_TARGET_ID,
        "releaseId": sdk_stage["component"]["releaseId"],
        "manifestUri": sdk_stage["component"]["manifestUri"],
        "manifestDigest": sdk_stage["component"]["manifestDigest"],
        "manifestAssetDigest": sdk_stage["component"]["manifestAssetDigest"],
        "digest": sdk_stage["component"]["digest"],
        "artifactDigest": sdk_stage["component"]["digest"],
        "releaseIdentity": sdk_stage["component"]["manifestDigest"],
        "releaseTag": "v0.1.0",
        "indexIdentity": sdk_stage["component"]["indexIdentity"],
        "publisherIdentity": sdk_stage["component"]["publisherIdentity"],
        "attestationRef": sdk_stage["component"]["attestationRef"],
        "wheelDigest": sdk_stage["staged"]["wheelDigest"],
        "releasePath": result["releasePath"],
        "archivePath": sdk_stage["staged"]["archivePath"],
        "pointerIdentity": Path(result["releasePath"]).name,
        "verification": {"identityAttested": True},
        "sourceIdentity": result["sourceIdentity"],
    }


def test_read_helper_returns_actual_attested_current_identity(sdk_stage: dict[str, Any]) -> None:
    """Inventory is projected from the active release receipt, not a journal."""

    installed = _prepare(sdk_stage, FakeCommandRunner(sdk_stage["privatePython"]))

    current = environment.read_workload_sdk_environment()

    assert current is not None
    assert current["installed"] is True
    assert current["active"] is True
    assert current["identityAttested"] is True
    assert current["verification"] == {"identityAttested": True}
    assert current["componentId"] == sdk_stage["component"]["componentId"]
    assert current["version"] == sdk_stage["component"]["version"]
    assert current["targetId"] == sdk_stage["component"]["targetId"]
    assert current["releaseId"] == sdk_stage["component"]["releaseId"]
    assert current["manifestUri"] == sdk_stage["component"]["manifestUri"]
    assert current["releaseIdentity"] == sdk_stage["component"]["manifestDigest"]
    assert current["manifestAssetDigest"] == sdk_stage["component"]["manifestAssetDigest"]
    assert current["digest"] == sdk_stage["component"]["digest"]
    assert current["indexIdentity"] == sdk_stage["component"]["indexIdentity"]
    assert current["publisherIdentity"] == sdk_stage["component"]["publisherIdentity"]
    assert current["attestationRef"] == sdk_stage["component"]["attestationRef"]
    assert current["releasePath"] == installed["releasePath"]
    assert current["pythonPath"] == installed["pythonPath"]


def test_read_helper_does_not_default_false_attestation_to_true(
    sdk_stage: dict[str, Any],
) -> None:
    """A valid receipt's explicit false attestation state remains false."""

    installed = _prepare(sdk_stage, FakeCommandRunner(sdk_stage["privatePython"]))
    receipt_path = Path(installed["releasePath"]) / environment.INSTALL_RECEIPT_NAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["verification"]["identityAttested"] = False
    receipt_path.chmod(0o644)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    receipt_path.chmod(0o444)

    current = environment.read_workload_sdk_environment()

    assert current is not None
    assert current["identityAttested"] is False
    assert current["verification"] == {"identityAttested": False}


def test_read_helper_rejects_inconsistent_release_provenance(sdk_stage: dict[str, Any]) -> None:
    """Inventory rejects receipt provenance that differs across identity fields."""

    installed = _prepare(sdk_stage, FakeCommandRunner(sdk_stage["privatePython"]))
    receipt_path = Path(installed["releasePath"]) / environment.INSTALL_RECEIPT_NAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["publisherIdentity"]["repository"] = "Other/Repository"
    receipt_path.chmod(0o644)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    receipt_path.chmod(0o444)

    with pytest.raises(environment.WorkloadSdkEnvironmentError, match="publisher identities"):
        environment.read_workload_sdk_environment()


def test_root_owner_gate_rejects_non_root_stage_metadata() -> None:
    """Root ownership is independently enforced for production stage files."""

    info = type("StatInfo", (), {"st_uid": 1000, "st_gid": 0})()
    with pytest.raises(environment.WorkloadSdkEnvironmentError, match="owned by root"):
        environment._require_root_owned(info, "test SDK wheel")


def test_restore_returns_to_the_exact_prior_receipt(sdk_stage: dict[str, Any]) -> None:
    """Rollback selects the saved immutable release and keeps both venvs intact."""

    runner = FakeCommandRunner(sdk_stage["privatePython"])
    _prepare(sdk_stage, runner)
    prior = environment.read_workload_sdk_environment()
    assert prior is not None
    expected_current = _prepare_alternate_release(sdk_stage, runner)

    restored = environment.restore_workload_sdk_environment(
        prior, expected_current=expected_current
    )

    assert restored is not None
    assert restored["releasePath"] == prior["releasePath"]
    assert restored["sourceIdentity"] == prior["sourceIdentity"]
    assert restored["active"] is True
    current = sdk_stage["operatorRoot"] / "current"
    assert current.readlink() == Path("releases") / Path(prior["releasePath"]).name
    assert Path(prior["releasePath"]).is_dir()
    assert Path(expected_current["releasePath"]).is_dir()


def test_first_install_restore_removes_only_current_pointer(sdk_stage: dict[str, Any]) -> None:
    """First-install rollback removes current while retaining its immutable release."""

    expected_current = _prepare(sdk_stage, FakeCommandRunner(sdk_stage["privatePython"]))
    release_path = Path(expected_current["releasePath"])
    current = sdk_stage["operatorRoot"] / "current"

    restored = environment.restore_workload_sdk_environment(None, expected_current=expected_current)

    assert restored is None
    assert not current.is_symlink()
    assert not current.exists()
    assert release_path.is_dir()
    assert environment.read_workload_sdk_environment() is None


def test_restore_rejects_a_stale_expected_current_identity(sdk_stage: dict[str, Any]) -> None:
    """A later activation wins over a stale rollback request without pointer changes."""

    runner = FakeCommandRunner(sdk_stage["privatePython"])
    prior = _prepare(sdk_stage, runner)
    expected_current = _prepare_alternate_release(sdk_stage, runner)
    current = sdk_stage["operatorRoot"] / "current"
    active_target = current.readlink()

    with pytest.raises(environment.WorkloadSdkEnvironmentError, match="expected_current"):
        environment.restore_workload_sdk_environment(prior, expected_current=prior)

    assert current.readlink() == active_target
    assert (
        environment.read_workload_sdk_environment()["releasePath"]
        == expected_current["releasePath"]
    )


def test_restore_rejects_invalid_prior_venv_before_switching(
    sdk_stage: dict[str, Any],
) -> None:
    """An incomplete prior interpreter cannot become active during rollback."""

    runner = FakeCommandRunner(sdk_stage["privatePython"])
    prior = _prepare(sdk_stage, runner)
    expected_current = _prepare_alternate_release(sdk_stage, runner)
    current = sdk_stage["operatorRoot"] / "current"
    active_target = current.readlink()
    Path(prior["releasePath"], "venv", "bin", "python").unlink()

    with pytest.raises(environment.WorkloadSdkEnvironmentError, match="SDK venv Python"):
        environment.restore_workload_sdk_environment(prior, expected_current=expected_current)

    assert current.readlink() == active_target


def test_restore_rejects_prior_receipt_changed_after_capture(
    sdk_stage: dict[str, Any],
) -> None:
    """A saved prior identity cannot authorize a different on-disk receipt."""

    runner = FakeCommandRunner(sdk_stage["privatePython"])
    prior = _prepare(sdk_stage, runner)
    expected_current = _prepare_alternate_release(sdk_stage, runner)
    current = sdk_stage["operatorRoot"] / "current"
    active_target = current.readlink()
    receipt_path = Path(prior["releasePath"]) / environment.INSTALL_RECEIPT_NAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["sourceIdentity"]["planId"] = "a-different-plan"
    receipt_path.chmod(0o644)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    receipt_path.chmod(0o444)

    with pytest.raises(environment.WorkloadSdkEnvironmentError, match="prior SDK receipt"):
        environment.restore_workload_sdk_environment(prior, expected_current=expected_current)

    assert current.readlink() == active_target


def test_restore_rejects_symlinked_operator_root(sdk_stage: dict[str, Any]) -> None:
    """Rollback never traverses a substituted operator-root symlink."""

    expected_current = _prepare(sdk_stage, FakeCommandRunner(sdk_stage["privatePython"]))
    operator_root = sdk_stage["operatorRoot"]
    actual_root = operator_root.with_name("operator-real")
    operator_root.rename(actual_root)
    operator_root.symlink_to(actual_root, target_is_directory=True)
    actual_current = actual_root / "current"
    active_target = actual_current.readlink()

    with pytest.raises(environment.WorkloadSdkEnvironmentError, match="symbolic link"):
        environment.restore_workload_sdk_environment(None, expected_current=expected_current)

    assert actual_current.readlink() == active_target


def test_restore_requires_root(sdk_stage: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Only root can compare or change the protected active pointer."""

    expected_current = _prepare(sdk_stage, FakeCommandRunner(sdk_stage["privatePython"]))
    current = sdk_stage["operatorRoot"] / "current"
    active_target = current.readlink()
    monkeypatch.setattr(environment.os, "geteuid", lambda: 1000)

    with pytest.raises(PermissionError, match="requires root"):
        environment.restore_workload_sdk_environment(None, expected_current=expected_current)

    assert current.readlink() == active_target


def test_failed_restore_switch_keeps_the_prepared_version_active(
    sdk_stage: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed atomic replacement restores the plan's prepared pointer."""

    runner = FakeCommandRunner(sdk_stage["privatePython"])
    prior = _prepare(sdk_stage, runner)
    expected_current = _prepare_alternate_release(sdk_stage, runner)
    current = sdk_stage["operatorRoot"] / "current"
    expected_target = current.readlink()
    replace = environment._replace_current_link
    failed = False

    def replace_then_fail_once(source: Path, destination: Path) -> None:
        nonlocal failed
        replace(source, destination)
        if destination == current and not failed:
            failed = True
            raise OSError("simulated rollback directory sync failure")

    monkeypatch.setattr(environment, "_replace_current_link", replace_then_fail_once)

    with pytest.raises(environment.WorkloadSdkEnvironmentError, match="previous version restored"):
        environment.restore_workload_sdk_environment(prior, expected_current=expected_current)

    assert current.readlink() == expected_target
