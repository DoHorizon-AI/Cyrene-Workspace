"""
┌──────────────────────────────────────────────────────────────────────────────┐
│  📄 azure_cd.py                                                              │
│  Module: tooling.deploy.teams_cloud.azure_cd                                │
│  Role: Verify signed release evidence and run one guarded Azure VM update.   │
│                                                                              │
│  模块职责：校验签名发布证据，并对 Azure VM 执行一次受保护的部署调用。       │
└──────────────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from release_bundle import ReleaseBundleError, proof_artifact_name, validate_release_manifest

WORKSPACE_REPOSITORY = "DoHorizon-AI/Cyrene-Workspace"
PUBLISHER_WORKFLOW = ".github/workflows/teams-cloud-release.yml"
SOURCE_WORKFLOW = ".github/workflows/product-contract-v1.yml"
SOURCE_REF = "refs/heads/develop"
PUBLISHER_EVENTS = {"workflow_run", "schedule", "workflow_dispatch"}
EVENT_SCHEMA = "cyrene-teams-arm64-cd-event-v1"
RECEIPT_SCHEMA = "cyrene-teams-release-deployment-receipt-v1"
RELEASE_ID_RE = re.compile(r"teams-arm64-[0-9a-f]{12}-[0-9a-f]{12}\Z")
GIT_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
ARTIFACT_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
ARTIFACT_NAME_RE = re.compile(r"cyrene-teams-arm64-[0-9a-f]{12}-[0-9a-f]{12}\Z")
ASSET_NAME_RE = re.compile(r"[A-Za-z0-9._/-]{1,256}\Z")
RELEASE_BASE_PREFIX = "https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/download/"
RELEASE_TAG_URL_PREFIX = "https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/tag/"
AZURE_VM_RESOURCE_ID = (
    "/subscriptions/6653fd82-217f-41f1-90a1-0bf74e8203ed/"
    "resourceGroups/Normal-x64-VM/providers/Microsoft.Compute/virtualMachines/Tailscale-VM-ARM"
)
GUEST_POLICY_PATH = "/etc/cyrene/teams-cloud-deployment.json"
GUEST_RELEASE_ROOT = "/var/lib/cyrene-connectors-runtime/.artifacts/releases"
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_TOOLS_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_PROOF_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_GUEST_RECEIPT_BYTES = 3584
MAX_HTTP_SECONDS = 30
AZURE_DEPLOY_TIMEOUT_SECONDS = 3300
AZURE_STATUS_TIMEOUT_SECONDS = 300
GITHUB_API_ROOT = "https://api.github.com"
ADO_WEBHOOK_URL = (
    "https://dev.azure.com/dohorizon/_apis/public/distributedtask/webhooks/"
    "cyrene-teams-arm64-releases?api-version=6.0-preview"
)
PROOF_FILES = {
    "release-manifest.json",
    "image-lock.json",
    "deployment-tools.tar.gz",
    "release-pointer.json",
}
ADO_BOOTSTRAP_PYTHON = r"""import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

MAX_EVENT = 2 * 1024 * 1024
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_MEMBERS = 8192
ALLOWED_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
REPO = "DoHorizon-AI/Cyrene-Workspace"
RELEASES = "https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/download/"


def reject(message):
    raise SystemExit(message)


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            reject("duplicate event JSON key")
        result[key] = value
    return result


def safe_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        reject("unsafe staging directory")
    os.chmod(path, 0o700)


raw = os.environ.get("CYRENE_EVENT_JSON", "")
if not raw or len(raw.encode("utf-8")) > MAX_EVENT:
    reject("event payload is missing or oversized")
try:
    event = json.loads(raw, object_pairs_hook=unique_pairs)
except (json.JSONDecodeError, UnicodeDecodeError):
    reject("event payload is invalid JSON")
if not isinstance(event, dict) or event.get("schemaVersion") != "cyrene-teams-arm64-cd-event-v1":
    reject("event schema is unsupported")
release = event.get("release")
manifest = event.get("manifest")
if not isinstance(release, dict) or not isinstance(manifest, dict):
    reject("release evidence is incomplete")
release_id = release.get("releaseId")
release_tag = release.get("releaseTag")
manifest_sha = release.get("manifestSha256")
manifest_source = manifest.get("sourceRun")
tools = manifest.get("deploymentTools")
sources = manifest.get("sources")
workspace_source = sources.get("workspace") if isinstance(sources, dict) else None
if (not isinstance(release_id, str) or re.fullmatch(r"teams-arm64-[0-9a-f]{12}-[0-9a-f]{12}", release_id) is None
        or release_tag != release_id
        or release.get("releaseBaseUrl") != RELEASES + release_id
        or not isinstance(manifest_sha, str) or re.fullmatch(r"[0-9a-f]{64}", manifest_sha) is None
        or manifest.get("releaseId") != release_id or manifest.get("releaseTag") != release_id
        or not isinstance(manifest_source, dict) or not isinstance(tools, dict)
        or not isinstance(workspace_source, dict)
        or tools.get("file") != "deployment-tools.tar.gz"
        or tools.get("sourceCommit") != manifest_source.get("headSha")
        or tools.get("sourceCommit") != workspace_source.get("commit")
        or not isinstance(tools.get("sizeBytes"), int) or isinstance(tools.get("sizeBytes"), bool)
        or not 0 < tools["sizeBytes"] <= MAX_ARCHIVE
        or not isinstance(tools.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", tools["sha256"]) is None):
    reject("release tool locator is invalid")
archive_url = RELEASES + release_id + "/deployment-tools.tar.gz"
request = urllib.request.Request(archive_url, headers={"User-Agent": "Cyrene-Azure-ADO-CD"})
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        final = urllib.parse.urlsplit(response.geturl())
        length = response.headers.get("Content-Length")
        if (final.scheme != "https" or final.hostname not in ALLOWED_HOSTS
                or (length is not None and int(length) > MAX_ARCHIVE)):
            reject("deployment tools redirect or size is invalid")
        archive_bytes = response.read(MAX_ARCHIVE + 1)
except (OSError, ValueError, TimeoutError):
    reject("deployment tools download failed")
if len(archive_bytes) != tools["sizeBytes"] or len(archive_bytes) > MAX_ARCHIVE:
    reject("deployment tools size mismatch")
if hashlib.sha256(archive_bytes).hexdigest() != tools["sha256"]:
    reject("deployment tools digest mismatch")

source_sha = tools["sourceCommit"]
prefix = PurePosixPath("Cyrene-Workspace-" + source_sha + "/tooling/deploy/teams-cloud")
required = {"azure_cd.py", "release_bundle.py", "build_arm64.py"}
seen = set()
extracted = set()
total = 0
with tempfile.TemporaryDirectory(prefix="cyrene-teams-cd-") as temporary:
    work = Path(temporary)
    os.chmod(work, 0o700)
    tool_root = work.joinpath(*prefix.parts)
    safe_directory(tool_root)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            for index, member in enumerate(archive):
                if index >= MAX_MEMBERS:
                    reject("deployment tools archive has too many files")
                if "\\" in member.name:
                    reject("deployment tools archive path is invalid")
                path = PurePosixPath(member.name)
                if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
                    reject("deployment tools archive path is invalid")
                is_prefix_member = path.parts[:len(prefix.parts)] == prefix.parts
                is_prefix_ancestor = (
                    member.isdir()
                    and 0 < len(path.parts) < len(prefix.parts)
                    and prefix.parts[:len(path.parts)] == path.parts
                )
                if path.as_posix() in seen or not (is_prefix_member or is_prefix_ancestor):
                    reject("deployment tools archive prefix or duplicate path is invalid")
                seen.add(path.as_posix())
                if not member.isdir() and not member.isfile():
                    reject("deployment tools archive contains a link or special file")
                total += member.size
                if total > MAX_ARCHIVE or member.size > MAX_ARCHIVE:
                    reject("deployment tools archive expands beyond its size bound")
                if is_prefix_ancestor:
                    continue
                relative = path.relative_to(prefix)
                if not relative.parts:
                    continue
                if member.isdir():
                    continue
                if len(relative.parts) != 1 or relative.name not in required:
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    reject("deployment tool entry is unreadable")
                content = stream.read(MAX_ARCHIVE + 1)
                if len(content) != member.size or len(content) > MAX_ARCHIVE:
                    reject("deployment tool entry has invalid size")
                destination = tool_root / relative.name
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                extracted.add(relative.name)
    except (OSError, EOFError, tarfile.TarError):
        reject("deployment tools archive is invalid")
    if extracted != required:
        reject("deployment tools archive is missing its pinned CD entrypoint")

    event_path = work / "event.json"
    descriptor = os.open(event_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    result = subprocess.run(
        [sys.executable, str(tool_root / "azure_cd.py"), "deploy", "--event-json", str(event_path)],
        check=False,
    )
    raise SystemExit(result.returncode)
"""


class AzureCdError(ValueError):
    """Raised when a publisher event, release asset, or guest receipt is unsafe."""


@dataclass(frozen=True)
class VerifiedRelease:
    """A publisher event after checking its run pins, manifest, and asset digests."""

    release_id: str
    release_tag: str
    release_base_url: str
    manifest_sha256: str
    manifest: dict[str, Any]
    event: dict[str, Any]


@dataclass(frozen=True)
class AzureCommandResult:
    """Bounded Azure CLI process result; stderr is never included in user receipts."""

    returncode: int | None
    stdout: str


@dataclass(frozen=True)
class DeploymentOutcome:
    """Final deployment state after a deploy call and any required readback."""

    success: bool
    state: str
    receipt: dict[str, Any]
    readback_performed: bool


RunCommand = Callable[[str, int], AzureCommandResult]
UrlOpener = Callable[..., Any]


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _require_text(value: object, label: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AzureCdError(f"{label} is invalid")
    return value


def _require_sha(value: object, label: str, *, length: int = 40) -> str:
    expression = GIT_SHA_RE if length == 40 else SHA256_RE
    if not isinstance(value, str) or expression.fullmatch(value) is None:
        raise AzureCdError(f"{label} is invalid")
    return value


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or ARTIFACT_DIGEST_RE.fullmatch(value) is None:
        raise AzureCdError(f"{label} is invalid")
    return value.removeprefix("sha256:")


def _safe_release_url(release_tag: str) -> str:
    if RELEASE_ID_RE.fullmatch(release_tag) is None:
        raise AzureCdError("release tag is invalid")
    return f"{RELEASE_BASE_PREFIX}{release_tag}"


def _read_bounded_bytes(
    response: Any,
    *,
    maximum_bytes: int,
    allowed_hosts: set[str],
    label: str,
    allowed_host_suffixes: tuple[str, ...] = (),
) -> bytes:
    """Read one bounded HTTPS response after checking every redirect destination."""

    final_url = response.geturl() if hasattr(response, "geturl") else ""
    split = urllib.parse.urlsplit(final_url)
    if (
        split.scheme != "https"
        or (
            split.hostname not in allowed_hosts
            and not any(
                split.hostname.endswith(suffix) and split.hostname != suffix.removeprefix(".")
                for suffix in allowed_host_suffixes
            )
        )
        or split.username
        or split.password
    ):
        raise AzureCdError(f"{label} redirected outside its trusted HTTPS hosts")
    headers = getattr(response, "headers", {})
    content_length = headers.get("Content-Length") if hasattr(headers, "get") else None
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except (TypeError, ValueError) as error:
            raise AzureCdError(f"{label} has invalid response headers") from error
        if declared_length < 0 or declared_length > maximum_bytes:
            raise AzureCdError(f"{label} exceeds its download size limit")
    body = response.read(maximum_bytes + 1)
    if len(body) > maximum_bytes:
        raise AzureCdError(f"{label} exceeds its download size limit")
    return body


def _github_json(endpoint: str, token: str, *, urlopen: UrlOpener) -> dict[str, Any]:
    """Read a bounded GitHub API object using the short-lived Actions token."""

    if not endpoint.startswith("/") or ".." in endpoint.split("/"):
        raise AzureCdError("GitHub API path is invalid")
    request = urllib.request.Request(
        GITHUB_API_ROOT + endpoint,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Cyrene-Teams-CD-Bridge",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=MAX_HTTP_SECONDS) as response:
            body = _read_bounded_bytes(
                response,
                maximum_bytes=MAX_MANIFEST_BYTES,
                allowed_hosts={"api.github.com"},
                label="GitHub API response",
            )
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        raise AzureCdError("GitHub publisher evidence could not be read") from error
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AzureCdError("GitHub API response is invalid JSON") from error
    if not isinstance(value, dict):
        raise AzureCdError("GitHub API response is not an object")
    return value


def _github_artifact_zip(artifact_id: int, token: str, *, urlopen: UrlOpener) -> bytes:
    """Download one exact proof artifact without extracting its untrusted ZIP paths."""

    request = urllib.request.Request(
        f"{GITHUB_API_ROOT}/repos/{WORKSPACE_REPOSITORY}/actions/artifacts/{artifact_id}/zip",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Cyrene-Teams-CD-Bridge",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=MAX_HTTP_SECONDS) as response:
            return _read_bounded_bytes(
                response,
                maximum_bytes=MAX_PROOF_ARCHIVE_BYTES,
                allowed_hosts={
                    "api.github.com",
                    "github.com",
                    "release-assets.githubusercontent.com",
                    "objects.githubusercontent.com",
                },
                label="publisher proof archive",
                allowed_host_suffixes=(".blob.core.windows.net",),
            )
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        raise AzureCdError("publisher proof artifact could not be downloaded") from error


def _proof_documents(
    archive_bytes: bytes,
) -> tuple[dict[str, Any], dict[str, Any], bytes, dict[str, bytes]]:
    """Read four flat proof files with strict size, member, duplicate, and type checks."""

    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            members = archive.infolist()
            if len(members) != len(PROOF_FILES):
                raise AzureCdError("publisher proof archive has an unexpected file count")
            names: set[str] = set()
            contents: dict[str, bytes] = {}
            limits = {
                "release-manifest.json": MAX_MANIFEST_BYTES,
                "image-lock.json": MAX_MANIFEST_BYTES,
                "deployment-tools.tar.gz": MAX_TOOLS_ARCHIVE_BYTES,
                "release-pointer.json": 16 * 1024,
            }
            total = 0
            for member in members:
                name = member.filename
                if (
                    name not in PROOF_FILES
                    or name in names
                    or "/" in name
                    or "\\" in name
                    or member.is_dir()
                    or member.flag_bits & 0x1
                    or member.file_size < 0
                    or member.file_size > limits.get(name, 0)
                ):
                    raise AzureCdError("publisher proof archive contains an unsafe member")
                mode = member.external_attr >> 16
                if mode and (mode & 0o170000) not in {0, 0o100000}:
                    raise AzureCdError("publisher proof archive contains a non-regular file")
                names.add(name)
                total += member.file_size
                if total > MAX_PROOF_ARCHIVE_BYTES:
                    raise AzureCdError("publisher proof archive expands beyond its size limit")
                payload = archive.read(member)
                if len(payload) != member.file_size:
                    raise AzureCdError("publisher proof archive member size is invalid")
                contents[name] = payload
            if names != PROOF_FILES:
                raise AzureCdError("publisher proof archive file set is incomplete")
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError) as error:
        if isinstance(error, AzureCdError):
            raise
        raise AzureCdError("publisher proof archive is invalid") from error

    def parse(name: str) -> dict[str, Any]:
        try:
            value = json.loads(contents[name].decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AzureCdError("publisher proof JSON is malformed") from error
        if not isinstance(value, dict):
            raise AzureCdError("publisher proof JSON root must be an object")
        return value

    manifest_bytes = contents["release-manifest.json"]
    return (
        parse("release-manifest.json"),
        parse("release-pointer.json"),
        manifest_bytes,
        contents,
    )


def _workflow_path_matches(run: dict[str, Any], expected_path: str) -> bool:
    """Require the exact workflow file and the trusted develop ref in GitHub run metadata."""

    path = run.get("path")
    return path == f"{expected_path}@refs/heads/develop"


def _trusted_run(
    run: dict[str, Any],
    *,
    workflow: str,
    event: str,
    run_id: int,
    run_attempt: int,
    head_sha: str,
    conclusion: str = "success",
) -> bool:
    """Check a completed canonical Workspace workflow run against its immutable pins."""

    repository = run.get("repository")
    head_repository = run.get("head_repository")
    return (
        run.get("id") == run_id
        and run.get("run_attempt") == run_attempt
        and run.get("status") == "completed"
        and run.get("conclusion") == conclusion
        and run.get("event") == event
        and run.get("head_branch") == "develop"
        and run.get("head_sha") == head_sha
        and isinstance(repository, dict)
        and repository.get("full_name") == WORKSPACE_REPOSITORY
        and isinstance(head_repository, dict)
        and head_repository.get("full_name") == WORKSPACE_REPOSITORY
        and _workflow_path_matches(run, workflow)
    )


def _release_asset_metadata(
    release_api: dict[str, Any], manifest: dict[str, Any], manifest_bytes: bytes
) -> list[dict[str, Any]]:
    """Bind every immutable release asset's API SHA-256 and byte count to the manifest."""

    expected: dict[str, tuple[str, int]] = {
        "release-manifest.json": (hashlib.sha256(manifest_bytes).hexdigest(), len(manifest_bytes)),
        "image-lock.json": (manifest["imageLock"]["sha256"], manifest["imageLock"]["sizeBytes"]),
        "deployment-tools.tar.gz": (
            manifest["deploymentTools"]["sha256"],
            manifest["deploymentTools"]["sizeBytes"],
        ),
    }
    for image in manifest["images"]:
        archive = image["archive"]
        expected[Path(archive["file"]).name] = (archive["sha256"], archive["sizeBytes"])
    observed = release_api.get("assets")
    if not isinstance(observed, list) or len(observed) != len(expected):
        raise AzureCdError("immutable release asset inventory is incomplete")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in observed:
        if not isinstance(row, dict):
            raise AzureCdError("immutable release asset metadata is malformed")
        name = row.get("name")
        digest = row.get("digest")
        size = row.get("size")
        if (
            not isinstance(name, str)
            or name in seen
            or name not in expected
            or not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or digest.removeprefix("sha256:") != expected[name][0]
            or not _is_positive_int(size)
            or (expected[name][1] and size != expected[name][1])
        ):
            raise AzureCdError("immutable release asset digest or size differs from its manifest")
        seen.add(name)
        result.append({"name": name, "digest": digest, "sizeBytes": size})
    if seen != set(expected):
        raise AzureCdError("immutable release asset inventory differs from its manifest")
    result.sort(key=lambda value: value["name"])
    return result


def _fetch_release_proof(
    *,
    run_id: int,
    run_attempt: int,
    expected_head_sha: str,
    expected_event: str,
    github_token: str,
    urlopen: UrlOpener,
) -> dict[str, Any] | None:
    """Resolve one immutable proof artifact and verify its publisher, source, and release."""

    if not _is_positive_int(run_id) or not _is_positive_int(run_attempt):
        raise AzureCdError("publisher workflow run identity is invalid")
    _require_sha(expected_head_sha, "publisher head SHA")
    if expected_event not in PUBLISHER_EVENTS:
        raise AzureCdError("publisher event is not permitted")
    if not isinstance(github_token, str) or not github_token:
        raise AzureCdError("GitHub Actions read token is missing")

    run = _github_json(
        f"/repos/{WORKSPACE_REPOSITORY}/actions/runs/{run_id}",
        github_token,
        urlopen=urlopen,
    )
    if not _trusted_run(
        run,
        workflow=PUBLISHER_WORKFLOW,
        event=expected_event,
        run_id=run_id,
        run_attempt=run_attempt,
        head_sha=expected_head_sha,
    ):
        raise AzureCdError("publisher run is not the exact successful trusted workflow attempt")
    if run.get("ref") != SOURCE_REF:
        raise AzureCdError("publisher workflow ref is not the trusted develop branch")

    artifact_list = _github_json(
        f"/repos/{WORKSPACE_REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100",
        github_token,
        urlopen=urlopen,
    )
    artifacts = artifact_list.get("artifacts")
    if (
        not isinstance(artifacts, list)
        or not isinstance(artifact_list.get("total_count"), int)
        or artifact_list["total_count"] != len(artifacts)
    ):
        raise AzureCdError("publisher proof artifact listing is incomplete")
    candidates = [
        row
        for row in artifacts
        if isinstance(row, dict)
        and isinstance(row.get("name"), str)
        and row["name"].startswith("cyrene-teams-arm64-")
        and row.get("expired") is not True
    ]
    if not candidates:
        # The publisher intentionally omits a proof when this immutable source was already released.
        return None
    if len(candidates) > 8:
        raise AzureCdError("publisher proof artifact attempt count exceeds its safe bound")
    repository = run.get("repository")
    repository_id = repository.get("id") if isinstance(repository, dict) else None
    matching: list[tuple[dict[str, Any], dict[str, Any], bytes, dict[str, Any]]] = []
    for artifact in candidates:
        artifact_id = artifact.get("id")
        artifact_size = artifact.get("size_in_bytes")
        digest = artifact.get("digest")
        workflow_run = artifact.get("workflow_run")
        if (
            not _is_positive_int(artifact_id)
            or not _is_positive_int(artifact_size)
            or artifact_size > MAX_PROOF_ARCHIVE_BYTES
            or not isinstance(digest, str)
            or ARTIFACT_DIGEST_RE.fullmatch(digest) is None
            or not isinstance(workflow_run, dict)
            or workflow_run.get("id") != run_id
            or workflow_run.get("repository_id") != repository_id
            or workflow_run.get("head_repository_id") != repository_id
            or workflow_run.get("head_branch") != "develop"
            or workflow_run.get("head_sha") != expected_head_sha
        ):
            raise AzureCdError("publisher artifact metadata is not bound to the successful run")
        archive_bytes = _github_artifact_zip(artifact_id, github_token, urlopen=urlopen)
        if (
            len(archive_bytes) != artifact_size
            or "sha256:" + hashlib.sha256(archive_bytes).hexdigest() != digest
        ):
            raise AzureCdError("publisher proof artifact digest differs from GitHub metadata")
        manifest, pointer, manifest_bytes, proof_files = _proof_documents(archive_bytes)
        try:
            summary = validate_release_manifest(manifest)
        except ReleaseBundleError as error:
            raise AzureCdError("publisher proof manifest failed source validation") from error
        for asset_record in (manifest["imageLock"], manifest["deploymentTools"]):
            proof_asset = proof_files[asset_record["file"]]
            if (
                len(proof_asset) != asset_record["sizeBytes"]
                or hashlib.sha256(proof_asset).hexdigest() != asset_record["sha256"]
            ):
                raise AzureCdError("proof artifact asset bytes differ from the release manifest")
        pointer_fields = {
            "schemaVersion",
            "publisherRun",
            "releaseId",
            "releaseTag",
            "releaseUrl",
            "sourceFingerprintSha256",
            "manifestSha256",
            "proofArtifactName",
        }
        pointer_publisher = pointer.get("publisherRun")
        manifest_publisher = manifest.get("publisherRun")
        expected_artifact_name = proof_artifact_name(manifest)
        if (
            set(pointer) != pointer_fields
            or not isinstance(pointer_publisher, dict)
            or not isinstance(manifest_publisher, dict)
            or pointer.get("schemaVersion") != "cyrene-teams-arm64-release-pointer-v1"
            or set(pointer_publisher)
            != {"repository", "workflow", "runId", "runAttempt", "headSha", "event"}
            or pointer.get("manifestSha256") != hashlib.sha256(manifest_bytes).hexdigest()
            or artifact.get("name") != expected_artifact_name
            or pointer.get("proofArtifactName") != expected_artifact_name
        ):
            raise AzureCdError("publisher proof pointer is malformed or not bound to its manifest")
        if (
            manifest_publisher.get("runId") != run_id
            or manifest_publisher.get("runAttempt") != run_attempt
            or manifest_publisher.get("headSha") != expected_head_sha
            or manifest_publisher.get("event") != expected_event
        ):
            # A rerun may leave earlier immutable artifacts listed for the same workflow run ID.
            continue
        if (
            manifest_publisher.get("repository") != WORKSPACE_REPOSITORY
            or manifest_publisher.get("workflow") != PUBLISHER_WORKFLOW
            or pointer_publisher != manifest_publisher
            or pointer.get("releaseId") != summary.get("releaseId")
            or pointer.get("releaseTag") != summary.get("releaseTag")
            or pointer.get("sourceFingerprintSha256") != summary.get("sourceFingerprintSha256")
            or pointer.get("releaseUrl")
            != f"https://github.com/{WORKSPACE_REPOSITORY}/releases/tag/{summary['releaseTag']}"
            or pointer.get("proofArtifactName") != artifact.get("name")
        ):
            raise AzureCdError("publisher proof pointer differs from the exact run and manifest")
        matching.append((artifact, manifest, manifest_bytes, pointer))

    if not matching:
        return None
    if len(matching) != 1:
        raise AzureCdError("multiple publisher proof artifacts match one run attempt")
    artifact, manifest, manifest_bytes, pointer = matching[0]
    source = manifest.get("sourceRun")
    if not isinstance(source, dict):
        raise AzureCdError("publisher manifest source run is missing")
    source_id = source.get("runId")
    source_attempt = source.get("runAttempt")
    source_sha = source.get("headSha")
    if not _is_positive_int(source_id) or not _is_positive_int(source_attempt):
        raise AzureCdError("pinned source workflow run identity is invalid")
    source_run = _github_json(
        f"/repos/{WORKSPACE_REPOSITORY}/actions/runs/{source_id}",
        github_token,
        urlopen=urlopen,
    )
    if (
        not _trusted_run(
            source_run,
            workflow=SOURCE_WORKFLOW,
            event="push",
            run_id=source_id,
            run_attempt=source_attempt,
            head_sha=source_sha,
        )
        or source_run.get("ref") != SOURCE_REF
    ):
        raise AzureCdError("manifest source run is not the exact successful develop push")

    release_id = manifest["releaseId"]
    release = _github_json(
        f"/repos/{WORKSPACE_REPOSITORY}/releases/tags/{release_id}",
        github_token,
        urlopen=urlopen,
    )
    if (
        release.get("tag_name") != release_id
        or release.get("immutable") is not True
        or release.get("draft") is not False
    ):
        raise AzureCdError("public GitHub release is not immutable or does not match the proof")
    assets = _release_asset_metadata(release, manifest, manifest_bytes)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    publisher_resource = {
        "repository": WORKSPACE_REPOSITORY,
        "workflow": PUBLISHER_WORKFLOW,
        "runId": run_id,
        "runAttempt": run_attempt,
        "headSha": expected_head_sha,
        "event": expected_event,
        "headBranch": "develop",
        "conclusion": "success",
    }
    return {
        "schemaVersion": EVENT_SCHEMA,
        "repository": WORKSPACE_REPOSITORY,
        "publisherRun": publisher_resource,
        "sourceRun": source,
        "artifact": {
            "id": artifact["id"],
            "name": artifact["name"],
            "digest": artifact["digest"],
            "sizeBytes": artifact["size_in_bytes"],
            "workflowRunId": run_id,
            "runAttempt": run_attempt,
            "headSha": expected_head_sha,
            "repository": WORKSPACE_REPOSITORY,
        },
        "release": {
            "releaseId": release_id,
            "releaseTag": release_id,
            "releaseBaseUrl": _safe_release_url(release_id),
            "immutable": True,
            "manifestSha256": manifest_sha256,
            "manifestSizeBytes": len(manifest_bytes),
            "assets": assets,
        },
        "releasePointer": pointer,
        "manifest": manifest,
    }


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent forwarding the one-time webhook HMAC header to a redirect target."""

    def redirect_request(
        self, request: Any, response: Any, code: int, message: str, headers: Any, new_url: str
    ) -> None:
        return None


def forward_github_release(
    *,
    run_id: int,
    run_attempt: int,
    head_sha: str,
    event: str,
    github_token: str,
    webhook_secret: str,
    github_urlopen: UrlOpener = urllib.request.urlopen,
    webhook_urlopen: UrlOpener | None = None,
) -> str:
    """Verify a successful publisher attempt and forward its immutable proof to ADO."""

    if not isinstance(webhook_secret, str) or len(webhook_secret.encode("utf-8")) < 32:
        raise AzureCdError("Azure DevOps webhook secret is missing or too short")
    resource = _fetch_release_proof(
        run_id=run_id,
        run_attempt=run_attempt,
        expected_head_sha=head_sha,
        expected_event=event,
        github_token=github_token,
        urlopen=github_urlopen,
    )
    if resource is None:
        return "NO_NEW_PROOF"
    body = json.dumps(
        {"resource": resource}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if len(body) > MAX_EVENT_BYTES:
        raise AzureCdError("verified Azure DevOps event exceeds its bounded payload size")
    signature = hmac.new(webhook_secret.encode("utf-8"), body, hashlib.sha1).hexdigest().upper()
    request = urllib.request.Request(
        ADO_WEBHOOK_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Cyrene-Signature": signature,
            "User-Agent": "Cyrene-Teams-CD-Bridge",
        },
    )
    opener = webhook_urlopen or urllib.request.build_opener(_NoRedirectHandler()).open
    try:
        with opener(request, timeout=MAX_HTTP_SECONDS) as response:
            status = getattr(response, "status", 200)
            if not isinstance(status, int) or not 200 <= status < 300:
                raise AzureCdError("Azure DevOps rejected the verified release event")
            response.read(4097)
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        raise AzureCdError("Azure DevOps webhook delivery failed") from error
    return "FORWARDED"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AzureCdError("release manifest contains duplicate JSON keys")
        result[key] = value
    return result


def _manifest_asset_rows(
    manifest: dict[str, Any], manifest_sha256: str, manifest_size: int
) -> dict[str, tuple[str, int]]:
    """Map public release asset basenames to manifest-bound SHA-256 digests and sizes."""

    rows: dict[str, tuple[str, int]] = {
        "release-manifest.json": (manifest_sha256, manifest_size),
    }
    lock = manifest["imageLock"]
    tools = manifest["deploymentTools"]
    rows[Path(lock["file"]).name] = (lock["sha256"], lock["sizeBytes"])
    rows[Path(tools["file"]).name] = (tools["sha256"], tools["sizeBytes"])
    for image in manifest["images"]:
        archive = image["archive"]
        name = Path(archive["file"]).name
        if name in rows:
            raise AzureCdError("manifest release asset names collide")
        rows[name] = (archive["sha256"], archive["sizeBytes"])
    return rows


def validate_release_event(resource: object) -> VerifiedRelease:
    """Validate the HMAC-authenticated Azure webhook body without provider calls."""

    event_fields = {
        "schemaVersion",
        "repository",
        "publisherRun",
        "sourceRun",
        "artifact",
        "release",
        "releasePointer",
        "manifest",
    }
    if (
        not isinstance(resource, dict)
        or set(resource) != event_fields
        or resource.get("schemaVersion") != EVENT_SCHEMA
    ):
        raise AzureCdError("release event schema is unsupported")
    if resource.get("repository") != WORKSPACE_REPOSITORY:
        raise AzureCdError("release event repository is not trusted")

    publisher = resource.get("publisherRun")
    source = resource.get("sourceRun")
    release = resource.get("release")
    manifest = resource.get("manifest")
    artifact = resource.get("artifact")
    if not all(isinstance(row, dict) for row in (publisher, source, release, manifest, artifact)):
        raise AzureCdError("release event is missing a required object")

    if (
        set(publisher)
        != {
            "repository",
            "workflow",
            "runId",
            "runAttempt",
            "headSha",
            "event",
            "headBranch",
            "conclusion",
        }
        or set(source)
        != {
            "repository",
            "workflow",
            "runId",
            "runAttempt",
            "headSha",
            "ref",
            "event",
            "conclusion",
        }
        or set(release)
        != {
            "releaseId",
            "releaseTag",
            "releaseBaseUrl",
            "immutable",
            "manifestSha256",
            "manifestSizeBytes",
            "assets",
        }
        or set(artifact)
        != {
            "id",
            "name",
            "digest",
            "sizeBytes",
            "workflowRunId",
            "runAttempt",
            "headSha",
            "repository",
        }
        or publisher.get("repository") != WORKSPACE_REPOSITORY
        or publisher.get("workflow") != PUBLISHER_WORKFLOW
        or publisher.get("headBranch") != "develop"
        or publisher.get("conclusion") != "success"
        or publisher.get("event") not in PUBLISHER_EVENTS
        or not _is_positive_int(publisher.get("runId"))
        or not _is_positive_int(publisher.get("runAttempt"))
    ):
        raise AzureCdError("publisher run is not a successful trusted develop release")
    publisher_head = _require_sha(publisher.get("headSha"), "publisher head SHA")

    if (
        source.get("repository") != WORKSPACE_REPOSITORY
        or source.get("workflow") != SOURCE_WORKFLOW
        or source.get("ref") != SOURCE_REF
        or source.get("event") != "push"
        or source.get("conclusion") != "success"
        or not _is_positive_int(source.get("runId"))
        or not _is_positive_int(source.get("runAttempt"))
    ):
        raise AzureCdError("source run is not a successful canonical develop push")
    source_head = _require_sha(source.get("headSha"), "source head SHA")

    try:
        summary = validate_release_manifest(manifest)
    except ReleaseBundleError as error:
        raise AzureCdError("release manifest failed its trusted source validation") from error
    if not isinstance(summary, dict) or summary.get("status") != "VERIFIED":
        raise AzureCdError("release manifest validator returned no verified result")

    manifest_publisher = manifest.get("publisherRun")
    manifest_source = manifest.get("sourceRun")
    sources = manifest.get("sources")
    if not isinstance(manifest_publisher, dict) or not isinstance(manifest_source, dict):
        raise AzureCdError("release manifest run records are malformed")
    if not isinstance(sources, dict) or not isinstance(sources.get("workspace"), dict):
        raise AzureCdError("release manifest workspace source pin is missing")
    publisher_fields = ("repository", "workflow", "runId", "runAttempt", "headSha", "event")
    if any(publisher.get(key) != manifest_publisher.get(key) for key in publisher_fields):
        raise AzureCdError("publisher event identity differs from the release manifest")
    source_fields = (
        "repository",
        "workflow",
        "runId",
        "runAttempt",
        "headSha",
        "ref",
        "event",
        "conclusion",
    )
    if any(source.get(key) != manifest_source.get(key) for key in source_fields):
        raise AzureCdError("source event identity differs from the release manifest")
    if sources["workspace"].get("commit") != source_head:
        raise AzureCdError("manifest workspace source SHA differs from the trusted source run")

    release_id = _require_text(release.get("releaseId"), "release ID", maximum=128)
    release_tag = _require_text(release.get("releaseTag"), "release tag", maximum=128)
    manifest_release_id = _require_text(
        manifest.get("releaseId"), "manifest release ID", maximum=128
    )
    if (
        release_id != manifest_release_id
        or release_tag != manifest.get("releaseTag")
        or release_tag != release_id
        or release_id != summary.get("releaseId")
        or release_id != summary.get("releaseTag")
        or release.get("immutable") is not True
    ):
        raise AzureCdError(
            "release identity or immutable status does not match the verified manifest"
        )
    release_base_url = _safe_release_url(release_tag)
    if release.get("releaseBaseUrl") != release_base_url:
        raise AzureCdError("release URL is not the canonical public Workspace release URL")

    manifest_sha256 = _require_sha(release.get("manifestSha256"), "manifest SHA-256", length=64)
    source_fingerprint = _require_sha(
        manifest.get("sourceFingerprintSha256"), "source fingerprint", length=64
    )
    artifact_name = _require_text(artifact.get("name"), "publisher artifact name", maximum=256)
    expected_artifact_name = proof_artifact_name(manifest)
    if (
        ARTIFACT_NAME_RE.fullmatch(artifact_name) is None
        or artifact_name != expected_artifact_name
        or artifact.get("workflowRunId") != publisher.get("runId")
        or artifact.get("headSha") != publisher_head
        or artifact.get("runAttempt") != publisher.get("runAttempt")
        or artifact.get("repository") != WORKSPACE_REPOSITORY
        or not _is_positive_int(artifact.get("id"))
        or not _is_positive_int(artifact.get("workflowRunId"))
        or not _is_positive_int(artifact.get("runAttempt"))
        or not _is_positive_int(artifact.get("sizeBytes"))
        or artifact["sizeBytes"] > 256 * 1024 * 1024
    ):
        raise AzureCdError("immutable publisher proof artifact is not bound to this run attempt")
    _require_digest(artifact.get("digest"), "publisher artifact digest")

    pointer = resource.get("releasePointer")
    if not isinstance(pointer, dict):
        raise AzureCdError("release pointer is missing")
    pointer_publisher = pointer.get("publisherRun")
    if (
        set(pointer)
        != {
            "schemaVersion",
            "publisherRun",
            "releaseId",
            "releaseTag",
            "releaseUrl",
            "sourceFingerprintSha256",
            "manifestSha256",
            "proofArtifactName",
        }
        or not isinstance(pointer_publisher, dict)
        or set(pointer_publisher)
        != {"repository", "workflow", "runId", "runAttempt", "headSha", "event"}
    ):
        raise AzureCdError("release pointer publisher identity is missing")
    if (
        pointer.get("schemaVersion") != "cyrene-teams-arm64-release-pointer-v1"
        or pointer.get("releaseId") != release_id
        or pointer.get("releaseTag") != release_tag
        or pointer.get("sourceFingerprintSha256") != source_fingerprint
        or pointer.get("manifestSha256") != manifest_sha256
        or pointer.get("proofArtifactName") != artifact_name
        or pointer.get("releaseUrl")
        != f"https://github.com/{WORKSPACE_REPOSITORY}/releases/tag/{release_tag}"
        or any(
            pointer_publisher.get(key) != manifest_publisher.get(key) for key in publisher_fields
        )
    ):
        raise AzureCdError("release pointer differs from the verified publisher manifest")

    raw_assets = release.get("assets")
    if not isinstance(raw_assets, list):
        raise AzureCdError("release asset metadata is missing")
    manifest_sizes = release.get("manifestSizeBytes")
    if not _is_positive_int(manifest_sizes) or manifest_sizes > MAX_MANIFEST_BYTES:
        raise AzureCdError("release manifest asset size is invalid")
    expected_assets = _manifest_asset_rows(manifest, manifest_sha256, manifest_sizes)
    if set(summary.get("assets", [])) != set(expected_assets):
        raise AzureCdError("manifest asset inventory differs from the trusted validator")
    observed_assets: dict[str, tuple[str, int]] = {}
    for row in raw_assets:
        if not isinstance(row, dict) or set(row) != {"name", "digest", "sizeBytes"}:
            raise AzureCdError("release asset metadata row is malformed")
        name = _require_text(row.get("name"), "release asset name", maximum=256)
        if ASSET_NAME_RE.fullmatch(name) is None or name in observed_assets:
            raise AzureCdError("release asset name is unsafe or duplicated")
        digest = _require_digest(row.get("digest"), f"{name} release digest")
        size = row.get("sizeBytes")
        if not _is_positive_int(size):
            raise AzureCdError(f"{name} release size is invalid")
        observed_assets[name] = (digest, size)
    if observed_assets != expected_assets:
        raise AzureCdError("immutable release asset digests differ from the manifest")

    return VerifiedRelease(
        release_id=release_id,
        release_tag=release_tag,
        release_base_url=release_base_url,
        manifest_sha256=manifest_sha256,
        manifest=manifest,
        event=resource,
    )


def _release_manifest_url(release_tag: str) -> str:
    return f"{_safe_release_url(release_tag)}/release-manifest.json"


def _read_bounded_response(response: Any, *, maximum_bytes: int, label: str) -> bytes:
    final_url = response.geturl() if hasattr(response, "geturl") else ""
    split = urllib.parse.urlsplit(final_url)
    if split.scheme == "https" and split.hostname == "api.github.com":
        hosts = {"api.github.com"}
    else:
        hosts = {
            "github.com",
            "release-assets.githubusercontent.com",
            "objects.githubusercontent.com",
        }
    return _read_bounded_bytes(
        response, maximum_bytes=maximum_bytes, allowed_hosts=hosts, label=label
    )


def fetch_and_validate_manifest(
    evidence: VerifiedRelease,
    *,
    urlopen: UrlOpener = urllib.request.urlopen,
) -> dict[str, Any]:
    """Fetch the public manifest and bind its bytes to the HMAC-authenticated event."""

    request = urllib.request.Request(
        _release_manifest_url(evidence.release_tag),
        headers={"Accept": "application/octet-stream", "User-Agent": "Cyrene-Azure-CD"},
    )
    try:
        with urlopen(request, timeout=MAX_HTTP_SECONDS) as response:
            body = _read_bounded_response(
                response, maximum_bytes=MAX_MANIFEST_BYTES, label="release manifest"
            )
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        raise AzureCdError("public release manifest could not be downloaded") from error
    digest = hashlib.sha256(body).hexdigest()
    if digest != evidence.manifest_sha256:
        raise AzureCdError("public release manifest digest differs from the signed publisher event")
    try:
        manifest = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AzureCdError("public release manifest is not valid UTF-8 JSON") from error
    if manifest != evidence.manifest:
        raise AzureCdError("public release manifest contents differ from the signed proof artifact")
    try:
        summary = validate_release_manifest(manifest)
    except ReleaseBundleError as error:
        raise AzureCdError("public release manifest failed its pinned-source validation") from error
    if summary.get("status") != "VERIFIED" or summary.get("releaseId") != evidence.release_id:
        raise AzureCdError("public release manifest did not return the expected verified identity")
    return manifest


def _guest_bootstrap_body() -> str:
    """Return fixed guest Python that stages tools and delegates to the trusted release CLI."""

    return r"""import base64
import gzip
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

CONFIG = json.loads(base64.b64decode(sys.argv[1], validate=True))
ROOT_BASE = Path("/var/lib/cyrene-connectors-runtime/.artifacts/releases")
POLICY = Path("/etc/cyrene/teams-cloud-deployment.json")
RECEIPT_SCHEMA = "cyrene-teams-release-deployment-receipt-v1"
RELEASE_ID_RE = re.compile(r"teams-arm64-[0-9a-f]{12}-[0-9a-f]{12}\Z")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_JSON = 4 * 1024 * 1024
MAX_TOOLS = 64 * 1024 * 1024
MAX_MEMBERS = 8192
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024


def fail(code):
    result = {"schemaVersion": RECEIPT_SCHEMA, "releaseId": CONFIG.get("releaseId", ""),
              "manifestSha256": CONFIG.get("manifestSha256", ""), "state": "HELD",
              "steps": [], "selection": None,
              "updatedAtUtc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
              "errorCode": code}
    print("CYRENE_RELEASE_RECEIPT_JSON: " + json.dumps(result, sort_keys=True, separators=(",", ":")))
    raise SystemExit(2)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def safe_fetch(url, limit, label):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "github.com" or parts.username or parts.password:
        fail("INVALID_RELEASE_URL")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Cyrene-Teams-CD"}), timeout=30) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname not in {
                "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"
            }:
                fail("RELEASE_REDIRECT_REJECTED")
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > limit:
                fail(label + "_OVERSIZED")
            body = response.read(limit + 1)
    except (OSError, ValueError, TimeoutError):
        fail(label + "_DOWNLOAD_FAILED")
    if len(body) > limit:
        fail(label + "_OVERSIZED")
    return body


def regular_path(path):
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(base):
        fail("RELEASE_PATH_ESCAPE")
    current = base
    relative = path.relative_to(base)
    if current.is_symlink() or not current.is_dir() or current.stat().st_uid != 0 or stat.S_IMODE(current.stat().st_mode) != 0o700:
        fail("UNSAFE_RELEASE_DIRECTORY")
    for item in relative.parts[:-1]:
        current = current / item
        if current.exists():
            metadata = current.lstat()
            if (stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700):
                fail("UNSAFE_RELEASE_DIRECTORY")
    if path.exists() and (path.is_symlink() or not path.is_file()):
        fail("UNSAFE_RELEASE_FILE")
    return path


def ensure_directory(path, *, private):
    if not path.exists():
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError:
            fail("RELEASE_DIRECTORY_UNAVAILABLE")
    try:
        metadata = path.lstat()
    except OSError:
        fail("RELEASE_DIRECTORY_UNAVAILABLE")
    if (stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or (private and stat.S_IMODE(metadata.st_mode) != 0o700)
            or (not private and stat.S_IMODE(metadata.st_mode) & 0o022)):
        fail("UNSAFE_RELEASE_DIRECTORY")


def ensure_nested_directories(path):
    relative = path.relative_to(base)
    current = base
    for part in relative.parts:
        current = current / part
        ensure_directory(current, private=True)


def store_file(path, data, expected_sha, expected_size):
    regular_path(path)
    if len(data) != expected_size or sha256(data) != expected_sha:
        fail("RELEASE_ASSET_DIGEST_MISMATCH")
    ensure_nested_directories(path.parent)
    regular_path(path)
    if path.exists():
        existing = path.read_bytes()
        if sha256(existing) != expected_sha or len(existing) != expected_size:
            fail("EXISTING_RELEASE_ASSET_CONFLICT")
        return
    temporary = path.with_name(path.name + ".part-" + uuid.uuid4().hex)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if sha256(temporary.read_bytes()) != expected_sha:
            fail("STAGED_RELEASE_ASSET_DIGEST_MISMATCH")
        os.link(temporary, path, follow_symlinks=False)
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            fail("EXISTING_RELEASE_ASSET_CONFLICT")
        existing = path.read_bytes()
        if sha256(existing) != expected_sha or len(existing) != expected_size:
            fail("EXISTING_RELEASE_ASSET_CONFLICT")
    except OSError:
        fail("RELEASE_ASSET_WRITE_FAILED")
    finally:
        temporary.unlink(missing_ok=True)
    os.chmod(path, 0o600)


def read_receipt(path, release_id, manifest_sha):
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        fail("UNSAFE_CONTROLLER_RECEIPT")
    try:
        document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        fail("INVALID_CONTROLLER_RECEIPT")
    if (not isinstance(document, dict) or document.get("schemaVersion") != RECEIPT_SCHEMA
            or document.get("releaseId") != release_id or document.get("manifestSha256") != manifest_sha):
        fail("CONTROLLER_RECEIPT_IDENTITY_MISMATCH")
    return document


def emit(receipt):
    encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 3584:
        receipt = {"schemaVersion": RECEIPT_SCHEMA, "releaseId": CONFIG["releaseId"],
                   "manifestSha256": CONFIG["manifestSha256"], "state": "UNKNOWN", "steps": [],
                   "selection": None, "updatedAtUtc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                   "errorCode": "GUEST_RECEIPT_TOO_LARGE"}
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    print("CYRENE_RELEASE_RECEIPT_JSON: " + encoded)
    raise SystemExit(0 if receipt.get("state") in {"APPLIED", "NO_CHANGE"} else 2)


release_id = CONFIG.get("releaseId")
release_tag = CONFIG.get("releaseTag")
release_url = CONFIG.get("releaseBaseUrl")
manifest_sha = CONFIG.get("manifestSha256")
tools_sha = CONFIG.get("deploymentToolsSha256")
source_sha = CONFIG.get("sourceCommit")
if (not isinstance(release_id, str) or RELEASE_ID_RE.fullmatch(release_id) is None
        or release_tag != release_id or not isinstance(manifest_sha, str) or SHA_RE.fullmatch(manifest_sha) is None
        or not isinstance(tools_sha, str) or SHA_RE.fullmatch(tools_sha) is None
        or not isinstance(source_sha, str) or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
        or release_url != "https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/download/" + release_tag):
    fail("INVALID_RELEASE_CONFIGURATION")

runtime_root = ROOT_BASE.parents[1]
artifact_root = ROOT_BASE.parent
ensure_directory(runtime_root, private=False)
ensure_directory(artifact_root, private=True)
ensure_directory(ROOT_BASE, private=True)
base = ROOT_BASE / release_id
ensure_directory(base, private=True)

manifest_url = release_url + "/release-manifest.json"
manifest_bytes = safe_fetch(manifest_url, MAX_JSON, "MANIFEST")
if sha256(manifest_bytes) != manifest_sha:
    fail("MANIFEST_DIGEST_MISMATCH")
try:
    manifest = json.loads(manifest_bytes.decode("utf-8"), object_pairs_hook=unique_pairs)
except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
    fail("MANIFEST_INVALID_JSON")
if (not isinstance(manifest, dict) or manifest.get("releaseId") != release_id
        or manifest.get("releaseTag") != release_tag or manifest.get("sourceRun", {}).get("headSha") != source_sha
        or manifest.get("deploymentTools", {}).get("sourceCommit") != source_sha
        or manifest.get("deploymentTools", {}).get("sha256") != tools_sha):
    fail("MANIFEST_IDENTITY_MISMATCH")
tools_record = manifest["deploymentTools"]
if (tools_record.get("file") != "deployment-tools.tar.gz" or not isinstance(tools_record.get("sizeBytes"), int)
        or isinstance(tools_record.get("sizeBytes"), bool) or not 0 < tools_record["sizeBytes"] <= MAX_TOOLS):
    fail("TOOLS_RECORD_INVALID")
manifest_path = base / "release-manifest.json"
store_file(manifest_path, manifest_bytes, manifest_sha, len(manifest_bytes))
tools_url = release_url + "/deployment-tools.tar.gz"
tools_bytes = safe_fetch(tools_url, MAX_TOOLS, "TOOLS")
store_file(base / "deployment-tools.tar.gz", tools_bytes, tools_sha, tools_record["sizeBytes"])

prefix = PurePosixPath("Cyrene-Workspace-" + source_sha + "/tooling/deploy/teams-cloud")
seen = set()
total = 0
required_files = {"azure_cd.py", "release_bundle.py", "build_arm64.py", "release_deploy.py"}
extracted = set()
try:
    archive = tarfile.open(fileobj=io.BytesIO(tools_bytes), mode="r:gz")
    with archive:
        for index, member in enumerate(archive):
            if index >= MAX_MEMBERS:
                fail("TOOLS_ARCHIVE_MEMBER_LIMIT")
            if "\\\\" in member.name:
                fail("TOOLS_ARCHIVE_PATH_INVALID")
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or ".." in member_path.parts or "." in member_path.parts:
                fail("TOOLS_ARCHIVE_PATH_INVALID")
            normalized = member_path.as_posix()
            if normalized in seen:
                fail("TOOLS_ARCHIVE_DUPLICATE_MEMBER")
            seen.add(normalized)
            if not member.isdir() and not member.isfile():
                fail("TOOLS_ARCHIVE_LINK_OR_SPECIAL_FILE")
            total += member.size
            if total > MAX_TOTAL_BYTES or member.size > MAX_TOTAL_BYTES:
                fail("TOOLS_ARCHIVE_SIZE_LIMIT")
            is_prefix_member = member_path.parts[:len(prefix.parts)] == prefix.parts
            is_prefix_ancestor = (
                member.isdir()
                and 0 < len(member_path.parts) < len(prefix.parts)
                and prefix.parts[:len(member_path.parts)] == member_path.parts
            )
            if not (is_prefix_member or is_prefix_ancestor):
                fail("TOOLS_ARCHIVE_PREFIX_INVALID")
            if is_prefix_ancestor:
                continue
            relative = member_path.relative_to(prefix)
            target = base.joinpath(*relative.parts)
            if member.isdir():
                if not relative.parts:
                    continue
                ensure_nested_directories(target)
                continue
            if not relative.parts or any(part.startswith(".") for part in relative.parts):
                fail("TOOLS_ARCHIVE_FILE_PATH_INVALID")
            ensure_nested_directories(target.parent)
            regular_path(target)
            stream = archive.extractfile(member)
            if stream is None:
                fail("TOOLS_ARCHIVE_FILE_UNREADABLE")
            data = stream.read(MAX_MEMBER_BYTES + 1)
            if len(data) != member.size or len(data) > MAX_MEMBER_BYTES:
                fail("TOOLS_ARCHIVE_FILE_SIZE_INVALID")
            store_file(target, data, sha256(data), len(data))
            extracted.add(relative.name if len(relative.parts) == 1 else "")
except (OSError, EOFError, tarfile.TarError):
    fail("TOOLS_ARCHIVE_INVALID")
if not required_files.issubset(extracted):
    fail("TOOLS_ARCHIVE_REQUIRED_SCRIPT_MISSING")
tools_dir = base
sys.path.insert(0, str(tools_dir))
try:
    from release_bundle import ReleaseBundleError, validate_release_manifest
    summary = validate_release_manifest(manifest)
except Exception:
    fail("MANIFEST_VALIDATION_FAILED")
if summary.get("status") != "VERIFIED" or summary.get("releaseId") != release_id:
    fail("MANIFEST_VALIDATION_FAILED")

deployer = tools_dir / "release_deploy.py"
if deployer.is_symlink() or not deployer.is_file():
    fail("DEPLOYMENT_TOOL_MISSING")
intent_path = base / "bootstrap-execution-intent.json"
status_receipt_path = base / ("status-readback-" + uuid.uuid4().hex + ".json")
deploy_receipt_path = base / ("deployment-" + uuid.uuid4().hex + ".json")
reconciler_receipt_path = base / ("reconciler-install-" + uuid.uuid4().hex + ".json")


def run_cli(arguments, receipt_path):
    command = [sys.executable, str(deployer), *arguments, "--receipt", str(receipt_path)]
    try:
        timeout = 5400 if arguments[0] == "deploy" else 300
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if receipt_path.exists():
        try:
            if receipt_path.is_symlink() or not receipt_path.is_file() or receipt_path.stat().st_size > 65536:
                return None
            value = json.loads(receipt_path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
            if (isinstance(value, dict) and value.get("schemaVersion") == RECEIPT_SCHEMA
                    and value.get("releaseId") == release_id
                    and (value.get("manifestSha256") == manifest_sha
                         or (value.get("state") == "NOT_FOUND"
                             and value.get("manifestSha256") in {None, ""}))):
                return value
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
    for line in result.stdout.splitlines():
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if (isinstance(value, dict) and value.get("schemaVersion") == RECEIPT_SCHEMA
                and value.get("releaseId") == release_id
                and (value.get("manifestSha256") == manifest_sha
                     or (value.get("state") == "NOT_FOUND"
                         and value.get("manifestSha256") in {None, ""}))):
            return value
    return None


status_args = ["status", "--policy", str(POLICY), "--release-id", release_id]
status = run_cli(status_args, status_receipt_path)
if status is None:
    fail("CANONICAL_STATUS_READBACK_UNKNOWN")
if status.get("state") in {"APPLIED", "NO_CHANGE"}:
    emit(status)
intent_exists = intent_path.exists()
resume_states = {"SAFE_TO_RESUME", "ACCEPTANCE_PENDING"}
if intent_exists:
    if intent_path.is_symlink() or not intent_path.is_file() or intent_path.stat().st_size > 4096:
        fail("UNSAFE_EXECUTION_INTENT")
    try:
        existing_intent = json.loads(intent_path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        fail("INVALID_EXECUTION_INTENT")
    if (not isinstance(existing_intent, dict) or existing_intent.get("releaseId") != release_id
            or existing_intent.get("manifestSha256") != manifest_sha):
        fail("EXECUTION_INTENT_IDENTITY_MISMATCH")
    if status.get("state") not in resume_states or status.get("mayResumeSafely") is not True:
        if status.get("state") == "NOT_FOUND":
            status["state"] = "RECOVERY_REQUIRED"
            status["manifestSha256"] = manifest_sha
            status["errorCode"] = "EXECUTION_INTENT_WITHOUT_CANONICAL_JOURNAL"
        emit(status)
elif (
    status.get("state") != "NOT_FOUND"
    and not (
        status.get("state") in resume_states
        and status.get("mayResumeSafely") is True
    )
):
    emit(status)

install_args = [
    "install-reconciler",
    "--policy",
    str(POLICY),
    "--release-id",
    release_id,
    "--expected-manifest-sha256",
    manifest_sha,
]
reconciler = run_cli(install_args, reconciler_receipt_path)
if reconciler is None:
    fail("RECONCILER_INSTALL_READBACK_UNKNOWN")
if reconciler.get("state") not in {"INSTALLED", "NO_CHANGE"}:
    emit(reconciler)

if not intent_exists:
    intent = {"releaseId": release_id, "manifestSha256": manifest_sha,
              "createdAtUtc": datetime.now(UTC).isoformat().replace("+00:00", "Z")}
    try:
        descriptor = os.open(intent_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(intent, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        fail("CONCURRENT_DEPLOYMENT_INTENT")

deploy_args = ["deploy", "--policy", str(POLICY), "--manifest", str(manifest_path),
               "--expected-manifest-sha256", manifest_sha, "--release-base-url", release_url, "--execute"]
deployed = run_cli(deploy_args, deploy_receipt_path)
if deployed is not None:
    emit(deployed)
fail("CANONICAL_DEPLOYMENT_RECEIPT_UNKNOWN")
"""


def build_guest_script(evidence: VerifiedRelease) -> str:
    """Build a fixed guest bootstrap script with only validated scalar configuration."""

    manifest = evidence.manifest
    tools = manifest["deploymentTools"]
    config = {
        "releaseId": evidence.release_id,
        "releaseTag": evidence.release_tag,
        "releaseBaseUrl": evidence.release_base_url,
        "manifestSha256": evidence.manifest_sha256,
        "deploymentToolsSha256": tools["sha256"],
        "sourceCommit": tools["sourceCommit"],
    }
    encoded = base64.b64encode(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return f"python3 - {encoded} <<'CYRENE_GUEST_BOOTSTRAP'\n{_guest_bootstrap_body()}\nCYRENE_GUEST_BOOTSTRAP"


def build_guest_status_script(release_id: str, manifest_sha256: str = "") -> str:
    """Build a fixed status-only guest script; it never downloads or deploys a release."""

    if RELEASE_ID_RE.fullmatch(release_id) is None:
        raise AzureCdError("release ID is invalid")
    if manifest_sha256:
        _require_sha(manifest_sha256, "manifest SHA-256", length=64)
    encoded = base64.b64encode(
        json.dumps(
            {"releaseId": release_id, "manifestSha256": manifest_sha256},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).decode("ascii")
    return f"python3 - {encoded} <<'CYRENE_RELEASE_STATUS'\n{_guest_status_body()}\nCYRENE_RELEASE_STATUS"


def _guest_status_body() -> str:
    """Return a read-only guest script for checking one fixed release ID."""

    return r"""import base64
import json
import os
import re
import subprocess
import sys
import uuid
import hashlib
import stat
from pathlib import Path

CONFIG = json.loads(base64.b64decode(sys.argv[1], validate=True))
release_id = CONFIG.get("releaseId")
expected_manifest_sha = CONFIG.get("manifestSha256", "")
root = Path("/var/lib/cyrene-connectors-runtime/.artifacts/releases")
policy = Path("/etc/cyrene/teams-cloud-deployment.json")
schema = "cyrene-teams-release-deployment-receipt-v1"
if not isinstance(release_id, str) or re.fullmatch(r"teams-arm64-[0-9a-f]{12}-[0-9a-f]{12}", release_id) is None:
    raise SystemExit(2)
base = root / release_id
if root.is_symlink() or base.is_symlink() or not base.is_dir():
    print("CYRENE_RELEASE_RECEIPT_JSON: " + json.dumps({"schemaVersion": schema, "releaseId": release_id,
          "manifestSha256": "", "state": "NOT_FOUND", "steps": [], "selection": None,
          "updatedAtUtc": "", "errorCode": None}, separators=(",", ":")))
    raise SystemExit(0)
binding_path = base / "deployment-binding.json"
intent_path = base / "bootstrap-execution-intent.json"
if binding_path.is_symlink() or not binding_path.exists():
    state = "RECOVERY_REQUIRED" if intent_path.exists() else "NOT_FOUND"
    print("CYRENE_RELEASE_RECEIPT_JSON: " + json.dumps({"schemaVersion": schema, "releaseId": release_id,
          "manifestSha256": expected_manifest_sha if state != "NOT_FOUND" else "", "state": state, "steps": [], "selection": None,
          "updatedAtUtc": "", "errorCode": "EXECUTION_INTENT_WITHOUT_CANONICAL_JOURNAL" if intent_path.exists() else None}, separators=(",", ":")))
    raise SystemExit(2 if state != "NOT_FOUND" else 0)
try:
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    manifest_sha = binding["manifestSha256"]
    manifest_path = base / "release-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    source_commit = manifest["sources"]["workspace"]["commit"]
except (OSError, KeyError, json.JSONDecodeError, TypeError):
    raise SystemExit(2)
if (hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha
        or (expected_manifest_sha and expected_manifest_sha != manifest_sha)
        or manifest.get("releaseId") != release_id):
    raise SystemExit(2)
deployer = base / "release_deploy.py"
if deployer.is_symlink() or not deployer.is_file():
    raise SystemExit(2)
receipt_path = base / ("status-only-" + uuid.uuid4().hex + ".json")
result = subprocess.run([sys.executable, str(deployer), "status", "--policy", str(policy),
                         "--release-id", release_id, "--receipt", str(receipt_path)],
                        capture_output=True, text=True, timeout=300, check=False)
try:
    if receipt_path.is_symlink() or not receipt_path.is_file() or receipt_path.stat().st_size > 65536:
        raise ValueError("unsafe status receipt")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    receipt = None
if (not isinstance(receipt, dict) or receipt.get("schemaVersion") != schema
        or receipt.get("releaseId") != release_id or receipt.get("manifestSha256") != manifest_sha):
    print("CYRENE_RELEASE_RECEIPT_JSON: " + json.dumps({"schemaVersion": schema, "releaseId": release_id,
          "manifestSha256": manifest_sha, "state": "UNKNOWN", "steps": [], "selection": None,
          "updatedAtUtc": "", "errorCode": "STATUS_READBACK_UNKNOWN"}, separators=(",", ":")))
    raise SystemExit(2)
if intent_path.exists() and receipt.get("state") == "NOT_FOUND":
    receipt["state"] = "RECOVERY_REQUIRED"
    receipt["errorCode"] = "EXECUTION_INTENT_WITHOUT_CANONICAL_JOURNAL"
encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
if len(encoded.encode("utf-8")) > 3584:
    raise SystemExit(2)
print("CYRENE_RELEASE_RECEIPT_JSON: " + encoded)
raise SystemExit(0 if receipt.get("state") in {"APPLIED", "NO_CHANGE"} else 2)
"""


def _receipt_from_azure_output(
    output: str, *, release_id: str, manifest_sha256: str
) -> dict[str, Any] | None:
    """Extract the single guest receipt line from Azure's bounded RunCommand envelope."""

    if not isinstance(output, str) or len(output.encode("utf-8", errors="replace")) > 1024 * 1024:
        return None
    candidates: list[dict[str, Any]] = []
    lines = output.splitlines()
    for line in lines:
        marker = "CYRENE_RELEASE_RECEIPT_JSON:"
        if marker not in line:
            continue
        encoded = line.split(marker, 1)[1].strip()
        try:
            value = json.loads(encoded)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            candidates.append(value)
    if len(candidates) != 1:
        return None
    receipt = candidates[0]
    if (
        receipt.get("schemaVersion") != RECEIPT_SCHEMA
        or receipt.get("releaseId") != release_id
        or receipt.get("state")
        not in {
            "APPLIED",
            "NO_CHANGE",
            "PLANNED",
            "HELD",
            "FAILED",
            "RECOVERY_REQUIRED",
            "IN_PROGRESS",
            "SAFE_TO_RESUME",
            "ACCEPTANCE_PENDING",
            "NOT_FOUND",
            "UNKNOWN",
        }
    ):
        return None
    if "mayResumeSafely" in receipt and not isinstance(receipt["mayResumeSafely"], bool):
        return None
    observed_sha = receipt.get("manifestSha256")
    if receipt.get("state") == "NOT_FOUND":
        if observed_sha not in {None, "", manifest_sha256}:
            return None
    elif manifest_sha256:
        if observed_sha != manifest_sha256:
            return None
    elif not isinstance(observed_sha, str) or SHA256_RE.fullmatch(observed_sha) is None:
        return None
    steps = receipt.get("steps")
    if not isinstance(steps, list) or len(steps) > 16:
        return None
    selection = receipt.get("selection")
    if selection is not None and not isinstance(selection, dict):
        return None
    return receipt


def _default_run_command(script: str, timeout_seconds: int) -> AzureCommandResult:
    argv = [
        "az",
        "vm",
        "run-command",
        "invoke",
        "--ids",
        AZURE_VM_RESOURCE_ID,
        "--command-id",
        "RunShellScript",
        "--scripts",
        script,
        "--only-show-errors",
        "--output",
        "json",
    ]
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        partial = error.stdout if isinstance(error.stdout, str) else ""
        return AzureCommandResult(returncode=None, stdout=partial)
    except OSError:
        return AzureCommandResult(returncode=None, stdout="")
    return AzureCommandResult(returncode=result.returncode, stdout=result.stdout or "")


def deploy_verified_release(
    resource: object,
    *,
    urlopen: UrlOpener = urllib.request.urlopen,
    run_command: RunCommand = _default_run_command,
) -> DeploymentOutcome:
    """Verify public release bytes, invoke one guest deploy, then read back status only."""

    evidence = validate_release_event(resource)
    fetch_and_validate_manifest(evidence, urlopen=urlopen)
    deploy_script = build_guest_script(evidence)
    deploy_result: AzureCommandResult | None
    try:
        deploy_result = run_command(deploy_script, AZURE_DEPLOY_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - an Azure timeout may follow a remotely accepted request.
        deploy_result = None

    # Every deploy attempt is followed by a separate canonical status-only VM command.
    try:
        status_result = run_command(
            build_guest_status_script(evidence.release_id, evidence.manifest_sha256),
            AZURE_STATUS_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - force a read-only status result after any uncertain call.
        status_result = AzureCommandResult(returncode=None, stdout="")
    readback = _receipt_from_azure_output(
        status_result.stdout,
        release_id=evidence.release_id,
        manifest_sha256=evidence.manifest_sha256,
    )
    if readback is not None and readback.get("state") in {"APPLIED", "NO_CHANGE"}:
        return DeploymentOutcome(True, readback["state"], readback, readback_performed=True)
    if readback is not None:
        return DeploymentOutcome(False, readback.get("state", "UNKNOWN"), readback, True)
    error_code = (
        "STATUS_READBACK_UNKNOWN"
        if deploy_result is not None
        else "DEPLOYMENT_AND_STATUS_READBACK_UNKNOWN"
    )
    failed = _fallback_receipt(evidence, "UNKNOWN", error_code)
    return DeploymentOutcome(False, "UNKNOWN", failed, True)


def read_release_status(
    release_id: str,
    *,
    manifest_sha256: str = "",
    run_command: RunCommand = _default_run_command,
) -> DeploymentOutcome:
    """Perform a read-only status query through the fixed VM RunCommand endpoint."""

    if RELEASE_ID_RE.fullmatch(release_id) is None:
        raise AzureCdError("release ID is invalid")
    try:
        result = run_command(
            build_guest_status_script(release_id, manifest_sha256), AZURE_STATUS_TIMEOUT_SECONDS
        )
    except Exception:  # noqa: BLE001 - status failures must be reported as UNKNOWN.
        result = AzureCommandResult(returncode=None, stdout="")
    receipt = _receipt_from_azure_output(
        result.stdout, release_id=release_id, manifest_sha256=manifest_sha256
    )
    if receipt is None:
        failed = {
            "schemaVersion": RECEIPT_SCHEMA,
            "releaseId": release_id,
            "manifestSha256": manifest_sha256,
            "state": "UNKNOWN",
            "steps": [],
            "selection": None,
            "updatedAtUtc": _now(),
            "errorCode": "STATUS_READBACK_UNKNOWN",
        }
        return DeploymentOutcome(False, "UNKNOWN", failed, True)
    success = receipt.get("state") in {"APPLIED", "NO_CHANGE"}
    return DeploymentOutcome(success, receipt.get("state", "UNKNOWN"), receipt, True)


def _fallback_receipt(evidence: VerifiedRelease, state: str, error_code: str) -> dict[str, Any]:
    return {
        "schemaVersion": RECEIPT_SCHEMA,
        "releaseId": evidence.release_id,
        "manifestSha256": evidence.manifest_sha256,
        "state": state,
        "steps": [],
        "selection": None,
        "updatedAtUtc": _now(),
        "errorCode": error_code,
    }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_event_file(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_EVENT_BYTES:
        raise AzureCdError("webhook event file is missing, unsafe, or oversized")
    try:
        document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AzureCdError("webhook event file is invalid JSON") from error
    if not isinstance(document, dict):
        raise AzureCdError("webhook event root must be a JSON object")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deploy one HMAC-authenticated Teams cloud release"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    bridge_parser = subparsers.add_parser(
        "bridge", help="forward one completed GitHub publisher run"
    )
    bridge_parser.add_argument("--run-id", type=int, required=True)
    bridge_parser.add_argument("--run-attempt", type=int, required=True)
    bridge_parser.add_argument("--head-sha", required=True)
    bridge_parser.add_argument("--event", choices=sorted(PUBLISHER_EVENTS), required=True)
    deploy_parser = subparsers.add_parser("deploy", help="verify and apply one signed release")
    deploy_parser.add_argument("--event-json", type=Path, required=True)
    status_parser = subparsers.add_parser(
        "status", help="read one release's canonical guest status"
    )
    status_parser.add_argument("--release-id", required=True)
    status_parser.add_argument("--manifest-sha256", default="")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "bridge":
            result = forward_github_release(
                run_id=arguments.run_id,
                run_attempt=arguments.run_attempt,
                head_sha=arguments.head_sha,
                event=arguments.event,
                github_token=os.environ.get("GH_TOKEN", ""),
                webhook_secret=os.environ.get("CYRENE_TEAMS_AZDO_WEBHOOK_SECRET", ""),
            )
            print(json.dumps({"status": result}, sort_keys=True, separators=(",", ":")))
            return 0
        if arguments.command == "deploy":
            event = _load_event_file(arguments.event_json)
            outcome = deploy_verified_release(event)
        else:
            if arguments.manifest_sha256:
                _require_sha(arguments.manifest_sha256, "manifest SHA-256", length=64)
            outcome = read_release_status(
                arguments.release_id, manifest_sha256=arguments.manifest_sha256
            )
    except AzureCdError:
        error_code = (
            "AZURE_CD_BRIDGE_REJECTED"
            if arguments.command == "bridge"
            else "AZURE_CD_PREFLIGHT_REJECTED"
        )
        print(json.dumps({"status": "FAILED", "errorCode": error_code}))
        return 2
    print(
        json.dumps(
            {
                "schemaVersion": RECEIPT_SCHEMA,
                "success": outcome.success,
                "state": outcome.state,
                "readbackPerformed": outcome.readback_performed,
                "receipt": outcome.receipt,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if outcome.success else 2


if __name__ == "__main__":
    raise SystemExit(main())
