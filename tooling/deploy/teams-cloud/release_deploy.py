"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 release_deploy.py                                               │
│  Module: tooling.deploy.teams_cloud.release_deploy                   │
│  Role: Verify a release and compose the existing guarded updates.   │
│                                                                     │
│  模块职责：验证发布包，按正式计划/日志/选择器逐项更新，不另建运行态库。 │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import preflight
import runtime_update
import stage_transition

RECEIPT_SCHEMA = "cyrene-teams-release-deployment-receipt-v1"
POLICY_SCHEMA = "cyrene-teams-release-deployment-policy-v1"
RELEASE_ID = re.compile(r"teams-arm64-[0-9a-f]{12}-[0-9a-f]{12}")
SHA256 = re.compile(r"[0-9a-f]{64}")
TARGETS = (
    "navigator-persistence",
    "github-issues",
    "huggingface",
    "cloud-management-phase2",
    "exchange",
    "navigator",
)
PROVIDER_TARGETS = {"github-issues", "huggingface", "cloud-management-phase2"}
MAX_JSON = 4 * 1024 * 1024
MAX_ARCHIVE = 2 * 1024**3


class DeploymentError(ValueError):
    """A safe, machine-readable stop; never include environment or provider data."""

    def __init__(self, code: str, *, state: str = "HELD") -> None:
        super().__init__(code)
        self.code = code
        self.state = state


def _now() -> str:
    """Return the receipt clock in UTC."""

    return datetime.now(UTC).isoformat()


def _sha(path: Path) -> str:
    """Hash a regular file with bounded memory."""

    if path.is_symlink() or not path.is_file():
        raise DeploymentError("ARTIFACT_NOT_REGULAR")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON fields rather than accepting the last value."""

    result: dict[str, Any] = {}
    for key, value in values:
        if key in result:
            raise DeploymentError("DUPLICATE_JSON_FIELD")
        result[key] = value
    return result


def _json(path: Path) -> dict[str, Any]:
    """Read one bounded document, without reporting any raw input on failure."""

    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON:
        raise DeploymentError("JSON_INPUT_UNAVAILABLE_OR_TOO_LARGE")
    try:
        value = json.loads(path.read_bytes(), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeploymentError("JSON_INPUT_INVALID") from error
    if not isinstance(value, dict):
        raise DeploymentError("JSON_OBJECT_REQUIRED")
    return value


def _write(path: Path, document: dict[str, Any], *, exclusive: bool = False) -> None:
    """Write a private receipt atomically; immutable bindings use O_EXCL."""

    raw = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()
    if path.is_symlink():
        raise DeploymentError("OUTPUT_SYMLINK_REJECTED")
    if exclusive:
        with path.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".receipt-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _directory(path: Path, *, create: bool) -> None:
    """Keep deployment state in root-owned private artifact directories."""

    if create and not path.exists():
        path.mkdir(mode=0o700, parents=True)
    if path.is_symlink() or not path.is_dir():
        raise DeploymentError("PRIVATE_DIRECTORY_REQUIRED")
    metadata = path.stat()
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise DeploymentError("PRIVATE_DIRECTORY_OWNER_OR_MODE_INVALID")


@dataclass(frozen=True)
class DeploymentPolicy:
    """One-time host configuration; credentials stay in existing service mounts."""

    path: Path
    sha256: str
    selection: Path
    journals: Path
    root: Path
    probe_policy: Path
    probe_policy_sha256: str
    targets: tuple[str, ...]
    max_memory_mib: int
    max_cpus: float
    required_free_gib: float
    minimum_available_mib: int
    drain_seconds: int
    health_seconds: int
    window_wait_seconds: int
    ingress_exposure: str
    observation_profile: dict[str, Any] | None = None


def load_policy(path: Path) -> DeploymentPolicy:
    """Load the root-owned allowlist; releases cannot supply shell or Docker argv."""

    if os.geteuid() != 0:
        raise DeploymentError("ROOT_OPERATOR_REQUIRED")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0:
        raise DeploymentError("ROOT_POLICY_REQUIRED")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise DeploymentError("PRIVATE_POLICY_REQUIRED")
    document = _json(path)
    required = {
        "schemaVersion",
        "selectionPath",
        "journalDirectory",
        "releaseDirectory",
        "probePolicyPath",
        "probePolicySha256",
        "managedTargets",
        "limits",
        "drainTimeoutSeconds",
        "healthTimeoutSeconds",
        "windowWaitSeconds",
        "ingressExposure",
    }
    if (
        set(document) - {"observationProfile"} != required
        or document.get("schemaVersion") != POLICY_SCHEMA
    ):
        raise DeploymentError("DEPLOYMENT_POLICY_SCHEMA_INVALID")
    observation = document.get("observationProfile")
    if observation is not None and not isinstance(observation, dict):
        raise DeploymentError("OBSERVATION_PROFILE_INVALID")
    paths: dict[str, Path] = {}
    for key in ("selectionPath", "journalDirectory", "releaseDirectory", "probePolicyPath"):
        value = document.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
            raise DeploymentError("POLICY_PATH_INVALID")
        paths[key] = Path(value)
    if ".artifacts" not in paths["releaseDirectory"].parts:
        raise DeploymentError("PRIVATE_ARTIFACT_ROOT_REQUIRED")
    digest = document.get("probePolicySha256")
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise DeploymentError("PROBE_POLICY_PIN_REQUIRED")
    if _sha(paths["probePolicyPath"]) != digest:
        raise DeploymentError("PROBE_POLICY_PIN_MISMATCH")
    selected = document.get("managedTargets")
    if (
        not isinstance(selected, list)
        or not selected
        or len(set(selected)) != len(selected)
        or any(value not in TARGETS for value in selected)
    ):
        raise DeploymentError("MANAGED_TARGETS_INVALID")
    limits = document.get("limits")
    if not isinstance(limits, dict) or set(limits) != {
        "maxMemoryMiB",
        "maxCpus",
        "requiredFreeGiB",
        "minimumMemAvailableMiB",
    }:
        raise DeploymentError("RESOURCE_POLICY_INVALID")
    for key in ("maxMemoryMiB", "minimumMemAvailableMiB"):
        if type(limits[key]) is not int or limits[key] <= 0:
            raise DeploymentError("RESOURCE_POLICY_INVALID")
    for key in ("maxCpus", "requiredFreeGiB"):
        if type(limits[key]) not in {int, float} or not 0 < limits[key] < 100000:
            raise DeploymentError("RESOURCE_POLICY_INVALID")
    for key, low, high in (
        ("drainTimeoutSeconds", 60, 900),
        ("healthTimeoutSeconds", 30, 600),
        ("windowWaitSeconds", 0, 1800),
    ):
        if type(document[key]) is not int or not low <= document[key] <= high:
            raise DeploymentError("TIMEOUT_POLICY_INVALID")
    if document["ingressExposure"] not in {"public-cutover", "host-fronted-staging"}:
        raise DeploymentError("INGRESS_POLICY_INVALID")
    _directory(paths["releaseDirectory"], create=True)
    return DeploymentPolicy(
        path,
        _sha(path),
        paths["selectionPath"],
        paths["journalDirectory"],
        paths["releaseDirectory"],
        paths["probePolicyPath"],
        digest,
        tuple(value for value in TARGETS if value in selected),
        limits["maxMemoryMiB"],
        float(limits["maxCpus"]),
        float(limits["requiredFreeGiB"]),
        limits["minimumMemAvailableMiB"],
        document["drainTimeoutSeconds"],
        document["healthTimeoutSeconds"],
        document["windowWaitSeconds"],
        document["ingressExposure"],
        observation,
    )


def validate_release_url(url: str, release_id: str) -> str:
    """Allow public assets only from the canonical repository and exact release."""

    parsed = urllib.parse.urlsplit(url)
    expected_path = f"/DoHorizon-AI/Cyrene-Workspace/releases/download/{release_id}"
    if (
        RELEASE_ID.fullmatch(release_id) is None
        or parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.path.rstrip("/") != expected_path
        or parsed.query
        or parsed.fragment
    ):
        raise DeploymentError("RELEASE_DOWNLOAD_ORIGIN_INVALID")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, expected_path, "", ""))


def _asset_path(root: Path, record: dict[str, Any]) -> Path:
    """Resolve a closed asset record before downloading or trusting cached bytes."""

    if not isinstance(record, dict) or set(record) != {"file", "sha256", "sizeBytes"}:
        raise DeploymentError("RELEASE_ASSET_SCHEMA_INVALID")
    value = record.get("file")
    if not isinstance(value, str) or "\\" in value:
        raise DeploymentError("RELEASE_ASSET_PATH_INVALID")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise DeploymentError("RELEASE_ASSET_PATH_INVALID")
    if not isinstance(record["sha256"], str) or SHA256.fullmatch(record["sha256"]) is None:
        raise DeploymentError("RELEASE_ASSET_DIGEST_INVALID")
    if type(record["sizeBytes"]) is not int or not 0 < record["sizeBytes"] <= MAX_ARCHIVE:
        raise DeploymentError("RELEASE_ASSET_SIZE_INVALID")
    destination = root.joinpath(*relative.parts)
    if destination.is_symlink() or any(parent.is_symlink() for parent in destination.parents):
        raise DeploymentError("RELEASE_ASSET_SYMLINK_REJECTED")
    return destination


class _HttpsRedirects(urllib.request.HTTPRedirectHandler):
    """Never downgrade a release download to plaintext or forward credentials."""

    def redirect_request(
        self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: str
    ) -> Any:
        if not _trusted_asset_url(newurl):
            raise DeploymentError("RELEASE_REDIRECT_PROTOCOL_INVALID")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _trusted_asset_url(url: str) -> bool:
    """Limit signed GitHub release redirects to GitHub's asset hosts."""

    value = urllib.parse.urlsplit(url)
    host = value.hostname or ""
    return (
        value.scheme == "https"
        and not value.username
        and not value.password
        and (host == "github.com" or host.endswith(".githubusercontent.com"))
        and value.port in {None, 443}
    )


def download_asset(root: Path, base_url: str, record: dict[str, Any]) -> Path:
    """Download, stream-hash, and atomically seal one asset before use."""

    target = _asset_path(root, record)
    if target.exists():
        if target.stat().st_size != record["sizeBytes"] or _sha(target) != record["sha256"]:
            raise DeploymentError("EXISTING_ASSET_DIGEST_CONFLICT")
        return target
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    url = base_url + "/" + urllib.parse.quote(target.name, safe="")
    digest = hashlib.sha256()
    size = 0
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=".download-", delete=False
        ) as out:
            temporary = Path(out.name)
            os.fchmod(out.fileno(), 0o600)
            with urllib.request.build_opener(_HttpsRedirects()).open(url, timeout=90) as response:
                if not _trusted_asset_url(response.url):
                    raise DeploymentError("RELEASE_DOWNLOAD_PROTOCOL_INVALID")
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > record["sizeBytes"]:
                        raise DeploymentError("RELEASE_ASSET_EXCEEDS_DECLARED_SIZE")
                    digest.update(chunk)
                    out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if size != record["sizeBytes"] or digest.hexdigest() != record["sha256"]:
            raise DeploymentError("RELEASE_ASSET_DIGEST_MISMATCH")
        os.link(temporary, target)
        return target
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DeploymentError("RELEASE_ASSET_DOWNLOAD_FAILED") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def slice_image_lock(
    active: dict[str, Any], published: dict[str, Any], target: str
) -> dict[str, Any]:
    """Replace only this step's image row; previous successful steps stay applied."""

    service = runtime_update.LOCK_SERVICE_FOR_TARGET[target]
    old_rows = runtime_update._image_rows_by_service(active)
    new_rows = runtime_update._image_rows_by_service(published)
    if service not in old_rows or service not in new_rows:
        raise DeploymentError("RELEASE_TARGET_NOT_IN_ACTIVE_LOCK")
    result = copy.deepcopy(active)
    result["images"] = [
        copy.deepcopy(new_rows[service] if row["service"] == service else row)
        for row in active["images"]
    ]
    # A mixed release retains exact per-image sourceCommits; aliases describe the
    # latest accepted source snapshots, never replace a non-target image row.
    for alias in new_rows[service].get("sourceCommits", {}):
        if alias not in published.get("sources", {}):
            raise DeploymentError("RELEASE_SOURCE_METADATA_MISSING")
        result["sources"][alias] = copy.deepcopy(published["sources"][alias])
    runtime_update._validate_lock_pair(active, result, [target])
    return result


def verify_image_archive(path: Path, image: dict[str, Any], maximum_bytes: int) -> None:
    """Stream-check the sole Docker image identity before the daemon imports it."""

    expected_id = image["imageId"]
    expected_config_sha = expected_id.removeprefix("sha256:")
    metadata: dict[str, Any] = {}
    file_hashes: dict[str, tuple[str, int]] = {}
    members: set[str] = set()
    total = 0
    metadata_bytes = 0
    try:
        with tarfile.open(path, mode="r|*") as archive:
            for member in archive:
                name = PurePosixPath(member.name)
                if name.is_absolute() or ".." in name.parts or "\\" in member.name:
                    raise DeploymentError("IMAGE_ARCHIVE_MEMBER_PATH_INVALID")
                if not member.isfile() and not member.isdir():
                    raise DeploymentError("IMAGE_ARCHIVE_LINK_OR_SPECIAL_FILE")
                total += member.size
                if total > maximum_bytes or len(members) > 10000 or member.name in members:
                    raise DeploymentError("IMAGE_ARCHIVE_SIZE_OR_MEMBER_LIMIT")
                members.add(member.name)
                if member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise DeploymentError("IMAGE_ARCHIVE_METADATA_MISSING")
                    digest = hashlib.sha256()
                    collected = bytearray()
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                        if member.size <= MAX_JSON:
                            collected.extend(chunk)
                    file_hashes[member.name] = ("sha256:" + digest.hexdigest(), member.size)
                    if name.parts[:2] == ("blobs", "sha256") and name.name != digest.hexdigest():
                        raise DeploymentError("OCI_CONTENT_ADDRESSED_BLOB_MISMATCH")
                    if collected and bytes(collected).lstrip()[:1] in {b"{", b"["}:
                        try:
                            document = json.loads(collected, object_pairs_hook=_pairs)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        metadata_bytes += member.size
                        if metadata_bytes > 64 * 1024**2:
                            raise DeploymentError("IMAGE_ARCHIVE_METADATA_MEMORY_LIMIT")
                        metadata[member.name] = document
    except (tarfile.TarError, EOFError, OSError, json.JSONDecodeError, RecursionError) as error:
        raise DeploymentError("IMAGE_ARCHIVE_INVALID") from error
    config_path: str | None = None
    if "oci-layout" in metadata or "index.json" in metadata:
        layout, index = metadata.get("oci-layout"), metadata.get("index.json")
        if (
            not isinstance(layout, dict)
            or layout.get("imageLayoutVersion") != "1.0.0"
            or not isinstance(index, dict)
            or index.get("schemaVersion") != 2
        ):
            raise DeploymentError("OCI_LAYOUT_OR_INDEX_INVALID")
        matches: list[str] = []
        seen: set[str] = set()

        def descriptor_document(descriptor: Any) -> tuple[str, dict[str, Any]]:
            if not isinstance(descriptor, dict):
                raise DeploymentError("OCI_DESCRIPTOR_INVALID")
            digest, size = descriptor.get("digest"), descriptor.get("size")
            if (
                not isinstance(digest, str)
                or runtime_update.IMAGE_ID_RE.fullmatch(digest) is None
                or type(size) is not int
                or size <= 0
            ):
                raise DeploymentError("OCI_DESCRIPTOR_INVALID")
            name = "blobs/sha256/" + digest.removeprefix("sha256:")
            if file_hashes.get(name) != (digest, size):
                raise DeploymentError("OCI_DESCRIPTOR_CONTENT_MISMATCH")
            document = metadata.get(name)
            if not isinstance(document, dict):
                raise DeploymentError("OCI_METADATA_UNAVAILABLE")
            return name, document

        def visit(descriptor: Any, depth: int) -> None:
            if depth > 4 or len(seen) >= 64:
                raise DeploymentError("OCI_DESCRIPTOR_DEPTH_OR_COUNT_LIMIT")
            name, document = descriptor_document(descriptor)
            if name in seen:
                raise DeploymentError("OCI_DUPLICATE_DESCRIPTOR")
            seen.add(name)
            if "manifests" in document:
                children = document["manifests"]
                if (
                    document.get("schemaVersion") != 2
                    or not isinstance(children, list)
                    or not children
                ):
                    raise DeploymentError("OCI_NESTED_INDEX_INVALID")
                for child in children:
                    visit(child, depth + 1)
                return
            platform = descriptor.get("platform", {})
            if isinstance(platform, dict) and (
                platform.get("os") == "unknown" or platform.get("architecture") == "unknown"
            ):
                return
            config_name, config = descriptor_document(document.get("config"))
            if descriptor["digest"] != expected_id and document["config"]["digest"] != expected_id:
                raise DeploymentError("OCI_UNSELECTED_IMAGE_IN_ARCHIVE")
            if config.get("os") != "linux" or config.get("architecture") not in {
                "arm64",
                "aarch64",
            }:
                raise DeploymentError("OCI_IMAGE_PLATFORM_INVALID")
            layers = document.get("layers")
            if document.get("schemaVersion") != 2 or not isinstance(layers, list) or not layers:
                raise DeploymentError("OCI_IMAGE_LAYERS_INVALID")
            for layer in layers:
                if not isinstance(layer, dict) or not isinstance(layer.get("digest"), str):
                    raise DeploymentError("OCI_LAYER_DESCRIPTOR_INVALID")
                name = "blobs/sha256/" + layer["digest"].removeprefix("sha256:")
                if file_hashes.get(name) != (layer["digest"], layer.get("size")):
                    raise DeploymentError("OCI_LAYER_CONTENT_MISMATCH")
            matches.append(config_name)

        descriptors = index.get("manifests")
        if not isinstance(descriptors, list) or not descriptors:
            raise DeploymentError("OCI_INDEX_EMPTY")
        for descriptor in descriptors:
            visit(descriptor, 0)
        if len(matches) != 1:
            raise DeploymentError("OCI_LOCKED_IMAGE_NOT_UNIQUE")
        config_path = matches[0]
    else:
        for candidate in (expected_config_sha + ".json", "blobs/sha256/" + expected_config_sha):
            if file_hashes.get(candidate, (None,))[0] == expected_id:
                config_path = candidate
    manifest = metadata.get("manifest.json")
    if (
        not isinstance(manifest, list)
        or len(manifest) != 1
        or not isinstance(manifest[0], dict)
        or manifest[0].get("Config") != config_path
        or config_path is None
        or manifest[0].get("RepoTags") != [image["image"]]
        or not isinstance(manifest[0].get("Layers"), list)
        or not manifest[0]["Layers"]
        or len(set(manifest[0]["Layers"])) != len(manifest[0]["Layers"])
        or any(value not in members for value in manifest[0]["Layers"])
    ):
        raise DeploymentError("IMAGE_ARCHIVE_DOES_NOT_MATCH_SOLE_LOCKED_IMAGE")
    config = metadata.get(config_path)
    if not isinstance(config, dict):
        raise DeploymentError("IMAGE_ARCHIVE_CONFIG_INVALID")
    labels = config.get("config", {}).get("Labels") or {}
    if any(
        labels.get(f"io.cyrene.source.{alias}.revision") != commit
        for alias, commit in image["sourceCommits"].items()
    ):
        raise DeploymentError("IMAGE_ARCHIVE_SOURCE_LABEL_MISMATCH")


def _selection_facts(document: dict[str, Any]) -> dict[str, Any]:
    """Return only safe authoritative selector fields for delivery receipts."""

    return {
        "operationId": document.get("operationId"),
        "planSha256": document.get("planSha256"),
        "imageLockSha256": document.get("imageLock", {}).get("sha256"),
    }


class CanonicalBackend:
    """Administrative adapter to existing probes, plans, journals and selector."""

    def __init__(self, policy: DeploymentPolicy) -> None:
        self.policy = policy

    def selection(self) -> dict[str, Any]:
        """Read exact selected files and require its completed canonical journal."""

        selected = _json(self.policy.selection)
        if selected.get("phase") != "issued-connector-activation":
            raise DeploymentError("ISSUED_RUNTIME_REQUIRED")
        for record in [
            *selected.get("composeFiles", []),
            *selected.get("envFiles", []),
            selected.get("imageLock"),
            selected.get("huggingfaceSeed"),
        ]:
            stage_transition._verify_record(record, "selected deployment input")
        if selected.get("runpodKey") is not None:
            raise DeploymentError("RUNPOD_ACCEPTANCE_NOT_IMPLEMENTED")
        status = stage_transition.operation_status(self.policy.journals, selected["operationId"])
        if status.get("state") != "APPLIED" or status.get("planSha256") != selected["planSha256"]:
            raise DeploymentError("ACTIVE_SELECTION_JOURNAL_NOT_APPLIED")
        return selected

    def image_rows(self, selected: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Resolve the selected image lock without changing any runtime input."""

        return runtime_update._image_rows_by_service(
            runtime_update._load_lock(Path(selected["imageLock"]["path"]))
        )

    def live_image(self, target: str) -> str:
        """Confirm the actual running, healthy container image for one service."""

        result = subprocess.run(
            [
                "docker",
                "ps",
                "--no-trunc",
                "-q",
                "--filter",
                "label=com.docker.compose.project=cyrene-teams-cloud",
                "--filter",
                f"label=com.docker.compose.service={target}",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        ids = result.stdout.split()
        if len(ids) != 1 or runtime_update.FULL_CONTAINER_ID_RE.fullmatch(ids[0]) is None:
            raise DeploymentError("ACTUAL_TARGET_CONTAINER_NOT_UNIQUE")
        inspected = subprocess.run(
            ["docker", "inspect", ids[0]], capture_output=True, text=True, check=True, timeout=20
        )
        rows = json.loads(inspected.stdout)
        if len(rows) != 1 or rows[0]["State"].get("Health", {}).get("Status") != "healthy":
            raise DeploymentError("ACTUAL_TARGET_NOT_HEALTHY")
        return rows[0]["Image"]

    def wait_window(self, target: str) -> None:
        """Wait read-only for the sole monitor's natural cycle; never edit its due time."""

        if target not in PROVIDER_TARGETS:
            return
        import deployment_probe

        deadline = time.monotonic() + self.policy.window_wait_seconds
        while True:
            value = deployment_probe.readonly_health_window(
                self.policy.probe_policy,
                targets=[target],
                minimum_lead_seconds=self.policy.health_seconds + 300,
            )
            if value.get("ready") is True:
                return
            if time.monotonic() >= deadline:
                raise DeploymentError("NORMAL_MONITOR_WINDOW_NOT_READY")
            time.sleep(min(10, max(0, deadline - time.monotonic())))

    def probe(self, run_id: str, workspace_commit: str) -> dict[str, Any]:
        """Use the existing Navigator→Exchange API, without invoking a model."""

        import deployment_probe

        if _sha(self.policy.probe_policy) != self.policy.probe_policy_sha256:
            raise DeploymentError("PROBE_POLICY_CHANGED")
        result = deployment_probe.run_deployment_probe(
            self.policy.probe_policy,
            run_id=run_id,
            workspace_source_commit=workspace_commit,
        )
        if result.get("status") != "REAL_NATIVE_EXCHANGE_READONLY_PROBE_PASS":
            raise DeploymentError("REAL_CONNECTOR_ACCEPTANCE_FAILED")
        return result

    def github_acceptance(self, run_id: str) -> dict[str, Any]:
        """Read approved GitHub repositories through the deployed native connector."""

        import deployment_probe

        result = deployment_probe.run_github_read_acceptance(
            self.policy.probe_policy, run_id=run_id
        )
        if result.get("status") != "REAL_NATIVE_GITHUB_READONLY_PROBE_PASS":
            raise DeploymentError("POST_CUTOVER_GITHUB_READ_ACCEPTANCE_FAILED", state="FAILED")
        return result

    def close_observation(self, release_root: Path) -> None:
        """Preserve a real version boundary before any service is replaced."""

        if self.policy.observation_profile is not None:
            import release_observation

            release_observation.close_active_observation(
                self.policy.selection,
                release_root,
                self.policy.observation_profile,
            )

    def start_observation(self, release_root: Path) -> dict[str, Any] | None:
        """Start health sampling for the exact accepted post-deployment selector."""

        if self.policy.observation_profile is None:
            return None
        import release_observation

        self.selection()
        return release_observation.start_post_deployment_observation(
            self.policy.selection,
            release_root,
            self.policy.observation_profile,
        )

    def observation_status(self) -> dict[str, Any] | None:
        """Read the exact current observation without starting or stopping a unit."""

        if self.policy.observation_profile is None:
            return None
        import release_observation

        return release_observation.read_observation_status(
            self.policy.selection, self.policy.observation_profile
        )

    def ensure_image(
        self, step: Path, asset: Path, image: dict[str, Any], locked: dict[str, Any]
    ) -> None:
        """Import a verified archive once; an uncertain import is reconciled by ID only."""

        inspected = subprocess.run(
            ["docker", "image", "inspect", image["image"]],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if inspected.returncode == 0:
            self._verify_image(json.loads(inspected.stdout), image)
            return
        intent = step / "image-import-intent.json"
        if intent.exists():
            raise DeploymentError("IMAGE_IMPORT_UNRESOLVED_NO_REPLAY", state="RECOVERY_REQUIRED")
        reserve = int(self.policy.required_free_gib * 1024**3)
        size = locked.get("sizeBytes")
        docker_root = subprocess.run(
            ["docker", "info", "--format", "{{json .DockerRootDir}}"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        docker_path = json.loads(docker_root.stdout)
        if not isinstance(docker_path, str) or not Path(docker_path).is_absolute():
            raise DeploymentError("DOCKER_ROOT_DIRECTORY_INVALID")
        if (
            type(size) is not int
            or size <= 0
            or min(shutil.disk_usage(step).free, shutil.disk_usage(docker_path).free)
            < size + reserve
        ):
            raise DeploymentError("IMAGE_IMPORT_DISK_RESERVE_INSUFFICIENT")
        verify_image_archive(asset, image, size * 2 + 64 * 1024**2)
        _write(
            intent,
            {
                "image": image["image"],
                "imageId": image["imageId"],
                "archiveSha256": _sha(asset),
                "at": _now(),
            },
            exclusive=True,
        )
        try:
            result = subprocess.run(
                ["docker", "image", "load", "--input", str(asset)],
                capture_output=True,
                timeout=600,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise DeploymentError(
                "IMAGE_IMPORT_TIMEOUT_NO_REPLAY", state="RECOVERY_REQUIRED"
            ) from error
        if result.returncode != 0:
            raise DeploymentError(
                "IMAGE_IMPORT_FAILED_REQUIRES_READBACK", state="RECOVERY_REQUIRED"
            )
        inspected = subprocess.run(
            ["docker", "image", "inspect", image["image"]],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        self._verify_image(json.loads(inspected.stdout), image)
        if min(shutil.disk_usage(step).free, shutil.disk_usage(docker_path).free) < reserve:
            raise DeploymentError("POST_IMPORT_DISK_RESERVE_INSUFFICIENT")

    def import_resolved(self, step: Path, image: dict[str, Any]) -> bool:
        """Reconcile an earlier image import by read-back, never by another load."""

        intent = step / "image-import-intent.json"
        if not intent.exists():
            return True
        record = _json(intent)
        if record.get("imageId") != image["imageId"] or record.get("image") != image["image"]:
            raise DeploymentError("IMAGE_IMPORT_INTENT_RELEASE_MISMATCH", state="RECOVERY_REQUIRED")
        result = subprocess.run(
            ["docker", "image", "inspect", image["image"]],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            return False
        self._verify_image(json.loads(result.stdout), image)
        return True

    @staticmethod
    def _verify_image(rows: list[dict[str, Any]], image: dict[str, Any]) -> None:
        """Compare the loaded image ID, ARM64 platform and exact source labels."""

        if len(rows) != 1 or rows[0].get("Id") != image["imageId"]:
            raise DeploymentError("LOADED_IMAGE_ID_MISMATCH")
        row = rows[0]
        if row.get("Os") != "linux" or row.get("Architecture") not in {"arm64", "aarch64"}:
            raise DeploymentError("LOADED_IMAGE_PLATFORM_MISMATCH")
        labels = row.get("Config", {}).get("Labels") or {}
        for alias, commit in image["sourceCommits"].items():
            if labels.get(f"io.cyrene.source.{alias}.revision") != commit:
                raise DeploymentError("LOADED_IMAGE_SOURCE_LABEL_MISMATCH")

    def plan(
        self,
        step: Path,
        target: str,
        image: dict[str, Any],
        published_lock: dict[str, Any],
        probe: dict[str, Any],
    ) -> dict[str, Any]:
        """Create one canonical image-only plan from freshly selected active inputs."""

        # A read-only plan that never acquired a canonical journal can expire.
        # Keep it intact and create a new attempt after fresh scope/health checks.
        existing = self.status(step)
        if existing is not None and existing["state"] != "PREPARED_NO_APPLY_JOURNAL":
            raise DeploymentError(
                "PRIOR_CANONICAL_OPERATION_MUST_NOT_BE_REPLAYED", state="RECOVERY_REQUIRED"
            )
        step = _new_plan_directory(step)
        selected = self.selection()
        lock = runtime_update._load_lock(Path(selected["imageLock"]["path"]))
        candidate_lock = slice_image_lock(lock, published_lock, target)
        _write(step / "image-lock.json", candidate_lock, exclusive=True)
        compose_files = [Path(value["path"]) for value in selected["composeFiles"]]
        env_files = [Path(value["path"]) for value in selected["envFiles"]]
        active = preflight._compose_config(["docker"], compose_files, env_files)
        overlay = {name: {"image": active["services"][name]["image"]} for name in TARGETS}
        overlay[target]["image"] = image["image"]
        # Consolidate only this controller's verified, image-only overlays. Keep
        # all other Compose and memory settings, including recovered aliases.
        candidate_files = []
        for path in compose_files:
            if path.name == "compose.image.json" and path.is_relative_to(self.policy.root):
                previous = _json(path)
                if (
                    set(previous) != {"services"}
                    or set(previous["services"]) != set(TARGETS)
                    or any(
                        set(value) != {"image"} or not isinstance(value["image"], str)
                        for value in previous["services"].values()
                    )
                ):
                    raise DeploymentError("OWNED_IMAGE_OVERLAY_UNEXPECTED_CONTENT")
            else:
                candidate_files.append(path)
        _write(step / "compose.image.json", {"services": overlay}, exclusive=True)
        backup = step / "backup"
        backup.mkdir(mode=0o700)
        return stage_transition.create_plan(
            phase=selected["phase"],
            image_update_target=target,
            active_compose_files=compose_files,
            active_env_files=env_files,
            active_image_lock=Path(selected["imageLock"]["path"]),
            candidate_compose_files=[*candidate_files, step / "compose.image.json"],
            candidate_env_files=env_files,
            candidate_image_lock=step / "image-lock.json",
            secrets_dir=Path(selected["secretsDir"]),
            active_config_dir=Path(selected["configDir"]),
            candidate_config_dir=Path(selected["configDir"]),
            candidate_huggingface_seed=Path(selected["huggingfaceSeed"]["path"]),
            candidate_runpod_key_file=None,
            connector_auth_evidence=Path(probe["evidencePath"]),
            connector_auth_task_id=probe["taskId"],
            notification_anchor_file=(
                Path(probe["notificationAnchorPath"])
                if probe.get("notificationAnchorPath")
                else None
            ),
            notification_provenance_file=Path(probe["provenancePath"])
            if probe.get("provenancePath")
            else None,
            active_selection_file=self.policy.selection,
            promote_selection_file=self.policy.selection,
            backup_dir=backup,
            journal_dir=self.policy.journals,
            plan_path=step / "plan.json",
            docker_prefix=["docker"],
            drain_timeout_seconds=self.policy.drain_seconds,
            health_timeout_seconds=self.policy.health_seconds,
            max_memory_mib=self.policy.max_memory_mib,
            max_cpus=self.policy.max_cpus,
            required_free_gib=self.policy.required_free_gib,
            minimum_mem_available_mib=self.policy.minimum_available_mib,
            ingress_exposure=self.policy.ingress_exposure,
        )

    def apply(self, step: Path, plan: dict[str, Any]) -> dict[str, Any]:
        """Delegate all runtime mutation and rollback decisions to the canonical engine."""

        return stage_transition.apply_plan_file(
            _plan_directories(step)[-1] / "plan.json", expected_digest=plan["planSha256"]
        )

    def status(self, step: Path) -> dict[str, Any] | None:
        """Read one canonical journal; an interrupted known operation is never replayed."""

        statuses = []
        for directory in _plan_directories(step):
            path = directory / "plan.json"
            if not path.exists():
                continue
            plan = _json(path)
            journal = self.policy.journals / (plan["operationId"] + ".json")
            if journal.exists():
                status = stage_transition.operation_status(
                    self.policy.journals, plan["operationId"]
                )
                if status["state"] not in {"APPLIED", "FAILED_NO_MUTATION", "FAILED_ROLLED_BACK"}:
                    return status
                statuses.append(status)
            else:
                statuses.append(
                    {
                        "state": "PREPARED_NO_APPLY_JOURNAL",
                        "operationId": plan["operationId"],
                        "planSha256": plan["planSha256"],
                    }
                )
        return statuses[-1] if statuses else None


def _plan_directories(step: Path) -> list[Path]:
    """Find bounded immutable canonical attempts, including the initial legacy path."""

    directories = sorted(step.glob("attempt-[0-9][0-9][0-9][0-9]"))
    if len(directories) > 100 or any(
        path.is_symlink() or not path.is_dir() for path in directories
    ):
        raise DeploymentError("CANONICAL_ATTEMPT_DIRECTORY_INVALID")
    return ([step] if (step / "plan.json").exists() else []) + directories


def _new_plan_directory(step: Path) -> Path:
    """Never overwrite a canonical plan, even when its read-only evidence expired."""

    count = len(_plan_directories(step))
    if count >= 100:
        raise DeploymentError("READONLY_PREPARATION_ATTEMPT_LIMIT")
    directory = step / f"attempt-{count + 1:04d}"
    directory.mkdir(mode=0o700)
    return directory


@contextmanager
def _controller_lock(root: Path):
    """Serialize release orchestration; canonical per-operation locks remain authoritative."""

    path = root / ".release-controller.lock"
    if path.is_symlink():
        raise DeploymentError("CONTROLLER_LOCK_SYMLINK_REJECTED")
    with path.open("a+b") as stream:
        os.fchmod(stream.fileno(), 0o600)
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DeploymentError("ANOTHER_RELEASE_IS_RUNNING", state="IN_PROGRESS") from error
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _step_receipt(target: str, value: dict[str, Any]) -> dict[str, Any]:
    """Limit public receipts to operation references and real canonical state."""

    return {
        "target": target,
        **{key: value.get(key) for key in ("operationId", "planSha256", "state")},
    }


def _acceptance_path(root: Path, selected: dict[str, Any]) -> Path:
    """Bind each acceptance receipt to one exact native selector plan."""

    digest = selected.get("planSha256")
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise DeploymentError("SELECTOR_PLAN_DIGEST_INVALID")
    return root / ("post-release-acceptance-" + digest + ".json")


def _controller_running(root: Path) -> bool:
    """Use the controller's process lock, never a guessed PID or stale state file."""

    path = root / ".release-controller.lock"
    if path.is_symlink():
        raise DeploymentError("CONTROLLER_LOCK_SYMLINK_REJECTED")
    with path.open("a+b") as stream:
        os.fchmod(stream.fileno(), 0o600)
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return False


def _selected_publication(
    policy: DeploymentPolicy, selected: dict[str, Any]
) -> tuple[int, int] | None:
    """Recognize the current deployment's owning publication without a second registry."""

    value = selected.get("imageLock", {}).get("path")
    if not isinstance(value, str) or not Path(value).is_relative_to(policy.root):
        return None
    relative = Path(value).relative_to(policy.root)
    if not relative.parts or RELEASE_ID.fullmatch(relative.parts[0]) is None:
        return None
    directory = policy.root / relative.parts[0]
    binding = _json(directory / "deployment-binding.json")
    manifest_path = directory / "release-manifest.json"
    if _sha(manifest_path) != binding.get("manifestSha256"):
        raise DeploymentError("CURRENT_PUBLICATION_MANIFEST_CHANGED", state="RECOVERY_REQUIRED")
    publisher = _json(manifest_path)["publisherRun"]
    if type(publisher.get("runId")) is not int or type(publisher.get("runAttempt")) is not int:
        raise DeploymentError("CURRENT_PUBLICATION_IDENTITY_INVALID", state="RECOVERY_REQUIRED")
    return publisher["runId"], publisher["runAttempt"]


def deployment_status(
    policy: DeploymentPolicy,
    release_id: str,
    *,
    backend: Any | None = None,
    lock_held: bool = False,
) -> dict[str, Any]:
    """Derive delivery state from canonical journals and the current selector only."""

    if RELEASE_ID.fullmatch(release_id) is None:
        raise DeploymentError("RELEASE_ID_INVALID")
    adapter = backend or CanonicalBackend(policy)
    root = policy.root / release_id
    binding_path = root / "deployment-binding.json"
    if not binding_path.exists():
        return {
            "schemaVersion": RECEIPT_SCHEMA,
            "releaseId": release_id,
            "state": "NOT_FOUND",
            "steps": [],
            "updatedAtUtc": _now(),
        }
    binding = _json(binding_path)
    if binding.get("policySha256") != policy.sha256:
        raise DeploymentError("DEPLOYMENT_POLICY_CHANGED_SINCE_START")
    manifest = _json(root / "release-manifest.json")
    if _sha(root / "release-manifest.json") != binding["manifestSha256"]:
        raise DeploymentError("BOUND_RELEASE_MANIFEST_CHANGED")
    selected = adapter.selection()
    images = {row["service"]: row for row in manifest["images"]}
    rows = adapter.image_rows(selected)
    steps = []
    unresolved = False
    failed = False
    all_match = True
    import_unknown = False
    for target in policy.targets:
        step = root / "steps" / target
        value = adapter.status(step)
        if value is not None:
            steps.append(_step_receipt(target, value))
            unresolved |= value["state"] not in {
                "APPLIED",
                "FAILED_NO_MUTATION",
                "FAILED_ROLLED_BACK",
                "PREPARED_NO_APPLY_JOURNAL",
            }
            failed |= value["state"] in {"FAILED_NO_MUTATION", "FAILED_ROLLED_BACK"}
        service = runtime_update.LOCK_SERVICE_FOR_TARGET[target]
        expected = images[service]["imageId"]
        if (value is None or value["state"] == "PREPARED_NO_APPLY_JOURNAL") and (
            step / "image-import-intent.json"
        ).exists():
            import_unknown |= not adapter.import_resolved(step, images[service])
        all_match &= rows[service]["imageId"] == expected and adapter.live_image(target) == expected
    accepted = False
    acceptance_path = _acceptance_path(root, selected)
    if acceptance_path.exists():
        acceptance = _json(acceptance_path)
        accepted = (
            acceptance.get("manifestSha256") == binding["manifestSha256"]
            and acceptance.get("status") == "REAL_NATIVE_EXCHANGE_READONLY_PROBE_PASS"
            and acceptance.get("selection") == _selection_facts(selected)
        )
        if accepted:
            evidence = Path(acceptance["evidencePath"])
            output_root = Path(_json(policy.probe_policy)["runtime"]["outputDirectory"])
            if not evidence.is_relative_to(output_root) or _sha(evidence) != acceptance.get(
                "evidenceSha256"
            ):
                raise DeploymentError(
                    "ACCEPTANCE_EVIDENCE_DIGEST_CHANGED", state="RECOVERY_REQUIRED"
                )
            document = _json(evidence)
            if (
                document.get("taskId") != acceptance.get("taskId")
                or document.get("schemaVersion") != "cyrene-connector-auth-readonly-evidence-v1"
                or document.get("executionPath") != "exchange_connector_api"
            ):
                raise DeploymentError(
                    "ACCEPTANCE_EVIDENCE_BINDING_CHANGED", state="RECOVERY_REQUIRED"
                )
            if "github-issues" in policy.targets:
                github = acceptance.get("github")
                if (
                    not isinstance(github, dict)
                    or github.get("status") != "REAL_NATIVE_GITHUB_READONLY_PROBE_PASS"
                ):
                    raise DeploymentError("GITHUB_ACCEPTANCE_MISSING", state="RECOVERY_REQUIRED")
                github_path = Path(github["evidencePath"])
                if (
                    not github_path.is_relative_to(output_root)
                    or _sha(github_path) != github["evidenceSha256"]
                ):
                    raise DeploymentError(
                        "GITHUB_ACCEPTANCE_DIGEST_CHANGED", state="RECOVERY_REQUIRED"
                    )
                github_evidence = _json(github_path)
                if (
                    github_evidence.get("schemaVersion")
                    != "cyrene-github-readonly-acceptance-evidence-v1"
                    or github_evidence.get("status") != github["status"]
                    or github_evidence.get("taskId") != github["taskId"]
                    or github_evidence.get("activeOperationId") != selected["operationId"]
                ):
                    raise DeploymentError(
                        "GITHUB_ACCEPTANCE_BINDING_CHANGED", state="RECOVERY_REQUIRED"
                    )
    observation = None
    observation_ready = policy.observation_profile is None
    if all_match and accepted and policy.observation_profile is not None:
        observation = adapter.observation_status()
        observation_ready = isinstance(observation, dict) and (
            observation.get("status") == "RUNNING"
            or (observation.get("status") == "COMPLETED" and observation.get("complete24h") is True)
        )
    running = False if lock_held else _controller_running(policy.root)
    owner = _selected_publication(policy, selected)
    publisher = manifest["publisherRun"]
    superseded = owner is not None and owner > (publisher["runId"], publisher["runAttempt"])
    state = (
        "IN_PROGRESS"
        if running and not (all_match and accepted and observation_ready)
        else "RECOVERY_REQUIRED"
        if import_unknown
        else "RECOVERY_REQUIRED"
        if unresolved
        else "FAILED"
        if failed
        else "SUPERSEDED"
        if superseded
        else "APPLIED"
        if all_match and accepted and observation_ready
        else "ACCEPTANCE_PENDING"
        if all_match
        else "SAFE_TO_RESUME"
    )
    return {
        "schemaVersion": RECEIPT_SCHEMA,
        "releaseId": release_id,
        "manifestSha256": binding["manifestSha256"],
        "state": state,
        "mayResumeSafely": state in {"SAFE_TO_RESUME", "ACCEPTANCE_PENDING"},
        "connectorAcceptanceVerified": accepted,
        "observation": observation,
        "steps": steps,
        "selection": _selection_facts(selected),
        "updatedAtUtc": _now(),
    }


def reconcile_pending_release(policy: DeploymentPolicy) -> dict[str, Any]:
    """Continue a verified pending request on the VM without replaying unknown work.

    The queue is the existing immutable release directories, and runtime state is
    still derived from canonical operation journals and the live selector.
    """

    backend = CanonicalBackend(policy)
    candidates = []
    for directory in policy.root.iterdir():
        if (
            not RELEASE_ID.fullmatch(directory.name)
            or directory.is_symlink()
            or not directory.is_dir()
        ):
            continue
        binding = directory / "deployment-binding.json"
        manifest_path = directory / "release-manifest.json"
        if not binding.exists():
            continue
        bound = _json(binding)
        if _sha(manifest_path) != bound.get("manifestSha256"):
            raise DeploymentError("BOUND_RELEASE_MANIFEST_CHANGED", state="RECOVERY_REQUIRED")
        manifest = _json(manifest_path)
        for target in TARGETS:
            step = directory / "steps" / target
            state = backend.status(step)
            if state is not None and state["state"] not in {
                "APPLIED",
                "FAILED_NO_MUTATION",
                "FAILED_ROLLED_BACK",
                "PREPARED_NO_APPLY_JOURNAL",
            }:
                return {
                    "schemaVersion": RECEIPT_SCHEMA,
                    "releaseId": directory.name,
                    "state": "RECOVERY_REQUIRED",
                    "errorCode": "PRIOR_NATIVE_OPERATION_UNRESOLVED",
                    "steps": [_step_receipt(target, state)],
                    "updatedAtUtc": _now(),
                }
            if (state is None or state["state"] == "PREPARED_NO_APPLY_JOURNAL") and (
                step / "image-import-intent.json"
            ).exists():
                image = next(
                    value
                    for value in manifest["images"]
                    if value["service"] == runtime_update.LOCK_SERVICE_FOR_TARGET[target]
                )
                if not backend.import_resolved(step, image):
                    return {
                        "schemaVersion": RECEIPT_SCHEMA,
                        "releaseId": directory.name,
                        "state": "RECOVERY_REQUIRED",
                        "errorCode": "PRIOR_IMAGE_IMPORT_UNRESOLVED",
                        "steps": [],
                        "updatedAtUtc": _now(),
                    }
        publisher = manifest["publisherRun"]
        candidates.append(
            ((publisher["runId"], publisher["runAttempt"]), directory, manifest, bound)
        )
    if not candidates:
        return {
            "schemaVersion": RECEIPT_SCHEMA,
            "state": "NO_CHANGE",
            "steps": [],
            "updatedAtUtc": _now(),
        }
    _, directory, manifest, bound = max(candidates, key=lambda value: value[0])
    status = deployment_status(policy, directory.name)
    if not status.get("mayResumeSafely"):
        return status
    base = "https://github.com/DoHorizon-AI/Cyrene-Workspace/releases/download/" + directory.name
    return deploy_release(
        policy, directory / "release-manifest.json", bound["manifestSha256"], base, execute=True
    )


def install_reconciler(
    policy: DeploymentPolicy, release_id: str, expected_digest: str
) -> dict[str, Any]:
    """Install a fixed host timer from the exact already-verified release tools.

    This is deployment control software only; it never mutates an application
    container, credential or conversation while installing the timer.
    """

    if RELEASE_ID.fullmatch(release_id) is None or SHA256.fullmatch(expected_digest) is None:
        raise DeploymentError("RECONCILER_RELEASE_IDENTITY_INVALID")
    base = policy.root / release_id
    manifest_path = base / "release-manifest.json"
    if _sha(manifest_path) != expected_digest or Path(__file__).resolve().parent != base.resolve():
        raise DeploymentError("RECONCILER_MUST_USE_VERIFIED_RELEASE_TOOLS")
    import release_bundle

    manifest = _json(manifest_path)
    release_bundle.validate_release_manifest(manifest)
    if manifest["releaseId"] != release_id:
        raise DeploymentError("RECONCILER_RELEASE_DIRECTORY_MISMATCH")
    directory = Path("/usr/local/lib/cyrene-teams-deployment")
    _directory(directory, create=True)
    pointer = directory / "current"
    if pointer.exists() or pointer.is_symlink():
        if not pointer.is_symlink() or pointer.lstat().st_uid != 0:
            raise DeploymentError("RECONCILER_POINTER_NOT_OWNED_SYMLINK")
        previous = pointer.resolve(strict=True)
        if not previous.is_relative_to(policy.root):
            raise DeploymentError("RECONCILER_POINTER_OUTSIDE_RELEASE_ROOT")
        previous_manifest = _json(previous / "release-manifest.json")
        old = previous_manifest["publisherRun"]
        new = manifest["publisherRun"]
        if (old["runId"], old["runAttempt"]) > (new["runId"], new["runAttempt"]):
            return {
                "schemaVersion": RECEIPT_SCHEMA,
                "releaseId": release_id,
                "manifestSha256": expected_digest,
                "state": "NO_CHANGE",
                "errorCode": "NEWER_RECONCILER_ALREADY_INSTALLED",
                "updatedAtUtc": _now(),
            }
    backup = base / "reconciler-install-backups"
    _directory(backup, create=True)
    for name in ("cyrene-teams-release-reconcile.service", "cyrene-teams-release-reconcile.timer"):
        source = base / name
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 8192:
            raise DeploymentError("RECONCILER_UNIT_SOURCE_INVALID")
        raw = source.read_bytes()
        target = Path("/etc/systemd/system") / name
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file() or target.stat().st_uid != 0:
                raise DeploymentError("RECONCILER_UNIT_NOT_OWNED_REGULAR_FILE")
            previous = target.read_bytes()
            if b"Description=Cyrene Teams verified release reconciliation" not in previous:
                raise DeploymentError("RECONCILER_UNIT_NOT_PREVIOUSLY_MANAGED")
            archived = backup / (name + "." + hashlib.sha256(previous).hexdigest())
            if not archived.exists():
                with archived.open("xb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(previous)
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=".cyrene-teams-unit-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            try:
                os.fchmod(stream.fileno(), 0o644)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
    temporary_pointer = directory / (".current-" + release_id)
    if temporary_pointer.exists() or temporary_pointer.is_symlink():
        raise DeploymentError("RECONCILER_POINTER_STAGING_CONFLICT")
    os.symlink(base, temporary_pointer)
    os.replace(temporary_pointer, pointer)
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=30, check=True)
    subprocess.run(
        ["systemctl", "enable", "--now", "cyrene-teams-release-reconcile.timer"],
        capture_output=True,
        timeout=30,
        check=True,
    )
    result = subprocess.run(
        [
            "systemctl",
            "show",
            "cyrene-teams-release-reconcile.timer",
            "-p",
            "ActiveState",
            "--value",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    if result.stdout.strip() != "active":
        raise DeploymentError("RECONCILER_TIMER_NOT_ACTIVE")
    return {
        "schemaVersion": RECEIPT_SCHEMA,
        "releaseId": release_id,
        "manifestSha256": expected_digest,
        "state": "INSTALLED",
        "timer": "cyrene-teams-release-reconcile.timer",
        "applicationContainersModified": False,
        "updatedAtUtc": _now(),
    }


def deploy_release(
    policy: DeploymentPolicy,
    manifest_path: Path,
    expected_manifest_sha256: str,
    base_url: str,
    *,
    execute: bool,
    backend: Any | None = None,
) -> dict[str, Any]:
    """Apply compatible images one at a time; stop on any uncertain previous operation."""

    import release_bundle

    if (
        SHA256.fullmatch(expected_manifest_sha256) is None
        or _sha(manifest_path) != expected_manifest_sha256
    ):
        raise DeploymentError("RELEASE_MANIFEST_PIN_MISMATCH")
    manifest = _json(manifest_path)
    release_bundle.validate_release_manifest(manifest)
    release_id = manifest["releaseId"]
    base_url = validate_release_url(base_url, release_id)
    root = policy.root / release_id
    _directory(root, create=True)
    adapter = backend or CanonicalBackend(policy)
    with _controller_lock(policy.root):
        local_manifest = root / "release-manifest.json"
        if local_manifest.exists():
            if _sha(local_manifest) != expected_manifest_sha256:
                raise DeploymentError("EXISTING_RELEASE_MANIFEST_CONFLICT")
        else:
            # Preserve the exact attested bytes; reformatting changes their SHA.
            with local_manifest.open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(manifest_path.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
        selected = adapter.selection()
        owner = _selected_publication(policy, selected)
        publisher = manifest["publisherRun"]
        if owner is not None and owner > (publisher["runId"], publisher["runAttempt"]):
            raise DeploymentError("OLDER_PUBLICATION_CANNOT_REPLACE_NEWER_RUNTIME")
        images = {row["service"]: row for row in manifest["images"]}
        rows = adapter.image_rows(selected)
        changed = [
            target
            for target in policy.targets
            if rows[runtime_update.LOCK_SERVICE_FOR_TARGET[target]]["imageId"]
            != images[runtime_update.LOCK_SERVICE_FOR_TARGET[target]]["imageId"]
        ]
        if not execute:
            return {
                "schemaVersion": RECEIPT_SCHEMA,
                "releaseId": release_id,
                "manifestSha256": expected_manifest_sha256,
                "state": "PLANNED",
                "targets": changed,
                "steps": [],
                "selection": _selection_facts(selected),
                "updatedAtUtc": _now(),
            }
        binding = root / "deployment-binding.json"
        if binding.exists():
            status = deployment_status(policy, release_id, backend=adapter, lock_held=True)
            if not status.get("mayResumeSafely"):
                return status
            previous = _json(binding)
            applied = [value for value in status["steps"] if value["state"] == "APPLIED"]
            expected_operation = (
                applied[-1]["operationId"]
                if applied
                else previous["initialSelection"]["operationId"]
            )
            if selected["operationId"] != expected_operation:
                raise DeploymentError("SELECTION_MOVED_OUTSIDE_RELEASE_LINEAGE")
        else:
            _write(
                binding,
                {
                    "releaseId": release_id,
                    "manifestSha256": expected_manifest_sha256,
                    "policySha256": policy.sha256,
                    "initialSelection": _selection_facts(selected),
                    "at": _now(),
                },
                exclusive=True,
            )
        workspace_commit = manifest["sources"]["workspace"]["commit"]
        if not changed:
            for target in policy.targets:
                service = runtime_update.LOCK_SERVICE_FOR_TARGET[target]
                if adapter.live_image(target) != images[service]["imageId"]:
                    raise DeploymentError(
                        "UNCHANGED_LOCK_DIFFERS_FROM_LIVE_RUNTIME", state="RECOVERY_REQUIRED"
                    )
            _record_acceptance(
                root, adapter, release_id, expected_manifest_sha256, workspace_commit
            )
            status = deployment_status(policy, release_id, backend=adapter, lock_held=True)
            if status["state"] not in {"APPLIED", "ACCEPTANCE_PENDING"}:
                raise DeploymentError(
                    "UNCHANGED_LOCK_DIFFERS_FROM_LIVE_RUNTIME", state="RECOVERY_REQUIRED"
                )
            if status["state"] == "APPLIED":
                status["state"] = "NO_CHANGE"
            return status
        lock_path = download_asset(root, base_url, manifest["imageLock"])
        published_lock = runtime_update._load_lock(lock_path)
        published_rows = runtime_update._image_rows_by_service(published_lock)
        for image in images.values():
            locked = published_rows.get(image["service"])
            if not isinstance(locked, dict) or any(
                locked.get(key) != image.get(key) for key in ("image", "imageId", "sourceCommits")
            ):
                raise DeploymentError("RELEASE_MANIFEST_LOCK_MISMATCH")
        last_selection = _sha(policy.selection)
        observation_closed = False
        for target in changed:
            if _sha(policy.selection) != last_selection:
                raise DeploymentError("ACTIVE_SELECTION_CHANGED_BETWEEN_STEPS")
            step = root / "steps" / target
            _directory(step, create=True)
            image = images[runtime_update.LOCK_SERVICE_FOR_TARGET[target]]
            reserve = int(policy.required_free_gib * 1024**3)
            if shutil.disk_usage(step).free < image["archive"]["sizeBytes"] + reserve:
                raise DeploymentError("ARCHIVE_DOWNLOAD_DISK_RESERVE_INSUFFICIENT")
            archive = download_asset(root, base_url, image["archive"])
            adapter.ensure_image(step, archive, image, published_rows[image["service"]])
            adapter.wait_window(target)
            run_id = hashlib.sha256((release_id + target + _now()).encode()).hexdigest()[:32]
            probe = adapter.probe(run_id, workspace_commit)
            plan = adapter.plan(step, target, image, published_lock, probe)
            if policy.observation_profile is not None and not observation_closed:
                adapter.close_observation(root)
                observation_closed = True
            value = adapter.apply(step, plan)
            if value.get("state") != "APPLIED":
                raise DeploymentError(
                    "CANONICAL_UPDATE_NOT_APPLIED",
                    state="RECOVERY_REQUIRED"
                    if value.get("state") == "RECOVERY_REQUIRED"
                    else "FAILED",
                )
            selected = adapter.selection()
            if (
                selected["operationId"] != plan["operationId"]
                or adapter.live_image(target) != image["imageId"]
            ):
                raise DeploymentError(
                    "CANONICAL_SELECTOR_OR_ACTUAL_IMAGE_MISMATCH", state="RECOVERY_REQUIRED"
                )
            last_selection = _sha(policy.selection)
            # Only discard this verified download cache, never runtime images,
            # canonical input locks, journals, backups or private configuration.
            if _sha(archive) != image["archive"]["sha256"]:
                raise DeploymentError("ARCHIVE_CHANGED_BEFORE_OWNED_CACHE_CLEANUP")
            archive.unlink()
        _record_acceptance(root, adapter, release_id, expected_manifest_sha256, workspace_commit)
        status = deployment_status(policy, release_id, backend=adapter, lock_held=True)
        if status["state"] not in {"APPLIED", "ACCEPTANCE_PENDING"}:
            raise DeploymentError("POST_RELEASE_READBACK_INCOMPLETE", state=status["state"])
        return status


def _record_acceptance(
    root: Path, adapter: Any, release_id: str, digest: str, workspace: str
) -> None:
    """Require real provider operations after cutover, not merely process exit zero."""

    before = adapter.selection()
    if _acceptance_path(root, before).exists():
        # A missing observer can be resumed independently of already proven API
        # reads. Validate the exact evidence again instead of overwriting it.
        previous = deployment_status(adapter.policy, release_id, backend=adapter, lock_held=True)
        if previous.get("connectorAcceptanceVerified"):
            return
        raise DeploymentError("EXISTING_ACCEPTANCE_RECEIPT_CONFLICT", state="RECOVERY_REQUIRED")
    run_id = hashlib.sha256((release_id + "post-release" + _now()).encode()).hexdigest()[:32]
    probe = adapter.probe(run_id, workspace)
    if probe.get("status") != "REAL_NATIVE_EXCHANGE_READONLY_PROBE_PASS":
        raise DeploymentError("POST_CUTOVER_REAL_ACCEPTANCE_FAILED", state="FAILED")
    github = None
    if "github-issues" in adapter.policy.targets:
        github = adapter.github_acceptance(
            hashlib.sha256((run_id + "github").encode()).hexdigest()[:32]
        )
        if github.get("status") != "REAL_NATIVE_GITHUB_READONLY_PROBE_PASS":
            raise DeploymentError("POST_CUTOVER_GITHUB_READ_ACCEPTANCE_FAILED", state="FAILED")
    after = adapter.selection()
    if _selection_facts(after) != _selection_facts(before):
        raise DeploymentError("SELECTION_CHANGED_DURING_POST_ACCEPTANCE", state="RECOVERY_REQUIRED")
    _write(
        _acceptance_path(root, after),
        {
            "manifestSha256": digest,
            "status": probe["status"],
            "taskId": probe["taskId"],
            "evidencePath": probe["evidencePath"],
            "evidenceSha256": probe["evidenceSha256"],
            "selection": _selection_facts(after),
            "github": github,
            "at": _now(),
        },
        exclusive=True,
    )


def main(argv: list[str] | None = None) -> int:
    """Print a bounded result; raw exception strings remain out of deployment logs."""

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("deploy", "status", "reconcile", "install-reconciler"):
        child = commands.add_parser(command)
        child.add_argument("--policy", type=Path, required=True)
        child.add_argument("--receipt", type=Path, required=True)
        if command == "deploy":
            child.add_argument("--manifest", type=Path, required=True)
            child.add_argument("--expected-manifest-sha256", required=True)
            child.add_argument("--release-base-url", required=True)
            child.add_argument("--execute", action="store_true")
        elif command == "status":
            child.add_argument("--release-id", required=True)
        elif command == "install-reconciler":
            child.add_argument("--release-id", required=True)
            child.add_argument("--expected-manifest-sha256", required=True)
    args = parser.parse_args(argv)
    result: dict[str, Any]
    policy: DeploymentPolicy | None = None
    try:
        policy = load_policy(args.policy)
        if (
            not args.receipt.is_absolute()
            or not args.receipt.parent.resolve().is_relative_to(policy.root.resolve())
            or args.receipt.is_symlink()
        ):
            raise DeploymentError("RECEIPT_OUTSIDE_PRIVATE_RELEASE_ROOT")
        if args.command == "install-reconciler":
            result = install_reconciler(policy, args.release_id, args.expected_manifest_sha256)
        elif args.command == "reconcile":
            result = reconcile_pending_release(policy)
        elif args.command == "status":
            result = deployment_status(policy, args.release_id)
        else:
            result = deploy_release(
                policy,
                args.manifest,
                args.expected_manifest_sha256,
                args.release_base_url,
                execute=args.execute,
            )
        _write(args.receipt, result)
    except DeploymentError as error:
        result = {
            "schemaVersion": RECEIPT_SCHEMA,
            "state": error.state,
            "errorCode": error.code,
            "updatedAtUtc": _now(),
        }
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        result = {
            "schemaVersion": RECEIPT_SCHEMA,
            "state": "HELD",
            "errorCode": "DEPLOYMENT_INPUT_OR_CANONICAL_CHECK_FAILED",
            "updatedAtUtc": _now(),
        }
    if (
        policy is not None
        and args.command == "deploy"
        and args.execute
        and result.get("state") not in {"APPLIED", "NO_CHANGE", "PLANNED"}
    ):
        try:
            manifest = _json(args.manifest)
            canonical = deployment_status(policy, manifest["releaseId"])
            if canonical.get("manifestSha256") == args.expected_manifest_sha256:
                if canonical["state"] in {
                    "RECOVERY_REQUIRED",
                    "FAILED",
                    "SAFE_TO_RESUME",
                    "ACCEPTANCE_PENDING",
                }:
                    result["state"] = canonical["state"]
                result.update(
                    {
                        key: canonical[key]
                        for key in (
                            "releaseId",
                            "manifestSha256",
                            "steps",
                            "selection",
                            "mayResumeSafely",
                        )
                        if key in canonical
                    }
                )
                if (
                    args.receipt.is_absolute()
                    and args.receipt.parent.resolve().is_relative_to(policy.root.resolve())
                    and not args.receipt.is_symlink()
                ):
                    _write(args.receipt, result)
        except (
            DeploymentError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.SubprocessError,
        ):
            result["errorCode"] = "DEPLOYMENT_FAILED_READBACK_INCOMPLETE_NO_REPLAY"
    if args.command == "deploy" and "releaseId" not in result:
        try:
            identity = _json(args.manifest).get("releaseId")
            if isinstance(identity, str) and RELEASE_ID.fullmatch(identity):
                result.update(releaseId=identity, manifestSha256=args.expected_manifest_sha256)
        except (DeploymentError, OSError, ValueError):
            pass
    elif args.command in {"status", "install-reconciler"}:
        result.setdefault("releaseId", args.release_id)
        if args.command == "install-reconciler":
            result.setdefault("manifestSha256", args.expected_manifest_sha256)
    if (
        policy is not None
        and policy.observation_profile is not None
        and args.command in {"deploy", "reconcile"}
        and result.get("state")
        in {"APPLIED", "NO_CHANGE", "FAILED", "SAFE_TO_RESUME", "ACCEPTANCE_PENDING"}
        and not (
            isinstance(result.get("observation"), dict)
            and (
                result["observation"].get("status") == "RUNNING"
                or (
                    result["observation"].get("status") == "COMPLETED"
                    and result["observation"].get("complete24h") is True
                )
            )
        )
        and isinstance(result.get("releaseId"), str)
        and RELEASE_ID.fullmatch(result["releaseId"])
    ):
        try:
            with _controller_lock(policy.root):
                result["observation"] = CanonicalBackend(policy).start_observation(
                    policy.root / result["releaseId"]
                )
            current = deployment_status(policy, result["releaseId"])
            if result.get("state") in {"APPLIED", "NO_CHANGE", "ACCEPTANCE_PENDING"}:
                previous_state = result["state"]
                result.update(current)
                if current["state"] == "APPLIED":
                    result.pop("errorCode", None)
                    if previous_state == "NO_CHANGE":
                        result["state"] = "NO_CHANGE"
        except (DeploymentError, OSError, ValueError, subprocess.SubprocessError):
            result["observation"] = {"state": "NOT_STARTED_REQUIRES_READBACK", "complete24h": False}
            if result.get("state") in {"APPLIED", "NO_CHANGE", "ACCEPTANCE_PENDING"}:
                result.update(
                    state="RECOVERY_REQUIRED",
                    mayResumeSafely=False,
                    errorCode="POST_RELEASE_OBSERVATION_REQUIRES_READBACK",
                )
    if (
        policy is not None
        and args.receipt.is_absolute()
        and args.receipt.parent.resolve().is_relative_to(policy.root.resolve())
        and not args.receipt.is_symlink()
    ):
        try:
            _write(args.receipt, result)
        except (DeploymentError, OSError):
            result.update(state="RECOVERY_REQUIRED", errorCode="FINAL_RECEIPT_WRITE_FAILED")
    print(json.dumps(result, separators=(",", ":")))
    if args.command == "status" and "errorCode" not in result:
        return 0
    return (
        0
        if result.get("state") in {"APPLIED", "NO_CHANGE", "PLANNED", "NOT_FOUND", "INSTALLED"}
        else 2
    )


if __name__ == "__main__":
    sys.exit(main())
