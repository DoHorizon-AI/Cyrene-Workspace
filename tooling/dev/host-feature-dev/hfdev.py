#!/usr/bin/env python3
"""
┌──────────────────────────────────────────────────────────────────────────┐
│  📄 hfdev.py                                                             │
│  Module: tooling.dev.host_feature_dev.hfdev                              │
│  Role: One-command isolated developer stack for host/feature separation. │
│                                                                          │
│  模块职责：在开发机上一键构建并运行隔离的最小 Teams→工具链路，并记录基线。   │
└──────────────────────────────────────────────────────────────────────────┘

The stack is the production Teams cloud Compose file plus the computer-runtime
overlay and one developer overlay. Images are built for the host architecture
from exact remote commits. Every network is internal, no host port is
published, and every key is a locally generated fixture. Microsoft endpoints
are replaced by a fixture server reachable only through private network
aliases; the Navigator model is a deterministic stub.

Host actions: Git fetches from the fixed Cyrene remotes, Docker image builds
and pulls, Docker Compose project lifecycle for ``cyrene-hfdev-<instance>``,
and one root helper container that changes ownership of files inside the
instance state directory. Nothing outside the state directory is written.

中文：只操作 ``cyrene-hfdev-<instance>`` Compose 项目和实例状态目录；不连接 VM、
不发布宿主端口、不使用生产凭据。
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TOOL_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = TOOL_DIR.parents[2]
TEAMS_CLOUD_DIR = WORKSPACE_ROOT / "tooling/deploy/teams-cloud"
BASE_COMPOSE_FILES = (
    TEAMS_CLOUD_DIR / "compose.yaml",
    TEAMS_CLOUD_DIR / "compose.computer-runtime.yaml",
)
# "legacy" is the production base Compose. "actor" adds the production actor-scoped Work
# overlay; Navigator develop (eadef91) reads Teams channel tasks only through it.
# "private" also enables the private-delivery policy, the production path: the fixture user
# installs the bot, sends a personal message, and the reply comes back to that conversation.
ACTOR_WORK_COMPOSE = TEAMS_CLOUD_DIR / "compose.automation-workflows.yaml"
DEV_COMPOSE = TOOL_DIR / "compose.dev.yaml"
WORK_MODES = ("actor", "legacy", "private")
ACTOR_WORK_MODES = ("actor", "private")
WORK_ACTOR_TOKENS = tuple(f"CYRENE_WORK_ACTOR_TOKEN_0{index}" for index in range(1, 6))
DEFAULT_STATE_ROOT = Path.home() / ".local/state/cyrene-host-feature-dev"
TASK_LABEL = "host-feature-split-20261102"
MANAGED_BY = "tooling/dev/host-feature-dev/hfdev.py"
PROJECT_PREFIX = "cyrene-hfdev-"
INSTANCE_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
FORBIDDEN_PORTS = range(18680, 18696)
ALLOWED_PORTS = range(18700, 18750)
# H0 acceptance §4: Persistence idle use fills its 0.10 quota near 30 tasks
# (throttle-period ratio 0.60) and replies stop measuring the Teams quota.
PERSISTENCE_TASK_SATURATION = 30
PERSISTENCE_THROTTLE_SATURATION = 0.60
PERSISTENCE_IDLE_SAMPLE_SECONDS = 10
NETWORK_POOL = ipaddress.ip_network("10.249.0.0/16")
INSTANCE_PREFIX = 20
NETWORK_PREFIX = 26
SERVICE_UID = 10001
POSTGRES_UID = 999
PLATFORM = "linux/amd64"

REPOSITORIES: dict[str, dict[str, str]] = {
    "workspace": {
        "remote": "https://github.com/DoHorizon-AI/Cyrene-Workspace.git",
        "checkout": "Cyrene-Workspace",
    },
    "platform": {
        "remote": "https://github.com/DoHorizon-AI/Cyrene-Platform.git",
        "checkout": "Cyrene-Platform",
    },
    "plugins": {
        "remote": "https://github.com/DoHorizon-AI/Cyrene-Plugins-Official.git",
        "checkout": "Cyrene-Plugins-Official",
    },
    "exchange": {
        "remote": "https://github.com/DoHorizon-AI/Cyrene-Exchange.git",
        "checkout": "Cyrene-Services/Cyrene-Exchange",
    },
    "navigator": {
        "remote": "https://github.com/DoHorizon-AI/Cyrene-Navigator.git",
        "checkout": "Cyrene-Services/Cyrene-Navigator",
    },
}


@dataclasses.dataclass(frozen=True)
class ImageSpec:
    """One locally built service image and the Compose variable that selects it."""

    service: str
    dockerfile: str
    sources: tuple[str, ...]
    label_repo: str
    compose_env: str
    dev_dockerfile: bool = False


IMAGES: tuple[ImageSpec, ...] = (
    ImageSpec(
        "workspace-authority",
        "docker/Dockerfile.workspace-authority",
        ("workspace", "platform"),
        "platform",
        "CYRENE_IMAGE_WORKSPACE_AUTHORITY",
        dev_dockerfile=True,
    ),
    ImageSpec(
        "directory-role-bootstrap",
        "Dockerfile.directory-role-bootstrap",
        ("workspace",),
        "workspace",
        "CYRENE_IMAGE_DIRECTORY_ROLE_BOOTSTRAP",
    ),
    ImageSpec(
        "account-center",
        "Dockerfile.account-center",
        ("workspace", "plugins"),
        "plugins",
        "CYRENE_IMAGE_ACCOUNT_CENTER",
    ),
    ImageSpec(
        "exchange",
        "Dockerfile.exchange-integration",
        ("workspace", "exchange", "plugins"),
        "exchange",
        "CYRENE_IMAGE_EXCHANGE",
    ),
    ImageSpec(
        "computer-runtime",
        "Dockerfile.computer-runtime",
        ("workspace", "plugins"),
        "plugins",
        "CYRENE_IMAGE_COMPUTER_RUNTIME",
    ),
    ImageSpec(
        "computer-runtime-proxy",
        "Dockerfile.computer-proxy",
        ("workspace",),
        "workspace",
        "CYRENE_IMAGE_COMPUTER_RUNTIME_PROXY",
    ),
    ImageSpec(
        "navigator-persistence",
        "Dockerfile.navigator-persistence",
        ("workspace", "navigator"),
        "navigator",
        "CYRENE_IMAGE_NAVIGATOR_PERSISTENCE",
    ),
    ImageSpec(
        "dsh-executor",
        "Dockerfile.dsh-executor",
        ("workspace", "navigator"),
        "navigator",
        "CYRENE_IMAGE_DSH_EXECUTOR",
    ),
    ImageSpec(
        "teams-ingress",
        "Dockerfile.teams-ingress",
        ("workspace", "plugins"),
        "plugins",
        "CYRENE_IMAGE_TEAMS_INGRESS",
    ),
)

# The executor Dockerfile installs a pinned Antigravity binary. This stack selects the
# OpenAI-compatible stub model, so a labelled placeholder satisfies the build check only.
# 中文：模型走桩，agy 永不执行；占位文件只用于通过构建期摘要与版本检查。
AGY_PLACEHOLDER = (
    "#!/bin/sh\n"
    "# cyrene-hfdev placeholder: not the Google Antigravity CLI; never selected at runtime.\n"
    'echo "agy 1.4.7-cyrene-hfdev-placeholder"\n'
)

# Every Compose network in the production base, the computer overlay, and this overlay.
NETWORKS: tuple[str, ...] = (
    "account-egress",
    "account-private",
    "cloud-management-egress",
    "cloud-management-private",
    "computer-runtime-private",
    "directory-private",
    "exchange-authority-private",
    "exchange-computer-private",
    "github-account-center-private",
    "github-egress",
    "github-exchange-private",
    "ingress-egress",
    "navigator-egress",
    "navigator-exchange-private",
    "navigator-persistence-private",
    "navigator-teams-private",
    "teams-egress",
    "teams-provider-egress",
    "teams-provider-private",
    "teams-proxy-private",
)

# Long-running services of the minimal stack, in start order.
FOUNDATION_SERVICES = ("postgres",)
PROVIDER_TIER = (
    "workspace-authority",
    "account-center",
    "computer-runtime",
    "computer-runtime-proxy",
    "microsoft-fixture",
    "teams-provider",
    "model-stub",
    "exchange",
)
EDGE_TIER = ("navigator-persistence", "navigator", "teams-ingress")
RUNNING_SERVICES = FOUNDATION_SERVICES + PROVIDER_TIER + EDGE_TIER


class DevStackError(RuntimeError):
    """A precondition or stack operation failed closed."""


# ── Process helpers ──────────────────────────────────────────────────────────


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
    check: bool = True,
    timeout: float | None = None,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run one command without a shell; captured output stays out of the terminal."""

    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        input=stdin,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = ""
        if capture and completed.stderr:
            detail = completed.stderr.decode("utf-8", "replace").strip()[-2000:]
        raise DevStackError(f"command failed ({completed.returncode}): {argv[:4]} {detail}")
    return completed


def _text(argv: list[str], **kwargs: Any) -> str:
    return _run(argv, capture=True, **kwargs).stdout.decode("utf-8").strip()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    """Atomically write deterministic JSON with a restrictive mode."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _write_bytes(path: Path, value: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ── Instance state ───────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Instance:
    """Paths and identity of one isolated developer stack."""

    name: str
    state_root: Path

    @property
    def root(self) -> Path:
        return self.state_root / "instances" / self.name

    @property
    def project(self) -> str:
        return PROJECT_PREFIX + self.name

    @property
    def secrets_dir(self) -> Path:
        return self.root / "secrets"

    @property
    def config_dir(self) -> Path:
        """Root-owned directory mounted into services (production layout)."""

        return self.root / "config"

    @property
    def config_source_dir(self) -> Path:
        """Host-owned staging copy; `_fix_ownership` installs it into `config_dir`."""

        return self.root / "config-src"

    @property
    def fixtures_dir(self) -> Path:
        return self.root / "fixtures"

    @property
    def runtime_dir(self) -> Path:
        return self.root / "runtime"

    @property
    def evidence_dir(self) -> Path:
        return self.root / "evidence"

    @property
    def lock_path(self) -> Path:
        return self.root / "images.lock.json"

    @property
    def identity_path(self) -> Path:
        return self.root / "instance.json"

    @property
    def env_path(self) -> Path:
        return self.root / "compose.env"


def _instance(args: argparse.Namespace) -> Instance:
    if not INSTANCE_RE.fullmatch(args.instance):
        raise DevStackError("instance names use lowercase letters, digits, and hyphens")
    state_root = Path(args.state_root).expanduser().resolve()
    if state_root.name != DEFAULT_STATE_ROOT.name and not state_root.name.startswith(
        DEFAULT_STATE_ROOT.name + "-"
    ):
        raise DevStackError(
            "state root must be ~/.local/state/cyrene-host-feature-dev or a -suffixed sibling"
        )
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_root, 0o700)
    return Instance(args.instance, state_root)


def _evidence_run(instance: Instance, label: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = instance.evidence_dir / f"{stamp}-{label}"
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    return path


# ── Sources and image build ──────────────────────────────────────────────────


def _parse_pins(values: Iterable[str]) -> dict[str, str]:
    pins: dict[str, str] = {}
    for value in values:
        alias, separator, commit = value.partition("=")
        if not separator or alias not in REPOSITORIES or alias == "workspace":
            raise DevStackError(
                f"--pin must be <platform|plugins|exchange|navigator>=<sha>: {value}"
            )
        if not SHA_RE.fullmatch(commit):
            raise DevStackError(f"--pin {alias} requires a full 40-character commit SHA")
        pins[alias] = commit
    return pins


def _remote_develop(alias: str) -> str:
    output = _text(["git", "ls-remote", REPOSITORIES[alias]["remote"], "refs/heads/develop"])
    commit = output.split()[0] if output else ""
    if not SHA_RE.fullmatch(commit):
        raise DevStackError(f"could not resolve develop for {alias}")
    return commit


def _workspace_head() -> str:
    commit = _text(["git", "-C", str(WORKSPACE_ROOT), "rev-parse", "HEAD"])
    dirty = _text(
        [
            "git",
            "-C",
            str(WORKSPACE_ROOT),
            "status",
            "--porcelain",
            "--",
            "tooling/deploy/teams-cloud",
        ]
    )
    if dirty:
        raise DevStackError("tooling/deploy/teams-cloud is dirty; build recipes must be committed")
    return commit


def _materialize(alias: str, commit: str, cache: Path, destination: Path) -> str:
    """Fetch one exact commit into a private bare cache and extract its tree."""

    if alias == "workspace":
        archive = _run(
            ["git", "-C", str(WORKSPACE_ROOT), "archive", "--format=tar", commit], capture=True
        ).stdout
        tree = _text(["git", "-C", str(WORKSPACE_ROOT), "rev-parse", f"{commit}^{{tree}}"])
    else:
        bare = cache / f"{alias}.git"
        if not bare.exists():
            _run(["git", "init", "--quiet", "--bare", str(bare)])
            _run(["git", "-C", str(bare), "remote", "add", "origin", REPOSITORIES[alias]["remote"]])
        if _run(
            ["git", "-C", str(bare), "cat-file", "-e", f"{commit}^{{commit}}"],
            capture=True,
            check=False,
        ).returncode:
            _run(
                [
                    "git",
                    "-C",
                    str(bare),
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--depth=1",
                    "origin",
                    commit,
                ]
            )
        archive = _run(
            ["git", "-C", str(bare), "archive", "--format=tar", commit], capture=True
        ).stdout
        tree = _text(["git", "-C", str(bare), "rev-parse", f"{commit}^{{tree}}"])
    destination.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryFile() as stream:
        stream.write(archive)
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r:") as tar:
            tar.extractall(destination, filter="data")
    for current, directories, _files in os.walk(destination):
        for directory in directories:
            os.chmod(Path(current) / directory, 0o755)
    return tree


def _build_plan_args(workspace_checkout: Path) -> dict[str, dict[str, str]]:
    """Read pinned base images and build arguments from the release build plan."""

    plan = _read_json(
        workspace_checkout / "tooling/deploy/teams-cloud/config/build-plan.template.json"
    )
    rows = {row["service"]: dict(row.get("buildArgs", {})) for row in plan["images"]}
    external = dict(plan.get("externalImages", {}))
    rows["__external__"] = external
    return rows


def _inspect_image(reference: str) -> dict[str, Any]:
    return json.loads(_text(["docker", "image", "inspect", reference]))[0]


def cmd_build(args: argparse.Namespace) -> int:
    """Build every stack image for the host architecture from exact commits."""

    instance = _instance(args)
    pins = _parse_pins(args.pin)
    commits = {"workspace": _workspace_head()}
    for alias in ("platform", "plugins", "exchange", "navigator"):
        commits[alias] = pins.get(alias) or _remote_develop(alias)
    cache = instance.state_root / "cache/git"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    selected = {spec.service for spec in IMAGES} if not args.only else set(args.only)
    unknown = selected - {spec.service for spec in IMAGES}
    if unknown:
        raise DevStackError(f"unknown image services: {sorted(unknown)}")
    previous = _read_json(instance.lock_path) if instance.lock_path.exists() else None
    lock: dict[str, Any] = {
        "schemaVersion": "cyrene-hfdev-image-lock-v1",
        "createdAt": _now(),
        "platform": PLATFORM,
        "sources": {},
        "images": dict(previous.get("images", {})) if previous and args.only else {},
        "externalImages": {},
        "placeholders": {},
    }
    with tempfile.TemporaryDirectory(prefix="cyrene-hfdev-build-") as temporary:
        staging = Path(temporary)
        for alias, commit in commits.items():
            tree = _materialize(alias, commit, cache, staging / REPOSITORIES[alias]["checkout"])
            lock["sources"][alias] = {
                "remote": REPOSITORIES[alias]["remote"],
                "commit": commit,
                "tree": tree,
            }
            print(f"source {alias} {commit}", flush=True)
        plan_args = _build_plan_args(staging / "Cyrene-Workspace")
        placeholder = staging / "runtime-artifacts/antigravity/agy"
        placeholder.parent.mkdir(parents=True)
        placeholder.write_text(AGY_PLACEHOLDER, encoding="utf-8")
        os.chmod(placeholder, 0o555)
        lock["placeholders"]["antigravity-cli"] = {
            "sha256": _sha256_file(placeholder),
            "reason": "stub model selected; agy is never executed",
        }
        workspace_short = commits["workspace"][:8]
        # A fixed image creation time makes identical sources produce identical image IDs.
        source_date_epoch = _text(
            ["git", "-C", str(WORKSPACE_ROOT), "show", "-s", "--format=%ct", commits["workspace"]]
        )
        lock["sourceDateEpoch"] = int(source_date_epoch)
        for spec in IMAGES:
            if spec.service not in selected:
                continue
            if spec.dev_dockerfile:
                dockerfile = TOOL_DIR / spec.dockerfile
            else:
                dockerfile = (
                    staging / "Cyrene-Workspace/tooling/deploy/teams-cloud" / spec.dockerfile
                )
            build_args = dict(plan_args[spec.service])
            for key in [name for name in build_args if name.endswith("_ARM64_SHA256")]:
                build_args.pop(key)
            if spec.service == "dsh-executor":
                build_args["ASSET_ANTIGRAVITY_CLI_SHA256"] = lock["placeholders"][
                    "antigravity-cli"
                ]["sha256"]
            label_commit = commits[spec.label_repo]
            build_args["SOURCE_REVISION"] = label_commit
            build_args["SOURCE_DATE_EPOCH"] = source_date_epoch
            reference = f"cyrene-hfdev/{spec.service}:{label_commit[:12]}-w{workspace_short}-{instance.name}"
            argv = [
                "docker",
                "buildx",
                "build",
                "--builder",
                "default",
                "--platform",
                PLATFORM,
                # Attestations carry build timestamps and would change the image ID.
                "--provenance=false",
                "--sbom=false",
                "--file",
                str(dockerfile),
                "--tag",
                reference,
                "--label",
                f"io.cyrene.task={TASK_LABEL}",
                "--label",
                f"io.cyrene.managed-by={MANAGED_BY}",
                "--label",
                f"org.opencontainers.image.revision={label_commit}",
                "--label",
                f"io.cyrene.hfdev.dockerfile-sha256={_sha256_file(dockerfile)}",
            ]
            for alias in spec.sources:
                argv += ["--label", f"io.cyrene.source.{alias}.revision={commits[alias]}"]
            for key, value in sorted(build_args.items()):
                argv += ["--build-arg", f"{key}={value}"]
            argv.append(str(staging))
            started = time.monotonic()
            print(f"build {spec.service} -> {reference}", flush=True)
            _run(argv)
            inspect = _inspect_image(reference)
            if f"{inspect.get('Os')}/{inspect.get('Architecture')}" != PLATFORM:
                raise DevStackError(f"{spec.service} built for an unexpected platform")
            lock["images"][spec.service] = {
                "image": reference,
                "imageId": inspect["Id"],
                "sizeBytes": inspect.get("Size"),
                "composeEnv": spec.compose_env,
                "dockerfile": spec.dockerfile,
                "dockerfileSource": "tooling/dev/host-feature-dev"
                if spec.dev_dockerfile
                else "tooling/deploy/teams-cloud",
                "dockerfileSha256": _sha256_file(dockerfile),
                "sourceCommits": {alias: commits[alias] for alias in spec.sources},
                "buildSeconds": round(time.monotonic() - started, 1),
            }
    postgres_ref = plan_args["__external__"]["POSTGRES"]
    _run(["docker", "pull", "--quiet", "--platform", PLATFORM, postgres_ref], capture=True)
    postgres_tag = f"cyrene-hfdev/postgres:{postgres_ref.rsplit(':', 1)[1][:12]}"
    _run(["docker", "image", "tag", postgres_ref, postgres_tag])
    lock["externalImages"]["postgres"] = {
        "reference": postgres_ref,
        "image": postgres_tag,
        "imageId": _inspect_image(postgres_tag)["Id"],
    }
    instance.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_json(instance.lock_path, lock)
    print(f"image lock: {instance.lock_path}")
    return 0


def _load_lock(instance: Instance) -> dict[str, Any]:
    if not instance.lock_path.is_file():
        raise DevStackError("run `build` for this instance first")
    lock = _read_json(instance.lock_path)
    missing = {spec.service for spec in IMAGES} - set(lock["images"])
    if missing:
        raise DevStackError(f"image lock lacks services: {sorted(missing)}")
    for row in [*lock["images"].values(), lock["externalImages"]["postgres"]]:
        if _inspect_image(row["image"])["Id"] != row["imageId"]:
            raise DevStackError(f"local image {row['image']} no longer matches the lock")
    return lock


# ── Fixture identities, keys, and certificates ───────────────────────────────

ISSUER_TEMPLATE = "https://login.microsoftonline.com/{tenant}/v2.0"
SERVICE_URL = "https://smba.trafficmanager.net/amer/"
AUTHORITY_KEY_ID = "hfdev-authority-v1"
EXCHANGE_WORKLOAD_ID = "exchange-service"
EXCHANGE_CALLER_ID = "exchange-service"
GITHUB_PROVIDER_CALLER_ID = "github-issues-provider"
TEAMS_PROFILE_REF = "teams-primary"
COMPUTER_PROFILE_REF = "computer-workspace-main"
ACCOUNT_CENTER_BINDING = "host-account-center-binding"
GITHUB_BINDING = "host-github-issues-binding"
TEAMS_BINDING = "host-teams-message-binding"
JWT_KEY_ID = "hfdev-bot-framework-fixture-1"
# PKCS#8 prefix for a raw 32-byte Ed25519 private key (RFC 8410).
ED25519_PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")


def _identities(name: str) -> dict[str, str]:
    """Derive stable fixture identifiers from the instance name."""

    namespace = uuid.uuid5(uuid.NAMESPACE_URL, f"https://cyrene.invalid/hfdev/{name}")

    def make(label: str) -> str:
        return str(uuid.uuid5(namespace, label))

    tenant = make("tenant")
    digest = hashlib.sha256(name.encode()).hexdigest()[:12]
    return {
        "organizationId": make("organization"),
        "workspaceId": make("workspace"),
        "tenantId": tenant,
        "botAppId": make("bot-app"),
        "actorOid": make("fixture-actor"),
        "operatorActorId": make("directory-operator"),
        "teamsConnectionRef": make("teams-connection"),
        "computerConnectionRef": make("computer-connection"),
        "teamId": f"19:hfdev{digest}team@thread.tacv2",
        "channelId": f"19:hfdev{digest}chan@thread.tacv2",
        "personalConversationId": f"a:hfdev{digest}personal",
        "issuer": ISSUER_TEMPLATE.format(tenant=tenant),
    }


class CertificateFactory:
    """Generate throwaway CAs and leaves with the openssl CLI (ECDSA P-256)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _key(self, path: Path, *, rsa: bool = False) -> None:
        argv = ["openssl", "genpkey", "-out", str(path)]
        argv += (
            ["-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"]
            if rsa
            else ["-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256"]
        )
        _run(argv, capture=True)
        os.chmod(path, 0o600)

    def ca(self, name: str) -> tuple[Path, Path]:
        key, cert = self.directory / f"{name}.key", self.directory / f"{name}.crt"
        self._key(key)
        _run(
            [
                "openssl",
                "req",
                "-x509",
                "-new",
                "-key",
                str(key),
                "-sha256",
                "-days",
                "120",
                "-subj",
                f"/O=Cyrene HFDev Fixture/CN={name}",
                "-out",
                str(cert),
                "-addext",
                "basicConstraints=critical,CA:TRUE,pathlen:0",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
                "-addext",
                "subjectKeyIdentifier=hash",
            ],
            capture=True,
        )
        return key, cert

    def leaf(
        self,
        ca: tuple[Path, Path],
        name: str,
        *,
        dns: tuple[str, ...] = (),
        client: bool = False,
    ) -> tuple[Path, Path]:
        key, cert = self.directory / f"{name}.key", self.directory / f"{name}.crt"
        csr = self.directory / f"{name}.csr"
        self._key(key)
        _run(
            ["openssl", "req", "-new", "-key", str(key), "-subj", f"/CN={name}", "-out", str(csr)],
            capture=True,
        )
        usage = "clientAuth" if client else "serverAuth"
        lines = [
            "basicConstraints=critical,CA:FALSE",
            "keyUsage=critical,digitalSignature,keyEncipherment",
            f"extendedKeyUsage={usage}",
            "subjectKeyIdentifier=hash",
            "authorityKeyIdentifier=keyid",
        ]
        if dns:
            lines.append("subjectAltName=" + ",".join(f"DNS:{host}" for host in dns))
        extensions = self.directory / f"{name}.ext"
        extensions.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _run(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(csr),
                "-CA",
                str(ca[1]),
                "-CAkey",
                str(ca[0]),
                "-set_serial",
                str(secrets.randbits(63)),
                "-days",
                "90",
                "-sha256",
                "-extfile",
                str(extensions),
                "-out",
                str(cert),
            ],
            capture=True,
        )
        csr.unlink()
        extensions.unlink()
        return key, cert

    def rsa_private_key(self, name: str) -> Path:
        key = self.directory / f"{name}.pem"
        self._key(key, rsa=True)
        return key


def _der_sha256(certificate: Path) -> str:
    der = _run(["openssl", "x509", "-in", str(certificate), "-outform", "DER"], capture=True).stdout
    return _sha256_bytes(der)


def _rsa_jwk(private_key: Path, kid: str) -> dict[str, str]:
    modulus = _text(["openssl", "rsa", "-in", str(private_key), "-noout", "-modulus"]).split(
        "=", 1
    )[1]
    n = bytes.fromhex(modulus)
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": base64.urlsafe_b64encode(n).rstrip(b"=").decode(),
        "e": "AQAB",
    }


def _ed25519_public(seed: bytes) -> bytes:
    der = _run(
        ["openssl", "pkey", "-inform", "DER", "-pubout", "-outform", "DER"],
        capture=True,
        stdin=ED25519_PKCS8_PREFIX + seed,
    ).stdout
    if len(der) != 44:
        raise DevStackError("unexpected Ed25519 public key encoding")
    return der[-32:]


def _subnets(instance: Instance) -> dict[str, str]:
    """Pick one free /20 inside 10.249.0.0/16 and carve one /26 per network."""

    taken: list[ipaddress.IPv4Network] = []
    for network_id in _text(["docker", "network", "ls", "-q"]).split():
        inspect = json.loads(_text(["docker", "network", "inspect", network_id]))[0]
        if inspect.get("Labels", {}).get("com.docker.compose.project") == instance.project:
            continue
        for row in (inspect.get("IPAM") or {}).get("Config") or []:
            if row.get("Subnet") and ":" not in row["Subnet"]:
                taken.append(ipaddress.ip_network(row["Subnet"]))
    for other in (instance.state_root / "instances").glob("*/instance.json"):
        if other.parent.name != instance.name:
            taken.append(ipaddress.ip_network(_read_json(other)["subnetBlock"]))
    for block in NETWORK_POOL.subnets(new_prefix=INSTANCE_PREFIX):
        if not any(block.overlaps(existing) for existing in taken):
            carved = list(block.subnets(new_prefix=NETWORK_PREFIX))
            return {
                "block": str(block),
                **{name: str(carved[index]) for index, name in enumerate(NETWORKS)},
            }
    raise DevStackError("no free /20 remains in 10.249.0.0/16")


def _random_hex(size: int = 32) -> bytes:
    return secrets.token_hex(size).encode()


def _project_volumes(instance: Instance) -> list[str]:
    """Named volumes Compose created for this project. Empty when `down --volumes` ran."""

    output = _text(
        [
            "docker",
            "volume",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={instance.project}",
        ]
    )
    return [line for line in output.splitlines() if line.strip()]


def _refuse_leftover_volumes(names: list[str]) -> None:
    """Stop `init` from writing a new Postgres password onto the previous data volume."""

    if not names:
        return
    shown = ", ".join(names[:4])
    extra = f" (+{len(names) - 4} more)" if len(names) > 4 else ""
    raise DevStackError(
        "Postgres and service volumes from an earlier run are still present "
        f"({shown}{extra}). They keep the previous database password, so a new `init` "
        'fails about a minute later as "Directory migration did not succeed". '
        "Run `down --volumes` before `init`."
    )


def cmd_init(args: argparse.Namespace) -> int:
    """Generate fixture identities, keys, certificates, configs, and compose.env."""

    instance = _instance(args)
    lock = _load_lock(instance)
    if instance.identity_path.exists() and not args.force:
        raise DevStackError("instance already initialized; pass --force to regenerate fixtures")
    if _project_containers(instance):
        raise DevStackError("stop the running stack (`down`) before regenerating fixtures")
    _refuse_leftover_volumes(_project_volumes(instance))
    ids = _identities(instance.name)
    subnets = _subnets(instance)
    for directory in (
        instance.secrets_dir,
        instance.config_source_dir,
        instance.fixtures_dir,
        instance.runtime_dir,
    ):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (instance.runtime_dir / "teams-delivery").mkdir(mode=0o700, exist_ok=True)
    (instance.runtime_dir / "codebuddy").mkdir(mode=0o700, exist_ok=True)
    (instance.runtime_dir / "automation-health").mkdir(mode=0o755, exist_ok=True)
    factory = CertificateFactory(instance.root / "ca")
    s = instance.secrets_dir

    def put(name: str, value: bytes) -> None:
        _write_bytes(s / name, value, mode=0o600)

    def copy(name: str, source: Path) -> None:
        put(name, source.read_bytes())

    # Postgres and Directory database logins.
    passwords = {role: secrets.token_urlsafe(24) for role in ("admin", "app", "operator")}
    postgres_ca = factory.ca("postgres-ca")
    key, cert = factory.leaf(postgres_ca, "postgres-server", dns=("postgres",))
    put("postgres-admin-password", passwords["admin"].encode())
    copy("postgres-server-cert", cert)
    copy("postgres-server-key", key)
    copy("postgres-root-ca", postgres_ca[1])
    query = "sslmode=verify-full&sslrootcert=/run/secrets/postgres-root-ca"
    for name, user, role in (
        ("directory-admin-database-url", "cyrene_directory_admin", "admin"),
        ("directory-runtime-reader-database-url", "cyrene_authority_app", "app"),
        ("directory-operator-database-url", "cyrene_directory_operator", "operator"),
    ):
        put(
            name,
            f"postgresql://{user}:{passwords[role]}@postgres:5432/cyrene_directory?{query}".encode(),
        )

    # Authority signing seed, public keyring, and the two Authority mTLS hops.
    seed = secrets.token_bytes(32)
    put("authority-signing-seed", seed)
    keyring = {"keys": {AUTHORITY_KEY_ID: base64.b64encode(_ed25519_public(seed)).decode()}}
    authority_server_ca = factory.ca("authority-server-ca")
    authority_client_ca = factory.ca("authority-client-ca")
    key, cert = factory.leaf(
        authority_server_ca, "workspace-authority", dns=("workspace-authority", "authority")
    )
    copy("authority-tls-cert", cert)
    copy("authority-tls-key", key)
    copy("authority-tls-client-ca", authority_client_ca[1])
    copy("exchange-authority-root-ca", authority_server_ca[1])
    key, cert = factory.leaf(authority_client_ca, EXCHANGE_WORKLOAD_ID, client=True)
    copy("exchange-authority-client-cert", cert)
    copy("exchange-authority-client-key", key)
    exchange_authority_fingerprint = _der_sha256(cert)

    # Private HTTPS (Exchange, Persistence, model stub) under one internal CA.
    internal_ca = factory.ca("internal-service-ca")
    copy("internal-service-root-ca", internal_ca[1])
    for secret_prefix, host in (
        ("exchange-tls", "exchange"),
        ("navigator-persistence-tls", "navigator-persistence"),
        ("hfdev-model-stub-tls", "model-stub"),
    ):
        key, cert = factory.leaf(internal_ca, host, dns=(host,))
        copy(f"{secret_prefix}-cert", cert)
        copy(f"{secret_prefix}-key", key)

    # DirectPluginRuntime servers and their clients.
    plugin_server_ca = factory.ca("plugin-server-ca")
    plugin_client_ca = factory.ca("plugin-client-ca")
    copy("exchange-plugin-root-ca", plugin_server_ca[1])
    copy("exchange-plugin-client-ca", plugin_client_ca[1])
    copy("account-center-client-ca", plugin_client_ca[1])
    for secret_prefix, host in (
        ("account-center-server", "account-center"),
        ("github-issues-server", "github-issues"),
        ("teams-connector-server", "teams-provider"),
        ("computer-runtime-proxy-server", "computer-runtime-proxy"),
        ("cloud-management-server", "cloud-management"),
    ):
        key, cert = factory.leaf(plugin_server_ca, host, dns=(host,))
        copy(f"{secret_prefix}-cert", cert)
        copy(f"{secret_prefix}-key", key)
    for secret_prefix in (
        "exchange-plugin-client",
        "exchange-account-center-client",
        "account-center-health-client",
        "teams-connector-health-client",
        "github-issues-health-client",
        "github-issues-account-center-client",
        "cloud-management-health-client",
    ):
        key, cert = factory.leaf(plugin_client_ca, secret_prefix, client=True)
        copy(f"{secret_prefix}-cert", cert)
        copy(f"{secret_prefix}-key", key)

    # Pairwise HMAC keys and tokens; each boundary has its own value.
    put("navigator-session-token", secrets.token_urlsafe(48).encode())
    put("teams-navigator-hmac-key", _random_hex())
    put("teams-delivery-admin-token", secrets.token_urlsafe(48).encode())
    put("navigator-exchange-hmac-key", secrets.token_bytes(32))
    for name in (
        "account-center-encryption-key",
        "account-center-exchange-hmac-key",
        "account-center-github-provider-hmac-key",
    ):
        put(name, _random_hex())
    copy("account-center-github-app-private-key", factory.rsa_private_key("unused-github-app"))
    put("github-issues-webhook-secret", secrets.token_urlsafe(32).encode())
    bot_secret = "hfdev-fixture-" + secrets.token_urlsafe(24)
    put("teams-bot-client-secret", bot_secret.encode())
    put("hfdev-model-stub-token", ("hfdev-" + secrets.token_hex(24)).encode())
    put("teams-private-bridge-token", secrets.token_urlsafe(48).encode())
    for index in range(1, 6):
        put(f"work-actor-token-0{index}", secrets.token_urlsafe(48).encode())

    # Microsoft endpoint fixtures and the inbound activity signing keys.
    external_ca = factory.ca("external-fixture-ca")
    copy("hfdev-external-fixture-root-ca", external_ca[1])
    key, cert = factory.leaf(
        external_ca,
        "microsoft-fixture",
        dns=("login.botframework.com", "login.microsoftonline.com", "smba.trafficmanager.net"),
    )
    copy("hfdev-microsoft-fixture-tls-cert", cert)
    copy("hfdev-microsoft-fixture-tls-key", key)
    signing_key = factory.rsa_private_key("teams-fixture-signing")
    attacker_key = factory.rsa_private_key("teams-attacker-signing")
    copy("hfdev-teams-fixture-signing-key", signing_key)
    copy("hfdev-teams-attacker-signing-key", attacker_key)
    put(
        "hfdev-microsoft-fixture-config",
        json.dumps(
            {
                "botAppId": ids["botAppId"],
                "tenantId": ids["tenantId"],
                "clientSecret": bot_secret,
                "jwks": {"keys": [_rsa_jwk(signing_key, JWT_KEY_ID)]},
            }
        ).encode(),
    )
    put(
        "hfdev-smoke-config",
        json.dumps(
            {
                "botAppId": ids["botAppId"],
                "tenantId": ids["tenantId"],
                "teamId": ids["teamId"],
                "channelId": ids["channelId"],
                "personalConversationId": ids["personalConversationId"],
                "workMode": args.work_mode,
                "actorOid": ids["actorOid"],
                "serviceUrl": SERVICE_URL,
                "jwtKeyId": JWT_KEY_ID,
                "ingressUrl": "http://teams-ingress:3978",
                "fixtureUrl": "https://smba.trafficmanager.net",
                "modelStubUrl": "https://model-stub:8443",
                "providerTarget": "teams-provider:7443",
                "navigatorUrl": "http://navigator:8080",
            }
        ).encode(),
    )

    # Non-secret configuration mounted into Navigator, Persistence, and Exchange.
    c = instance.config_source_dir
    _write_json(c / "authority-signing-keyring.json", keyring)
    principals: list[dict[str, Any]] = [
        {
            "token_env": "NAVIGATOR_SESSION_TOKEN",
            "actor_id": ids["actorOid"],
            "workspace_ids": [ids["workspaceId"]],
            "organization_id": ids["organizationId"],
            "can_takeover": True,
            # Persistence rejects Harness session writes from an organization-scoped
            # principal without this flag; the Workspace template omits it.
            "can_write_harness": True,
            "can_publish_credential_health": False,
        }
    ]
    if args.work_mode in ACTOR_WORK_MODES:
        # Same shape as config/automation-work-principals.patch.template.json, one actor.
        principals += [
            {
                "token_env": WORK_ACTOR_TOKENS[0],
                "actor_id": ids["actorOid"],
                "workspace_ids": [ids["workspaceId"]],
                "organization_id": ids["organizationId"],
                "can_takeover": True,
                "actor_private_scope": True,
                "can_write_harness": True,
                "can_publish_credential_health": False,
                "can_bind_teams_private_conversations": False,
            },
            {
                "token_env": "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN",
                "actor_id": "teams-private-bridge",
                "workspace_ids": [ids["workspaceId"]],
                "organization_id": ids["organizationId"],
                "can_takeover": False,
                "actor_private_scope": False,
                "can_write_harness": False,
                "can_publish_credential_health": False,
                "can_bind_teams_private_conversations": True,
            },
        ]
        _write_json(
            c / "work-actor-token-map.json",
            {
                "schemaVersion": 1,
                "workspaceId": ids["workspaceId"],
                "actors": [{"actorAadObjectId": ids["actorOid"], "tokenEnv": WORK_ACTOR_TOKENS[0]}],
            },
        )
    principal_config: dict[str, Any] = {
        "principals": principals,
        "product_directory": [],
        "product_service_credentials": [],
    }
    if args.work_mode == "private":
        principal_config["teams_private_conversations"] = {
            "actor_subjects_file_env": "TEAMS_TASK_ACTOR_SUBJECTS_FILE",
            "workspace_id": ids["workspaceId"],
            "issuer": ids["issuer"],
            "tenant_id": ids["tenantId"],
            "bot_app_id": ids["botAppId"],
        }
    _write_json(c / "navigator-principal-config.json", principal_config)
    _write_json(
        c / "teams-actor-subjects.json",
        {
            "schemaVersion": 1,
            "workspaceId": ids["workspaceId"],
            "issuer": ids["issuer"],
            "subjects": [ids["actorOid"]],
        },
    )
    _write_json(
        c / "connector-bootstrap.json",
        {
            "schemaVersion": 1,
            "actor": {
                "issuer": ids["issuer"],
                "subject": ids["actorOid"],
                "organizationId": ids["organizationId"],
                "workspaceId": ids["workspaceId"],
            },
            "github": {
                "installationId": 1,
                "profileIdempotencyKey": "hfdev-unused-github-profile",
                "repositories": [{"repositoryId": 1, "owner": "hfdev-unused", "name": "unused"}],
            },
        },
    )
    # Navigator refuses empty production maps; `up` replaces both with Exchange exports.
    for name in ("connector-bindings.json", "connector-tool-catalog.json"):
        _write_json(c / name, {"schemaVersion": 1, "pending": "exported by hfdev.py up"})

    environment = _compose_environment(
        instance, lock, ids, subnets, exchange_authority_fingerprint, args.work_mode
    )
    lines = [f"{key}='{value}'" for key, value in sorted(environment.items())]
    if any("'" in str(value) for value in environment.values()):
        raise DevStackError("compose environment values must not contain single quotes")
    instance.env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(instance.env_path, 0o600)
    _write_json(
        instance.identity_path,
        {
            "schemaVersion": "cyrene-hfdev-instance-v1",
            "instance": instance.name,
            "project": instance.project,
            "createdAt": _now(),
            "workMode": args.work_mode,
            "identities": ids,
            "subnetBlock": subnets["block"],
            "subnets": {name: subnets[name] for name in NETWORKS},
            "exchangeAuthorityClientSha256": exchange_authority_fingerprint,
            "imageLockSha256": _sha256_file(instance.lock_path),
        },
    )
    _fix_ownership(instance, lock)
    print(f"initialized {instance.root}")
    return 0


def _plugin_mtls(root: str, cert: str, key: str) -> dict[str, str]:
    return {"rootCertificateRef": root, "clientCertificateRef": cert, "clientKeyRef": key}


def _exchange_configuration(ids: dict[str, str], work_mode: str = "legacy") -> dict[str, Any]:
    """Closed Exchange integration-only host configuration for this stack."""

    plugin = _plugin_mtls(
        "tls/plugin-root", "tls/exchange-plugin-client-cert", "tls/exchange-plugin-client-key"
    )
    return {
        "schemaVersion": 1,
        "hostSecretRefs": {
            "platform/connector-root": "PLATFORM_CONNECTOR_AUTHORITY_ROOT_CERTIFICATE_PATH",
            "platform/connector-client-cert": "EXCHANGE_CONNECTOR_CLIENT_CERTIFICATE_PATH",
            "platform/connector-client-key": "EXCHANGE_CONNECTOR_CLIENT_PRIVATE_KEY_PATH",
            "tls/plugin-root": "PLUGIN_RUNTIME_ROOT_CERTIFICATE_PATH",
            "tls/exchange-account-center-client-cert": "EXCHANGE_ACCOUNT_CENTER_CLIENT_CERTIFICATE_PATH",
            "tls/exchange-account-center-client-key": "EXCHANGE_ACCOUNT_CENTER_CLIENT_PRIVATE_KEY_PATH",
            "tls/exchange-plugin-client-cert": "EXCHANGE_PLUGIN_CLIENT_CERTIFICATE_PATH",
            "tls/exchange-plugin-client-key": "EXCHANGE_PLUGIN_CLIENT_PRIVATE_KEY_PATH",
            "computer/root-ca": "EXCHANGE_COMPUTER_PROXY_ROOT_CA_FILE",
            "computer/client-cert": "EXCHANGE_COMPUTER_PROXY_CLIENT_CERT_FILE",
            "computer/client-key": "EXCHANGE_COMPUTER_PROXY_CLIENT_KEY_FILE",
            "account/signing": "EXCHANGE_ACCOUNT_CONTEXT_HMAC_KEY",
            "navigator/exchange-hmac": "NAVIGATOR_EXCHANGE_HMAC_KEY_FILE",
        },
        "platformAuthority": {
            "connectorWorkload": {
                "connectionRef": "grpcs://workspace-authority:7443",
                "rootCertificateRef": "platform/connector-root",
                "clientCertificateRef": "platform/connector-client-cert",
                "clientKeyRef": "platform/connector-client-key",
            }
        },
        "accountCenter": {
            "bindingId": ACCOUNT_CENTER_BINDING,
            "connectionRef": "grpcs://account-center:7443",
            "transport": "mtls",
            "mtls": _plugin_mtls(
                "tls/plugin-root",
                "tls/exchange-account-center-client-cert",
                "tls/exchange-account-center-client-key",
            ),
            "callerId": EXCHANGE_CALLER_ID,
            "signingKeyRef": "account/signing",
        },
        "githubIssues": {
            "bindingId": GITHUB_BINDING,
            "connectionRef": "grpcs://github-issues:7443",
            "transport": "mtls",
            "mtls": plugin,
            "callerId": EXCHANGE_CALLER_ID,
            "signingKeyRef": "account/signing",
        },
        "connectorHost": {
            "navigatorConnector": {
                "keyId": "navigator-v1",
                "keyFileRef": "navigator/exchange-hmac",
            },
            "teamsMessages": {
                "profileRef": TEAMS_PROFILE_REF,
                "connectionRef": ids["teamsConnectionRef"],
                "organizationId": ids["organizationId"],
                "workspaceId": ids["workspaceId"],
                "tenantId": ids["tenantId"],
                "allowedTeamIds": [ids["teamId"]],
                "botAppId": ids["botAppId"],
                "expectedIssuer": ids["issuer"],
                **({"personalActorSubjects": [ids["actorOid"]]} if work_mode == "private" else {}),
                "directPlugin": {
                    "bindingId": TEAMS_BINDING,
                    "connectionRef": "grpcs://teams-provider:7443",
                    "transport": "mtls",
                    "mtls": plugin,
                },
            },
            "computerRunner": {
                "profileRef": COMPUTER_PROFILE_REF,
                "connectionRef": ids["computerConnectionRef"],
                "organizationId": ids["organizationId"],
                "workspaceId": ids["workspaceId"],
                "expectedIssuer": ids["issuer"],
                "directPlugin": {
                    "bindingId": "computer-runtime",
                    "connectionRef": "grpcs://computer-runtime-proxy:7443",
                    "transport": "mtls",
                    "mtls": _plugin_mtls(
                        "computer/root-ca", "computer/client-cert", "computer/client-key"
                    ),
                },
            },
        },
    }


def _compose_environment(
    instance: Instance,
    lock: dict[str, Any],
    ids: dict[str, str],
    subnets: dict[str, str],
    exchange_authority_fingerprint: str,
    work_mode: str,
) -> dict[str, str]:
    images = {row["composeEnv"]: row["image"] for row in lock["images"].values()}
    teams_image = images["CYRENE_IMAGE_TEAMS_INGRESS"]
    compact = lambda value: json.dumps(value, separators=(",", ":"), sort_keys=True)
    environment = {
        **images,
        "CYRENE_POSTGRES_IMAGE": lock["externalImages"]["postgres"]["image"],
        # Disabled services still need interpolation values; they never start.
        "CYRENE_IMAGE_GITHUB_ISSUES_PROVIDER": teams_image,
        "CYRENE_CADDY_IMAGE": teams_image,
        "TEAMS_FQDN": "hfdev.invalid",
        "CYRENE_CONFIG_DIR": str(instance.config_dir),
        "CYRENE_SECRETS_DIR": str(instance.secrets_dir),
        "CYRENE_CODEBUDDY_HOME": str(instance.runtime_dir / "codebuddy"),
        "CYRENE_TEAMS_DELIVERY_STATE_DIR": str(instance.runtime_dir / "teams-delivery"),
        "CYRENE_HF_ENABLED": "false",
        "CYRENE_ORGANIZATION_ID": ids["organizationId"],
        "CYRENE_WORKSPACE_ID": ids["workspaceId"],
        "CYRENE_DIRECTORY_OPERATOR_ACTOR_ID": ids["operatorActorId"],
        "CYRENE_AUTHORITY_AAD_TENANT_ID": ids["tenantId"],
        "CYRENE_AUTHORITY_SIGNING_KEY_ID": AUTHORITY_KEY_ID,
        "CYRENE_AUTHORITY_EXCHANGE_WORKLOAD_ID": EXCHANGE_WORKLOAD_ID,
        "CYRENE_AUTHORITY_EXCHANGE_WORKLOAD_AUDIENCE": "exchange",
        "CYRENE_AUTHORITY_EXCHANGE_MTLS_SHA256": exchange_authority_fingerprint,
        "CYRENE_NAVIGATOR_EXCHANGE_HMAC_KEY_ID": "navigator-v1",
        "CYRENE_TEAMS_NAVIGATOR_HMAC_KEY_ID": "teams-v1",
        "CYRENE_TEAMS_EXCHANGE_CONNECTION_REF": ids["teamsConnectionRef"],
        "CYRENE_TEAMS_EXCHANGE_PROFILE_REF": TEAMS_PROFILE_REF,
        "TEAMS_TENANT_ID": ids["tenantId"],
        "TEAMS_BOT_APP_ID": ids["botAppId"],
        "TEAMS_ALLOWED_TEAM_IDS": ids["teamId"],
        "CYRENE_ACCOUNT_CENTER_BINDING_ID": ACCOUNT_CENTER_BINDING,
        "CYRENE_ACCOUNT_CENTER_CONFIGURATION_JSON": compact(
            {
                "encryption_key_ref": "account-center/encryption",
                "caller_key_refs": {
                    EXCHANGE_CALLER_ID: "account-center/exchange-hmac",
                    GITHUB_PROVIDER_CALLER_ID: "account-center/github-provider-hmac",
                },
                "hostSecretRefs": {
                    "account-center/encryption": "CYRENE_ACCOUNT_CENTER_ENCRYPTION_KEY",
                    "account-center/exchange-hmac": "CYRENE_ACCOUNT_CENTER_EXCHANGE_HMAC_KEY",
                    "account-center/github-provider-hmac": "CYRENE_ACCOUNT_CENTER_GITHUB_PROVIDER_HMAC_KEY",
                },
                "exchange_caller_id": EXCHANGE_CALLER_ID,
                "github_provider_caller_id": GITHUB_PROVIDER_CALLER_ID,
                "method_callers": {},
            }
        ),
        "CYRENE_EXCHANGE_INTEGRATION_CONFIGURATION_JSON": compact(
            _exchange_configuration(ids, work_mode)
        ),
        "CYRENE_GITHUB_ISSUES_BINDING_ID": GITHUB_BINDING,
        "CYRENE_GITHUB_ISSUES_CONFIGURATION_JSON": "{}",
        "CYRENE_TEAMS_CONNECTOR_BINDING_ID": TEAMS_BINDING,
        "CYRENE_TEAMS_CONNECTOR_CONFIGURATION_JSON": compact(
            {
                "binding_id": TEAMS_BINDING,
                "bot_app_id": ids["botAppId"],
                "tenant_id": ids["tenantId"],
                "allowed_team_ids": [ids["teamId"]],
                "personal_chat_enabled": work_mode == "private",
                "client_secret_env_ref": "TEAMS_BOT_CLIENT_SECRET",
                "timeout_seconds": 10,
            }
        ),
        "HFDEV_TOOL_DIR": str(TOOL_DIR),
        "CYRENE_AUTOMATION_HEALTH_EXPORT_DIR": str(instance.runtime_dir / "automation-health"),
        "CYRENE_WORKFLOWS_ENABLED": "false",
        "CYRENE_TEAMS_PERSONAL_CHAT_ENABLED": "true" if work_mode == "private" else "false",
        "CYRENE_TEAMS_MODEL_ACTOR_SUBJECT": ids["actorOid"]
        if work_mode in ACTOR_WORK_MODES
        else "",
    }
    navigator_secrets = [
        "CYRENE_SESSION_TOKEN=/run/secrets/navigator-session-token",
        "TEAMS_NAVIGATOR_HMAC_KEY=/run/secrets/teams-navigator-hmac-key",
    ]
    if work_mode in ACTOR_WORK_MODES:
        navigator_secrets.append(
            "CYRENE_PRIVATE_TEAMS_BRIDGE_TOKEN=/run/secrets/teams-private-bridge-token"
        )
        navigator_secrets += [
            f"{name}=/run/secrets/work-actor-token-0{index}"
            for index, name in enumerate(WORK_ACTOR_TOKENS, 1)
        ]
    navigator_secrets.append("CYRENE_EXCHANGE_TOKEN=/run/secrets/hfdev-model-stub-token")
    environment["HFDEV_NAVIGATOR_SECRET_FILE_MAP"] = ",".join(navigator_secrets)
    for name in NETWORKS:
        environment["HFDEV_SUBNET_" + name.upper().replace("-", "_")] = subnets[name]
    return environment


def _fix_ownership(instance: Instance, lock: dict[str, Any]) -> None:
    """Give container UIDs ownership of their mounted files via one root helper container.

    Bind-mounted secrets keep host ownership, so service UIDs 10001 (and Postgres 999)
    could not read 0400 files otherwise. Configuration follows the production layout:
    a root-owned directory with ``root:10001 0640`` files, which Navigator requires for
    its Work actor map. The helper only touches this instance's files.
    中文：仅修改本实例目录下文件的属主与权限；配置目录沿用生产的 root 属主布局。
    """

    postgres_files = ("postgres-server-cert", "postgres-server-key", "postgres-admin-password")
    script = "\n".join(
        [
            "set -eu",
            f"find /hfdev/secrets -maxdepth 1 -type f -exec chown {SERVICE_UID}:{SERVICE_UID} {{}} +",
            "find /hfdev/secrets -maxdepth 1 -type f -exec chmod 0400 {} +",
            *[
                f"chown {POSTGRES_UID}:{POSTGRES_UID} /hfdev/secrets/{name}"
                for name in postgres_files
            ],
            "install -d -o 0 -g 0 -m 0755 /hfdev/config",
            f'for file in /hfdev/config-src/*; do install -o 0 -g {SERVICE_UID} -m 0640 "$file" /hfdev/config/; done',
            f"chown {SERVICE_UID}:{SERVICE_UID} /hfdev/runtime/teams-delivery /hfdev/runtime/codebuddy",
            "chmod 0700 /hfdev/runtime/teams-delivery /hfdev/runtime/codebuddy",
        ]
    )
    _run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--label",
            f"io.cyrene.task={TASK_LABEL}",
            "--entrypoint",
            "sh",
            "-v",
            f"{instance.root}:/hfdev",
            lock["images"]["teams-ingress"]["image"],
            "-c",
            script,
        ],
        capture=True,
    )


# ── Compose lifecycle ────────────────────────────────────────────────────────

EXCHANGE_CLI = "/app/Cyrene-Services/Cyrene-Exchange/product/.venv/bin/cyrene-exchange"
SECRET_ENTRYPOINT = "/usr/local/bin/cyrene-secret-env-entrypoint"


def _compose_files(instance: Instance) -> tuple[Path, ...]:
    mode = (
        _read_json(instance.identity_path).get("workMode", "legacy")
        if instance.identity_path.exists()
        else "legacy"
    )
    extra = (ACTOR_WORK_COMPOSE,) if mode in ACTOR_WORK_MODES else ()
    return (*BASE_COMPOSE_FILES, *extra, DEV_COMPOSE)


def _compose_argv(instance: Instance, *args: str) -> list[str]:
    argv = [
        "docker",
        "compose",
        "--project-name",
        instance.project,
        "--env-file",
        str(instance.env_path),
    ]
    for path in _compose_files(instance):
        argv += ["-f", str(path)]
    return argv + list(args)


def _compose(
    instance: Instance,
    *args: str,
    capture: bool = False,
    check: bool = True,
    timeout: float | None = 900,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ)
    env.update(env_extra or {})
    return _run(
        _compose_argv(instance, *args), capture=capture, check=check, timeout=timeout, env=env
    )


def _project_containers(instance: Instance, *, all_states: bool = True) -> list[dict[str, Any]]:
    argv = [
        "docker",
        "ps",
        "--filter",
        f"label=com.docker.compose.project={instance.project}",
        "--format",
        "{{json .}}",
    ]
    if all_states:
        argv.insert(2, "-a")
    rows = [json.loads(line) for line in _text(argv).splitlines() if line.strip()]
    for row in rows:
        labels = dict(
            part.split("=", 1) for part in row.get("Labels", "").split(",") if "=" in part
        )
        if labels.get("io.cyrene.task") != TASK_LABEL:
            raise DevStackError(
                f"project {instance.project} contains a container this tool does not own"
            )
    return rows


def _container_id(instance: Instance, service: str) -> str:
    output = _text(_compose_argv(instance, "ps", "-q", service))
    if not output:
        raise DevStackError(f"service {service} has no container")
    return output.splitlines()[0]


def _inspect_container(container: str) -> dict[str, Any]:
    return json.loads(_text(["docker", "inspect", container]))[0]


def _wait_healthy(instance: Instance, service: str, timeout: float) -> float:
    """Wait for Docker health; return the host CLOCK_MONOTONIC instant it was observed.

    Wall-clock time is stepped by WSL time sync under load, so every duration in this
    tool uses CLOCK_MONOTONIC, which containers share with the host.
    中文：WSL 在高负载时会回拨墙钟，因此所有耗时都用宿主与容器共享的单调时钟。
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = _inspect_container(_container_id(instance, service))["State"]
        except DevStackError:
            time.sleep(0.5)
            continue
        if (state.get("Health") or {}).get("Status") == "healthy":
            return time.monotonic()
        if state.get("Status") in {"exited", "dead"}:
            raise DevStackError(f"{service} exited during startup; see `logs {service}`")
        time.sleep(0.5)
    raise DevStackError(f"{service} did not become healthy within {timeout:.0f}s")


def _guard_runtime(instance: Instance) -> dict[str, Any]:
    """Fail closed if any project container publishes a host port or reaches the internet."""

    published: list[dict[str, Any]] = []
    for row in _project_containers(instance, all_states=False):
        inspect = _inspect_container(row["ID"])
        for container_port, bindings in (inspect["NetworkSettings"].get("Ports") or {}).items():
            for binding in bindings or []:
                port = int(binding.get("HostPort") or 0)
                published.append(
                    {"container": row["Names"], "port": container_port, "hostPort": port}
                )
                if port in FORBIDDEN_PORTS or port not in ALLOWED_PORTS:
                    raise DevStackError(f"{row['Names']} publishes forbidden host port {port}")
    networks = []
    for network_id in _text(
        [
            "docker",
            "network",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={instance.project}",
        ]
    ).split():
        inspect = json.loads(_text(["docker", "network", "inspect", network_id]))[0]
        networks.append(
            {
                "name": inspect["Name"],
                "internal": inspect["Internal"],
                "subnet": inspect["IPAM"]["Config"][0]["Subnet"],
            }
        )
        if not inspect["Internal"]:
            raise DevStackError(f"network {inspect['Name']} is not internal")
    return {"publishedPorts": published, "networks": networks}


def _exchange_export(instance: Instance, command: str, ids: dict[str, str]) -> Any:
    argv = [
        "exec",
        "-T",
        "exchange",
        SECRET_ENTRYPOINT,
        EXCHANGE_CLI,
        "integrations",
        command,
        "--integration-database",
        "/data/integrations.sqlite3",
        "--issuer",
        ids["issuer"],
        "--subject",
        ids["actorOid"],
        "--organization-id",
        ids["organizationId"],
        "--workspace-id",
        ids["workspaceId"],
    ]
    output = _compose(instance, *argv, capture=True).stdout.decode("utf-8").strip()
    return json.loads(output.splitlines()[-1])


def _directory_maintainer(instance: Instance, ids: dict[str, str]) -> dict[str, Any]:
    scope = [
        "--issuer",
        ids["issuer"],
        "--subject",
        ids["actorOid"],
        "--organization",
        ids["organizationId"],
        "--workspace",
        ids["workspaceId"],
    ]
    base = ["--profile", "hfdev-bootstrap", "run", "--rm", "-T", "hfdev-directory-maintainer"]
    current = (
        _compose(instance, *base, "membership", "get", *scope, capture=True).stdout.decode().strip()
    )
    if "navigator.connector.operator.v1" not in current:
        _compose(
            instance,
            *base,
            "maintainer",
            "add",
            *scope,
            "--reason",
            "hfdev fixture owner",
            capture=True,
        )
        current = (
            _compose(instance, *base, "membership", "get", *scope, capture=True)
            .stdout.decode()
            .strip()
        )
    if "navigator.connector.operator.v1" not in current:
        raise DevStackError("fixture actor did not receive the connector operator role")
    return json.loads(current.splitlines()[-1])


def _private_installation_active(instance: Instance, ids: dict[str, str]) -> bool:
    """Read the install from Work: ingress counters only show that something was delivered."""

    script = (
        "import sqlite3,sys\n"
        "c=sqlite3.connect('file:/var/lib/cyrene-navigator/persistence.db?mode=ro',uri=True)\n"
        "row=c.execute('SELECT active FROM work_private_installations "
        "WHERE actor_aad_object_id=? AND bot_app_id=?',sys.argv[1:3]).fetchone()\n"
        "print(1 if row and row[0] else 0)\n"
    )
    for _ in range(30):
        completed = _run(
            [
                "docker",
                "exec",
                _container_id(instance, "navigator-persistence"),
                "/opt/venv/bin/python",
                "-c",
                script,
                ids["actorOid"].lower(),
                ids["botAppId"].lower(),
            ],
            capture=True,
        )
        if completed.stdout.strip() == b"1":
            return True
        time.sleep(1)
    return False


def cmd_up(args: argparse.Namespace) -> int:
    """Start the stack in production cold-start order and record phase timings."""

    instance = _instance(args)
    lock = _load_lock(instance)
    identity = _read_json(instance.identity_path)
    if identity["imageLockSha256"] != _sha256_file(instance.lock_path):
        raise DevStackError("image lock changed after init; run `init --force`")
    ids = identity["identities"]
    evidence = _evidence_run(instance, "up")
    _project_containers(instance)
    _compose(instance, "config", "--quiet", capture=True)
    phases: list[dict[str, Any]] = []
    started = time.monotonic()
    started_wall = _now()

    def phase(name: str, began: float, **extra: Any) -> None:
        phases.append({"phase": name, "seconds": round(time.monotonic() - began, 2), **extra})
        print(f"{name}: {phases[-1]['seconds']}s", flush=True)

    began = time.monotonic()
    _compose(instance, "up", "-d", "--no-build", "postgres", capture=True)
    _wait_healthy(instance, "postgres", 120)
    phase("postgres", began)
    began = time.monotonic()
    # pg_isready already passes while the image's temporary init server runs on a local
    # socket; retry the TLS migration until the final server accepts network clients.
    last_migration: subprocess.CompletedProcess[bytes] | None = None
    for attempt in range(1, 21):
        last_migration = _compose(
            instance,
            "--profile",
            "directory-bootstrap",
            "run",
            "--rm",
            "-T",
            "directory-migrate",
            capture=True,
            check=False,
        )
        if last_migration.returncode == 0:
            break
        if attempt == 20:
            # The output can contain the fixture database URL, so keep it in the evidence
            # directory instead of the terminal.
            captured = (last_migration.stderr or b"") + b"\n" + (last_migration.stdout or b"")
            log_path = evidence / "directory-migrate-last.txt"
            _write_bytes(log_path, captured[-4000:])
            raise DevStackError(
                "Directory migration did not succeed after 20 attempts "
                f"(last exit {last_migration.returncode}). If fixtures were regenerated "
                "while Postgres volumes from an earlier `down` still existed, those volumes "
                "still have the previous password; run `down --volumes`, then `init`. "
                f"The last command output is in {log_path}."
            )
        time.sleep(3)
    _compose(
        instance,
        "--profile",
        "directory-bootstrap",
        "run",
        "--rm",
        "-T",
        "directory-runtime-role-bootstrap",
        capture=True,
    )
    membership = _directory_maintainer(instance, ids)
    phase("directory-bootstrap", began)
    began = time.monotonic()
    _compose(instance, "up", "-d", "--no-build", *PROVIDER_TIER, capture=True)
    ready = {service: _wait_healthy(instance, service, 240) for service in PROVIDER_TIER}
    phase(
        "provider-tier",
        began,
        healthyAfterSeconds={k: round(v - began, 1) for k, v in ready.items()},
    )
    began = time.monotonic()
    bindings = _exchange_export(instance, "connector-bindings", ids)
    catalog = _exchange_export(instance, "connector-tool-catalog", ids)
    if not bindings.get("bindings") or not catalog.get("items"):
        raise DevStackError("Exchange exported no connector bindings or tool catalog")
    _write_json(instance.config_source_dir / "connector-bindings.json", bindings)
    _write_json(instance.config_source_dir / "connector-tool-catalog.json", catalog)
    _write_json(evidence / "connector-bindings.json", bindings)
    _write_json(evidence / "connector-tool-catalog.json", catalog)
    _fix_ownership(instance, lock)
    phase("connector-export", began)
    began = time.monotonic()
    _compose(instance, "up", "-d", "--no-build", *EDGE_TIER, capture=True)
    ready = {service: _wait_healthy(instance, service, 300) for service in EDGE_TIER}
    phase(
        "edge-tier", began, healthyAfterSeconds={k: round(v - began, 1) for k, v in ready.items()}
    )
    _compose(
        instance, "--profile", "hfdev-tools", "up", "-d", "--no-build", "hfdev-probe", capture=True
    )
    if _read_json(instance.identity_path).get("workMode") == "private":
        began = time.monotonic()
        installation = _probe(instance, "install")
        if not installation["delivered"]:
            raise DevStackError(f"Private bot installation was not delivered: {installation}")
        if not _private_installation_active(instance, ids):
            raise DevStackError("Work has no active private installation for the fixture user")
        phase("private-installation", began)
    guard = _guard_runtime(instance)
    report = {
        "schemaVersion": "cyrene-hfdev-up-v1",
        "instance": instance.name,
        "startedAt": started_wall,
        "totalSeconds": round(time.monotonic() - started, 2),
        "clock": "CLOCK_MONOTONIC; per-service values are first host observation of Docker health",
        "phases": phases,
        "directoryMembership": membership,
        "guard": guard,
        "images": {service: row["imageId"] for service, row in lock["images"].items()},
        "sources": lock["sources"],
    }
    _write_json(evidence / "up.json", report)
    print(
        json.dumps({"up": "ok", "totalSeconds": report["totalSeconds"], "evidence": str(evidence)})
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    instance = _instance(args)
    rows = _project_containers(instance)
    for row in rows:
        print(f"{row['Names']:<55} {row['State']:<9} {row['Status']}")
    if rows and any(row["State"] == "running" for row in rows):
        print(json.dumps(_guard_runtime(instance), indent=2))
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    instance = _instance(args)
    _compose(instance, "logs", "--no-color", "--timestamps", *args.services, timeout=120)
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    """Stop and remove this instance's containers (and volumes with --volumes)."""

    instance = _instance(args)
    _project_containers(instance)
    argv = [
        "--profile",
        "hfdev-tools",
        "--profile",
        "hfdev-bootstrap",
        "--profile",
        "directory-bootstrap",
        "down",
        "--remove-orphans",
    ]
    if args.volumes:
        argv.append("--volumes")
    _compose(instance, *argv, capture=True)
    if args.volumes:
        lock = _read_json(instance.lock_path)
        _run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--user",
                "0:0",
                "--label",
                f"io.cyrene.task={TASK_LABEL}",
                "--entrypoint",
                "sh",
                "-v",
                f"{instance.runtime_dir / 'teams-delivery'}:/state",
                lock["images"]["teams-ingress"]["image"],
                "-c",
                "find /state -mindepth 1 -maxdepth 1 -name 'delivery-spool.sqlite3*' -exec rm -f {} +",
            ],
            capture=True,
        )
    print(f"stopped {instance.project}")
    return 0


# ── Smoke, measurement, and CPU comparison ───────────────────────────────────

PROBE = ("exec", "-T", "hfdev-probe", "python", "/opt/hfdev/teams_smoke.py")
SHA256_HEX = re.compile(r"\b[0-9a-f]{64}\b")


def _probe(instance: Instance, *args: str, timeout: float = 600) -> dict[str, Any]:
    output = _compose(instance, *PROBE, *args, capture=True, timeout=timeout).stdout.decode()
    return json.loads(output.strip().splitlines()[-1])


def _final_tool_digests(model: list[dict[str, Any]]) -> list[str]:
    """Digests of the tool results the model answered from."""

    return [
        row["toolResultSha256"]
        for row in model
        if row.get("toolResultSha256") and row.get("decision") == "final_after_tool"
    ]


def evaluate_smoke(result: dict[str, Any]) -> dict[str, Any]:
    """Turn one driver result into named checks plus a run-independent digest."""

    positive = result["positive"]
    reply = positive.get("reply")
    model = positive.get("model") or []
    decisions = [row.get("decision") for row in model]
    tool_digests = _final_tool_digests(model)
    checks: dict[str, bool] = {}
    if "negative" in result:
        checks["forgedOrUnscopedActivitiesRejectedWithoutQueue"] = bool(result["negative"]["ok"])
    checks["signedActivityAcknowledged"] = positive.get("ackStatus") == 200
    checks["replyRecordedByConnectorFixture"] = reply is not None
    checks["replyUsedFixtureBotToken"] = bool(reply and reply.get("authorized"))
    checks["replyTargetsOriginalThread"] = bool(reply and reply.get("conversationMatches"))
    checks["modelRequestedDirectoryTool"] = "tool_call" in decisions
    checks["modelReceivedToolResult"] = "final_after_tool" in decisions
    checks["toolResultIsNotError"] = "final_after_tool_error" not in decisions
    checks["replyQuotesToolResult"] = bool(reply) and any(
        digest in reply["text"] for digest in tool_digests
    )
    normalized = {
        "checks": checks,
        "negativeStatuses": {
            name: row["status"]
            for name, row in (result.get("negative") or {}).get("cases", {}).items()
        },
        "ackStatus": positive.get("ackStatus"),
        "replyTemplate": SHA256_HEX.sub("<sha256>", reply["textTemplate"]) if reply else None,
        "modelDecisions": decisions,
        "toolResultSha256": sorted(set(tool_digests)),
    }
    digest = _sha256_bytes(json.dumps(normalized, sort_keys=True).encode())
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "normalized": normalized,
        "normalizedSha256": digest,
    }


def _save_logs(instance: Instance, evidence: Path, since: float) -> None:
    # Wall clock: WSL may step it backwards, so keep a generous margin.
    stamp = datetime.fromtimestamp(since - 120, UTC).isoformat()
    for service in RUNNING_SERVICES:
        completed = _compose(
            instance,
            "logs",
            "--no-color",
            "--timestamps",
            "--since",
            stamp,
            service,
            capture=True,
            check=False,
            timeout=120,
        )
        (evidence / f"{service}.log").write_bytes(completed.stdout)
        os.chmod(evidence / f"{service}.log", 0o600)


LEDGER_QUERIES = {
    "exchange": (
        "/app/Cyrene-Services/Cyrene-Exchange/product/.venv/bin/python",
        "/data/integrations.sqlite3",
        {
            "connectorInvocations": "SELECT provider_id,profile_ref,operation_id,state,is_mutating,"
            "created_at,response_status FROM connector_invocations ORDER BY rowid",
        },
    ),
    "navigator-persistence": (
        "/opt/venv/bin/python",
        "/var/lib/cyrene-navigator/persistence.db",
        {
            "tasks": "SELECT task_id,status,created_at,started_at,ended_at,duration_ms,error "
            "FROM work_tasks ORDER BY created_at",
            "notifications": "SELECT notification_type,recipient,status,created_at,updated_at,"
            "result_json FROM work_notifications ORDER BY created_at",
        },
    ),
}


def _ledgers(instance: Instance) -> dict[str, Any]:
    """Read-only, credential-free rows proving which operations Exchange and Work ran."""

    output: dict[str, Any] = {}
    for service, (python, database, queries) in LEDGER_QUERIES.items():
        script = (
            "import json,sqlite3,sys\n"
            f"c=sqlite3.connect('file:{database}?mode=ro',uri=True)\n"
            "c.row_factory=sqlite3.Row\n"
            "queries=json.loads(sys.argv[1])\n"
            "print(json.dumps({k:[dict(r) for r in c.execute(q)] for k,q in queries.items()}))\n"
        )
        completed = _run(
            [
                "docker",
                "exec",
                _container_id(instance, service),
                python,
                "-c",
                script,
                json.dumps(queries),
            ],
            capture=True,
        )
        output[service] = json.loads(completed.stdout)
    return output


def _notification_count(instance: Instance) -> int:
    """Rows already in the ledger; 0 if unreadable, so statuses are then over-reported."""

    try:
        return len(_ledgers(instance)["navigator-persistence"]["notifications"])
    except (DevStackError, KeyError, subprocess.TimeoutExpired):
        return 0


def _smoke_summary(instance: Instance, runs: list[dict[str, Any]]) -> dict[str, Any]:
    digests = {run["verdict"]["normalizedSha256"] for run in runs}
    return {
        "schemaVersion": "cyrene-hfdev-smoke-v1",
        "instance": instance.name,
        "runs": len(runs),
        "allPassed": all(run["verdict"]["passed"] for run in runs),
        "consistent": len(digests) == 1,
        "normalizedDigests": sorted(digests),
        "replySeconds": [run["result"]["positive"].get("replySeconds") for run in runs],
        # False until the ledgers and fixture records are saved beside it.
        "evidenceComplete": False,
    }


def cmd_smoke(args: argparse.Namespace) -> int:
    """Run the signed-event → Navigator → Exchange → tool → reply path N times."""

    instance = _instance(args)
    evidence = _evidence_run(instance, "smoke")
    started = time.time()
    notifications_before = _notification_count(instance)
    runs = []
    for index in range(args.repeat):
        extra = ["--skip-negative"] if args.skip_negative else []
        result = _probe(instance, "smoke", "--timeout", str(args.timeout), *extra)
        verdict = evaluate_smoke(result)
        runs.append({"index": index + 1, "result": result, "verdict": verdict})
        print(
            f"run {index + 1}: passed={verdict['passed']} ack={result['positive']['ackStatus']} "
            f"reply={result['positive'].get('replySeconds')}s digest={verdict['normalizedSha256'][:12]}",
            flush=True,
        )
        # Each finished run goes to disk at once, so a later failure cannot lose it.
        _write_json(
            evidence / "smoke.json", {"summary": _smoke_summary(instance, runs), "runs": runs}
        )
    summary = _smoke_summary(instance, runs)
    _write_json(evidence / "fixture-ledgers.json", _probe(instance, "captured"))
    ledgers = _ledgers(instance)
    _write_json(evidence / "service-ledgers.json", ledgers)
    # Observation, not a pass criterion: Navigator may record a delivered reply as
    # "uncertain" when the Teams provider's first send outlasts its 10 s request timeout.
    # Written into smoke.json itself; a later print-only field is not evidence.
    # Rows from this smoke: a private task has two notifications, a channel task one.
    summary["replyNotificationStatuses"] = [
        row["status"]
        for row in ledgers["navigator-persistence"]["notifications"][notifications_before:]
    ]
    summary["evidenceComplete"] = True
    _write_json(evidence / "smoke.json", {"summary": summary, "runs": runs})
    _save_logs(instance, evidence, started)
    print(json.dumps({**summary, "evidence": str(evidence)}))
    return 0 if summary["allPassed"] and summary["consistent"] else 1


def _cgroup(container: str) -> dict[str, int] | None:
    completed = _run(
        [
            "docker",
            "exec",
            container,
            "cat",
            "/sys/fs/cgroup/cpu.stat",
            "/sys/fs/cgroup/memory.current",
        ],
        capture=True,
        check=False,
    )
    if completed.returncode:
        return None
    values: dict[str, int] = {}
    lines = completed.stdout.decode().split("\n")
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            values[parts[0]] = int(parts[1])
        elif len(parts) == 1 and parts[0].isdigit():
            values["memory_current"] = int(parts[0])
    return values


def _throttle_period_ratio(
    before: dict[str, int] | None, after: dict[str, int] | None
) -> float | None:
    """Fraction of cgroup CPU periods that were throttled between two samples."""

    if not before or not after:
        return None
    periods = after.get("nr_periods", 0) - before.get("nr_periods", 0)
    if periods <= 0:
        return None
    throttled = after.get("nr_throttled", 0) - before.get("nr_throttled", 0)
    return round(throttled / periods, 3)


def _persistence_saturated(task_count: int | None, throttle_ratio: float | None) -> bool:
    """True unless both readings show reply time is still evidence about the Teams quota."""

    if task_count is None or throttle_ratio is None:
        return True
    return (
        task_count >= PERSISTENCE_TASK_SATURATION
        or throttle_ratio >= PERSISTENCE_THROTTLE_SATURATION
    )


def _persistence_task_count(instance: Instance) -> int | None:
    try:
        tasks = _ledgers(instance)["navigator-persistence"]["tasks"]
    except (DevStackError, KeyError, subprocess.TimeoutExpired):
        return None
    return len(tasks) if isinstance(tasks, list) else None


def _cpu_limit(container: str) -> float | None:
    nano = int(_inspect_container(container)["HostConfig"].get("NanoCpus") or 0)
    return nano / 1e9 if nano else None


def _cpu_sample_order(cpus: list[str], repeat: int) -> list[tuple[str, int]]:
    """Alternate quotas so the later samples are not all on the last quota.

    中文：配额交替，避免后一组只因为任务变多而显得更慢。
    """

    return [(cpu, index) for index in range(1, repeat + 1) for cpu in cpus]


def cmd_measure(args: argparse.Namespace) -> int:
    """Record container inventory, limits, memory, and idle CPU over a window."""

    instance = _instance(args)
    evidence = _evidence_run(instance, "measure")
    rows = []
    for container in _project_containers(instance, all_states=False):
        inspect = _inspect_container(container["ID"])
        labels = inspect["Config"]["Labels"]
        rows.append(
            {
                "id": container["ID"],
                "service": labels.get("com.docker.compose.service"),
                "role": labels.get("io.cyrene.hfdev.role", "stack"),
                "image": inspect["Config"]["Image"],
                "imageSizeBytes": _inspect_image(inspect["Image"]).get("Size"),
                "cpuLimit": inspect["HostConfig"]["NanoCpus"] / 1e9
                if inspect["HostConfig"]["NanoCpus"]
                else None,
                "memoryLimitBytes": inspect["HostConfig"]["Memory"] or None,
                "health": (inspect["State"].get("Health") or {}).get("Status"),
                "before": _cgroup(container["ID"]),
            }
        )
    time.sleep(args.window)
    persistence_ratio: float | None = None
    for row in rows:
        after = _cgroup(row["id"])
        before = row.pop("before")
        row["memoryCurrentBytes"] = after.get("memory_current") if after else None
        if before and after:
            row["cpuSecondsInWindow"] = round((after["usage_usec"] - before["usage_usec"]) / 1e6, 3)
            row["throttledSecondsInWindow"] = round(
                (after.get("throttled_usec", 0) - before.get("throttled_usec", 0)) / 1e6, 3
            )
            row["cpuSecondsSinceStart"] = round(after["usage_usec"] / 1e6, 3)
            if row["service"] == "navigator-persistence":
                persistence_ratio = _throttle_period_ratio(before, after)
    task_count = _persistence_task_count(instance)
    saturated = _persistence_saturated(task_count, persistence_ratio)
    stack = [row for row in rows if row["role"] == "stack"]
    summary = {
        "schemaVersion": "cyrene-hfdev-measure-v1",
        "instance": instance.name,
        "windowSeconds": args.window,
        "stackContainers": len(stack),
        "fixtureContainers": len([row for row in rows if row["role"] == "fixture"]),
        "toolContainers": len([row for row in rows if row["role"] == "tool"]),
        "stackMemoryCurrentMiB": round(
            sum(row.get("memoryCurrentBytes") or 0 for row in stack) / 2**20, 1
        ),
        "stackMemoryLimitMiB": round(
            sum(row.get("memoryLimitBytes") or 0 for row in stack) / 2**20, 1
        ),
        "stackCpuLimit": round(sum(row.get("cpuLimit") or 0 for row in stack), 2),
        "stackIdleCpuSecondsPerMinute": round(
            sum(row.get("cpuSecondsInWindow") or 0 for row in stack) * 60 / args.window, 3
        ),
        "stackImageBytes": sum(row.get("imageSizeBytes") or 0 for row in stack),
        "persistenceTaskCount": task_count,
        "persistenceThrottlePeriodRatio": persistence_ratio,
        "persistenceSaturated": saturated,
        # Idle CPU grows with the task count. A saturated sample is not a baseline.
        "idleCpuComparable": not saturated,
    }
    _write_json(evidence / "measure.json", {"summary": summary, "containers": rows})
    print(json.dumps({**summary, "evidence": str(evidence)}))
    return 0


def _recreate(instance: Instance, service: str, cpus: str) -> str:
    env = {"HFDEV_TEAMS_INGRESS_CPUS": cpus, "HFDEV_TEAMS_PROVIDER_CPUS": cpus}
    _compose(
        instance,
        "up",
        "-d",
        "--no-deps",
        "--no-build",
        "--force-recreate",
        service,
        capture=True,
        env_extra=env,
    )
    return _container_id(instance, service)


def _cold_start(
    instance: Instance, service: str, role: str, cpus: str
) -> tuple[str, dict[str, Any]]:
    """Stop one Teams role, start polling, recreate it, and time readiness.

    ``readySeconds`` runs from the recreate command to the probe's first successful
    readiness check, both on CLOCK_MONOTONIC.
    中文：从重建命令开始计时，到探针首次就绪成功为止。
    """

    _compose(instance, "stop", "--timeout", "20", service, capture=True)
    probe = subprocess.Popen(
        _compose_argv(instance, *PROBE, "wait-ready", "--role", role, "--timeout", "300"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert probe.stdout is not None
    first = probe.stdout.readline()
    if b'"polling"' not in first:
        probe.kill()
        raise DevStackError(f"readiness probe for {role} did not start")
    began = time.monotonic()
    container = _recreate(instance, service, cpus)
    started = time.monotonic()
    output, errors = probe.communicate(timeout=360)
    if probe.returncode:
        raise DevStackError(f"readiness probe for {role} failed: {errors.decode()[-500:]}")
    ready = json.loads(output.decode().strip().splitlines()[-1])["readyMonotonic"][role]
    return container, {
        "recreateCommandSeconds": round(started - began, 3),
        "readySeconds": round(ready - began, 3) if ready else None,
        "cgroupAtReady": _cgroup(container),
    }


def _reply_is_real(positive: dict[str, Any]) -> bool:
    """A reply counts only if it quotes a tool result: a failure notice also quotes the nonce."""

    reply = positive.get("reply")
    digests = _final_tool_digests(positive.get("model") or [])
    return bool(reply) and any(digest in reply.get("text", "") for digest in digests)


def _persistence_idle_throttle(container: str) -> float | None:
    """Throttle ratio while nothing is sent; the 0.60 threshold came from idle samples."""

    before = _cgroup(container)
    time.sleep(PERSISTENCE_IDLE_SAMPLE_SECONDS)
    return _throttle_period_ratio(before, _cgroup(container))


def _ready_after_recreate(start: dict[str, Any]) -> float | None:
    ready = start.get("readySeconds")
    command = start.get("recreateCommandSeconds")
    if ready is None or command is None:
        return None
    return round(ready - command, 3)


def _cpu_compare_medians(rows: list[dict[str, Any]], quotas: list[str]) -> dict[str, Any]:
    """Median each metric per quota; any invalid sample nulls what depends on the messages."""

    def median(values: list[float | None]) -> float | None:
        clean = sorted(value for value in values if value is not None)
        if not clean:
            return None
        middle = len(clean) // 2
        return (
            clean[middle] if len(clean) % 2 else round((clean[middle - 1] + clean[middle]) / 2, 3)
        )

    summary: dict[str, Any] = {}
    reply_keys = {"firstAckSeconds", "firstReplySeconds", "warmAckSeconds", "warmReplySeconds"}
    for cpus in quotas:
        subset = [row for row in rows if row["cpus"] == float(cpus)]
        replies_valid = all(row.get("replyValid") for row in subset) and bool(subset)
        keys = sorted(
            {key for row in subset for key in row}
            - {"cpus", "repeat", "firstReplyOk", "persistenceSaturated", "replyValid"}
        )
        summary[cpus] = {}
        for key in keys:
            if not replies_valid and (key in reply_keys or "FirstMessage" in key):
                summary[cpus][key] = None
            else:
                summary[cpus][key] = median([row.get(key) for row in subset])
        summary[cpus]["allRepliesOk"] = all(row["firstReplyOk"] for row in subset)
        summary[cpus]["samples"] = len(subset)
        summary[cpus]["replyComparison"] = "valid" if replies_valid else "invalid"
    return summary


def cmd_cpu_compare(args: argparse.Namespace) -> int:
    """Cold-start both Teams roles under each CPU quota and measure real CPU seconds.

    Samples alternate quotas (0.05, 0.5, 0.05, 0.5, ...) so one quota is not entirely
    after the other. Each cycle still follows production order: Teams provider, a fresh
    Exchange, then Teams ingress. ``readySeconds`` includes the compose recreate command;
    ``recreateCommandSeconds`` is recorded separately. First and warm replies are marked
    invalid when Persistence is saturated.
    中文：配额交替取样。就绪时间含 compose 命令耗时，并单独记录该耗时。Persistence 饱和时回复时间无效。
    """

    instance = _instance(args)
    if _read_json(instance.identity_path).get("workMode") == "private":
        raise DevStackError(
            "cpu-compare needs legacy mode: each cycle runs longer than the two-minute idle "
            "limit of a DSH personal session, so every sample would fail"
        )
    evidence = _evidence_run(instance, "cpu-compare")
    rows = []
    for cpus, repeat in _cpu_sample_order(args.cpus, args.repeat):
        provider, provider_start = _cold_start(instance, "teams-provider", "provider", cpus)
        _recreate(instance, "exchange", cpus)
        _wait_healthy(instance, "exchange", 300)
        ingress, ingress_start = _cold_start(instance, "teams-ingress", "ingress", cpus)
        persistence = _container_id(instance, "navigator-persistence")
        # Read before the window opens: this read runs Python inside Persistence.
        task_count = _persistence_task_count(instance)
        throttle = _persistence_idle_throttle(persistence)
        saturated = _persistence_saturated(task_count, throttle)
        before = {"ingress": _cgroup(ingress), "provider": _cgroup(provider)}
        first = _probe(instance, "send", "--timeout", "300")["positive"]
        after = {"ingress": _cgroup(ingress), "provider": _cgroup(provider)}
        second = _probe(instance, "send", "--timeout", "300")["positive"]
        first_real, second_real = _reply_is_real(first), _reply_is_real(second)
        row: dict[str, Any] = {
            "cpus": float(cpus),
            "repeat": repeat,
            "providerCpuLimit": _cpu_limit(provider),
            "ingressCpuLimit": _cpu_limit(ingress),
            "providerRecreateCommandSeconds": provider_start["recreateCommandSeconds"],
            "ingressRecreateCommandSeconds": ingress_start["recreateCommandSeconds"],
            "providerReadySeconds": provider_start["readySeconds"],
            "ingressReadySeconds": ingress_start["readySeconds"],
            "providerReadyAfterRecreateSeconds": _ready_after_recreate(provider_start),
            "ingressReadyAfterRecreateSeconds": _ready_after_recreate(ingress_start),
            "firstAckSeconds": first.get("ackSeconds"),
            "firstReplySeconds": first.get("replySeconds") if first_real else None,
            "warmAckSeconds": second.get("ackSeconds"),
            "warmReplySeconds": second.get("replySeconds") if second_real else None,
            "firstReplyOk": first_real and second_real,
            "persistenceTaskCount": task_count,
            "persistenceThrottlePeriodRatio": throttle,
            "persistenceSaturated": saturated,
            "replyValid": first_real and second_real and not saturated,
        }
        for role, start in (("ingress", ingress_start), ("provider", provider_start)):
            at_ready, message_before, message_after = (
                start["cgroupAtReady"],
                before[role],
                after[role],
            )
            if at_ready and message_before and message_after:
                row[f"{role}StartupCpuSeconds"] = round(at_ready["usage_usec"] / 1e6, 3)
                row[f"{role}StartupThrottledSeconds"] = round(
                    at_ready.get("throttled_usec", 0) / 1e6, 3
                )
                row[f"{role}FirstMessageCpuSeconds"] = round(
                    (message_after["usage_usec"] - message_before["usage_usec"]) / 1e6, 3
                )
                row[f"{role}FirstMessageThrottledSeconds"] = round(
                    (
                        message_after.get("throttled_usec", 0)
                        - message_before.get("throttled_usec", 0)
                    )
                    / 1e6,
                    3,
                )
                row[f"{role}MemoryMiB"] = round(message_after.get("memory_current", 0) / 2**20, 1)
        rows.append(row)
        print(json.dumps(row), flush=True)
    # Leave the stack on production quotas.
    _cold_start(instance, "teams-provider", "provider", "0.05")
    _recreate(instance, "exchange", "0.05")
    _wait_healthy(instance, "exchange", 300)
    _cold_start(instance, "teams-ingress", "ingress", "0.05")

    summary = _cpu_compare_medians(rows, args.cpus)
    _write_json(
        evidence / "cpu-compare.json",
        {
            "clock": "CLOCK_MONOTONIC",
            "order": "alternating",
            "replyPolicy": (
                "invalid when a reply is not a real tool answer, persistenceTaskCount>=30, or "
                "the idle throttle period ratio>=0.60; first and warm reply medians are then null"
            ),
            "rows": rows,
            "medians": summary,
        },
    )
    print(json.dumps({"medians": summary, "evidence": str(evidence)}, indent=2))
    return 0 if all(row["replyValid"] for row in rows) else 1


def cmd_run(args: argparse.Namespace) -> int:
    """One-command path: build, init, up, smoke (3x), measure."""

    # Refuse before `build` rewrites the image lock, which would also block `up` on these volumes.
    _refuse_leftover_volumes(_project_volumes(_instance(args)))
    for step in (
        ["build", *[part for pin in args.pin for part in ("--pin", pin)]],
        ["init", "--force", "--work-mode", args.work_mode],
        ["up"],
        ["smoke", "--repeat", "3"],
        ["measure", "--window", "60"],
    ):
        print(f"== {' '.join(step)}", flush=True)
        code = main(["--instance", args.instance, "--state-root", args.state_root, *step])
        if code:
            return code
    return 0


# ── Command line ─────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--instance", default="h0")
    parser.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT))
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="build host-architecture images from exact commits")
    build.add_argument(
        "--pin", action="append", default=[], help="alias=40-char SHA (default: develop)"
    )
    build.add_argument("--only", action="append", default=[], help="rebuild one image service")
    build.set_defaults(handler=cmd_build)

    init = commands.add_parser(
        "init", help="generate fixture keys, certificates, and configuration"
    )
    init.add_argument("--force", action="store_true")
    init.add_argument(
        "--work-mode",
        choices=WORK_MODES,
        default="legacy",
        help="legacy: production base Compose (default); actor: add "
        "compose.automation-workflows.yaml; private: also enable private delivery",
    )
    init.set_defaults(handler=cmd_init)

    commands.add_parser("up", help="start the stack in cold-start order").set_defaults(
        handler=cmd_up
    )
    commands.add_parser("status", help="show containers, ports, and networks").set_defaults(
        handler=cmd_status
    )

    logs = commands.add_parser("logs", help="print service logs")
    logs.add_argument("services", nargs="*")
    logs.set_defaults(handler=cmd_logs)

    smoke = commands.add_parser("smoke", help="signed Teams event → tool → reply, with negatives")
    smoke.add_argument("--repeat", type=int, default=1)
    smoke.add_argument("--timeout", type=float, default=240)
    smoke.add_argument("--skip-negative", action="store_true", help="positive path only")
    smoke.set_defaults(handler=cmd_smoke)

    measure = commands.add_parser("measure", help="inventory, memory, and idle CPU")
    measure.add_argument("--window", type=float, default=60)
    measure.set_defaults(handler=cmd_measure)

    cpu = commands.add_parser("cpu-compare", help="Teams cold start under several CPU quotas")
    cpu.add_argument("--cpus", action="append", default=[])
    cpu.add_argument("--repeat", type=int, default=5)
    cpu.set_defaults(handler=cmd_cpu_compare)

    down = commands.add_parser("down", help="stop this instance")
    down.add_argument("--volumes", action="store_true", help="also remove state volumes")
    down.set_defaults(handler=cmd_down)

    run = commands.add_parser("run", help="build, init, up, smoke x3, measure")
    run.add_argument("--pin", action="append", default=[])
    run.add_argument("--work-mode", choices=WORK_MODES, default="legacy")
    run.set_defaults(handler=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "cpu-compare" and not args.cpus:
        args.cpus = ["0.05", "0.5"]
    try:
        return args.handler(args)
    except DevStackError as error:
        print(f"hfdev: {error}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired as error:
        print(f"hfdev: command timed out after {error.timeout} s", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
