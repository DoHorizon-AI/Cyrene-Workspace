"""
┌──────────────────────────────────────────────────────────────────────────────┐
│  📄 release_bundle.py                                                        │
│  Module: tooling.deploy.teams_cloud.release_bundle                          │
│  Role: Resolve trusted green sources and package immutable ARM64 releases.  │
│                                                                              │
│  模块职责：锁定可信绿色源码，打包不可变 ARM64 发布资产。                    │
└──────────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

import build_arm64

SCHEMA_VERSION = "cyrene-teams-arm64-release-v1"
SOURCE_PINS_SCHEMA_VERSION = "cyrene-teams-arm64-source-pins-v1"
WORKSPACE_REPOSITORY = "DoHorizon-AI/Cyrene-Workspace"
WORKSPACE_CI_WORKFLOW = ".github/workflows/product-contract-v1.yml"
RELEASE_WORKFLOW = ".github/workflows/teams-cloud-release.yml"
DEVELOP_REF = "refs/heads/develop"
IMAGE_SERVICES = (
    "dsh-executor",
    "navigator-persistence",
    "exchange",
    "huggingface-provider",
    "cloud-management",
    "github-issues-provider",
)
IMAGE_SOURCE_ALIASES = {
    "dsh-executor": ("workspace", "navigator", "dsh"),
    "navigator-persistence": ("workspace", "navigator"),
    "exchange": ("workspace", "exchange", "plugins"),
    "huggingface-provider": ("workspace", "plugins"),
    "cloud-management": ("workspace", "plugins"),
    "github-issues-provider": ("workspace", "plugins"),
}
IMAGE_LABEL_SOURCE = {
    "dsh-executor": "navigator",
    "navigator-persistence": "navigator",
    "exchange": "exchange",
    "huggingface-provider": "plugins",
    "cloud-management": "plugins",
    "github-issues-provider": "plugins",
}
SOURCE_WORKFLOWS = {
    "workspace": (WORKSPACE_REPOSITORY, WORKSPACE_CI_WORKFLOW),
    "navigator": ("DoHorizon-AI/Cyrene-Navigator", ".github/workflows/ci.yml"),
    "exchange": ("DoHorizon-AI/Cyrene-Exchange", ".github/workflows/ci.yml"),
    "plugins": ("DoHorizon-AI/Cyrene-Plugins-Official", ".github/workflows/public-ci.yml"),
}
GITHUB_API_REPOSITORIES = frozenset(
    {WORKSPACE_REPOSITORY, *(repository for repository, _workflow in SOURCE_WORKFLOWS.values())}
)
SOURCE_REMOTES = {
    "workspace": "https://github.com/DoHorizon-AI/Cyrene-Workspace.git",
    "navigator": "https://github.com/DoHorizon-AI/Cyrene-Navigator.git",
    "exchange": "https://github.com/DoHorizon-AI/Cyrene-Exchange.git",
    "plugins": "https://github.com/DoHorizon-AI/Cyrene-Plugins-Official.git",
    "dsh": "https://github.com/deepseek-ai/deepseek-harness.git",
}
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_RELEASE_ASSET_BYTES = 1_900_000_000
MAX_DEPLOYMENT_TOOLS_BYTES = 64 * 1024**2
MAX_RELEASE_TOTAL_BYTES = 10_000_000_000
MAX_PROOF_ARTIFACT_BYTES = 250 * 1024**2
MAX_RELEASE_SCAN_PAGES = 20
MAX_ARCHIVE_MEMBERS = 8192
MAX_ARCHIVE_METADATA_BYTES = 4 * 1024 * 1024
REQUIRED_DEPLOYMENT_TOOL_FILES = frozenset(
    {
        "azure_cd.py",
        "build_arm64.py",
        "deployment_probe.py",
        "preflight.py",
        "release_observation.py",
        "release_bundle.py",
        "release_deploy.py",
        "runtime_update.py",
        "stage_transition.py",
        "host_fronted_canary.py",
        "cyrene-teams-release-reconcile.service",
        "cyrene-teams-release-reconcile.timer",
        "config/acceptance-evidence.schema.json",
        "config/acceptance-evidence.template.json",
        "config/acceptance-plan.runpod.template.json",
        "config/acceptance-plan.schema.json",
        "config/acceptance-plan.template.json",
        "config/antigravity-model-only-profile.template.md",
        "config/build-plan.template.json",
        "config/cloud-api-evidence.azure-gcp.template.json",
        "config/cloud-api-evidence.runpod.template.json",
        "config/cloud-api-evidence.schema.json",
        "config/codebuddy-acp.env.template",
        "config/computer-runner.integration-fragment.v1.template.json",
        "config/connector-bindings.bootstrap.schema.json",
        "config/connector-bindings.empty.template.json",
        "config/connector-bindings.schema.json",
        "config/connector-tool-catalog.bootstrap.schema.json",
        "config/connector-tool-catalog.empty.template.json",
        "config/connector-tool-catalog.schema.json",
        "config/cyrene-github-targets.v2.template.json",
        "config/deployment-policy.template.json",
        "config/deployment-probe.template.json",
        "config/github-only.env.template",
        "config/image-lock.schema.json",
        "config/navigator-principal-config.template.json",
        "config/private-connector-bootstrap.github-only.template.json",
        "config/private-connector-bootstrap.template.json",
        "config/proactive-assistant-v1.env.template",
        "config/proactive-assistant-workflow.v2.template.json",
        "config/requirements.huggingface-cli.in",
        "config/requirements.huggingface-cli.lock",
        "config/responsibility-targets.v1.schema.json",
        "config/responsibility-targets.v1.template.json",
        "config/runtime.env.template",
        "config/secret-files.template.json",
        "config/teams-actor-subjects.template.json",
        "config/teams-connector.template.json",
        "config/teams-credential-notifications-exchange-profile.schema.json",
        "config/teams-credential-notifications-exchange-profile.template.json",
        "config/teams-credential-notifications.schema.json",
        "config/teams-credential-notifications.template.json",
    }
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_ID_RE = re.compile(r"^teams-arm64-[0-9a-f]{12}-[0-9a-f]{12}$")


class ReleaseBundleError(ValueError):
    """A source pin, release package, or content-integrity check failed closed."""


@dataclass(frozen=True)
class SourceRun:
    """One successful, repository-scoped develop push workflow run."""

    repository: str
    workflow: str
    run_id: int
    run_attempt: int
    head_sha: str

    def manifest_row(self) -> dict[str, Any]:
        """Return the public source provenance fields used by the release manifest."""

        return {
            "repository": self.repository,
            "workflow": self.workflow,
            "workflowRunId": self.run_id,
            "workflowRunAttempt": self.run_attempt,
            "ref": DEVELOP_REF,
            "commit": self.head_sha,
            "event": "push",
            "conclusion": "success",
        }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys before they can change the signed meaning."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseBundleError("JSON contains duplicate object keys")
        result[key] = value
    return result


def _read_json(path: Path, label: str, *, maximum_bytes: int = MAX_JSON_BYTES) -> Any:
    """Read one bounded JSON file with duplicate-key rejection."""

    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum_bytes:
            raise ReleaseBundleError(f"{label} is missing, unsafe, or oversized")
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseBundleError(f"{label} is not readable JSON") from error


def _canonical_bytes(value: Any) -> bytes:
    """Serialize JSON deterministically for fingerprints and immutable manifests."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _write_json(path: Path, value: Any) -> None:
    """Atomically write owner-private JSON within a task artifact directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _require_sha(value: Any, label: str, *, length: int) -> str:
    """Validate a lowercase Git or SHA-256 digest without accepting placeholders."""

    pattern = GIT_SHA_RE if length == 40 else SHA256_RE
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ReleaseBundleError(f"{label} is not a lowercase {length}-character digest")
    return value


def _safe_relative_path(value: Any, label: str) -> PurePosixPath:
    """Accept only contained POSIX asset paths with no traversal or backslashes."""

    if not isinstance(value, str) or not value or "\\" in value:
        raise ReleaseBundleError(f"{label} is not a safe relative path")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ReleaseBundleError(f"{label} is not a safe relative path")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ReleaseBundleError(f"{label} is not a safe relative path")
    return path


def _resolve_asset(root: Path, value: Any, label: str) -> Path:
    """Resolve a regular release asset beneath its package root."""

    relative = _safe_relative_path(value, label)
    root_resolved = root.resolve(strict=True)
    candidate = root_resolved.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ReleaseBundleError(f"{label} is missing") from error
    parents = (candidate, *candidate.parents)
    has_symlink_parent = any(parent.is_symlink() for parent in parents if parent != root_resolved)
    if (
        root_resolved not in resolved.parents
        or has_symlink_parent
        or candidate.is_symlink()
        or not resolved.is_file()
    ):
        raise ReleaseBundleError(f"{label} escapes the release bundle or is not a regular file")
    return resolved


def _file_digest(path: Path, *, maximum_bytes: int) -> tuple[str, int]:
    """Hash a bounded regular file in chunks without loading it into memory."""

    try:
        metadata = path.lstat()
        if not path.is_file() or path.is_symlink() or metadata.st_size <= 0:
            raise ReleaseBundleError("release asset is not a non-empty regular file")
        if metadata.st_size > maximum_bytes:
            raise ReleaseBundleError("release asset exceeds its byte limit")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > maximum_bytes:
                    raise ReleaseBundleError("release asset exceeded its byte limit while reading")
                digest.update(chunk)
        return digest.hexdigest(), size
    except OSError as error:
        raise ReleaseBundleError("release asset could not be read") from error


class _RejectApiRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward a short-lived repository token through an API redirect."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url


def _trusted_api_url(url: str) -> urllib.parse.SplitResult:
    """Accept only the fixed GitHub REST origin and canonical source repositories."""

    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ReleaseBundleError("GitHub API URL is malformed") from error
    parts = parsed.path.split("/")
    repository = "/".join(parts[2:4]) if len(parts) >= 4 and parts[1] == "repos" else ""
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or repository not in GITHUB_API_REPOSITORIES
    ):
        raise ReleaseBundleError("GitHub API request is outside the fixed trusted origin")
    return parsed


def _github_api_json_url(url: str, token: str | None) -> Any:
    """Read a bounded canonical REST response; credentials are never sent to other hosts."""

    _trusted_api_url(url)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Cyrene-Teams-Release",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token is not None:
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise ReleaseBundleError("GitHub API token is missing or malformed")
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        opener = urllib.request.build_opener(_RejectApiRedirects())
        with opener.open(request, timeout=20) as response:
            if response.status != 200:
                raise ReleaseBundleError("GitHub API lookup failed")
            payload = response.read(MAX_JSON_BYTES + 1)
        if len(payload) > MAX_JSON_BYTES:
            raise ReleaseBundleError("GitHub API response is oversized")
        return json.loads(payload, object_pairs_hook=_unique_object)
    except (urllib.error.URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseBundleError("GitHub API lookup failed") from error


def _public_api_json(url: str, token: str | None = None) -> dict[str, Any]:
    """Read one bounded canonical GitHub API object, optionally with the job token."""

    value = _github_api_json_url(url, token)
    if not isinstance(value, dict):
        raise ReleaseBundleError("GitHub source workflow response is not an object")
    return value


def _github_api_json(path: str, token: str) -> Any:
    """Read one bounded canonical-repository API response with a task token."""

    if not path.startswith("/repos/DoHorizon-AI/Cyrene-Workspace/"):
        raise ReleaseBundleError("authenticated GitHub lookup is outside the canonical repository")
    return _github_api_json_url("https://api.github.com" + path, token)


def _public_asset_bytes(url: str, *, maximum_bytes: int) -> bytes:
    """Download one bounded public asset only from the canonical GitHub release host."""

    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or not parsed.path.startswith("/DoHorizon-AI/Cyrene-Workspace/releases/download/")
    ):
        raise ReleaseBundleError("release asset URL is outside the canonical GitHub repository")
    request = urllib.request.Request(url, headers={"User-Agent": "Cyrene-Teams-Release"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise ReleaseBundleError("public release manifest download failed")
            payload = response.read(maximum_bytes + 1)
    except (urllib.error.URLError, OSError) as error:
        raise ReleaseBundleError("public release manifest download failed") from error
    if len(payload) > maximum_bytes:
        raise ReleaseBundleError("public release manifest exceeds its byte limit")
    return payload


def _source_run_from_row(row: Any, *, repository: str, workflow: str) -> SourceRun:
    """Validate one successful develop push run returned for a fixed workflow."""

    if not isinstance(row, dict):
        raise ReleaseBundleError("GitHub source workflow run is malformed")
    head_repository = row.get("head_repository")
    if not isinstance(head_repository, dict) or head_repository.get("full_name") != repository:
        raise ReleaseBundleError("GitHub source workflow run is not from its canonical repository")
    if (
        row.get("path") != workflow
        or row.get("head_branch") != "develop"
        or row.get("event") != "push"
        or row.get("status") != "completed"
        or row.get("conclusion") != "success"
    ):
        raise ReleaseBundleError("GitHub source workflow run is not a green develop push")
    run_id, run_attempt = row.get("id"), row.get("run_attempt")
    if (
        isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id <= 0
        or isinstance(run_attempt, bool)
        or not isinstance(run_attempt, int)
        or run_attempt <= 0
    ):
        raise ReleaseBundleError("GitHub source workflow run identity is malformed")
    return SourceRun(
        repository=repository,
        workflow=workflow,
        run_id=run_id,
        run_attempt=run_attempt,
        head_sha=_require_sha(row.get("head_sha"), "source head SHA", length=40),
    )


def _latest_green_run(
    repository: str, workflow: str, *, github_token: str | None = None
) -> SourceRun:
    """Require the exact current develop head to have a successful canonical CI run."""

    branch = _public_api_json(
        f"https://api.github.com/repos/{repository}/branches/develop", github_token
    )
    branch_commit = branch.get("commit") if isinstance(branch, dict) else None
    current_head = _require_sha(
        branch_commit.get("sha") if isinstance(branch_commit, dict) else None,
        "current develop branch head",
        length=40,
    )
    encoded_workflow = urllib.parse.quote(workflow, safe="")
    url = (
        f"https://api.github.com/repos/{repository}/actions/workflows/{encoded_workflow}/runs"
        "?branch=develop&event=push&per_page=100&sort=created&direction=desc"
    )
    response = _public_api_json(url, github_token)
    runs = response.get("workflow_runs")
    if not isinstance(runs, list):
        raise ReleaseBundleError("GitHub source workflow list is malformed")
    for row in runs:
        if (
            isinstance(row, dict)
            and row.get("head_branch") == "develop"
            and row.get("event") == "push"
            and row.get("head_sha") == current_head
        ):
            return _source_run_from_row(row, repository=repository, workflow=workflow)
    raise ReleaseBundleError(
        f"current develop head has no successful {workflow} run for {repository}"
    )


def _trigger_source_run(
    event_name: str, event_path: Path, *, github_token: str | None = None
) -> SourceRun:
    """Resolve the trusted Workspace source run from workflow_run or schedule."""

    if event_name in {"schedule", "workflow_dispatch"}:
        return _latest_green_run(
            WORKSPACE_REPOSITORY, WORKSPACE_CI_WORKFLOW, github_token=github_token
        )
    if event_name != "workflow_run":
        raise ReleaseBundleError(
            "release workflow accepts only trusted workflow_run, schedule, or workflow_dispatch events"
        )
    event = _read_json(event_path, "GitHub workflow event")
    run = event.get("workflow_run") if isinstance(event, dict) else None
    repository = event.get("repository") if isinstance(event, dict) else None
    if not isinstance(run, dict) or not isinstance(repository, dict):
        raise ReleaseBundleError("GitHub workflow_run event is malformed")
    head_repository = run.get("head_repository")
    pull_requests = run.get("pull_requests", [])
    if (
        repository.get("full_name") != WORKSPACE_REPOSITORY
        or not isinstance(head_repository, dict)
        or head_repository.get("full_name") != WORKSPACE_REPOSITORY
        or pull_requests != []
    ):
        raise ReleaseBundleError("release source is not a same-repository push")
    source = _source_run_from_row(
        run, repository=WORKSPACE_REPOSITORY, workflow=WORKSPACE_CI_WORKFLOW
    )
    latest = _latest_green_run(
        WORKSPACE_REPOSITORY, WORKSPACE_CI_WORKFLOW, github_token=github_token
    )
    if source.run_id != latest.run_id or source.head_sha != latest.head_sha:
        raise ReleaseBundleError("successful Workspace source run was superseded")
    return source


def _source_fingerprint(sources: dict[str, Any]) -> str:
    """Hash only immutable repository/ref/commit pins, excluding changing run IDs."""

    identity: dict[str, dict[str, str]] = {}
    for alias in sorted(sources):
        row = sources[alias]
        if not isinstance(row, dict):
            raise ReleaseBundleError("source pin is malformed")
        identity[alias] = {
            "repository": str(row.get("repository", "")),
            "ref": str(row.get("ref", "")),
            "commit": _require_sha(row.get("commit"), f"{alias} source commit", length=40),
        }
    return hashlib.sha256(_canonical_bytes(identity)).hexdigest()


def _publication_identity(
    source_fingerprint: str, *, publisher_run_id: int, publisher_run_attempt: int
) -> str:
    """Derive a distinct immutable publication identity for each publisher attempt."""

    if SHA256_RE.fullmatch(source_fingerprint) is None:
        raise ReleaseBundleError("source fingerprint is malformed")
    if (
        isinstance(publisher_run_id, bool)
        or not isinstance(publisher_run_id, int)
        or publisher_run_id <= 0
        or isinstance(publisher_run_attempt, bool)
        or not isinstance(publisher_run_attempt, int)
        or publisher_run_attempt <= 0
    ):
        raise ReleaseBundleError("publisher run identity is malformed")
    identity = {
        "publisherRunAttempt": publisher_run_attempt,
        "publisherRunId": publisher_run_id,
        "sourceFingerprintSha256": source_fingerprint,
    }
    return hashlib.sha256(_canonical_bytes(identity)).hexdigest()[:12]


def _release_id(source_sha: str, fingerprint: str, publisher_run: dict[str, Any]) -> str:
    """Bind a release tag to its source and exact immutable publisher attempt."""

    workspace_sha = _require_sha(source_sha, "Workspace source SHA", length=40)
    suffix = _publication_identity(
        fingerprint,
        publisher_run_id=publisher_run.get("runId"),
        publisher_run_attempt=publisher_run.get("runAttempt"),
    )
    return f"teams-arm64-{workspace_sha[:12]}-{suffix}"


def resolve_source_pins(
    event_name: str, event_path: Path, *, github_token: str | None = None
) -> dict[str, Any]:
    """Resolve exact green source commits for the six immutable runtime images."""

    workspace_run = _trigger_source_run(event_name, event_path, github_token=github_token)
    sources: dict[str, Any] = {"workspace": workspace_run.manifest_row()}
    for alias in ("navigator", "exchange", "plugins"):
        repository, workflow = SOURCE_WORKFLOWS[alias]
        sources[alias] = _latest_green_run(
            repository, workflow, github_token=github_token
        ).manifest_row()

    return {
        "schemaVersion": SOURCE_PINS_SCHEMA_VERSION,
        "sourceRun": {
            "repository": workspace_run.repository,
            "workflow": workspace_run.workflow,
            "runId": workspace_run.run_id,
            "runAttempt": workspace_run.run_attempt,
            "headSha": workspace_run.head_sha,
            "ref": DEVELOP_REF,
            "event": "push",
            "conclusion": "success",
        },
        "sources": sources,
    }


def _validate_pins(value: Any) -> dict[str, Any]:
    """Validate the closed source-run record before plan generation or packaging."""

    if not isinstance(value, dict) or value.get("schemaVersion") != SOURCE_PINS_SCHEMA_VERSION:
        raise ReleaseBundleError("source pins use an unsupported schema")
    source_run = value.get("sourceRun")
    sources = value.get("sources")
    if not isinstance(source_run, dict) or not isinstance(sources, dict):
        raise ReleaseBundleError("source pins are missing their run or source map")
    if set(sources) != set(SOURCE_WORKFLOWS):
        raise ReleaseBundleError("source pins do not match the fixed runtime repository set")
    if (
        source_run.get("repository") != WORKSPACE_REPOSITORY
        or source_run.get("workflow") != WORKSPACE_CI_WORKFLOW
        or source_run.get("ref") != DEVELOP_REF
        or source_run.get("event") != "push"
        or source_run.get("conclusion") != "success"
    ):
        raise ReleaseBundleError("source run is not a successful canonical Workspace develop push")
    source_sha = _require_sha(source_run.get("headSha"), "Workspace source SHA", length=40)
    for alias, (repository, workflow) in SOURCE_WORKFLOWS.items():
        row = sources.get(alias)
        if not isinstance(row, dict):
            raise ReleaseBundleError(f"{alias} source pin is malformed")
        if (
            row.get("repository") != repository
            or row.get("workflow") != workflow
            or row.get("ref") != DEVELOP_REF
            or row.get("event") != "push"
            or row.get("conclusion") != "success"
        ):
            raise ReleaseBundleError(f"{alias} source pin is not a green canonical develop push")
        _require_sha(row.get("commit"), f"{alias} source commit", length=40)
        run_id, attempt = row.get("workflowRunId"), row.get("workflowRunAttempt")
        if (
            isinstance(run_id, bool)
            or not isinstance(run_id, int)
            or run_id <= 0
            or isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or attempt <= 0
        ):
            raise ReleaseBundleError(f"{alias} source run identity is malformed")
    if sources["workspace"].get("commit") != source_sha:
        raise ReleaseBundleError("Workspace source pin differs from the triggering CI run")
    run_id, attempt = source_run.get("runId"), source_run.get("runAttempt")
    if (
        isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id <= 0
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt <= 0
    ):
        raise ReleaseBundleError("Workspace source run identity is malformed")
    return value


def _prepared_source_pins(
    template: dict[str, Any], pins: dict[str, Any], publisher: dict[str, Any]
) -> dict[str, Any]:
    """Add the exact DSH source and publisher-attempt identity to the build pins."""

    sources = dict(pins["sources"])
    template_sources = template.get("sources")
    images = template.get("images")
    if not isinstance(template_sources, dict) or not isinstance(images, list):
        raise ReleaseBundleError("ARM64 build plan template is malformed")
    dsh_source = template_sources.get("dsh")
    dsh_image = next(
        (row for row in images if isinstance(row, dict) and row.get("service") == "dsh-executor"),
        None,
    )
    if not isinstance(dsh_source, dict) or not isinstance(dsh_image, dict):
        raise ReleaseBundleError("build template is missing the pinned DSH executor")
    if dsh_source.get("remote") != SOURCE_REMOTES["dsh"]:
        raise ReleaseBundleError("DSH upstream remote is not canonical")
    args = dsh_image.get("buildArgs")
    if not isinstance(args, dict):
        raise ReleaseBundleError("DSH build arguments are malformed")
    dsh_commit = _require_sha(dsh_source.get("commit"), "DSH upstream commit", length=40)
    if args.get("DSH_COMMIT") != dsh_commit:
        raise ReleaseBundleError("DSH Dockerfile commit differs from the pinned upstream source")
    dsh_tag = args.get("DSH_TAG")
    if not isinstance(dsh_tag, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", dsh_tag) is None:
        raise ReleaseBundleError("DSH upstream tag is malformed")
    sources["dsh"] = {
        "repository": "deepseek-ai/deepseek-harness",
        "workflow": None,
        "workflowRunId": None,
        "workflowRunAttempt": None,
        "ref": f"refs/tags/{dsh_tag}",
        "commit": dsh_commit,
        "event": "tag",
        "conclusion": "pinned",
    }
    fingerprint = _source_fingerprint(sources)
    workspace_sha = _require_sha(
        pins["sourceRun"].get("headSha"), "Workspace source SHA", length=40
    )
    release_id = _release_id(workspace_sha, fingerprint, publisher)
    prepared = dict(pins)
    prepared.update(
        {
            "sources": sources,
            "publisherRun": publisher,
            "sourceFingerprintSha256": fingerprint,
            "releaseId": release_id,
            "releaseTag": release_id,
        }
    )
    return prepared


def _validate_prepared_pins(value: Any) -> dict[str, Any]:
    """Validate resolved GitHub CI pins plus the fixed DSH upstream tag."""

    if not isinstance(value, dict):
        raise ReleaseBundleError("prepared source pins are malformed")
    github_pins = {
        "schemaVersion": value.get("schemaVersion"),
        "sourceRun": value.get("sourceRun"),
        "sources": {alias: value.get("sources", {}).get(alias) for alias in SOURCE_WORKFLOWS}
        if isinstance(value.get("sources"), dict)
        else None,
    }
    _validate_pins(github_pins)
    sources = value.get("sources")
    if not isinstance(sources, dict) or set(sources) != {*SOURCE_WORKFLOWS, "dsh"}:
        raise ReleaseBundleError("prepared source set is not the fixed runtime dependency set")
    dsh = sources.get("dsh")
    if (
        not isinstance(dsh, dict)
        or set(dsh)
        != {
            "repository",
            "workflow",
            "workflowRunId",
            "workflowRunAttempt",
            "ref",
            "commit",
            "event",
            "conclusion",
        }
        or dsh.get("repository") != "deepseek-ai/deepseek-harness"
        or dsh.get("workflow") is not None
        or dsh.get("workflowRunId") is not None
        or dsh.get("workflowRunAttempt") is not None
        or dsh.get("event") != "tag"
        or dsh.get("conclusion") != "pinned"
        or not isinstance(dsh.get("ref"), str)
        or not dsh["ref"].startswith("refs/tags/")
    ):
        raise ReleaseBundleError("prepared DSH upstream source record is malformed")
    _require_sha(dsh.get("commit"), "DSH upstream commit", length=40)
    fingerprint = _source_fingerprint(sources)
    workspace_sha = _require_sha(
        value["sourceRun"].get("headSha"), "Workspace source SHA", length=40
    )
    publisher = value.get("publisherRun")
    _validate_run_manifest(publisher, publisher=True)
    expected_release_id = _release_id(workspace_sha, fingerprint, publisher)
    if (
        value.get("sourceFingerprintSha256") != fingerprint
        or value.get("releaseId") != expected_release_id
        or value.get("releaseTag") != expected_release_id
    ):
        raise ReleaseBundleError("prepared release identity differs from its exact source pins")
    return value


def prepare_release(
    output_dir: Path,
    source_pins_path: Path,
    *,
    publisher_run_id: int,
    publisher_run_attempt: int,
    publisher_head_sha: str,
    publisher_event: str,
) -> dict[str, Any]:
    """Write a private canonical plan bound to the exact publisher attempt."""

    output = output_dir.resolve()
    if not build_arm64._private_artifact(output):
        raise ReleaseBundleError(
            "release build directory must be under the Workspace .artifacts tree"
        )
    pins = _validate_pins(_read_json(source_pins_path, "source pins"))
    template_path = Path(__file__).resolve().parent / "config" / "build-plan.template.json"
    template = _read_json(template_path, "ARM64 build plan template")
    publisher = _publisher_run(
        run_id=publisher_run_id,
        run_attempt=publisher_run_attempt,
        head_sha=publisher_head_sha,
        event=publisher_event,
    )
    prepared_pins = _prepared_source_pins(template, pins, publisher)
    plan_path = output / ".private-build-plan.json"
    plan = _create_plan(template_path, prepared_pins, output_path=plan_path)
    _write_json(output / ".private-source-pins.json", prepared_pins)
    _write_json(
        output / ".private-context.json",
        {
            "schemaVersion": SCHEMA_VERSION,
            "releaseId": prepared_pins["releaseId"],
            "releaseTag": prepared_pins["releaseTag"],
            "sourceFingerprintSha256": prepared_pins["sourceFingerprintSha256"],
            "sourceRun": prepared_pins["sourceRun"],
            "publisherRun": prepared_pins["publisherRun"],
            "sources": prepared_pins["sources"],
            "services": [row["service"] for row in plan["images"]],
        },
    )
    return {
        "status": "PREPARED",
        "releaseId": prepared_pins["releaseId"],
        "releaseTag": prepared_pins["releaseTag"],
        "sourceFingerprintSha256": prepared_pins["sourceFingerprintSha256"],
        "workspaceSha": prepared_pins["sourceRun"]["headSha"],
        "imageCount": len(plan["images"]),
        "services": [row["service"] for row in plan["images"]],
    }


class _HashingWriter:
    """Hash and bound compressed bytes while a gzip stream writes them."""

    def __init__(self, stream: BinaryIO, maximum_bytes: int) -> None:
        self._stream = stream
        self._maximum_bytes = maximum_bytes
        self.digest = hashlib.sha256()
        self.size_bytes = 0

    def write(self, value: bytes) -> int:
        next_size = self.size_bytes + len(value)
        if next_size > self._maximum_bytes:
            raise ReleaseBundleError("compressed image archive exceeds its per-image size limit")
        written = self._stream.write(value)
        if written != len(value):
            raise ReleaseBundleError("compressed image archive write was incomplete")
        self.digest.update(value)
        self.size_bytes = next_size
        return written

    def flush(self) -> None:
        self._stream.flush()


def _write_image_archive(image: dict[str, Any], destination: Path) -> dict[str, Any]:
    """Stream one inspected Docker image into a bounded, deterministic gzip tar."""

    image_ref = image.get("image")
    if not isinstance(image_ref, str) or not image_ref:
        raise ReleaseBundleError("built image reference is missing")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    if temporary.exists() or destination.exists():
        raise ReleaseBundleError("image archive output already exists")
    process: subprocess.Popen[bytes] | None = None
    raw_bytes = 0
    try:
        with temporary.open("xb") as stream:
            os.chmod(temporary, 0o600)
            writer = _HashingWriter(stream, MAX_RELEASE_ASSET_BYTES)
            process = subprocess.Popen(
                ["docker", "image", "save", image_ref],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if process.stdout is None:
                raise ReleaseBundleError("Docker image save did not provide an output stream")
            with gzip.GzipFile(
                filename="", mode="wb", compresslevel=3, fileobj=writer, mtime=0
            ) as compressed:
                while chunk := process.stdout.read(1024 * 1024):
                    raw_bytes += len(chunk)
                    if raw_bytes > build_arm64.MAX_IMAGE_SAVE_ARCHIVE_BYTES:
                        raise ReleaseBundleError(
                            "uncompressed Docker image archive exceeds its size limit"
                        )
                    compressed.write(chunk)
            process.stdout.close()
            exit_code = process.wait()
            if exit_code != 0:
                raise ReleaseBundleError("Docker image save failed")
            if raw_bytes == 0 or writer.size_bytes == 0:
                raise ReleaseBundleError("Docker image save produced an empty archive")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
        return {
            "file": destination.relative_to(destination.parents[1]).as_posix(),
            "sha256": writer.digest.hexdigest(),
            "sizeBytes": writer.size_bytes,
        }
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseBundleError("Docker image archive could not be written") from error
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        temporary.unlink(missing_ok=True)


def _write_deployment_tools_archive(
    workspace_root: Path, workspace_sha: str, destination: Path
) -> None:
    """Archive only committed deployment tooling from the pinned Workspace tree."""

    observed = subprocess.run(
        ["git", "-C", str(workspace_root), "rev-parse", "HEAD"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if observed.returncode != 0 or observed.stdout.strip() != workspace_sha:
        raise ReleaseBundleError("deployment tools are not from the pinned Workspace commit")
    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"Cyrene-Workspace-{workspace_sha}/"
    try:
        with destination.open("xb") as stream:
            process = subprocess.run(
                [
                    "git",
                    "-C",
                    str(workspace_root),
                    "archive",
                    "--format=tar.gz",
                    f"--prefix={prefix}",
                    workspace_sha,
                    "--",
                    "tooling/deploy/teams-cloud",
                ],
                check=False,
                stdout=stream,
                stderr=subprocess.DEVNULL,
            )
            if process.returncode != 0:
                raise ReleaseBundleError("pinned deployment tools could not be archived")
        os.chmod(destination, 0o600)
        _file_digest(destination, maximum_bytes=MAX_DEPLOYMENT_TOOLS_BYTES)
    except OSError as error:
        destination.unlink(missing_ok=True)
        raise ReleaseBundleError("pinned deployment tools archive could not be written") from error


def _asset_record(root: Path, path: Path) -> dict[str, Any]:
    """Return a relative release asset record after a bounded digest pass."""

    digest, size_bytes = _file_digest(path, maximum_bytes=MAX_RELEASE_ASSET_BYTES)
    return {
        "file": path.relative_to(root).as_posix(),
        "sha256": digest,
        "sizeBytes": size_bytes,
    }


def _publisher_run(*, run_id: int, run_attempt: int, head_sha: str, event: str) -> dict[str, Any]:
    """Validate the publishing workflow identity written to a public manifest."""

    if (
        isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id <= 0
        or isinstance(run_attempt, bool)
        or not isinstance(run_attempt, int)
        or run_attempt <= 0
        or event not in {"workflow_run", "schedule", "workflow_dispatch"}
    ):
        raise ReleaseBundleError("publisher workflow run identity is invalid")
    return {
        "repository": WORKSPACE_REPOSITORY,
        "workflow": RELEASE_WORKFLOW,
        "runId": run_id,
        "runAttempt": run_attempt,
        "headSha": _require_sha(head_sha, "publisher head SHA", length=40),
        "event": event,
    }


def build_release_bundle(
    output_dir: Path,
    *,
    publisher_run_id: int,
    publisher_run_attempt: int,
    publisher_head_sha: str,
    publisher_event: str,
) -> dict[str, Any]:
    """Build, archive, and locally verify one immutable release package."""

    root = output_dir.resolve(strict=True)
    if not build_arm64._private_artifact(root):
        raise ReleaseBundleError("release bundle must be under the Workspace .artifacts tree")
    plan_path = root / ".private-build-plan.json"
    pins_path = root / ".private-source-pins.json"
    context_path = root / ".private-context.json"
    build_arm64.validate_plan(plan_path)
    pins = _validate_prepared_pins(_read_json(pins_path, "prepared source pins"))
    context = _read_json(context_path, "prepared release context")
    if (
        not isinstance(context, dict)
        or context.get("schemaVersion") != SCHEMA_VERSION
        or context.get("releaseId") != pins.get("releaseId")
        or context.get("sourceFingerprintSha256") != pins.get("sourceFingerprintSha256")
        or context.get("publisherRun") != pins.get("publisherRun")
    ):
        raise ReleaseBundleError("prepared release context differs from its source pins")
    publisher = _publisher_run(
        run_id=publisher_run_id,
        run_attempt=publisher_run_attempt,
        head_sha=publisher_head_sha,
        event=publisher_event,
    )
    if publisher != pins.get("publisherRun"):
        raise ReleaseBundleError("publisher run differs from the prepared immutable identity")

    workspace_root = Path(__file__).resolve().parents[3]
    built_dir = root / ".private-built-images"
    lock = build_arm64.build(plan_path, built_dir, allow_emulated=False)
    lock_path = built_dir / "image-lock.json"
    build_arm64.check_local_lock(lock_path)
    if {row.get("service") for row in lock["images"]} != set(IMAGE_SERVICES):
        raise ReleaseBundleError("ARM64 builder returned a different runtime image set")

    public_lock_path = root / "image-lock.json"
    shutil.copyfile(lock_path, public_lock_path)
    os.chmod(public_lock_path, 0o600)
    images: list[dict[str, Any]] = []
    total_asset_bytes = public_lock_path.stat().st_size
    for row in sorted(lock["images"], key=lambda item: item["service"]):
        service = row["service"]
        archive_path = root / "images" / f"{service}.docker.tar.gz"
        archive = _write_image_archive(row, archive_path)
        total_asset_bytes += archive["sizeBytes"]
        if total_asset_bytes > MAX_RELEASE_TOTAL_BYTES:
            raise ReleaseBundleError("release image archives exceed the total release byte limit")
        images.append(
            {
                "service": service,
                "image": row["image"],
                "imageId": row["imageId"],
                "platform": row["platform"],
                "sourceCommits": row["sourceCommits"],
                "archive": archive,
            }
        )

    workspace_sha = pins["sourceRun"]["headSha"]
    tools_path = root / "deployment-tools.tar.gz"
    _write_deployment_tools_archive(workspace_root, workspace_sha, tools_path)
    tools_sha, tools_size = _file_digest(tools_path, maximum_bytes=MAX_DEPLOYMENT_TOOLS_BYTES)
    tools_record = {
        "file": tools_path.relative_to(root).as_posix(),
        "sha256": tools_sha,
        "sizeBytes": tools_size,
    }
    tools_record["sourceCommit"] = workspace_sha
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "releaseId": pins["releaseId"],
        "releaseTag": pins["releaseTag"],
        "targetPlatform": "linux/arm64",
        "publisherRun": publisher,
        "sourceRun": pins["sourceRun"],
        "sourceFingerprintSha256": pins["sourceFingerprintSha256"],
        "sources": pins["sources"],
        "imageLock": _asset_record(root, public_lock_path),
        "deploymentTools": tools_record,
        "images": images,
        "createdAtUtc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    _write_json(root / "release-manifest.json", manifest)
    summary = verify_release_bundle(root)
    return summary


def _validate_run_manifest(run: Any, *, publisher: bool) -> None:
    """Validate source and publisher run identities without trusting filenames."""

    if not isinstance(run, dict):
        raise ReleaseBundleError("release workflow run record is malformed")
    if publisher:
        if set(run) != {"repository", "workflow", "runId", "runAttempt", "headSha", "event"}:
            raise ReleaseBundleError("publisher run fields are not closed")
        if (
            run.get("repository") != WORKSPACE_REPOSITORY
            or run.get("workflow") != RELEASE_WORKFLOW
            or run.get("event") not in {"workflow_run", "schedule", "workflow_dispatch"}
        ):
            raise ReleaseBundleError("publisher run is not the trusted Teams release workflow")
        run_id, attempt = run.get("runId"), run.get("runAttempt")
        _require_sha(run.get("headSha"), "publisher head SHA", length=40)
    else:
        if set(run) != {
            "repository",
            "workflow",
            "runId",
            "runAttempt",
            "headSha",
            "ref",
            "event",
            "conclusion",
        }:
            raise ReleaseBundleError("source run fields are not closed")
        if (
            run.get("repository") != WORKSPACE_REPOSITORY
            or run.get("workflow") != WORKSPACE_CI_WORKFLOW
            or run.get("ref") != DEVELOP_REF
            or run.get("event") != "push"
            or run.get("conclusion") != "success"
        ):
            raise ReleaseBundleError("source run is not a successful Workspace develop push")
        run_id, attempt = run.get("runId"), run.get("runAttempt")
        _require_sha(run.get("headSha"), "Workspace source head SHA", length=40)
    if (
        isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id <= 0
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt <= 0
    ):
        raise ReleaseBundleError("release workflow run ID or attempt is invalid")


def _validate_sources_manifest(sources: Any, source_run: dict[str, Any]) -> None:
    """Require every runtime source pin to be canonical and green or fixed-upstream."""

    if not isinstance(sources, dict) or set(sources) != {
        "workspace",
        "navigator",
        "exchange",
        "plugins",
        "dsh",
    }:
        raise ReleaseBundleError("release source map differs from the fixed image build sources")
    for alias, (repository, workflow) in SOURCE_WORKFLOWS.items():
        row = sources.get(alias)
        if not isinstance(row, dict):
            raise ReleaseBundleError(f"{alias} release source record is malformed")
        if set(row) != {
            "repository",
            "workflow",
            "workflowRunId",
            "workflowRunAttempt",
            "ref",
            "commit",
            "event",
            "conclusion",
        }:
            raise ReleaseBundleError(f"{alias} release source fields are not closed")
        if (
            row.get("repository") != repository
            or row.get("workflow") != workflow
            or row.get("ref") != DEVELOP_REF
            or row.get("event") != "push"
            or row.get("conclusion") != "success"
        ):
            raise ReleaseBundleError(f"{alias} is not pinned to a green canonical develop push")
        _require_sha(row.get("commit"), f"{alias} source commit", length=40)
        if alias == "workspace" and (
            row.get("workflowRunId") != source_run.get("runId")
            or row.get("workflowRunAttempt") != source_run.get("runAttempt")
            or row.get("commit") != source_run.get("headSha")
        ):
            raise ReleaseBundleError("Workspace source pin differs from its successful CI run")
        for key in ("workflowRunId", "workflowRunAttempt"):
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ReleaseBundleError(f"{alias} source workflow {key} is invalid")

    dsh = sources.get("dsh")
    if (
        not isinstance(dsh, dict)
        or set(dsh)
        != {
            "repository",
            "workflow",
            "workflowRunId",
            "workflowRunAttempt",
            "ref",
            "commit",
            "event",
            "conclusion",
        }
        or dsh.get("repository") != "deepseek-ai/deepseek-harness"
        or dsh.get("workflow") is not None
        or dsh.get("workflowRunId") is not None
        or dsh.get("workflowRunAttempt") is not None
        or dsh.get("event") != "tag"
        or dsh.get("conclusion") != "pinned"
        or not isinstance(dsh.get("ref"), str)
        or not dsh["ref"].startswith("refs/tags/")
    ):
        raise ReleaseBundleError("DSH upstream source record is malformed")
    _require_sha(dsh.get("commit"), "DSH upstream commit", length=40)


def validate_release_manifest(document: dict[str, Any]) -> dict[str, Any]:
    """Validate manifest provenance and asset locators without local files or image archives.

    Azure's stream importer uses this closed validator before downloading one image at a time.
    It validates public identities and digest metadata only; callers still verify the downloaded
    bytes against each returned asset record before loading the image.
    """

    if not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION:
        raise ReleaseBundleError("release manifest uses an unsupported schema")
    expected_keys = {
        "schemaVersion",
        "releaseId",
        "releaseTag",
        "targetPlatform",
        "publisherRun",
        "sourceRun",
        "sourceFingerprintSha256",
        "sources",
        "imageLock",
        "deploymentTools",
        "images",
        "createdAtUtc",
    }
    if set(document) != expected_keys:
        raise ReleaseBundleError("release manifest fields do not match its closed schema")
    _validate_run_manifest(document.get("publisherRun"), publisher=True)
    _validate_run_manifest(document.get("sourceRun"), publisher=False)
    sources = document.get("sources")
    _validate_sources_manifest(sources, document["sourceRun"])
    fingerprint = _source_fingerprint(sources)
    if document.get("sourceFingerprintSha256") != fingerprint:
        raise ReleaseBundleError("release source fingerprint does not match its pinned sources")
    workspace_sha = document["sourceRun"]["headSha"]
    publisher = document["publisherRun"]
    release_id = _release_id(workspace_sha, fingerprint, publisher)
    if (
        document.get("releaseId") != release_id
        or document.get("releaseTag") != release_id
        or RELEASE_ID_RE.fullmatch(release_id) is None
        or document.get("targetPlatform") != "linux/arm64"
    ):
        raise ReleaseBundleError("release identity does not match its source and publisher attempt")
    created_at = document.get("createdAtUtc")
    if not isinstance(created_at, str):
        raise ReleaseBundleError("release creation time is missing")
    try:
        parsed_time = datetime.fromisoformat(created_at)
    except ValueError as error:
        raise ReleaseBundleError("release creation time is invalid") from error
    if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
        raise ReleaseBundleError("release creation time must include a timezone")

    image_lock = document.get("imageLock")
    if (
        not isinstance(image_lock, dict)
        or set(image_lock) != {"file", "sha256", "sizeBytes"}
        or image_lock.get("file") != "image-lock.json"
        or not isinstance(image_lock.get("sizeBytes"), int)
        or isinstance(image_lock.get("sizeBytes"), bool)
        or not 0 < image_lock["sizeBytes"] <= MAX_JSON_BYTES
        or not isinstance(image_lock.get("sha256"), str)
        or SHA256_RE.fullmatch(image_lock["sha256"]) is None
    ):
        raise ReleaseBundleError("release image lock asset record is malformed")

    tools = document.get("deploymentTools")
    if (
        not isinstance(tools, dict)
        or set(tools) != {"file", "sha256", "sizeBytes", "sourceCommit"}
        or tools.get("file") != "deployment-tools.tar.gz"
        or tools.get("sourceCommit") != workspace_sha
        or not isinstance(tools.get("sizeBytes"), int)
        or isinstance(tools.get("sizeBytes"), bool)
        or not 0 < tools["sizeBytes"] <= MAX_DEPLOYMENT_TOOLS_BYTES
        or not isinstance(tools.get("sha256"), str)
        or SHA256_RE.fullmatch(tools["sha256"]) is None
    ):
        raise ReleaseBundleError("deployment tools asset record is malformed")

    image_rows = document.get("images")
    if not isinstance(image_rows, list) or len(image_rows) != len(IMAGE_SERVICES):
        raise ReleaseBundleError("release must contain exactly six runtime images")
    assets = ["release-manifest.json", image_lock["file"], tools["file"]]
    services: list[str] = []
    total_bytes = image_lock["sizeBytes"] + tools["sizeBytes"]
    for image in image_rows:
        if not isinstance(image, dict) or set(image) != {
            "service",
            "image",
            "imageId",
            "platform",
            "sourceCommits",
            "archive",
        }:
            raise ReleaseBundleError("release image record is malformed")
        service = image.get("service")
        if (
            not isinstance(service, str)
            or service not in IMAGE_SOURCE_ALIASES
            or service in services
        ):
            raise ReleaseBundleError("release contains an unknown or duplicate image service")
        source_aliases = IMAGE_SOURCE_ALIASES[service]
        source_commits = image.get("sourceCommits")
        expected_commits = {alias: sources[alias]["commit"] for alias in source_aliases}
        if source_commits != expected_commits:
            raise ReleaseBundleError(f"{service} source commits differ from the locked references")
        image_id = image.get("imageId")
        if (
            not isinstance(image_id, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
            or image.get("platform") != "linux/arm64"
        ):
            raise ReleaseBundleError(f"{service} image identity is invalid")
        label_alias = IMAGE_LABEL_SOURCE[service]
        expected_image = f"cyrene/{service}:{release_id}-{sources[label_alias]['commit'][:12]}"
        if image.get("image") != expected_image:
            raise ReleaseBundleError(f"{service} image tag is not derived from the pinned source")
        archive = image.get("archive")
        expected_path = f"images/{service}.docker.tar.gz"
        if (
            not isinstance(archive, dict)
            or set(archive) != {"file", "sha256", "sizeBytes"}
            or archive.get("file") != expected_path
            or not isinstance(archive.get("sizeBytes"), int)
            or isinstance(archive.get("sizeBytes"), bool)
            or not 0 < archive["sizeBytes"] <= MAX_RELEASE_ASSET_BYTES
            or not isinstance(archive.get("sha256"), str)
            or SHA256_RE.fullmatch(archive["sha256"]) is None
        ):
            raise ReleaseBundleError(f"{service} image archive record is malformed")
        assets.append(PurePosixPath(expected_path).name)
        total_bytes += archive["sizeBytes"]
        services.append(service)
    if set(services) != set(IMAGE_SERVICES):
        raise ReleaseBundleError("release image set differs from the fixed runtime services")
    if len(assets) != len(set(assets)):
        raise ReleaseBundleError("release asset basenames collide")
    if total_bytes > MAX_RELEASE_TOTAL_BYTES:
        raise ReleaseBundleError("release assets exceed their total byte limit")
    return {
        "status": "VERIFIED",
        "releaseId": release_id,
        "releaseTag": release_id,
        "sourceFingerprintSha256": fingerprint,
        "sourceRunId": document["sourceRun"]["runId"],
        "publisherRunId": document["publisherRun"]["runId"],
        "publisherRunAttempt": document["publisherRun"]["runAttempt"],
        "imageCount": len(services),
        "services": sorted(services),
        "assets": sorted(assets),
    }


def proof_artifact_name(document: dict[str, Any]) -> str:
    """Return the immutable Actions proof artifact name for a validated manifest."""

    summary = validate_release_manifest(document)
    return f"cyrene-{summary['releaseId']}"


def create_proof_bundle(
    release_root: Path, proof_root: Path, *, release_url: str
) -> dict[str, Any]:
    """Stage only the small immutable Actions proof, excluding all image archives."""

    source_root = release_root.resolve(strict=True)
    manifest = _read_json(source_root / "release-manifest.json", "release manifest")
    summary = validate_release_manifest(manifest)
    expected_url = (
        "https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/" + summary["releaseTag"]
    )
    if release_url != expected_url:
        raise ReleaseBundleError("release URL does not match the immutable release tag")

    proof = proof_root.resolve()
    if proof.exists() or not build_arm64._private_artifact(proof):
        raise ReleaseBundleError("proof artifact output must be a new private artifact directory")
    source_files = (
        source_root / "release-manifest.json",
        source_root / manifest["imageLock"]["file"],
        source_root / manifest["deploymentTools"]["file"],
    )
    for path, record in zip(source_files[1:], (manifest["imageLock"], manifest["deploymentTools"])):
        digest, size_bytes = _file_digest(path, maximum_bytes=MAX_RELEASE_ASSET_BYTES)
        if digest != record["sha256"] or size_bytes != record["sizeBytes"]:
            raise ReleaseBundleError("small proof asset digest differs from the release manifest")
    total_bytes = sum(path.stat().st_size for path in source_files)
    if total_bytes > MAX_PROOF_ARTIFACT_BYTES:
        raise ReleaseBundleError("small release proof exceeds its byte limit")

    proof.mkdir(parents=True)
    for source in source_files:
        shutil.copyfile(source, proof / source.name)
    manifest_sha, _manifest_size = _file_digest(
        proof / "release-manifest.json", maximum_bytes=MAX_JSON_BYTES
    )
    for name, record in (
        ("image-lock.json", manifest["imageLock"]),
        ("deployment-tools.tar.gz", manifest["deploymentTools"]),
    ):
        digest, size_bytes = _file_digest(proof / name, maximum_bytes=MAX_RELEASE_ASSET_BYTES)
        if digest != record["sha256"] or size_bytes != record["sizeBytes"]:
            raise ReleaseBundleError("copied proof asset differs from the release manifest")
    pointer = {
        "schemaVersion": "cyrene-teams-arm64-release-pointer-v1",
        "publisherRun": manifest["publisherRun"],
        "releaseId": summary["releaseId"],
        "releaseTag": summary["releaseTag"],
        "releaseUrl": release_url,
        "sourceFingerprintSha256": summary["sourceFingerprintSha256"],
        "manifestSha256": manifest_sha,
        "proofArtifactName": proof_artifact_name(manifest),
    }
    _write_json(proof / "release-pointer.json", pointer)
    actual_total = sum(path.stat().st_size for path in proof.iterdir() if path.is_file())
    if actual_total > MAX_PROOF_ARTIFACT_BYTES:
        raise ReleaseBundleError("proof artifact exceeds its byte limit")
    return {
        "status": "PROOF_READY",
        "artifactName": pointer["proofArtifactName"],
        "releaseTag": pointer["releaseTag"],
        "manifestSha256": manifest_sha,
        "sizeBytes": actual_total,
        "files": sorted(path.name for path in proof.iterdir() if path.is_file()),
    }


def _release_asset_records(release: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return unique release-asset metadata keyed by basename."""

    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ReleaseBundleError("immutable release asset list is malformed")
    records: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise ReleaseBundleError("immutable release asset metadata is malformed")
        name = asset.get("name")
        digest = asset.get("digest")
        size = asset.get("size")
        if (
            not isinstance(name, str)
            or PurePosixPath(name).name != name
            or name in records
            or not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
        ):
            raise ReleaseBundleError("immutable release asset digest or size is invalid")
        records[name] = asset
    return records


def _verified_existing_release(
    source_fingerprint: str, workspace_sha: str, token: str
) -> dict[str, Any] | None:
    """Find a release only when its assets, successful run, and immutable proof all agree."""

    if SHA256_RE.fullmatch(source_fingerprint) is None:
        raise ReleaseBundleError("source fingerprint is malformed")
    workspace_sha = _require_sha(workspace_sha, "Workspace source SHA", length=40)
    marker = f"sourceFingerprintSha256={source_fingerprint}"
    base = "/repos/DoHorizon-AI/Cyrene-Workspace/releases?per_page=100&page="
    for page in range(1, MAX_RELEASE_SCAN_PAGES + 1):
        releases = _github_api_json(base + str(page), token)
        if not isinstance(releases, list):
            raise ReleaseBundleError("GitHub release list is malformed")
        for release in releases:
            if not isinstance(release, dict):
                continue
            tag = release.get("tag_name")
            body = release.get("body")
            if (
                release.get("immutable") is not True
                or release.get("draft") is not False
                or not isinstance(tag, str)
                or RELEASE_ID_RE.fullmatch(tag) is None
                or not tag.startswith(f"teams-arm64-{workspace_sha[:12]}-")
                or not isinstance(body, str)
                or marker not in body.splitlines()
            ):
                continue
            manifest_url = (
                "https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/download/"
                + urllib.parse.quote(tag, safe="")
                + "/release-manifest.json"
            )
            try:
                manifest_bytes = _public_asset_bytes(manifest_url, maximum_bytes=MAX_JSON_BYTES)
                manifest = json.loads(manifest_bytes, object_pairs_hook=_unique_object)
                summary = validate_release_manifest(manifest)
                if (
                    summary["sourceFingerprintSha256"] != source_fingerprint
                    or summary["releaseTag"] != tag
                    or manifest["sourceRun"]["headSha"] != workspace_sha
                ):
                    continue
                records = _release_asset_records(release)
                if set(records) != set(summary["assets"]):
                    continue
                expected_records = {
                    "release-manifest.json": (
                        hashlib.sha256(manifest_bytes).hexdigest(),
                        len(manifest_bytes),
                    ),
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
                    expected_records[PurePosixPath(image["archive"]["file"]).name] = (
                        image["archive"]["sha256"],
                        image["archive"]["sizeBytes"],
                    )
                if any(
                    records[name].get("digest") != f"sha256:{digest}"
                    or records[name].get("size") != size
                    for name, (digest, size) in expected_records.items()
                ):
                    continue

                publisher = manifest["publisherRun"]
                run = _github_api_json(
                    f"/repos/DoHorizon-AI/Cyrene-Workspace/actions/runs/{publisher['runId']}",
                    token,
                )
                repository = run.get("repository") if isinstance(run, dict) else None
                head_repository = run.get("head_repository") if isinstance(run, dict) else None
                if (
                    not isinstance(run, dict)
                    or run.get("id") != publisher["runId"]
                    or run.get("run_attempt") != publisher["runAttempt"]
                    or run.get("path") != RELEASE_WORKFLOW
                    or run.get("head_branch") != "develop"
                    or run.get("head_sha") != publisher["headSha"]
                    or run.get("event") != publisher["event"]
                    or run.get("status") != "completed"
                    or run.get("conclusion") != "success"
                    or not isinstance(repository, dict)
                    or repository.get("full_name") != WORKSPACE_REPOSITORY
                    or not isinstance(head_repository, dict)
                    or head_repository.get("full_name") != WORKSPACE_REPOSITORY
                ):
                    continue
                artifacts_response = _github_api_json(
                    f"/repos/DoHorizon-AI/Cyrene-Workspace/actions/runs/{publisher['runId']}/artifacts?per_page=100",
                    token,
                )
                artifacts = (
                    artifacts_response.get("artifacts")
                    if isinstance(artifacts_response, dict)
                    else None
                )
                if not isinstance(artifacts, list):
                    continue
                expected_artifact = proof_artifact_name(manifest)
                if any(
                    isinstance(artifact, dict)
                    and artifact.get("name") == expected_artifact
                    and artifact.get("expired") is False
                    and isinstance(artifact.get("size_in_bytes"), int)
                    and 0 < artifact["size_in_bytes"] <= MAX_PROOF_ARTIFACT_BYTES
                    and isinstance(artifact.get("digest"), str)
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"]) is not None
                    and isinstance(artifact.get("workflow_run"), dict)
                    and artifact["workflow_run"].get("id") == publisher["runId"]
                    and artifact["workflow_run"].get("head_sha") == publisher["headSha"]
                    and artifact["workflow_run"].get("head_branch") == "develop"
                    and isinstance(artifact["workflow_run"].get("repository"), dict)
                    and artifact["workflow_run"]["repository"].get("full_name")
                    == WORKSPACE_REPOSITORY
                    for artifact in artifacts
                ):
                    return {
                        "releaseId": summary["releaseId"],
                        "releaseTag": summary["releaseTag"],
                        "publisherRunId": publisher["runId"],
                        "publisherRunAttempt": publisher["runAttempt"],
                        "proofArtifactName": expected_artifact,
                    }
            except (
                ReleaseBundleError,
                TypeError,
                KeyError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ):
                continue
        if len(releases) < 100:
            return None
    raise ReleaseBundleError("verified-release scan exceeded its bounded page limit")


def publication_decision(source_fingerprint: str, workspace_sha: str, token: str) -> dict[str, Any]:
    """Skip a build only if an immutable release has a matching successful run and proof."""

    existing = _verified_existing_release(source_fingerprint, workspace_sha, token)
    if existing is None:
        return {"status": "NO_VERIFIED_RELEASE", "publish": True}
    return {"status": "VERIFIED_EXISTING_RELEASE", "publish": False, **existing}


def _verify_gzip_tar(path: Path, *, expected_prefix: str | None = None) -> set[str]:
    """Validate a gzip tar member list before an Azure consumer extracts tooling."""

    prefix = PurePosixPath(expected_prefix) if expected_prefix is not None else None
    seen: set[str] = set()
    total_member_bytes = 0
    try:
        with (
            gzip.open(path, "rb") as compressed,
            tarfile.open(fileobj=compressed, mode="r|") as archive,
        ):
            for index, member in enumerate(archive):
                if index >= MAX_ARCHIVE_MEMBERS:
                    raise ReleaseBundleError("deployment tools archive has too many members")
                member_name = member.name
                if member.isdir() and member_name.endswith("/"):
                    member_name = member_name[:-1]
                member_path = _safe_relative_path(member_name, "archive member")
                normalized = member_path.as_posix()
                if normalized in seen:
                    raise ReleaseBundleError("archive contains duplicate member paths")
                seen.add(normalized)
                if not member.isdir() and not member.isfile():
                    raise ReleaseBundleError("archive contains a link or special file")
                if prefix is not None:
                    is_in_prefix = member_path.parts[: len(prefix.parts)] == prefix.parts
                    is_prefix_ancestor = (
                        member.isdir()
                        and 0 < len(member_path.parts) < len(prefix.parts)
                        and prefix.parts[: len(member_path.parts)] == member_path.parts
                    )
                    if not is_in_prefix and not is_prefix_ancestor:
                        raise ReleaseBundleError(
                            "deployment tools archive escaped its source prefix"
                        )
                total_member_bytes += member.size
                if total_member_bytes > build_arm64.MAX_IMAGE_SAVE_ARCHIVE_BYTES:
                    raise ReleaseBundleError("archive uncompressed contents exceed the size limit")
                if member.size > MAX_ARCHIVE_METADATA_BYTES and expected_prefix is not None:
                    raise ReleaseBundleError("deployment tooling contains an oversized file")
    except (gzip.BadGzipFile, EOFError, OSError, tarfile.TarError) as error:
        raise ReleaseBundleError("release archive is not a valid bounded gzip tar") from error
    if not seen:
        raise ReleaseBundleError("release archive is empty")
    return seen


def verify_release_bundle(bundle_root: Path) -> dict[str, Any]:
    """Verify manifest metadata and all downloaded bytes in one release directory."""

    root = bundle_root.resolve(strict=True)
    if not root.is_dir() or bundle_root.is_symlink():
        raise ReleaseBundleError("release bundle root is not a real directory")
    manifest = _read_json(root / "release-manifest.json", "release manifest")
    summary = validate_release_manifest(manifest)

    lock_record = manifest["imageLock"]
    lock_path = _resolve_asset(root, lock_record["file"], "image lock")
    lock_sha, lock_size = _file_digest(lock_path, maximum_bytes=MAX_JSON_BYTES)
    if lock_record["sha256"] != lock_sha or lock_record["sizeBytes"] != lock_size:
        raise ReleaseBundleError("image lock digest or size does not match the manifest")
    lock = _read_json(lock_path, "image lock")
    if (
        not isinstance(lock, dict)
        or lock.get("schemaVersion") != "cyrene-teams-cloud-image-lock-v1"
        or lock.get("targetPlatform") != "linux/arm64"
        or lock.get("state") != "LOCAL_ONLY"
        or lock.get("binaryArtifacts") != []
        or lock.get("externalImages") != {}
    ):
        raise ReleaseBundleError("image lock is not a CodeBuddy-only local ARM64 delta")
    sources = manifest["sources"]
    locked_sources = lock.get("sources")
    if not isinstance(locked_sources, dict) or set(locked_sources) != set(sources):
        raise ReleaseBundleError("image lock source set differs from the release manifest")
    for alias, source in sources.items():
        row = locked_sources.get(alias)
        if (
            not isinstance(row, dict)
            or row.get("remote") != SOURCE_REMOTES[alias]
            or row.get("commit") != source.get("commit")
        ):
            raise ReleaseBundleError(f"image lock source pin for {alias} differs from the manifest")
        _require_sha(row.get("tree"), f"{alias} source tree", length=40)

    lock_rows = lock.get("images")
    if not isinstance(lock_rows, list):
        raise ReleaseBundleError("image lock rows are malformed")
    if {row.get("service") for row in lock_rows if isinstance(row, dict)} != set(IMAGE_SERVICES):
        raise ReleaseBundleError("image lock does not contain the exact six runtime images")
    locked_by_service = {row["service"]: row for row in lock_rows}
    for image in manifest["images"]:
        locked = locked_by_service[image["service"]]
        if (
            image["image"] != locked.get("image")
            or image["imageId"] != locked.get("imageId")
            or image["platform"] != locked.get("platform")
            or image["sourceCommits"] != locked.get("sourceCommits")
            or locked.get("registryDigest") is not None
        ):
            raise ReleaseBundleError(
                f"image manifest differs from the local-only lock for {image['service']}"
            )

    tools_record = manifest["deploymentTools"]
    tools_path = _resolve_asset(root, tools_record["file"], "deployment tools archive")
    tools_sha, tools_size = _file_digest(tools_path, maximum_bytes=MAX_DEPLOYMENT_TOOLS_BYTES)
    if tools_record["sha256"] != tools_sha or tools_record["sizeBytes"] != tools_size:
        raise ReleaseBundleError("deployment tools digest or size does not match the manifest")
    workspace_sha = manifest["sourceRun"]["headSha"]
    tools_prefix = f"Cyrene-Workspace-{workspace_sha}/tooling/deploy/teams-cloud"
    tools_members = _verify_gzip_tar(
        tools_path,
        expected_prefix=tools_prefix,
    )
    required_tool_members = {
        f"{tools_prefix}/{relative_path}" for relative_path in REQUIRED_DEPLOYMENT_TOOL_FILES
    }
    missing_tool_members = required_tool_members - tools_members
    if missing_tool_members:
        raise ReleaseBundleError(
            "deployment tools archive is missing required runtime members: "
            + ", ".join(
                sorted(path.removeprefix(tools_prefix + "/") for path in missing_tool_members)
            )
        )

    for image in manifest["images"]:
        archive = image["archive"]
        archive_path = _resolve_asset(root, archive["file"], f"{image['service']} image archive")
        archive_sha, archive_size = _file_digest(
            archive_path, maximum_bytes=MAX_RELEASE_ASSET_BYTES
        )
        if archive["sha256"] != archive_sha or archive["sizeBytes"] != archive_size:
            raise ReleaseBundleError(
                f"{image['service']} Docker image archive digest or size is invalid"
            )
    return summary


def _write_github_output(path: Path | None, values: dict[str, str]) -> None:
    """Append validated single-line workflow outputs when GitHub Actions requests it."""

    if path is None:
        return
    with path.open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ReleaseBundleError("GitHub Actions output contains a line break")
            stream.write(f"{key}={value}\n")


def _print_summary(summary: dict[str, Any]) -> None:
    """Emit only bounded public release metadata, never source bodies or credentials."""

    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))


def _build_parser() -> argparse.ArgumentParser:
    """Create a closed CLI for source resolution, plan preparation, build, and verify."""

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    resolve = commands.add_parser(
        "resolve-sources", help="pin successful canonical develop CI runs"
    )
    resolve.add_argument("--event-name", required=True)
    resolve.add_argument("--event-path", type=Path, required=True)
    resolve.add_argument("--output", type=Path, required=True)
    resolve.add_argument("--github-output", type=Path)
    prepare = commands.add_parser("prepare", help="prepare the exact six-image release plan")
    prepare.add_argument("--source-pins", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--publisher-run-id", type=int, required=True)
    prepare.add_argument("--publisher-run-attempt", type=int, required=True)
    prepare.add_argument("--publisher-head-sha", required=True)
    prepare.add_argument("--publisher-event", required=True)
    prepare.add_argument("--github-output", type=Path)
    decision = commands.add_parser(
        "publication-decision", help="skip only a fully verified existing release"
    )
    decision.add_argument("--source-fingerprint", required=True)
    decision.add_argument("--workspace-sha", required=True)
    decision.add_argument("--github-output", type=Path)
    build = commands.add_parser("build", help="build and package the prepared ARM64 image set")
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument("--publisher-run-id", type=int, required=True)
    build.add_argument("--publisher-run-attempt", type=int, required=True)
    build.add_argument("--publisher-head-sha", required=True)
    build.add_argument("--publisher-event", required=True)
    verify = commands.add_parser("verify", help="verify an immutable release bundle")
    verify.add_argument("--bundle-root", type=Path, required=True)
    proof = commands.add_parser("proof", help="stage the small immutable Actions proof bundle")
    proof.add_argument("--release-root", type=Path, required=True)
    proof.add_argument("--output-dir", type=Path, required=True)
    proof.add_argument("--release-url", required=True)
    proof.add_argument("--github-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one bounded release operation and return a stable machine exit code."""

    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "resolve-sources":
            github_token = os.environ.get("GH_TOKEN")
            if not github_token:
                raise ReleaseBundleError(
                    "source resolution requires the short-lived GitHub job token"
                )
            pins = resolve_source_pins(
                arguments.event_name,
                arguments.event_path,
                github_token=github_token,
            )
            _write_json(arguments.output, pins)
            _write_github_output(
                arguments.github_output,
                {"workspace_sha": pins["sourceRun"]["headSha"]},
            )
            _print_summary(
                {
                    "status": "SOURCES_PINNED",
                    "workspaceSha": pins["sourceRun"]["headSha"],
                    "sourceCount": len(pins["sources"]),
                }
            )
        elif arguments.command == "prepare":
            summary = prepare_release(
                arguments.output_dir,
                arguments.source_pins,
                publisher_run_id=arguments.publisher_run_id,
                publisher_run_attempt=arguments.publisher_run_attempt,
                publisher_head_sha=arguments.publisher_head_sha,
                publisher_event=arguments.publisher_event,
            )
            _write_github_output(
                arguments.github_output,
                {
                    "release_id": summary["releaseId"],
                    "release_tag": summary["releaseTag"],
                    "source_fingerprint": summary["sourceFingerprintSha256"],
                    "workspace_sha": summary["workspaceSha"],
                },
            )
            _print_summary(summary)
        elif arguments.command == "publication-decision":
            token = os.environ.get("GH_TOKEN", "")
            decision = publication_decision(
                arguments.source_fingerprint, arguments.workspace_sha, token
            )
            _write_github_output(
                arguments.github_output,
                {"publish": "true" if decision["publish"] else "false"},
            )
            _print_summary(decision)
        elif arguments.command == "build":
            summary = build_release_bundle(
                arguments.output_dir,
                publisher_run_id=arguments.publisher_run_id,
                publisher_run_attempt=arguments.publisher_run_attempt,
                publisher_head_sha=arguments.publisher_head_sha,
                publisher_event=arguments.publisher_event,
            )
            _print_summary(summary)
        elif arguments.command == "verify":
            _print_summary(verify_release_bundle(arguments.bundle_root))
        else:
            summary = create_proof_bundle(
                arguments.release_root,
                arguments.output_dir,
                release_url=arguments.release_url,
            )
            _write_github_output(
                arguments.github_output,
                {
                    "artifact_name": summary["artifactName"],
                    "release_tag": summary["releaseTag"],
                    "proof_dir": str(arguments.output_dir.resolve()),
                },
            )
            _print_summary(summary)
    except (
        ReleaseBundleError,
        build_arm64.BuildError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Teams ARM64 release blocked: {error}", file=sys.stderr)
        return 2
    return 0


def _create_plan(
    template_path: Path,
    pins: dict[str, Any],
    *,
    output_path: Path,
) -> dict[str, Any]:
    """Derive a closed six-image plan from the canonical template and green pins."""

    template = _read_json(template_path, "ARM64 build plan template")
    if (
        not isinstance(template, dict)
        or template.get("schemaVersion") != "cyrene-teams-cloud-build-v1"
    ):
        raise ReleaseBundleError("ARM64 build plan template is unsupported")
    template_sources = template.get("sources")
    template_images = template.get("images")
    if not isinstance(template_sources, dict) or not isinstance(template_images, list):
        raise ReleaseBundleError("ARM64 build plan template is malformed")

    source_rows: dict[str, dict[str, Any]] = {}
    for alias, (repository, workflow) in SOURCE_WORKFLOWS.items():
        template_row = template_sources.get(alias)
        pin = pins["sources"][alias]
        remote = SOURCE_REMOTES[alias]
        if not isinstance(template_row, dict) or template_row.get("remote") != remote:
            raise ReleaseBundleError(f"build template remote for {alias} is not canonical")
        source_rows[alias] = {
            "remote": remote,
            "commit": _require_sha(pin.get("commit"), f"{alias} source commit", length=40),
            "checkoutPath": template_row.get("checkoutPath"),
        }
        if pin.get("repository") != repository or pin.get("workflow") != workflow:
            raise ReleaseBundleError(f"{alias} source pin does not match its trusted workflow")

    dsh_template = template_sources.get("dsh")
    if not isinstance(dsh_template, dict) or dsh_template.get("remote") != SOURCE_REMOTES["dsh"]:
        raise ReleaseBundleError("DSH upstream source is not canonical")
    dsh_commit = _require_sha(dsh_template.get("commit"), "DSH upstream commit", length=40)
    source_rows["dsh"] = {
        "remote": SOURCE_REMOTES["dsh"],
        "commit": dsh_commit,
        "checkoutPath": dsh_template.get("checkoutPath"),
    }

    selected: list[dict[str, Any]] = []
    for template_image in template_images:
        if not isinstance(template_image, dict):
            raise ReleaseBundleError("ARM64 image template row is malformed")
        service = template_image.get("service")
        if service not in IMAGE_SERVICES:
            continue
        image = dict(template_image)
        image.pop("binaryArtifacts", None)
        image["sourceRepos"] = list(image.get("sourceRepos", []))
        if service == "dsh-executor":
            image["dockerfile"] = (
                "Cyrene-Workspace/tooling/deploy/teams-cloud/Dockerfile.dsh-executor-codebuddy"
            )
            args = dict(image.get("buildArgs", {}))
            args.pop("ASSET_ANTIGRAVITY_CLI_SHA256", None)
            image["buildArgs"] = args
        selected.append(image)
    if {row.get("service") for row in selected} != set(IMAGE_SERVICES):
        raise ReleaseBundleError("build template is missing one or more fixed runtime images")

    required_aliases = {alias for image in selected for alias in image["sourceRepos"]}
    if not required_aliases <= set(source_rows):
        raise ReleaseBundleError("runtime image references an unpinned source repository")
    plan = {
        "schemaVersion": template["schemaVersion"],
        "taskId": pins["releaseId"],
        "targetPlatform": "linux/arm64",
        "sources": {alias: source_rows[alias] for alias in sorted(required_aliases)},
        "images": selected,
        "externalImages": {},
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(output_path, plan)
    try:
        build_arm64.validate_plan(output_path)
    except (build_arm64.BuildError, OSError) as error:
        raise ReleaseBundleError(
            "generated ARM64 release plan failed its canonical validation"
        ) from error
    return plan


if __name__ == "__main__":
    raise SystemExit(main())
