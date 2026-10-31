"""
┌──────────────────────────────────────────────────────────────────────────────┐
│  📄 test_release_bundle.py                                                   │
│  Module: tooling.deploy.teams_cloud.tests.test_release_bundle               │
│  Role: Verify trusted ARM64 release plans, manifests, and streamed assets.   │
│                                                                              │
│  模块职责：验证可信 ARM64 发布计划、清单及流式资产。                        │
└──────────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path
from typing import Any, Self

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_bundle


class _FakeHttpResponse(io.BytesIO):
    """Minimal bounded response object for request-boundary tests."""

    status = 200

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _source_records() -> tuple[dict[str, Any], dict[str, Any]]:
    """Build deterministic green workflow pins for the closed runtime source set."""

    commits = {
        "workspace": "a" * 40,
        "navigator": "b" * 40,
        "exchange": "c" * 40,
        "plugins": "d" * 40,
        "dsh": "e" * 40,
    }
    sources: dict[str, Any] = {}
    for index, alias in enumerate(("workspace", "navigator", "exchange", "plugins"), start=11):
        repository, workflow = release_bundle.SOURCE_WORKFLOWS[alias]
        sources[alias] = {
            "repository": repository,
            "workflow": workflow,
            "workflowRunId": index,
            "workflowRunAttempt": 1,
            "ref": release_bundle.DEVELOP_REF,
            "commit": commits[alias],
            "event": "push",
            "conclusion": "success",
        }
    sources["dsh"] = {
        "repository": "deepseek-ai/deepseek-harness",
        "workflow": None,
        "workflowRunId": None,
        "workflowRunAttempt": None,
        "ref": "refs/tags/dsh-v0.2.0-rc.2",
        "commit": commits["dsh"],
        "event": "tag",
        "conclusion": "pinned",
    }
    source_run = {
        "repository": release_bundle.WORKSPACE_REPOSITORY,
        "workflow": release_bundle.WORKSPACE_CI_WORKFLOW,
        "runId": sources["workspace"]["workflowRunId"],
        "runAttempt": 1,
        "headSha": commits["workspace"],
        "ref": release_bundle.DEVELOP_REF,
        "event": "push",
        "conclusion": "success",
    }
    return sources, source_run


def _manifest() -> dict[str, Any]:
    """Create a pure manifest fixture with every source and image pin bound."""

    sources, source_run = _source_records()
    fingerprint = release_bundle._source_fingerprint(sources)
    publisher = {
        "repository": release_bundle.WORKSPACE_REPOSITORY,
        "workflow": release_bundle.RELEASE_WORKFLOW,
        "runId": 500,
        "runAttempt": 1,
        "headSha": "f" * 40,
        "event": "workflow_run",
    }
    release_id = release_bundle._release_id(source_run["headSha"], fingerprint, publisher)
    images = []
    for index, service in enumerate(release_bundle.IMAGE_SERVICES, start=1):
        aliases = release_bundle.IMAGE_SOURCE_ALIASES[service]
        source_commits = {alias: sources[alias]["commit"] for alias in aliases}
        label_source = release_bundle.IMAGE_LABEL_SOURCE[service]
        images.append(
            {
                "service": service,
                "image": f"cyrene/{service}:{release_id}-{sources[label_source]['commit'][:12]}",
                "imageId": "sha256:" + f"{index:064x}",
                "platform": "linux/arm64",
                "sourceCommits": source_commits,
                "archive": {
                    "file": f"images/{service}.docker.tar.gz",
                    "sha256": hashlib.sha256(service.encode()).hexdigest(),
                    "sizeBytes": 200 + index,
                },
            }
        )
    return {
        "schemaVersion": release_bundle.SCHEMA_VERSION,
        "releaseId": release_id,
        "releaseTag": release_id,
        "targetPlatform": "linux/arm64",
        "publisherRun": publisher,
        "sourceRun": source_run,
        "sourceFingerprintSha256": fingerprint,
        "sources": sources,
        "imageLock": {
            "file": "image-lock.json",
            "sha256": "1" * 64,
            "sizeBytes": 1000,
        },
        "deploymentTools": {
            "file": "deployment-tools.tar.gz",
            "sha256": "2" * 64,
            "sizeBytes": 1000,
            "sourceCommit": source_run["headSha"],
        },
        "images": images,
        "createdAtUtc": "2026-10-27T16:00:00Z",
    }


def _write_tools_archive(
    path: Path,
    workspace_sha: str,
    *,
    omit: set[str] | None = None,
    automation: bool = False,
    office_graph: bool = False,
    antigravity_acp: bool = False,
) -> None:
    """Create a git-archive-shaped fixture with ancestor directories and required tools."""

    source_root = f"Cyrene-Workspace-{workspace_sha}"
    prefix = f"{source_root}/tooling/deploy/teams-cloud/"
    omitted = omit or set()
    with (
        path.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive,
    ):
        for directory in (
            source_root,
            source_root + "/tooling",
            source_root + "/tooling/deploy",
            prefix.rstrip("/"),
        ):
            info = tarfile.TarInfo(directory + "/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.size = 0
            archive.addfile(info)
        members = release_bundle.REQUIRED_DEPLOYMENT_TOOL_FILES
        if automation:
            members |= release_bundle.AUTOMATION_DEPLOYMENT_TOOL_FILES
        if office_graph:
            members |= release_bundle.OFFICE_GRAPH_DEPLOYMENT_TOOL_FILES
        if antigravity_acp:
            members |= release_bundle.ANTIGRAVITY_ACP_DEPLOYMENT_TOOL_FILES
        for name in sorted(members - omitted):
            content = f"fixture:{name}\n".encode()
            info = tarfile.TarInfo(prefix + name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


def _write_bundle(root: Path) -> dict[str, Any]:
    """Write a complete digest-bound fixture bundle for verifier tests."""

    root.mkdir(parents=True)
    manifest = _manifest()
    lock_sources = {
        alias: {
            "remote": release_bundle.SOURCE_REMOTES[alias],
            "commit": source["commit"],
            "tree": hashlib.sha1((alias + "-tree").encode()).hexdigest(),
        }
        for alias, source in manifest["sources"].items()
    }
    lock_images = []
    for image in manifest["images"]:
        lock_images.append(
            {
                "service": image["service"],
                "image": image["image"],
                "imageId": image["imageId"],
                "platform": "linux/arm64",
                "sizeBytes": 1024,
                "sourceCommits": image["sourceCommits"],
                "registryDigest": None,
            }
        )
        archive_path = root / image["archive"]["file"]
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(f"archive:{image['service']}".encode())
        image["archive"]["sha256"] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        image["archive"]["sizeBytes"] = archive_path.stat().st_size
    lock = {
        "schemaVersion": "cyrene-teams-cloud-image-lock-v1",
        "taskId": manifest["releaseId"],
        "targetPlatform": "linux/arm64",
        "state": "LOCAL_ONLY",
        "createdAt": "2026-10-27T16:00:00Z",
        "planSha256": "3" * 64,
        "sources": lock_sources,
        "binaryArtifacts": [],
        "baseImages": {},
        "externalImages": {},
        "images": lock_images,
    }
    lock_path = root / "image-lock.json"
    lock_path.write_text(json.dumps(lock, sort_keys=True), encoding="utf-8")
    manifest["imageLock"] = {
        "file": lock_path.name,
        "sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        "sizeBytes": lock_path.stat().st_size,
    }
    tools_path = root / "deployment-tools.tar.gz"
    _write_tools_archive(tools_path, manifest["sourceRun"]["headSha"])
    manifest["deploymentTools"].update(
        {
            "sha256": hashlib.sha256(tools_path.read_bytes()).hexdigest(),
            "sizeBytes": tools_path.stat().st_size,
        }
    )
    (root / "release-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    return manifest


def test_prepare_pins_only_six_codebuddy_runtime_images(tmp_path: Path) -> None:
    """Generate a public-safe plan from green source SHAs without the local AGY asset."""

    sources, source_run = _source_records()
    pins = {
        "schemaVersion": release_bundle.SOURCE_PINS_SCHEMA_VERSION,
        "sourceRun": source_run,
        "sources": {alias: row for alias, row in sources.items() if alias != "dsh"},
    }
    pins_path = tmp_path / "source-pins.json"
    pins_path.write_text(json.dumps(pins), encoding="utf-8")
    artifact_root = tmp_path / ".artifacts" / "teams-release"

    result = release_bundle.prepare_release(
        artifact_root,
        pins_path,
        publisher_run_id=900,
        publisher_run_attempt=1,
        publisher_head_sha="f" * 40,
        publisher_event="workflow_dispatch",
    )

    assert set(result["services"]) == set(release_bundle.IMAGE_SERVICES)
    plan = json.loads((artifact_root / ".private-build-plan.json").read_text())
    assert {row["service"] for row in plan["images"]} == set(release_bundle.IMAGE_SERVICES)
    assert plan["externalImages"] == {}
    assert "binaryArtifacts" not in plan
    assert set(plan["sources"]) == {"workspace", "navigator", "exchange", "plugins", "dsh"}
    dsh_image = next(row for row in plan["images"] if row["service"] == "dsh-executor")
    assert dsh_image["dockerfile"].endswith("Dockerfile.dsh-executor-codebuddy")
    assert "binaryArtifacts" not in dsh_image
    assert "sourcePath" not in json.dumps(plan)
    assert result["releaseTag"] == result["releaseId"]


def test_cli_prepare_subprocess_writes_plan_and_source_pins(
    tmp_path: Path,
) -> None:
    """Execute the public CLI in a fresh interpreter without Docker or mocked main."""

    sources, source_run = _source_records()
    pins = {
        "schemaVersion": release_bundle.SOURCE_PINS_SCHEMA_VERSION,
        "sourceRun": source_run,
        "sources": {alias: row for alias, row in sources.items() if alias != "dsh"},
    }
    pins_path = tmp_path / "source-pins.json"
    pins_path.write_text(json.dumps(pins, sort_keys=True), encoding="utf-8")
    output_dir = tmp_path / ".artifacts" / "cli-prepare"
    script = Path(release_bundle.__file__).resolve()

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "prepare",
            "--source-pins",
            str(pins_path),
            "--output-dir",
            str(output_dir),
            "--publisher-run-id",
            "909",
            "--publisher-run-attempt",
            "2",
            "--publisher-head-sha",
            "f" * 40,
            "--publisher-event",
            "workflow_dispatch",
        ],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    assert summary["status"] == "PREPARED"
    assert summary["imageCount"] == 6
    assert set(summary["services"]) == set(release_bundle.IMAGE_SERVICES)
    plan = json.loads((output_dir / ".private-build-plan.json").read_text(encoding="utf-8"))
    source_lock = json.loads((output_dir / ".private-source-pins.json").read_text(encoding="utf-8"))
    context = json.loads((output_dir / ".private-context.json").read_text(encoding="utf-8"))
    assert {row["service"] for row in plan["images"]} == set(release_bundle.IMAGE_SERVICES)
    assert plan["externalImages"] == {}
    assert source_lock["releaseId"] == summary["releaseId"]
    assert source_lock["publisherRun"]["runId"] == 909
    assert context["releaseId"] == summary["releaseId"]
    assert context["publisherRun"]["runAttempt"] == 2
    assert "sourcePath" not in json.dumps(plan)


def test_source_resolution_rejects_pull_request_event(tmp_path: Path) -> None:
    """Only a successful canonical develop push or controlled lookup is accepted."""

    event_path = tmp_path / "event.json"
    event_path.write_text("{}", encoding="utf-8")
    with pytest.raises(release_bundle.ReleaseBundleError, match="trusted workflow_run"):
        release_bundle.resolve_source_pins("pull_request", event_path)


def test_manual_retry_selects_green_refs_without_accepting_source_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual rerun uses only latest canonical green pins, never caller-supplied SHAs."""

    source_run = release_bundle.SourceRun(
        repository=release_bundle.WORKSPACE_REPOSITORY,
        workflow=release_bundle.WORKSPACE_CI_WORKFLOW,
        run_id=500,
        run_attempt=1,
        head_sha="a" * 40,
    )
    pinned_runs = {
        alias: release_bundle.SourceRun(
            repository=repository,
            workflow=workflow,
            run_id=510 + index,
            run_attempt=1,
            head_sha=chr(ord("b") + index) * 40,
        )
        for index, (alias, (repository, workflow)) in enumerate(
            release_bundle.SOURCE_WORKFLOWS.items()
        )
    }

    def latest(
        repository: str, workflow: str, *, github_token: str | None = None
    ) -> release_bundle.SourceRun:
        assert github_token == "test-github-token"
        if repository == release_bundle.WORKSPACE_REPOSITORY:
            return source_run
        return next(
            run
            for run in pinned_runs.values()
            if run.repository == repository and run.workflow == workflow
        )

    monkeypatch.setattr(release_bundle, "_latest_green_run", latest)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"sourceSha": "f" * 40}), encoding="utf-8")

    pins = release_bundle.resolve_source_pins(
        "workflow_dispatch", event_path, github_token="test-github-token"
    )

    assert pins["sourceRun"]["headSha"] == "a" * 40
    assert pins["sources"]["navigator"]["commit"] == "c" * 40
    assert all(row["commit"] != "f" * 40 for row in pins["sources"].values())


def test_source_resolution_uses_only_green_same_repository_develop_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolve the triggering Workspace run and fixed upstream CI workflows by API identity."""

    commits = {alias: letter * 40 for alias, letter in zip(release_bundle.SOURCE_WORKFLOWS, "abcd")}

    def run_row(alias: str, run_id: int) -> dict[str, Any]:
        repository, workflow = release_bundle.SOURCE_WORKFLOWS[alias]
        return {
            "id": run_id,
            "run_attempt": 1,
            "path": workflow,
            "head_branch": "develop",
            "head_sha": commits[alias],
            "event": "push",
            "status": "completed",
            "conclusion": "success",
            "head_repository": {"full_name": repository},
        }

    rows = {alias: run_row(alias, 100 + index) for index, alias in enumerate(commits)}

    seen_tokens: list[str | None] = []

    def fake_api(url: str, token: str | None = None) -> dict[str, Any]:
        seen_tokens.append(token)
        for alias, (repository, _workflow) in release_bundle.SOURCE_WORKFLOWS.items():
            if repository in url:
                if "/branches/develop" in url:
                    return {"commit": {"sha": commits[alias]}}
                return {"workflow_runs": [rows[alias]]}
        raise AssertionError("unexpected public source API route")

    monkeypatch.setattr(release_bundle, "_public_api_json", fake_api)
    workspace_row = rows["workspace"]
    event = {
        "repository": {"full_name": release_bundle.WORKSPACE_REPOSITORY},
        "workflow_run": {
            **workspace_row,
            "repository": {"full_name": release_bundle.WORKSPACE_REPOSITORY},
            "pull_requests": [],
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")

    pins = release_bundle.resolve_source_pins(
        "workflow_run", event_path, github_token="test-github-token"
    )

    assert pins["sourceRun"]["runId"] == workspace_row["id"]
    assert pins["sources"]["navigator"]["commit"] == commits["navigator"]
    assert pins["sources"]["plugins"]["conclusion"] == "success"
    assert "dsh" not in pins["sources"]
    assert seen_tokens and set(seen_tokens) == {"test-github-token"}

    event["workflow_run"]["head_repository"] = {"full_name": "fork/Workspace"}
    event_path.write_text(json.dumps(event), encoding="utf-8")
    with pytest.raises(release_bundle.ReleaseBundleError, match="same-repository push"):
        release_bundle.resolve_source_pins("workflow_run", event_path)


def test_manifest_validator_binds_sources_ids_and_flat_release_assets() -> None:
    """Pure validation supports streaming download without staging image archives."""

    manifest = _manifest()
    result = release_bundle.validate_release_manifest(manifest)

    assert result["status"] == "VERIFIED"
    assert result["imageCount"] == 6
    assert len(result["assets"]) == 9
    artifact_name = release_bundle.proof_artifact_name(manifest)
    assert artifact_name == "cyrene-" + manifest["releaseId"]

    bad_source = json.loads(json.dumps(manifest))
    bad_source["sources"]["plugins"]["repository"] = "attacker/Plugins"
    with pytest.raises(release_bundle.ReleaseBundleError, match="green canonical develop push"):
        release_bundle.validate_release_manifest(bad_source)

    bad_assets = json.loads(json.dumps(manifest))
    bad_assets["images"][1]["archive"]["file"] = "images/../outside.tar.gz"
    with pytest.raises(release_bundle.ReleaseBundleError, match="archive record is malformed"):
        release_bundle.validate_release_manifest(bad_assets)


def test_publication_identity_changes_per_attempt_without_changing_source_fingerprint() -> None:
    """A failed immutable publication can be retried with a new run-bound tag."""

    manifest = _manifest()
    fingerprint = manifest["sourceFingerprintSha256"]
    first_release_id = manifest["releaseId"]
    second_publisher = dict(manifest["publisherRun"], runAttempt=2)
    second_release_id = release_bundle._release_id(
        manifest["sourceRun"]["headSha"], fingerprint, second_publisher
    )

    assert release_bundle._source_fingerprint(manifest["sources"]) == fingerprint
    assert second_release_id != first_release_id

    second_attempt = json.loads(json.dumps(manifest))
    second_attempt["publisherRun"]["runAttempt"] = 2
    second_attempt["releaseId"] = second_release_id
    second_attempt["releaseTag"] = second_release_id
    for image in second_attempt["images"]:
        source_alias = release_bundle.IMAGE_LABEL_SOURCE[image["service"]]
        image["image"] = (
            f"cyrene/{image['service']}:{second_release_id}-"
            f"{second_attempt['sources'][source_alias]['commit'][:12]}"
        )
    assert (
        release_bundle.validate_release_manifest(second_attempt)["releaseId"] == second_release_id
    )


def test_latest_green_run_fails_if_current_develop_head_is_not_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A previous green commit cannot be silently selected while develop has advanced."""

    repository, workflow = release_bundle.SOURCE_WORKFLOWS["navigator"]
    old_row = {
        "id": 700,
        "run_attempt": 1,
        "path": workflow,
        "head_branch": "develop",
        "head_sha": "a" * 40,
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_repository": {"full_name": repository},
    }

    def fake_api(url: str, _token: str | None = None) -> dict[str, Any]:
        if "/branches/develop" in url:
            return {"commit": {"sha": "b" * 40}}
        return {"workflow_runs": [old_row]}

    monkeypatch.setattr(release_bundle, "_public_api_json", fake_api)
    with pytest.raises(
        release_bundle.ReleaseBundleError, match="current develop head has no successful"
    ):
        release_bundle._latest_green_run(repository, workflow)


def test_latest_green_run_does_not_fall_back_past_failed_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer failed run on the current head blocks fallback to an older green run."""

    repository, workflow = release_bundle.SOURCE_WORKFLOWS["plugins"]
    latest = {
        "id": 702,
        "run_attempt": 1,
        "path": workflow,
        "head_branch": "develop",
        "head_sha": "c" * 40,
        "event": "push",
        "status": "completed",
        "conclusion": "failure",
        "head_repository": {"full_name": repository},
    }
    previous_green = dict(latest, id=701, conclusion="success")

    def fake_api(url: str, _token: str | None = None) -> dict[str, Any]:
        if "/branches/develop" in url:
            return {"commit": {"sha": "c" * 40}}
        return {"workflow_runs": [latest, previous_green]}

    monkeypatch.setattr(release_bundle, "_public_api_json", fake_api)
    with pytest.raises(release_bundle.ReleaseBundleError, match="not a green develop push"):
        release_bundle._latest_green_run(repository, workflow)


def test_github_api_token_is_limited_to_canonical_origin_and_redirects_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source lookups authenticate only to api.github.com and never follow redirects."""

    captured: list[urllib.request.Request] = []
    handlers: list[Any] = []

    class _FakeOpener:
        def open(self, request: urllib.request.Request, *, timeout: int) -> _FakeHttpResponse:
            assert timeout == 20
            captured.append(request)
            return _FakeHttpResponse(b'{"commit":{"sha":"' + b"a" * 40 + b'"}}')

    def build_opener(*rows: Any) -> _FakeOpener:
        handlers.extend(rows)
        return _FakeOpener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    value = release_bundle._public_api_json(
        "https://api.github.com/repos/DoHorizon-AI/Cyrene-Workspace/branches/develop",
        "short-lived-test-token",
    )

    assert value["commit"]["sha"] == "a" * 40
    assert len(captured) == 1
    assert captured[0].full_url.startswith("https://api.github.com/")
    assert captured[0].get_header("Authorization") == "Bearer short-lived-test-token"
    assert len(handlers) == 1
    assert isinstance(handlers[0], release_bundle._RejectApiRedirects)
    assert (
        handlers[0].redirect_request(
            captured[0], None, 302, "Found", {}, "https://attacker.example/collect"
        )
        is None
    )

    with pytest.raises(release_bundle.ReleaseBundleError, match="fixed trusted origin"):
        release_bundle._public_api_json(
            "https://attacker.example/repos/DoHorizon-AI/Cyrene-Workspace/branches/develop",
            "short-lived-test-token",
        )
    assert len(captured) == 1


def test_public_release_asset_download_never_receives_api_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public release assets are fetched separately without an Authorization header."""

    captured: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, *, timeout: int) -> _FakeHttpResponse:
        assert timeout == 30
        captured.append(request)
        return _FakeHttpResponse(b"public manifest")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    payload = release_bundle._public_asset_bytes(
        "https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/download/"
        "teams-arm64-aaaaaaaaaaaa-bbbbbbbbbbbb/release-manifest.json",
        maximum_bytes=1024,
    )

    assert payload == b"public manifest"
    assert len(captured) == 1
    assert captured[0].get_header("Authorization") is None


def test_publication_decision_requires_matching_successful_run_and_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a digest-bound release with its exact successful proof artifact is skipped."""

    manifest = _manifest()
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    asset_digests = {
        "release-manifest.json": (hashlib.sha256(manifest_bytes).hexdigest(), len(manifest_bytes)),
        manifest["imageLock"]["file"]: (
            manifest["imageLock"]["sha256"],
            manifest["imageLock"]["sizeBytes"],
        ),
        manifest["deploymentTools"]["file"]: (
            manifest["deploymentTools"]["sha256"],
            manifest["deploymentTools"]["sizeBytes"],
        ),
    }
    for image in manifest["images"]:
        asset_digests[Path(image["archive"]["file"]).name] = (
            image["archive"]["sha256"],
            image["archive"]["sizeBytes"],
        )
    release = {
        "tag_name": manifest["releaseTag"],
        "immutable": True,
        "draft": False,
        "body": "release\nsourceFingerprintSha256=" + manifest["sourceFingerprintSha256"],
        "assets": [
            {"name": name, "digest": "sha256:" + digest, "size": size}
            for name, (digest, size) in asset_digests.items()
        ],
    }
    publisher = manifest["publisherRun"]
    run = {
        "id": publisher["runId"],
        "run_attempt": publisher["runAttempt"],
        "path": release_bundle.RELEASE_WORKFLOW,
        "head_branch": "develop",
        "head_sha": publisher["headSha"],
        "event": publisher["event"],
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": release_bundle.WORKSPACE_REPOSITORY},
        "head_repository": {"full_name": release_bundle.WORKSPACE_REPOSITORY},
    }
    artifact = {
        "name": release_bundle.proof_artifact_name(manifest),
        "expired": False,
        "size_in_bytes": 4096,
        "digest": "sha256:" + "a" * 64,
        "workflow_run": {
            "id": publisher["runId"],
            "head_sha": publisher["headSha"],
            "head_branch": "develop",
            "repository": {"full_name": release_bundle.WORKSPACE_REPOSITORY},
        },
    }

    def fake_api(path: str, _token: str) -> Any:
        if "/releases?" in path:
            return [release]
        if "/artifacts?" in path:
            return {"artifacts": [artifact]}
        if "/actions/runs/" in path:
            return run
        raise AssertionError(f"unexpected authenticated GitHub API path: {path}")

    monkeypatch.setattr(release_bundle, "_github_api_json", fake_api)
    monkeypatch.setattr(
        release_bundle,
        "_public_asset_bytes",
        lambda _url, maximum_bytes: manifest_bytes[:maximum_bytes],
    )

    decision = release_bundle.publication_decision(
        manifest["sourceFingerprintSha256"], manifest["sourceRun"]["headSha"], "test-token"
    )

    assert decision["publish"] is False
    assert decision["releaseId"] == manifest["releaseId"]
    assert decision["proofArtifactName"] == artifact["name"]

    artifact["name"] = "wrong-proof"
    decision = release_bundle.publication_decision(
        manifest["sourceFingerprintSha256"], manifest["sourceRun"]["headSha"], "test-token"
    )
    assert decision == {"status": "NO_VERIFIED_RELEASE", "publish": True}


def test_bundle_verifier_checks_image_lock_tools_and_each_asset_sha(tmp_path: Path) -> None:
    """Verify all public bundle bytes and the exact deployment-tools tar prefix."""

    root = tmp_path / "release"
    _write_bundle(root)

    verified = release_bundle.verify_release_bundle(root)

    assert verified["status"] == "VERIFIED"
    assert verified["services"] == sorted(release_bundle.IMAGE_SERVICES)

    image = release_bundle.IMAGE_SERVICES[0]
    archive = root / "images" / f"{image}.docker.tar.gz"
    archive.write_bytes(b"modified")
    with pytest.raises(
        release_bundle.ReleaseBundleError, match="archive digest or size is invalid"
    ):
        release_bundle.verify_release_bundle(root)


def test_bundle_verifier_requires_deployer_reconcile_and_preflight_inputs(tmp_path: Path) -> None:
    """The tools archive must include every runtime entry point, unit, and public config."""

    core_members = {
        "release_deploy.py",
        "deployment_probe.py",
        "stage_transition.py",
        "runtime_update.py",
        "preflight.py",
        "release_observation.py",
        "host_fronted_canary.py",
        "build_arm64.py",
        "release_bundle.py",
        "azure_cd.py",
        "cyrene-teams-release-reconcile.service",
        "cyrene-teams-release-reconcile.timer",
        "config/image-lock.schema.json",
        "config/deployment-policy.template.json",
        "config/deployment-probe.template.json",
        "config/deployment-probe.runpod.template.json",
        "config/responsibility-targets.v1.schema.json",
        "config/responsibility-targets.v1.template.json",
    }
    assert core_members <= release_bundle.REQUIRED_DEPLOYMENT_TOOL_FILES

    root = tmp_path / "release"
    manifest = _write_bundle(root)
    tools_path = root / "deployment-tools.tar.gz"
    _write_tools_archive(
        tools_path,
        manifest["sourceRun"]["headSha"],
        omit={"deployment_probe.py"},
    )
    tools_bytes = tools_path.read_bytes()
    manifest["deploymentTools"].update(
        sha256=hashlib.sha256(tools_bytes).hexdigest(), sizeBytes=len(tools_bytes)
    )
    (root / "release-manifest.json").write_text(json.dumps(manifest, sort_keys=True))

    with pytest.raises(
        release_bundle.ReleaseBundleError,
        match="deployment tools archive is missing required runtime members",
    ):
        release_bundle.verify_release_bundle(root)


@pytest.mark.parametrize("missing", [None, "config/work-actor-token-map.schema.json"])
def test_optional_automation_tools_are_verified_as_one_feature_closure(
    tmp_path: Path, missing: str | None
) -> None:
    """Old bundles remain valid; an advertised pilot cannot omit its private-map contract."""

    root = tmp_path / "release"
    manifest = _write_bundle(root)
    release_bundle.verify_release_bundle(root)
    tools_path = root / "deployment-tools.tar.gz"
    _write_tools_archive(
        tools_path,
        manifest["sourceRun"]["headSha"],
        automation=True,
        omit=set() if missing is None else {missing},
    )
    tools_bytes = tools_path.read_bytes()
    manifest["deploymentTools"].update(
        sha256=hashlib.sha256(tools_bytes).hexdigest(), sizeBytes=len(tools_bytes)
    )
    (root / "release-manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    if missing is None:
        release_bundle.verify_release_bundle(root)
    else:
        with pytest.raises(release_bundle.ReleaseBundleError, match="missing required runtime"):
            release_bundle.verify_release_bundle(root)


@pytest.mark.parametrize(
    "missing", [None, "config/microsoft-graph-office-v2-provider-settings.schema.json"]
)
def test_optional_office_graph_tools_are_verified_as_one_feature_closure(
    tmp_path: Path, missing: str | None
) -> None:
    """Graph-only bootstrap carries provider and versioned observation contracts together."""

    root = tmp_path / "release"
    manifest = _write_bundle(root)
    release_bundle.verify_release_bundle(root)
    tools_path = root / "deployment-tools.tar.gz"
    _write_tools_archive(
        tools_path,
        manifest["sourceRun"]["headSha"],
        office_graph=True,
        omit=set() if missing is None else {missing},
    )
    tools_bytes = tools_path.read_bytes()
    manifest["deploymentTools"].update(
        sha256=hashlib.sha256(tools_bytes).hexdigest(), sizeBytes=len(tools_bytes)
    )
    (root / "release-manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    if missing is None:
        release_bundle.verify_release_bundle(root)
    else:
        with pytest.raises(release_bundle.ReleaseBundleError, match="missing required runtime"):
            release_bundle.verify_release_bundle(root)


@pytest.mark.parametrize("missing", [None, "config/antigravity-acp.env.schema.json"])
def test_optional_antigravity_acp_tools_are_verified_as_one_feature_closure(
    tmp_path: Path, missing: str | None
) -> None:
    """Keep the standalone Google ACP recipe, selector, schema, and notice together."""

    root = tmp_path / "release"
    manifest = _write_bundle(root)
    release_bundle.verify_release_bundle(root)
    tools_path = root / "deployment-tools.tar.gz"
    _write_tools_archive(
        tools_path,
        manifest["sourceRun"]["headSha"],
        antigravity_acp=True,
        omit=set() if missing is None else {missing},
    )
    tools_bytes = tools_path.read_bytes()
    manifest["deploymentTools"].update(
        sha256=hashlib.sha256(tools_bytes).hexdigest(), sizeBytes=len(tools_bytes)
    )
    (root / "release-manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    if missing is None:
        release_bundle.verify_release_bundle(root)
    else:
        with pytest.raises(release_bundle.ReleaseBundleError, match="missing required runtime"):
            release_bundle.verify_release_bundle(root)


def test_tools_archive_rejects_nonancestor_directory_outside_pinned_prefix(tmp_path: Path) -> None:
    """Allow git archive prefix ancestors without broadening the accepted tree."""

    workspace_sha = "a" * 40
    prefix = f"Cyrene-Workspace-{workspace_sha}/tooling/deploy/teams-cloud"
    archive_path = tmp_path / "tools.tar.gz"
    with (
        archive_path.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive,
    ):
        member = tarfile.TarInfo(f"Cyrene-Workspace-{workspace_sha}/elsewhere/")
        member.type = tarfile.DIRTYPE
        member.mode = 0o755
        archive.addfile(member)

    with pytest.raises(release_bundle.ReleaseBundleError, match="escaped its source prefix"):
        release_bundle._verify_gzip_tar(archive_path, expected_prefix=prefix)


def test_proof_artifact_contains_only_manifest_lock_tools_and_pointer(tmp_path: Path) -> None:
    """Stage the immutable small proof without duplicating large Docker archives."""

    root = tmp_path / ".artifacts" / "release"
    manifest = _write_bundle(root)
    release_url = (
        "https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/" + manifest["releaseTag"]
    )
    proof_root = root / "proof"

    result = release_bundle.create_proof_bundle(root, proof_root, release_url=release_url)

    assert result["status"] == "PROOF_READY"
    assert result["artifactName"] == release_bundle.proof_artifact_name(manifest)
    assert set(result["files"]) == {
        "release-manifest.json",
        "image-lock.json",
        "deployment-tools.tar.gz",
        "release-pointer.json",
    }
    pointer = json.loads((proof_root / "release-pointer.json").read_text())
    assert pointer["publisherRun"] == manifest["publisherRun"]
    assert pointer["releaseUrl"] == release_url
    assert (
        pointer["manifestSha256"]
        == hashlib.sha256((proof_root / "release-manifest.json").read_bytes()).hexdigest()
    )


class _FakeProcess:
    """Provide a bounded fake Docker archive process for the streaming writer tests."""

    def __init__(self, payload: bytes) -> None:
        self.stdout = io.BytesIO(payload)
        self._returncode: int | None = None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self._returncode is None:
            self._returncode = 0
        return self._returncode

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self._returncode = -15

    def kill(self) -> None:
        self._returncode = -9


def test_image_archive_writer_streams_and_enforces_compressed_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stream Docker bytes through gzip while hashing and rejecting oversized output."""

    payload = b"OCI archive bytes" * 64
    monkeypatch.setattr(
        release_bundle.subprocess,
        "Popen",
        lambda *_args, **_kwargs: _FakeProcess(payload),
    )
    root = tmp_path / ".artifacts" / "release"
    root.mkdir(parents=True)
    output = root / "images" / "runtime.docker.tar.gz"
    record = release_bundle._write_image_archive({"image": "cyrene/runtime:test"}, output)

    with gzip.open(output, "rb") as compressed:
        assert compressed.read() == payload
    assert record["file"] == "images/runtime.docker.tar.gz"
    assert record["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert record["sizeBytes"] == output.stat().st_size

    monkeypatch.setattr(release_bundle, "MAX_RELEASE_ASSET_BYTES", 16)
    oversized = root / "images" / "oversized.docker.tar.gz"
    with pytest.raises(release_bundle.ReleaseBundleError, match="per-image size limit"):
        release_bundle._write_image_archive({"image": "cyrene/runtime:test"}, oversized)
    assert not oversized.exists()


def test_release_workflow_has_no_direct_push_or_pull_request_deploy_trigger() -> None:
    """Keep publishing bound to trusted CI completion, schedule, and controlled retry only."""

    workflow_path = (
        Path(__file__).resolve().parents[4] / ".github" / "workflows" / "teams-cloud-release.yml"
    )
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    event_key = "on" if "on" in workflow else True
    events = workflow[event_key]

    assert set(events) == {"workflow_run", "schedule", "workflow_dispatch"}
    assert events["workflow_run"]["workflows"] == ["CI and immutable release artifact"]
    assert events["workflow_run"]["branches"] == ["develop"]
    assert events["workflow_run"]["types"] == ["completed"]
    assert "pull_request" not in events
    assert "push" not in events
    assert "inputs" not in (events["workflow_dispatch"] or {})
    job = workflow["jobs"]["publish"]
    assert job["runs-on"] == "ubuntu-24.04-arm"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert "head_repository.full_name == 'DoHorizon-AI/Cyrene-Workspace'" in job["if"]
    assert "head_branch == 'develop'" in job["if"]
    assert workflow["permissions"]["contents"] == "write"
    assert workflow["permissions"]["actions"] == "read"
    steps = [step for step in job["steps"] if isinstance(step, dict)]
    source_step = next(step for step in steps if step.get("id") == "sources")
    assert source_step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert not any("setup-qemu-action" in step.get("uses", "") for step in steps)
    docker_setup = next(
        step for step in steps if step.get("uses") == "docker/setup-docker-action@v5"
    )
    assert docker_setup["with"]["version"] == "v29.8.0"
    assert docker_setup["with"]["set-host"] is True
    assert '"containerd-snapshotter": true' in docker_setup["with"]["daemon-config"]
    docker_setup_index = steps.index(docker_setup)
    buildx_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("uses") == "docker/setup-buildx-action@v3"
    )
    assert docker_setup_index < buildx_index
    assert any(
        step.get("uses") == "actions/upload-artifact@v4"
        for step in job["steps"]
        if isinstance(step, dict)
    )
