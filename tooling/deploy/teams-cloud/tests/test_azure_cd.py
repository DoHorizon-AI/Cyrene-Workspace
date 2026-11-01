"""
┌──────────────────────────────────────────────────────────────────────────────┐
│  📄 test_azure_cd.py                                                         │
│  Module: tooling.deploy.teams_cloud.tests.test_azure_cd                     │
│  Role: Verify publisher proof forwarding and guarded Azure release delivery. │
│                                                                              │
│  模块职责：验证发布证明转发、Azure 部署门禁及不确定状态的只读回读。          │
└──────────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import builtins
import copy
import hashlib
import hmac
import io
import json
import os
import pathlib
import subprocess
import sys
import tarfile
import zipfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import azure_cd
import release_bundle


class _Response:
    """Provide a bounded HTTP response compatible with the production urllib reader."""

    def __init__(self, payload: bytes, url: str, *, status: int = 200) -> None:
        self._payload = payload
        self._url = url
        self.status = status
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, limit: int = -1) -> bytes:
        return self._payload if limit < 0 else self._payload[:limit]


def _manifest() -> dict[str, Any]:
    """Build a valid manifest from the shared simulated source fixture."""

    path = Path(__file__).parent / "fixtures/simulated-release-manifest-v1.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    publisher = manifest["publisherRun"]
    publisher.update(runId=500, runAttempt=2, headSha="f" * 40, event="workflow_run")
    manifest["releaseId"] = release_bundle._release_id(
        manifest["sourceRun"]["headSha"], manifest["sourceFingerprintSha256"], publisher
    )
    manifest["releaseTag"] = manifest["releaseId"]
    for image in manifest["images"]:
        source_alias = release_bundle.IMAGE_LABEL_SOURCE[image["service"]]
        image["image"] = (
            f"cyrene/{image['service']}:{manifest['releaseId']}-"
            f"{manifest['sources'][source_alias]['commit'][:12]}"
        )
    release_bundle.validate_release_manifest(manifest)
    return manifest


def _proof(manifest: dict[str, Any]) -> tuple[bytes, dict[str, Any], bytes]:
    """Build the four-file immutable proof archive and its exact pointer."""

    image_lock = b"{}"
    tools_size = manifest["deploymentTools"]["sizeBytes"]
    tools_bytes = b"t" * tools_size
    manifest["imageLock"].update(
        sha256=hashlib.sha256(image_lock).hexdigest(), sizeBytes=len(image_lock)
    )
    manifest["deploymentTools"].update(
        sha256=hashlib.sha256(tools_bytes).hexdigest(), sizeBytes=len(tools_bytes)
    )
    release_bundle.validate_release_manifest(manifest)
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    release_tag = manifest["releaseTag"]
    pointer = {
        "schemaVersion": "cyrene-teams-arm64-release-pointer-v1",
        "publisherRun": manifest["publisherRun"],
        "releaseId": release_tag,
        "releaseTag": release_tag,
        "releaseUrl": f"https://github.com/{azure_cd.WORKSPACE_REPOSITORY}/releases/tag/{release_tag}",
        "sourceFingerprintSha256": manifest["sourceFingerprintSha256"],
        "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "proofArtifactName": release_bundle.proof_artifact_name(manifest),
    }
    pointer_bytes = json.dumps(pointer, sort_keys=True, separators=(",", ":")).encode()
    proof = io.BytesIO()
    with zipfile.ZipFile(proof, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("release-manifest.json", manifest_bytes)
        archive.writestr("image-lock.json", image_lock)
        archive.writestr("deployment-tools.tar.gz", tools_bytes)
        archive.writestr("release-pointer.json", pointer_bytes)
    return proof.getvalue(), pointer, manifest_bytes


def _release_assets(manifest: dict[str, Any], manifest_bytes: bytes) -> list[dict[str, Any]]:
    """Represent immutable release API asset digest fields for every manifest asset."""

    records = [
        {
            "name": "release-manifest.json",
            "digest": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
            "size": len(manifest_bytes),
        },
        {
            "name": "image-lock.json",
            "digest": "sha256:" + manifest["imageLock"]["sha256"],
            "size": manifest["imageLock"]["sizeBytes"],
        },
        {
            "name": "deployment-tools.tar.gz",
            "digest": "sha256:" + manifest["deploymentTools"]["sha256"],
            "size": manifest["deploymentTools"]["sizeBytes"],
        },
    ]
    for image in manifest["images"]:
        asset = image["archive"]
        records.append(
            {
                "name": Path(asset["file"]).name,
                "digest": "sha256:" + asset["sha256"],
                "size": asset["sizeBytes"],
            }
        )
    return records


def _github_run(
    run_id: int,
    attempt: int,
    sha: str,
    event: str,
    workflow: str,
) -> dict[str, Any]:
    """Create GitHub API run metadata for one trusted develop attempt."""

    return {
        "id": run_id,
        "run_attempt": attempt,
        "status": "completed",
        "conclusion": "success",
        "event": event,
        "head_branch": "develop",
        "head_sha": sha,
        "path": workflow,
        "repository": {"id": 77, "full_name": azure_cd.WORKSPACE_REPOSITORY},
        "head_repository": {"id": 77, "full_name": azure_cd.WORKSPACE_REPOSITORY},
    }


class _GitHubApi:
    """Route mocked GitHub API and artifact requests using their exact endpoint paths."""

    def __init__(self, manifest: dict[str, Any]) -> None:
        self.manifest = manifest
        self.archive, self.pointer, self.manifest_bytes = _proof(manifest)
        self.artifact_id = 222
        self.artifact = {
            "id": self.artifact_id,
            "name": release_bundle.proof_artifact_name(manifest),
            "size_in_bytes": len(self.archive),
            "digest": "sha256:" + hashlib.sha256(self.archive).hexdigest(),
            "expired": False,
            "workflow_run": {
                "id": 500,
                "repository_id": 77,
                "head_repository_id": 77,
                "head_branch": "develop",
                "head_sha": "f" * 40,
            },
        }
        self.publisher_run = _github_run(
            500, 2, "f" * 40, "workflow_run", azure_cd.PUBLISHER_WORKFLOW
        )
        source = manifest["sourceRun"]
        self.source_run = _github_run(
            source["runId"],
            source["runAttempt"],
            source["headSha"],
            "push",
            azure_cd.SOURCE_WORKFLOW,
        )
        self.release = {
            "tag_name": manifest["releaseTag"],
            "immutable": True,
            "draft": False,
            "assets": _release_assets(manifest, self.manifest_bytes),
        }
        self.requests: list[str] = []

    def open(self, request: Any, *, timeout: int) -> _Response:
        del timeout
        url = request.full_url
        self.requests.append(url)
        parsed = urlsplit(url)
        if parsed.path.endswith("/actions/runs/500"):
            payload = self.publisher_run
        elif parsed.path.endswith("/actions/runs/500/artifacts"):
            payload = {"total_count": 1, "artifacts": [self.artifact]}
        elif parsed.path.endswith(f"/actions/artifacts/{self.artifact_id}/zip"):
            return _Response(self.archive, url)
        elif parsed.path.endswith(f"/actions/runs/{self.manifest['sourceRun']['runId']}"):
            payload = self.source_run
        elif "/releases/tags/" in parsed.path:
            payload = self.release
        else:
            raise AssertionError(f"unexpected URL {url}")
        return _Response(json.dumps(payload).encode(), "https://api.github.com" + parsed.path)


def _event() -> tuple[dict[str, Any], bytes]:
    """Create the HMAC-authenticated event shape after exact artifact/API checks."""

    manifest = _manifest()
    api = _GitHubApi(manifest)
    resource = azure_cd._fetch_release_proof(
        run_id=500,
        run_attempt=2,
        expected_head_sha="f" * 40,
        expected_event="workflow_run",
        github_token="test-read-token",
        urlopen=api.open,
    )
    assert resource is not None
    return resource, api.manifest_bytes


def _receipt(state: str, release_id: str, manifest_sha: str) -> str:
    """Return one bounded host marker matching the guest receipt contract."""

    value = {
        "schemaVersion": azure_cd.RECEIPT_SCHEMA,
        "releaseId": release_id,
        "manifestSha256": manifest_sha,
        "state": state,
        "steps": [],
        "selection": None,
        "updatedAtUtc": "2026-10-27T12:00:00Z",
        "errorCode": None,
    }
    return "CYRENE_RELEASE_RECEIPT_JSON: " + json.dumps(value)


def test_forwarder_verifies_attempt_artifact_and_every_release_asset() -> None:
    """Forward only when run, proof artifact, source workflow, and release API all agree."""

    manifest = _manifest()
    github = _GitHubApi(manifest)
    received: dict[str, Any] = {}
    secret = "test-only-hmac-secret-with-enough-entropy"

    def webhook_open(request: Any, *, timeout: int) -> _Response:
        del timeout
        received["body"] = request.data
        received["url"] = request.full_url
        received["headerName"] = next(
            key for key in request.headers if key.lower() == "x-cyrene-signature"
        )
        received["signature"] = next(
            value for key, value in request.headers.items() if key.lower() == "x-cyrene-signature"
        )
        return _Response(b"{}", azure_cd.ADO_WEBHOOK_URL, status=202)

    status = azure_cd.forward_github_release(
        run_id=500,
        run_attempt=2,
        head_sha="f" * 40,
        event="workflow_run",
        github_token="test-read-token",
        webhook_secret=secret,
        github_urlopen=github.open,
        webhook_urlopen=webhook_open,
    )

    assert status == "FORWARDED"
    assert received["url"] == (
        "https://dev.azure.com/dohorizon/_apis/public/distributedtask/webhooks/"
        "cyrene-teams-arm64-releases?api-version=6.0-preview"
    )
    assert received["headerName"] == "X-cyrene-signature"
    assert (
        json.loads(received["body"])["resource"]["artifact"]["digest"] == github.artifact["digest"]
    )
    assert (
        received["signature"]
        == hmac.new(secret.encode(), received["body"], hashlib.sha1).hexdigest().upper()
    )
    assert any("/actions/artifacts/222/zip" in url for url in github.requests)
    assert any("/releases/tags/" in url for url in github.requests)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda api: api.publisher_run.update(path=".github/workflows/wrong.yml@refs/heads/develop"),
        lambda api: api.artifact.update(digest="sha256:" + "0" * 64),
        lambda api: api.release.update(immutable=False),
        lambda api: api.release["assets"][0].update(digest="sha256:" + "0" * 64),
    ],
)
def test_forwarder_rejects_mismatched_run_artifact_or_release(mutation: Any) -> None:
    """Every API readback mismatch stops before the Azure DevOps webhook is called."""

    manifest = _manifest()
    github = _GitHubApi(manifest)
    mutation(github)
    calls: list[Any] = []

    with pytest.raises(azure_cd.AzureCdError):
        azure_cd.forward_github_release(
            run_id=500,
            run_attempt=2,
            head_sha="f" * 40,
            event="workflow_run",
            github_token="test-read-token",
            webhook_secret="test-only-hmac-secret-with-enough-entropy",
            github_urlopen=github.open,
            webhook_urlopen=lambda request, **_kwargs: calls.append(request),
        )
    assert calls == []


def test_no_proof_artifact_skips_without_posting() -> None:
    """The publisher's already-published-source path is a quiet no-op for Azure CD."""

    manifest = _manifest()
    github = _GitHubApi(manifest)
    original_open = github.open

    def no_artifacts(request: Any, *, timeout: int) -> _Response:
        url = request.full_url
        if url.endswith("/artifacts?per_page=100"):
            return _Response(
                json.dumps({"total_count": 0, "artifacts": []}).encode(),
                "https://api.github.com" + urlsplit(url).path,
            )
        return original_open(request, timeout=timeout)

    posts: list[Any] = []
    result = azure_cd.forward_github_release(
        run_id=500,
        run_attempt=2,
        head_sha="f" * 40,
        event="workflow_run",
        github_token="test-read-token",
        webhook_secret="test-only-hmac-secret-with-enough-entropy",
        github_urlopen=no_artifacts,
        webhook_urlopen=lambda request, **_kwargs: posts.append(request),
    )

    assert result == "NO_NEW_PROOF"
    assert posts == []


def test_release_event_binds_manifest_artifact_and_api_digests() -> None:
    """The ADO resource must bind exact publisher metadata and the closed manifest."""

    resource, _ = _event()
    evidence = azure_cd.validate_release_event(resource)
    assert evidence.release_id == resource["manifest"]["releaseId"]
    assert evidence.release_base_url.endswith("/" + evidence.release_tag)

    changed = copy.deepcopy(resource)
    changed["release"]["assets"][0]["digest"] = "0" * 64
    with pytest.raises(azure_cd.AzureCdError):
        azure_cd.validate_release_event(changed)


def test_manifest_download_hash_is_checked_before_guest_invocation() -> None:
    """A public asset that differs from the HMAC-bound proof never reaches RunCommand."""

    resource, manifest_bytes = _event()
    evidence = azure_cd.validate_release_event(resource)
    calls: list[str] = []

    def urlopen(_request: Any, *, timeout: int) -> _Response:
        del timeout
        return _Response(
            manifest_bytes + b" ",
            azure_cd._release_manifest_url(evidence.release_tag),
        )

    def run_command(script: str, _timeout: int) -> azure_cd.AzureCommandResult:
        calls.append(script)
        return azure_cd.AzureCommandResult(returncode=0, stdout="")

    with pytest.raises(azure_cd.AzureCdError):
        azure_cd.deploy_verified_release(resource, urlopen=urlopen, run_command=run_command)
    assert calls == []


def test_unknown_deploy_outcome_gets_only_one_read_only_status_call() -> None:
    """An uncertain guest call is followed by one status-only readback."""

    resource, manifest_bytes = _event()
    evidence = azure_cd.validate_release_event(resource)
    calls: list[str] = []

    def urlopen(_request: Any, *, timeout: int) -> _Response:
        del timeout
        return _Response(manifest_bytes, azure_cd._release_manifest_url(evidence.release_tag))

    def run_command(script: str, _timeout: int) -> azure_cd.AzureCommandResult:
        calls.append(script)
        if len(calls) == 1:
            raise TimeoutError("simulated outer command timeout")
        return azure_cd.AzureCommandResult(
            returncode=2,
            stdout=_receipt("RECOVERY_REQUIRED", evidence.release_id, evidence.manifest_sha256),
        )

    outcome = azure_cd.deploy_verified_release(resource, urlopen=urlopen, run_command=run_command)

    assert not outcome.success
    assert outcome.state == "RECOVERY_REQUIRED"
    assert outcome.readback_performed is True
    assert len(calls) == 2
    assert "CYRENE_GUEST_BOOTSTRAP" in calls[0]
    assert "CYRENE_RELEASE_STATUS" in calls[1]
    assert "--release-id" in calls[1]
    assert "--execute" not in calls[1]


def test_applied_guest_receipt_is_still_verified_by_separate_status_readback() -> None:
    """A guest APPLIED receipt is not accepted until a second VM command confirms it."""

    resource, manifest_bytes = _event()
    evidence = azure_cd.validate_release_event(resource)
    calls: list[str] = []

    def urlopen(_request: Any, *, timeout: int) -> _Response:
        del timeout
        return _Response(manifest_bytes, azure_cd._release_manifest_url(evidence.release_tag))

    def run_command(script: str, _timeout: int) -> azure_cd.AzureCommandResult:
        calls.append(script)
        return azure_cd.AzureCommandResult(
            returncode=0,
            stdout=_receipt("APPLIED", evidence.release_id, evidence.manifest_sha256),
        )

    outcome = azure_cd.deploy_verified_release(resource, urlopen=urlopen, run_command=run_command)

    assert outcome.success
    assert outcome.state == "APPLIED"
    assert outcome.readback_performed is True
    assert len(calls) == 2
    assert "CYRENE_GUEST_BOOTSTRAP" in calls[0]
    assert "CYRENE_RELEASE_STATUS" in calls[1]
    assert "--execute" not in calls[1]


def test_status_failure_reports_unknown_without_retrying() -> None:
    """Status errors stay UNKNOWN and the wrapper does not issue a second read."""

    calls: list[str] = []

    def run_command(script: str, _timeout: int) -> azure_cd.AzureCommandResult:
        calls.append(script)
        raise TimeoutError("no response")

    result = azure_cd.read_release_status(
        "teams-arm64-aaaaaaaaaaaa-123456abcdef", run_command=run_command
    )
    assert result.state == "UNKNOWN"
    assert len(calls) == 1
    assert "CYRENE_RELEASE_STATUS" in calls[0]


def test_host_preserves_only_boolean_safe_resume_authorization() -> None:
    """Safe retry status is carried from the canonical guest receipt without coercion."""

    release_id = "teams-arm64-aaaaaaaaaaaa-123456abcdef"
    manifest_sha = "b" * 64
    receipt = json.loads(_receipt("SAFE_TO_RESUME", release_id, manifest_sha).split(":", 1)[1])
    receipt["mayResumeSafely"] = True
    output = "CYRENE_RELEASE_RECEIPT_JSON: " + json.dumps(receipt)
    parsed = azure_cd._receipt_from_azure_output(
        output, release_id=release_id, manifest_sha256=manifest_sha
    )
    assert parsed is not None
    assert parsed["mayResumeSafely"] is True

    receipt["mayResumeSafely"] = "true"
    malformed = "CYRENE_RELEASE_RECEIPT_JSON: " + json.dumps(receipt)
    assert (
        azure_cd._receipt_from_azure_output(
            malformed, release_id=release_id, manifest_sha256=manifest_sha
        )
        is None
    )


def test_guest_scripts_keep_safe_resume_states_and_private_paths() -> None:
    """Guest staging is digest-bound and controller retries require explicit safe status."""

    deployment = azure_cd._guest_bootstrap_body()
    status = azure_cd._guest_status_body()
    compile(deployment, "guest-deployment", "exec")
    compile(status, "guest-status", "exec")
    assert "os.O_EXCL" in deployment
    assert "SAFE_TO_RESUME" in deployment
    assert "ACCEPTANCE_PENDING" in deployment
    assert 'status.get("mayResumeSafely") is not True' in deployment
    assert "EXECUTION_INTENT_WITHOUT_CANONICAL_JOURNAL" in deployment
    assert "--execute" in deployment
    assert "--execute" not in status
    assert "timeout = 5400" in deployment
    assert "deployment-binding.json" not in deployment
    assert "deployment-binding.json" in status
    assert (
        'ROOT_BASE = Path("/var/lib/cyrene-connectors-runtime/.artifacts/releases")' in deployment
    )


class _RootOwnedStat:
    """Make temporary test paths satisfy the guest's root-owned directory checks."""

    def __init__(self, result: os.stat_result) -> None:
        self._result = result

    @property
    def st_uid(self) -> int:
        return 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._result, name)


class _RootOwnedPath(pathlib.PosixPath):
    """Preserve real filesystem operations while simulating root-owned guest files."""

    def stat(self, *, follow_symlinks: bool = True) -> _RootOwnedStat:
        return _RootOwnedStat(super().stat(follow_symlinks=follow_symlinks))

    def lstat(self) -> _RootOwnedStat:
        return _RootOwnedStat(super().lstat())


def _guest_archive(path: Path, manifest: dict[str, Any]) -> bytes:
    """Create a pinned source archive with the actual guest entrypoints and timer units."""

    source_sha = manifest["sources"]["workspace"]["commit"]
    prefix = f"Cyrene-Workspace-{source_sha}/tooling/deploy/teams-cloud"
    source_root = Path(__file__).resolve().parents[1]
    files = (
        "azure_cd.py",
        "release_bundle.py",
        "build_arm64.py",
        "release_deploy.py",
        "cyrene-teams-release-reconcile.service",
        "cyrene-teams-release-reconcile.timer",
    )
    with tarfile.open(path, mode="w:gz") as archive:
        for name in (
            f"Cyrene-Workspace-{source_sha}",
            f"Cyrene-Workspace-{source_sha}/tooling",
            f"Cyrene-Workspace-{source_sha}/tooling/deploy",
            prefix,
        ):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = 0o755
            archive.addfile(member)
        for name in files:
            archive.add(source_root / name, arcname=f"{prefix}/{name}")
    return path.read_bytes()


@pytest.mark.parametrize(
    (
        "initial_state",
        "with_intent",
        "install_state",
        "expected_calls",
        "expected_exit",
    ),
    [
        ("NOT_FOUND", False, "INSTALLED", ["status", "install-reconciler", "deploy"], 0),
        ("SAFE_TO_RESUME", True, "NO_CHANGE", ["status", "install-reconciler", "deploy"], 0),
        ("APPLIED", False, "INSTALLED", ["status"], 0),
        ("IN_PROGRESS", True, "INSTALLED", ["status"], 2),
        ("NOT_FOUND", False, "HELD", ["status", "install-reconciler"], 2),
    ],
)
def test_guest_bootstrap_executes_first_deploy_safe_resume_and_applied_repeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_state: str,
    with_intent: bool,
    install_state: str,
    expected_calls: list[str],
    expected_exit: int,
) -> None:
    """Run the guest body with stubbed network and CLI calls to exercise Path operations."""

    manifest = _manifest()
    tools_root = tmp_path / "test-tools"
    tools_root.mkdir()
    archive_path = tools_root / "deployment-tools.tar.gz"
    archive_bytes = _guest_archive(archive_path, manifest)
    manifest["deploymentTools"].update(
        sha256=hashlib.sha256(archive_bytes).hexdigest(),
        sizeBytes=len(archive_bytes),
    )
    release_bundle.validate_release_manifest(manifest)
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    release_id = manifest["releaseId"]
    guest_root = (
        tmp_path / "guest" / "var" / "lib" / "cyrene-connectors-runtime" / ".artifacts" / "releases"
    )
    guest_root.parents[2].mkdir(mode=0o700, parents=True, exist_ok=True)
    guest_root.parents[2].chmod(0o700)
    policy_path = tmp_path / "guest" / "etc" / "cyrene" / "teams-cloud-deployment.json"
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text("{}", encoding="utf-8")
    release_dir = guest_root / release_id
    if with_intent:
        for directory in (
            guest_root.parents[2],
            guest_root.parents[1],
            guest_root.parent,
            guest_root,
            release_dir,
        ):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        intent = release_dir / "bootstrap-execution-intent.json"
        intent.write_text(
            json.dumps({"releaseId": release_id, "manifestSha256": manifest_sha}),
            encoding="utf-8",
        )
        intent.chmod(0o600)

    config = {
        "releaseId": release_id,
        "releaseTag": release_id,
        "releaseBaseUrl": azure_cd._safe_release_url(release_id),
        "manifestSha256": manifest_sha,
        "deploymentToolsSha256": manifest["deploymentTools"]["sha256"],
        "sourceCommit": manifest["deploymentTools"]["sourceCommit"],
    }
    encoded_config = azure_cd.base64.b64encode(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).decode("ascii")
    calls: list[str] = []
    cli_argv: list[list[str]] = []

    def fake_urlopen(request: Any, *, timeout: int) -> _Response:
        del timeout
        if request.full_url.endswith("/release-manifest.json"):
            return _Response(
                manifest_bytes,
                azure_cd.RELEASE_BASE_PREFIX + release_id + "/release-manifest.json",
            )
        if request.full_url.endswith("/deployment-tools.tar.gz"):
            return _Response(
                archive_bytes,
                azure_cd.RELEASE_BASE_PREFIX + release_id + "/deployment-tools.tar.gz",
            )
        raise AssertionError(f"unexpected guest URL {request.full_url}")

    def fake_cli(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        command = argv[2]
        calls.append(command)
        cli_argv.append(argv)
        if command == "status":
            state = initial_state
        elif command == "install-reconciler":
            state = install_state
        elif command == "deploy":
            state = "APPLIED"
        else:
            raise AssertionError(f"unexpected guest CLI command {command}")
        receipt: dict[str, Any] = {
            "schemaVersion": azure_cd.RECEIPT_SCHEMA,
            "releaseId": release_id,
            "state": state,
            "steps": [],
            "selection": None,
            "updatedAtUtc": "2026-10-27T12:00:00Z",
            "errorCode": None,
            "manifestSha256": "" if state == "NOT_FOUND" else manifest_sha,
        }
        if state == "SAFE_TO_RESUME":
            receipt["mayResumeSafely"] = True
        Path(argv[-1]).write_text(json.dumps(receipt), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=json.dumps(receipt))

    real_import = builtins.__import__

    def mapped_path(value: Any, *parts: str) -> pathlib.Path:
        value_text = os.fspath(value)
        if value_text == "/var/lib/cyrene-connectors-runtime/.artifacts/releases":
            return _RootOwnedPath(guest_root, *parts)
        if value_text == "/etc/cyrene/teams-cloud-deployment.json":
            return _RootOwnedPath(policy_path, *parts)
        return _RootOwnedPath(value, *parts)

    def import_hook(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        module = real_import(name, globals, locals, fromlist, level)
        if name == "pathlib" and level == 0:
            return SimpleNamespace(Path=mapped_path, PurePosixPath=module.PurePosixPath)
        return module

    builtins_namespace = dict(vars(builtins))
    builtins_namespace["__import__"] = import_hook
    namespace = {"__builtins__": builtins_namespace}
    old_release_bundle = sys.modules.pop("release_bundle", None)
    old_build_arm64 = sys.modules.pop("build_arm64", None)
    try:
        with ExitStack() as stack:
            stack.enter_context(patch("urllib.request.urlopen", fake_urlopen))
            stack.enter_context(patch.object(subprocess, "run", fake_cli))
            stack.enter_context(patch.object(sys, "argv", ["guest-bootstrap", encoded_config]))
            stack.enter_context(patch.object(sys, "path", sys.path.copy()))
            with pytest.raises(SystemExit) as raised:
                exec(  # noqa: S102 - exercise the actual guest bootstrap with mocked provider edges.
                    compile(azure_cd._guest_bootstrap_body(), "guest-bootstrap", "exec"), namespace
                )
            assert raised.value.code == expected_exit
    finally:
        sys.modules.pop("release_bundle", None)
        sys.modules.pop("build_arm64", None)
        if old_release_bundle is not None:
            sys.modules["release_bundle"] = old_release_bundle
        if old_build_arm64 is not None:
            sys.modules["build_arm64"] = old_build_arm64

    assert calls == expected_calls
    if "install-reconciler" in calls:
        install_argv = cli_argv[calls.index("install-reconciler")]
        assert Path(install_argv[1]).parent == release_dir
        assert "--expected-manifest-sha256" in install_argv
        assert install_argv[install_argv.index("--expected-manifest-sha256") + 1] == manifest_sha
        assert "--release-id" in install_argv
        assert install_argv[install_argv.index("--release-id") + 1] == release_id
        assert Path(install_argv[-1]).name.startswith("reconciler-install-")
    if expected_calls[-1] == "deploy":
        intent = json.loads((release_dir / "bootstrap-execution-intent.json").read_text())
        assert intent["releaseId"] == release_id
        assert intent["manifestSha256"] == manifest_sha


def test_ado_pipeline_uses_wif_and_checked_bootstrap_without_checkout() -> None:
    """The Azure pipeline consumes the webhook event and verified tools without source checkout."""

    pipeline_path = Path(__file__).resolve().parents[4] / "ci/teams-cloud-cd.yml"
    pipeline = yaml.safe_load(pipeline_path.read_text(encoding="utf-8"))
    job = pipeline["jobs"][0]
    assert job["timeoutInMinutes"] == 70
    steps = job["steps"]
    assert steps[0] == {"checkout": "none"}
    task = steps[1]
    assert task["task"] == "AzureCLI@2"
    assert task["timeoutInMinutes"] == 60
    assert task["inputs"]["azureSubscription"] == "cyrene-teams-vm-arm-wif"
    assert task["inputs"]["useGlobalConfig"] is False
    script = task["inputs"]["inlineScript"]
    body = script.split("python3 - <<'CYRENE_ADO_BOOTSTRAP'\n", 1)[1].split(
        "\nCYRENE_ADO_BOOTSTRAP", 1
    )[0]
    assert body.strip() == azure_cd.ADO_BOOTSTRAP_PYTHON.strip()
    compile(body, "ADO-bootstrap", "exec")
    assert "convertToJson(parameters.teamsCloudRelease.resource)" in str(task["env"])
    assert "docker build" not in script.lower()


@pytest.mark.parametrize("with_link", [False, True])
def test_ado_bootstrap_accepts_only_safe_archive_prefix_members(with_link: bool) -> None:
    """The trusted tar prefix may include its git-archive directory ancestors, never links."""

    source_sha = "a" * 40
    release_id = "teams-arm64-aaaaaaaaaaaa-123456abcdef"
    prefix = f"Cyrene-Workspace-{source_sha}/tooling/deploy/teams-cloud"
    raw_archive = io.BytesIO()
    with tarfile.open(fileobj=raw_archive, mode="w:gz") as archive:
        for name in (
            f"Cyrene-Workspace-{source_sha}",
            f"Cyrene-Workspace-{source_sha}/tooling",
            f"Cyrene-Workspace-{source_sha}/tooling/deploy",
            prefix,
        ):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = 0o755
            archive.addfile(member)
        for name in ("azure_cd.py", "release_bundle.py", "build_arm64.py"):
            payload = b"pass\n"
            member = tarfile.TarInfo(f"{prefix}/{name}")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        if with_link:
            member = tarfile.TarInfo(f"{prefix}/unsafe-link")
            member.type = tarfile.SYMTYPE
            member.linkname = "../../outside"
            archive.addfile(member)
    archive_bytes = raw_archive.getvalue()
    resource = {
        "schemaVersion": azure_cd.EVENT_SCHEMA,
        "release": {
            "releaseId": release_id,
            "releaseTag": release_id,
            "releaseBaseUrl": azure_cd.RELEASE_BASE_PREFIX + release_id,
            "manifestSha256": "b" * 64,
        },
        "manifest": {
            "releaseId": release_id,
            "releaseTag": release_id,
            "sourceRun": {"headSha": source_sha},
            "sources": {"workspace": {"commit": source_sha}},
            "deploymentTools": {
                "file": "deployment-tools.tar.gz",
                "sourceCommit": source_sha,
                "sha256": hashlib.sha256(archive_bytes).hexdigest(),
                "sizeBytes": len(archive_bytes),
            },
        },
    }
    command_calls: list[list[str]] = []

    def fake_open(_request: Any, *, timeout: int) -> _Response:
        del timeout
        return _Response(
            archive_bytes,
            azure_cd.RELEASE_BASE_PREFIX + release_id + "/deployment-tools.tar.gz",
        )

    def fake_run(argv: list[str], *, check: bool = False) -> SimpleNamespace:
        assert check is False
        command_calls.append(argv)
        return SimpleNamespace(returncode=0)

    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"CYRENE_EVENT_JSON": json.dumps(resource)}))
        stack.enter_context(patch("urllib.request.urlopen", fake_open))
        stack.enter_context(patch.object(subprocess, "run", fake_run))
        if with_link:
            with pytest.raises(SystemExit):
                exec(compile(azure_cd.ADO_BOOTSTRAP_PYTHON, "ADO bootstrap", "exec"), {})  # noqa: S102
        else:
            with pytest.raises(SystemExit) as raised:
                exec(compile(azure_cd.ADO_BOOTSTRAP_PYTHON, "ADO bootstrap", "exec"), {})  # noqa: S102
            assert raised.value.code == 0

    if with_link:
        assert command_calls == []
    else:
        assert len(command_calls) == 1
        assert command_calls[0][1].endswith("/azure_cd.py")
        assert command_calls[0][2:] == ["deploy", "--event-json", command_calls[0][-1]]


def test_github_bridge_is_manual_only_behind_its_disabled_by_default_gate() -> None:
    """The bridge is manual-only and remains behind its disabled-by-default gate."""

    workflow_path = Path(__file__).resolve().parents[4] / ".github/workflows/teams-cloud-cd.yml"
    workflow = yaml.load(workflow_path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {
        "publisher_run_id",
        "publisher_run_attempt",
        "publisher_head_sha",
        "publisher_event",
    }
    assert inputs["publisher_run_id"]["type"] == "number"
    assert inputs["publisher_run_attempt"]["type"] == "number"
    assert all(spec["required"] == "true" for spec in inputs.values())
    assert inputs["publisher_event"]["options"] == [
        "workflow_run",
        "schedule",
        "workflow_dispatch",
    ]
    assert workflow["permissions"] == {"actions": "read", "contents": "read"}
    job = workflow["jobs"]["forward-verified-release"]
    assert "github.event_name == 'workflow_dispatch'" in job["if"]
    assert "CYRENE_TEAMS_AZDO_CD_ENABLED == 'true'" in job["if"]
    assert job["steps"][0]["with"]["persist-credentials"] == "false"
    publisher_env = job["steps"][2]["env"]
    assert publisher_env["PUBLISHER_RUN_ID"] == "${{ inputs.publisher_run_id }}"
    assert publisher_env["PUBLISHER_RUN_ATTEMPT"] == "${{ inputs.publisher_run_attempt }}"
    assert publisher_env["PUBLISHER_HEAD_SHA"] == "${{ inputs.publisher_head_sha }}"
    assert publisher_env["PUBLISHER_EVENT"] == "${{ inputs.publisher_event }}"
    assert "azure_cd.py bridge" in job["steps"][2]["run"]


@pytest.mark.parametrize("suffix", ["", "@refs/heads/develop"])
def test_trusted_run_accepts_official_api_workflow_path_forms(suffix: str) -> None:
    """The live REST shape stays bound to exact repository, branch and attempt."""

    run = _github_run(500, 2, "f" * 40, "workflow_run", azure_cd.PUBLISHER_WORKFLOW)
    run["path"] = azure_cd.PUBLISHER_WORKFLOW + suffix
    kwargs = {
        "workflow": azure_cd.PUBLISHER_WORKFLOW,
        "event": "workflow_run",
        "run_id": 500,
        "run_attempt": 2,
        "head_sha": "f" * 40,
    }
    assert azure_cd._trusted_run(run, **kwargs)
    run["head_branch"] = "feature/untrusted"
    assert not azure_cd._trusted_run(run, **kwargs)
    run["head_branch"] = "develop"
    run["ref"] = "refs/heads/main"
    assert not azure_cd._trusted_run(run, **kwargs)
    run.pop("ref")
    run["path"] = azure_cd.PUBLISHER_WORKFLOW + "@refs/heads/main"
    assert not azure_cd._trusted_run(run, **kwargs)


def test_artifact_storage_redirect_does_not_receive_github_api_credentials() -> None:
    """Signed Azure storage rejects the API Bearer header and must never receive it."""

    request = azure_cd.urllib.request.Request(
        "https://api.github.com/repos/DoHorizon-AI/Cyrene-Workspace/actions/artifacts/1/zip",
        headers={"Authorization": "Bearer unit-test-only", "User-Agent": "Cyrene-Test"},
    )
    handler = azure_cd._GitHubArtifactRedirectHandler()
    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://productionresultssa5.blob.core.windows.net/artifacts/proof.zip?unit-test=signed",
    )
    assert redirected.get_header("Authorization") is None
    assert redirected.get_header("User-agent") == "Cyrene-Test"
    with pytest.raises(azure_cd.AzureCdError, match="outside trusted HTTPS storage"):
        handler.redirect_request(
            request, None, 302, "Found", {}, "https://example.invalid/proof.zip"
        )
